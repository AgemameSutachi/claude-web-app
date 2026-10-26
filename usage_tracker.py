# -*- coding: utf-8 -*-
"""セッションごとの今週の使用量（推定コスト）と会話の長さを数えるモジュール。

情報源は claude が書く会話記録（``~/.claude/projects/<フォルダ>/<sessionId>.jsonl`` と、
サブエージェントの ``<sessionId>/subagents/agent-*.jsonl``）。使用量モニター（5006）と
同じ数え方をするが、5006 には期間を区切る口が無く「日曜 20:00 からの今週」を出せないため、
ここで差分読みしながら集計する。

- 今週: 毎週日曜 20:00（日本時間）から（ユーザーの指定・2026-10-12）。
- 使用量: 応答ごとの usage を単価表で米ドルに換算した推定額（API 換算の目安で、請求額ではない）。
  1つの応答は thinking / text / tool_use の複数行に同じ usage が写されるので、
  message.id（無ければ requestId）で畳む（5006 と同じ）。
- 会話の長さ: いまの会話の最後の応答が読んだ文脈の大きさ
  （input + cache_creation + cache_read。/clear や compact で小さくなる）。
- プロセス（一覧の1行）は /clear のたびに会話の番号が変わる。行の使用量は、
  そのプロセスで見た会話の番号すべての合計にする（SessionLineage が覚える）。
"""

import glob
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

CLAUDE_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")

JST = timezone(timedelta(hours=9))

# 週の始まり（日曜 20:00 日本時間）。weekday は月曜 0 … 日曜 6
WEEK_START_WEEKDAY = 6
WEEK_START_HOUR = 20

# 色を変える目安（settings.json の usage_warn_share_percent / usage_warn_context_tokens で変えられる）
DEFAULT_WARN_SHARE_PERCENT = 5.0
DEFAULT_WARN_CONTEXT_TOKENS = 150000

# 会話の長さを読むとき、会話記録の末尾から読む大きさ
CONTEXT_TAIL_BYTES = 512 * 1024

# 単価（100万トークンあたりの米ドル）: 入力 / 出力 / キャッシュ書込5分 / 書込1時間 / 読出。
# 出典は使用量モニター（claude-usage-monitor/src/services/pricing.py。公式価格表の逐語値）。
# 単価を変えるときは向こうと揃えること。表に無いモデルは費用 0 で数え、unknown_models に出す
MODEL_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 8.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 6.25, 10.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 4.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 2.00, 0.10),
    "claude-fable-5-1": (10.00, 50.00, 12.50, 20.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 12.50, 20.00, 1.00),
}


def model_price(model):
    """モデルの単価。末尾の日付（-YYYYMMDD）は外して引く。無ければ None。"""
    if not model:
        return None
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    head, _, tail = model.rpartition("-")
    if len(tail) == 8 and tail.isdigit():
        return MODEL_PRICES.get(head)
    return None


def week_start(now=None):
    """いま（または now）を含む週の始まり（直近の日曜 20:00 日本時間）を返す。"""
    now = (now or datetime.now(JST)).astimezone(JST)
    days_back = (now.weekday() - WEEK_START_WEEKDAY) % 7
    start = (now - timedelta(days=days_back)).replace(
        hour=WEEK_START_HOUR, minute=0, second=0, microsecond=0
    )
    if start > now:
        start -= timedelta(days=7)
    return start


def _parse_ts(text):
    """会話記録の時刻（"...Z" の ISO 8601）を epoch 秒にする。読めなければ None。"""
    if not text:
        return None
    try:
        text = str(text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def usage_of(record):
    """会話記録の1行が数えるべき応答なら (畳む鍵, モデル, usage) を返す。違えば None。"""
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message") or {}
    if not isinstance(message, dict):
        return None
    # モデル名の無い応答は 5006 と同じく claude-sonnet-5 とみなす（数え方を揃える）
    model = message.get("model") or record.get("model") or "claude-sonnet-5"
    if model == "<synthetic>" or record.get("requestId") == "<synthetic>":
        return None
    usage = message.get("usage") or record.get("usage")
    if not isinstance(usage, dict):
        return None
    key = message.get("id") or record.get("requestId") or record.get("uuid")
    return key, model, usage


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def cost_of(model, usage):
    """1つの応答の推定額（米ドル）。単価の分からないモデルは None。"""
    price = model_price(model)
    if price is None:
        return None
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    detail = usage.get("cache_creation") or {}
    write_1h = _int(detail.get("ephemeral_1h_input_tokens"))
    write_5m = _int(detail.get("ephemeral_5m_input_tokens"))
    if write_1h + write_5m == 0:
        # 内訳が無いときは5分として数える（安い側に倒す。5006 と同じ）
        write_5m = cache_write
    return (
        _int(usage.get("input_tokens")) * price[0]
        + _int(usage.get("output_tokens")) * price[1]
        + write_5m * price[2]
        + write_1h * price[3]
        + _int(usage.get("cache_read_input_tokens")) * price[4]
    ) / 1_000_000.0


def tokens_of(usage):
    """1つの応答のトークンの合計（入力・出力・キャッシュ書込・読出）。"""
    return sum(
        _int(usage.get(key))
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )


def context_of(usage):
    """その応答が読んだ文脈の大きさ（入力・キャッシュ書込・キャッシュ読出の合計）。"""
    return (
        _int(usage.get("input_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
    )


def owner_session_id(path):
    """会話記録のパスから、使用量を付ける会話の番号を返す。

    本体の記録 ``<フォルダ>/<id>.jsonl`` は id、サブエージェントの記録
    ``<フォルダ>/<id>/subagents/agent-*.jsonl`` は親の id に付ける。
    """
    parent = os.path.dirname(path)
    if os.path.basename(parent) == "subagents":
        return os.path.basename(os.path.dirname(parent))
    return os.path.splitext(os.path.basename(path))[0]


class _FileState:
    """1つの会話記録の読み進み具合と、今週分の小計。"""

    __slots__ = ("offset", "seen", "cost", "tokens", "unknown")

    def __init__(self):
        self.offset = 0
        self.seen = set()
        self.cost = 0.0
        self.tokens = 0
        self.unknown = set()


class UsageTracker:
    """会話記録を差分読みして、会話の番号ごとの今週の使用量を数える。"""

    def __init__(self, projects_dir=CLAUDE_PROJECTS_DIR):
        self.projects_dir = projects_dir
        self._files = {}
        self._week_start = None
        self._snapshot = None
        self._context_cache = {}
        self._lock = threading.Lock()

    # --- 集計 ---------------------------------------------------------------

    def refresh(self, now=None):
        """今週に書かれた会話記録の増えた分を読み、集計の写しを作り直す。"""
        start = week_start(
            datetime.fromtimestamp(now, JST) if now is not None else None
        )
        start_ts = start.timestamp()
        if self._week_start != start_ts:
            # 週が替わったら数え直す
            self._files = {}
            self._week_start = start_ts

        pattern_main = os.path.join(self.projects_dir, "*", "*.jsonl")
        pattern_sub = os.path.join(self.projects_dir, "*", "*", "subagents", "*.jsonl")
        for path in glob.glob(pattern_main) + glob.glob(pattern_sub):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            if stat.st_mtime < start_ts:
                continue
            state = self._files.get(path)
            if state is None or stat.st_size < state.offset:
                state = self._files[path] = _FileState()
            if stat.st_size > state.offset:
                self._read_new(path, state, stat.st_size, start_ts)

        per_session = {}
        unknown = set()
        for path, state in self._files.items():
            owner = owner_session_id(path)
            entry = per_session.setdefault(owner, {"cost": 0.0, "tokens": 0})
            entry["cost"] += state.cost
            entry["tokens"] += state.tokens
            unknown |= state.unknown
        snapshot = {
            "week_start": start.isoformat(timespec="minutes"),
            "per_session": per_session,
            "total_cost": sum(e["cost"] for e in per_session.values()),
            "total_tokens": sum(e["tokens"] for e in per_session.values()),
            "unknown_models": sorted(unknown),
            "refreshed_at": time.time() if now is None else now,
        }
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _read_new(self, path, state, size, start_ts):
        try:
            with open(path, "rb") as f:
                f.seek(state.offset)
                chunk = f.read(size - state.offset)
        except OSError:
            return
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        for line in chunk[: end + 1].splitlines():
            # 応答の行だけを json として読む（記録の大半は道具の結果で、読むと重い）
            if b'"usage"' not in line or b'"assistant"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            found = usage_of(record)
            if not found:
                continue
            key, model, usage = found
            if key is not None:
                if key in state.seen:
                    continue
                state.seen.add(key)
            ts = _parse_ts(record.get("timestamp"))
            if ts is None or ts < start_ts:
                continue
            cost = cost_of(model, usage)
            if cost is None:
                state.unknown.add(model)
                cost = 0.0
            state.cost += cost
            state.tokens += tokens_of(usage)
        state.offset += end + 1

    def snapshot(self):
        """最後に作った集計の写し（まだ無ければ None）。"""
        with self._lock:
            return self._snapshot

    # --- 会話の長さ ---------------------------------------------------------

    def context_tokens(self, transcript_path):
        """会話記録の最後の応答が読んだ文脈の大きさ。読めなければ None。

        末尾だけを読む。大きさと更新時刻が変わらなければ前の結果を使う。
        """
        try:
            stat = os.stat(transcript_path)
        except OSError:
            return None
        cache_key = (stat.st_size, stat.st_mtime)
        cached = self._context_cache.get(transcript_path)
        if cached and cached[0] == cache_key:
            return cached[1]
        value = None
        try:
            with open(transcript_path, "rb") as f:
                f.seek(max(0, stat.st_size - CONTEXT_TAIL_BYTES))
                tail = f.read()
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            if b'"usage"' not in line or b'"assistant"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("isSidechain"):
                continue
            found = usage_of(record)
            if found:
                value = context_of(found[2])
                break
        self._context_cache[transcript_path] = (cache_key, value)
        return value


class SessionLineage:
    """プロセス（pid と開始時刻）ごとに、見たことのある会話の番号を覚える。

    /clear で会話の番号が変わっても、行の使用量に前の会話の分を含めるため。
    アプリを再起動しても失わないよう、ファイルに書く。
    """

    # 見かけなくなってから、これだけ経った記録は捨てる（週をまたいだら要らない）
    KEEP_SEC = 8 * 24 * 3600

    def __init__(self, path):
        self.path = path
        self._data = self._load()
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def key_for(session):
        return f"{session.get('pid')}:{session.get('started_at')}"

    def observe(self, sessions, now=None):
        """一覧で見た会話の番号を足す。変わったときだけ書き出す。"""
        now = time.time() if now is None else now
        changed = False
        with self._lock:
            for session in sessions:
                session_id = session.get("session_id")
                if not session_id:
                    continue
                entry = self._data.setdefault(
                    self.key_for(session), {"ids": [], "seen": now}
                )
                if session_id not in entry["ids"]:
                    entry["ids"].append(session_id)
                    changed = True
                if now - entry.get("seen", 0) > 3600:
                    changed = True
                entry["seen"] = now
            for key in [
                k
                for k, v in self._data.items()
                if now - v.get("seen", 0) > self.KEEP_SEC
            ]:
                del self._data[key]
                changed = True
            if changed:
                self._save()

    def session_ids(self, session):
        """そのプロセスで見た会話の番号（いまの番号を必ず含む）。"""
        with self._lock:
            ids = list(self._data.get(self.key_for(session), {}).get("ids", []))
        current = session.get("session_id")
        if current and current not in ids:
            ids.append(current)
        return ids

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[Usage] 会話の番号の記録を書けませんでした: {e}", flush=True)


def _thresholds(settings):
    """色を変える目安を設定から読む。数でない・0 以下の値は既定に戻す（一覧を止めない）。"""
    settings = settings or {}

    def number(key, default, cast):
        try:
            value = cast(settings.get(key))
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default

    return (
        number("usage_warn_share_percent", DEFAULT_WARN_SHARE_PERCENT, float),
        number("usage_warn_context_tokens", DEFAULT_WARN_CONTEXT_TOKENS, int),
    )


def describe(session, tracker, lineage, transcript_path, settings=None):
    """一覧の1行に付ける使用量（今週の推定額・割合・会話の長さ・目安超え）を作る。"""
    warn_share, warn_context = _thresholds(settings)
    context = tracker.context_tokens(transcript_path) if transcript_path else None
    snapshot = tracker.snapshot()
    result = {
        "week_cost": None,
        "week_tokens": None,
        "week_share": None,
        "context_tokens": context,
        "warn_usage": False,
        "warn_context": bool(context and context > warn_context),
    }
    if snapshot:
        per_session = snapshot["per_session"]
        cost = tokens = 0
        for session_id in lineage.session_ids(session):
            entry = per_session.get(session_id)
            if entry:
                cost += entry["cost"]
                tokens += entry["tokens"]
        total = snapshot["total_cost"]
        share = (cost / total * 100.0) if total > 0 else 0.0
        result.update(
            {
                "week_cost": round(cost, 2),
                "week_tokens": tokens,
                "week_share": round(share, 1),
                "warn_usage": share > warn_share,
            }
        )
    return result


def summary(tracker, settings=None):
    """一覧の上に出す今週の合計と目安。集計がまだなら None。"""
    snapshot = tracker.snapshot()
    if not snapshot:
        return None
    warn_share, warn_context = _thresholds(settings)
    return {
        "week_start": snapshot["week_start"],
        "total_cost": round(snapshot["total_cost"], 2),
        "total_tokens": snapshot["total_tokens"],
        "unknown_models": snapshot["unknown_models"],
        "refreshed_at": snapshot["refreshed_at"],
        "warn_share_percent": warn_share,
        "warn_context_tokens": warn_context,
    }

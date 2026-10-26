# -*- coding: utf-8 -*-
"""管理下のセッションを、適切なときに自動で区切る（引き継ぎを書かせてから /clear）。

文脈が大きいまま会話を続けると、1往復ごとに大きな文脈を読み直す（運用での実測:
前の週の使用量の 41.6% が文脈 5万超のまま続けた分の読み直し、6.9% が1時間以上
止まった後の読み直し）。そこで、本アプリが起こした子（台帳に載っているもの）を
見回り、次の2つの規則に当たったら「自動の区切りです。引き継ぎを書いて予約し、
ターンを終えてください」を送る。子が引き継ぎを書いて request_self_clear.py で予約すると、
あとは self_clear の道（/clear → 「引き継ぎを読んで、次の依頼を待って」）で区切られる。

    (1) 大きさ: 応答を終えて止まり（入力待ちが SETTLE_SEC 続き）、文脈が size_tokens を超える
        （人の発言から human_quiet_minutes 分は当てない＝人がついている会話に割り込まない）
    (2) 止まった時間: 最後の応答から idle_minutes 分たち、文脈が idle_tokens を超える
    どちらも、人の最後の発言から10分（self_clear.HUMAN_QUIET_SEC）は当てない。本アプリが送った
    自動の文は人の発言に数えない（self_clear.is_human_utterance）。
    どちらも、最後の応答から58分未満（読み直しの控え＝プロンプトキャッシュが生きている間）だけ。
    控えが切れた後に送ると、依頼の1往復そのものが大きな文脈の読み直しになるため。
    どちらも、会話の最初の応答から文脈が min_growth_tokens 以上伸びたものだけ。区切って引き継ぎを
    読んだだけの会話（約6.1〜6.4万→6.4〜7万）を区切り直しても同じ大きさに戻るだけなので
    （2026-10-14 実測: (2) で送った145件のうち132件が伸び1万未満で、約51分ごとに空回りしていた）。

除外: 裏の仕事（サブエージェント・バックグラウンドのコマンド・Monitor・ScheduleWakeup・
セッションの CronCreate）が走っているもの、許可待ち・答え待ちのもの、利用者が「残す」印を
付けたもの、区切りの予約が既にあるもの。
同じ会話（sessionId）へ送るのは一度だけ（送った記録はファイルに残し、再起動しても送り直さない）。

動き方は設定 auto_clear_mode で選ぶ: off（何もしない）/ dry_run（当たりを記録するだけ。
既定）/ on（送る）。当たりと送った結果は RESULT_LOG と GET /api/auto-clear で見る。
"""

import datetime
import json
import os
import re
import sys
import threading
import time

import safe_console
import send_history
import self_clear
import session_manager as sm
import usage_tracker

MODES = ("off", "dry_run", "on")
DEFAULT_MODE = "dry_run"

# 既定の閾値（設定で替えられる）
DEFAULT_SIZE_TOKENS = 100_000
DEFAULT_IDLE_TOKENS = 50_000
DEFAULT_IDLE_MINUTES = 50
# 会話の最初の応答からこれだけ伸びていなければ当てない（区切り直しても縮まないため）
DEFAULT_MIN_GROWTH_TOKENS = 20_000
# 人の発言からこの分数は (1) を当てない（返事を読んで考えている間に割り込まない）
DEFAULT_HUMAN_QUIET_MINUTES = 15
# 送ってから予約が来るのを待つ上限（分）。過ぎたら「予約なし」と記録する
DEFAULT_HANDOFF_WAIT_MINUTES = 30

# プロンプトキャッシュの寿命（分）。規則はこれより前に当てる。
# 見回りの間隔と送るのにかかる時間を見込んで、寿命の少し前で締め切る
CACHE_TTL_MINUTES = 60
IDLE_RULE_MARGIN_MINUTES = 2
CACHE_DEADLINE_MINUTES = CACHE_TTL_MINUTES - IDLE_RULE_MARGIN_MINUTES

# 応答を終えた直後は Stop フックの差し戻しで作業へ戻ることがあるので、
# 入力待ちがこれだけ続いてから (1) を当てる（秒）
SETTLE_SEC = 120

# 見回りの間隔（秒）
POLL_SEC = 60

# 送る本文（利用者の決定・2026-10-12。AI 自身に「残す」を選ばせる文は入れない）。
# 道具の打ち方を添える: 添えないと、/clear で前の指示を失った子が道具の場所を推測で作り、
# 存在しないパスを打って止まった（2026-10-12 試験・haiku）。照合は先頭40字で行うので、
# 先頭は変えないこと
TRIGGER_TEXT = (
    "自動の区切りです。引き継ぎを書いて予約し、ターンを終えてください。"
    "予約の打ち方: {python} {tool} --url http://127.0.0.1:{port} "
    "--handoff <引き継ぎファイルの絶対パス（/ 区切り）>"
)
REQUEST_TOOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "request_self_clear.py"
).replace("\\", "/")


def trigger_text(port, python=None):
    """送る本文（道具の打ち方つき）。python は省略で本アプリの Python。"""
    python = (python or sys.executable).replace("\\", "/")
    return TRIGGER_TEXT.format(python=python, tool=REQUEST_TOOL, port=int(port))


# 人の書きかけに当たったときに、同じ相手へ送り直すまでの間（秒）
DRAFT_RETRY_SEC = 600

# 状態ファイル（送った会話・起こし直した会話）と結果の記録
STATE_FILE = os.path.join(sm.APP_STATE_DIR, "auto_clear.json")
KEEP_FILE = os.path.join(sm.APP_STATE_DIR, "keep.json")
RESULT_LOG = os.path.join(sm.APP_STATE_DIR, "auto_clear.log.jsonl")
RESULT_LOG_MAX_BYTES = 1024 * 1024
RECENT_LIMIT = 100

# 送った会話の記録を残す日数（会話の番号は使い回されないので、古いものは捨ててよい）
STATE_KEEP_SEC = 8 * 24 * 3600

# 裏の仕事が終わった知らせが来ないまま、これだけたったら走っていないとみなす（秒）。
# 知らせが落ちた（セッションの終了・/clear で失われた等）ものに、いつまでも除外されないため
BACKGROUND_STALE_SEC = 6 * 3600
# セッションの CronCreate（durable でないもの）を走っているとみなす長さ（秒）。
# 繰り返しのものは CronDelete まで、1回きりのものは1日
CRON_RECURRING_SEC = 7 * 24 * 3600
CRON_ONCE_SEC = 24 * 3600
# ScheduleWakeup の起床の時刻から、まだ走っているとみなす余裕（秒）
WAKEUP_GRACE_SEC = 120

# task-notification の中身から、仕事の番号と状態を取り出す
TASK_ID_RE = re.compile(r"<task-id>([^<\s]+)</task-id>")
TASK_STATUS_RE = re.compile(r"<status>(\w+)</status>")
# Monitor の見張りが切れた知らせ（状態の欄が無い）
MONITOR_EXPIRED_RE = re.compile(r"\[Monitor (?:expired|stopped)")
# 終わりを表す状態
TASK_END_STATUSES = ("completed", "failed", "stopped", "killed", "cancelled", "error")
# TaskStop の結果（終わりの知らせは来ない・2026-10-12 評価役の実測）
TASK_STOPPED_RE = re.compile(r"Successfully stopped task:\s*([^\s(]+)")
# 起床の予約とセッションの定期の仕事の番号の頭（/clear をまたいで引き継ぐ）
WAKEUP_ID = "wakeup"
CRON_PREFIX = "cron:"


def _now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse_ts(text):
    return self_clear.record_epoch({"timestamp": text})


# --- 設定 -------------------------------------------------------------------


def config_from(settings):
    """設定から動き方と閾値を読む。おかしな値は既定に戻す（見回りを止めない）。"""
    settings = settings or {}

    def number(key, default, cast):
        try:
            value = cast(settings.get(key))
            return value if value > 0 else default
        except (TypeError, ValueError):
            return default

    mode = settings.get("auto_clear_mode")
    idle_minutes = number("auto_clear_idle_minutes", DEFAULT_IDLE_MINUTES, float)
    warnings = []
    if idle_minutes >= CACHE_DEADLINE_MINUTES:
        # 控えの締め切り以降では (2) が一度も当たらないので、締め切りの1分前に丸める
        warnings.append(
            f"auto_clear_idle_minutes={idle_minutes:g} は控えの締め切り"
            f"（{CACHE_DEADLINE_MINUTES} 分）以降なので {CACHE_DEADLINE_MINUTES - 1} 分として扱います"
        )
        idle_minutes = CACHE_DEADLINE_MINUTES - 1
    return {
        "mode": mode if mode in MODES else DEFAULT_MODE,
        "size_tokens": number("auto_clear_size_tokens", DEFAULT_SIZE_TOKENS, int),
        "idle_tokens": number("auto_clear_idle_tokens", DEFAULT_IDLE_TOKENS, int),
        "idle_minutes": idle_minutes,
        "min_growth_tokens": number(
            "auto_clear_min_growth_tokens", DEFAULT_MIN_GROWTH_TOKENS, int
        ),
        "human_quiet_minutes": number(
            "auto_clear_human_quiet_minutes", DEFAULT_HUMAN_QUIET_MINUTES, float
        ),
        "handoff_wait_minutes": number(
            "auto_clear_handoff_wait_minutes", DEFAULT_HANDOFF_WAIT_MINUTES, float
        ),
        # 子が予約を送る先（本アプリの待ち受けのポート）
        "port": number("port", 5002, int),
        "warnings": warnings,
    }


# --- 会話記録の見張り（裏の仕事・最後の応答・最後の人の発言） -----------------------


def _flatten(content):
    if isinstance(content, list):
        return " ".join(
            str(c.get("text") or "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content or "")


def notification_text(record):
    """会話記録の1件が裏の仕事の知らせなら、その本文を返す（違えば空文字）。

    知らせは届き方で3つの形になる（2026-10-12 実測）:
    - ターンの外で届いた: user の記録（origin.kind=task-notification・message.content）
    - ターンの途中で届いた: queue-operation（operation=enqueue・content）と、
      attachment（attachment.origin.kind=task-notification・attachment.prompt）
    ほかの記録（AI の発言・ツールの結果）に同じ文字列が引用されても数えない。
    """
    kind = record.get("type")
    if kind == "user":
        if (record.get("origin") or {}).get("kind") != "task-notification":
            return ""
        text = _flatten((record.get("message") or {}).get("content"))
    elif kind == "queue-operation":
        if record.get("operation") != "enqueue":
            return ""
        text = _flatten(record.get("content"))
    elif kind == "attachment":
        attachment = record.get("attachment") or {}
        if not isinstance(attachment, dict) or (
            (attachment.get("origin") or {}).get("kind") != "task-notification"
        ):
            return ""
        text = _flatten(attachment.get("prompt"))
    else:
        return ""
    return text if "<task-notification" in text else ""


def _carried(task_id):
    """/clear をまたいで引き継ぐ仕事か（起床の予約・セッションの定期の仕事）。"""
    return task_id == WAKEUP_ID or task_id.startswith(CRON_PREFIX)


class BackgroundWork:
    """会話記録を差分で読み、走っている裏の仕事・最後の応答・最後の人の発言を覚える。

    実測（2026-10-12・開発機の会話記録。結果は toolUseResult に載る）:
    - サブエージェント: status=async_launched と agentId
    - Bash / PowerShell の裏の実行: backgroundTaskId
    - Monitor: taskId と timeoutMs・persistent
    - ScheduleWakeup（/loop の起床）: scheduledFor（epoch ミリ秒。止めると stopped=true）
    - CronCreate: id・recurring・durable（durable はディスクに残るので数えない）/ CronDelete: id
    - 終わり: 知らせの <task-id> と <status>completed|failed|stopped|killed</status>
      （Monitor は「[Monitor expired …」）。TaskStop で止めたものは知らせが来ず、
      結果の message「Successfully stopped task: <番号> …」で終わりとみなす。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # パス → {"offset", "running": {番号: 走っているとみなす期限}, "last_response", "last_human"}
        self._files = {}

    def scan(self, transcript_path, now=None):
        """会話記録を読み進め、見立てに使う値を返す。

        Returns:
            dict: running（走っている裏の仕事の番号の一覧）/ carried（/clear をまたいで
            引き継ぐ番号と期限）/ last_response（最後の応答の epoch 秒）/ last_human
            （最後の人の発言の epoch 秒）/ base_context（最初の応答が読んだ文脈）。
            読めなければ空の値
        """
        empty = {
            "running": [],
            "carried": {},
            "last_response": None,
            "last_human": None,
            "base_context": None,
        }
        if not transcript_path:
            return empty
        now = time.time() if now is None else now
        with self._lock:
            state = self._files.setdefault(transcript_path, self._new_state())
            try:
                size = os.path.getsize(transcript_path)
            except OSError:
                return empty
            if size < state["offset"]:
                # 書き直された（ふつうは無い）。最初から読み直す
                state.clear()
                state.update(self._new_state())
            if size > state["offset"]:
                records, state["offset"] = self_clear._iter_records(
                    transcript_path, state["offset"]
                )
                for record in records:
                    self._apply(state, record)
            live = {k: v for k, v in state["running"].items() if v > now}
            return {
                "running": sorted(live),
                "carried": {k: v for k, v in live.items() if _carried(k)},
                "last_response": state["last_response"],
                "last_human": state["last_human"],
                "base_context": state["base_context"],
            }

    def running(self, transcript_path, now=None):
        """走っている裏の仕事の番号の一覧（期限を過ぎたものは除く）。"""
        return self.scan(transcript_path, now)["running"]

    @staticmethod
    def _new_state():
        return {
            "offset": 0,
            "running": {},
            "last_response": None,
            "last_human": None,
            "base_context": None,
        }

    @staticmethod
    def _apply(state, record):
        if record.get("isSidechain"):
            return
        running = state["running"]
        at = _parse_ts(record.get("timestamp")) or time.time()
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "compact_boundary":
            # 要約（自動・/compact）で文脈が縮んだ。同じ会話記録に続くので、基準を取り直す
            state["base_context"] = None
        if kind == "assistant":
            state["last_response"] = at
            if state["base_context"] is None:
                found = usage_tracker.usage_of(record)
                context = usage_tracker.context_of(found[2]) if found else 0
                if context:
                    state["base_context"] = context
        human_at = self_clear.human_utterance_epoch(record)
        if human_at is not None:
            # 作業中に吸い込まれた発言も数え、本アプリが送った自動の文
            # （区切りの依頼・/clear の後の本文）は数えない
            state["last_human"] = max(state["last_human"] or 0.0, human_at)
        result = record.get("toolUseResult")
        if isinstance(result, dict):
            BackgroundWork._apply_result(running, result, at)
        text = notification_text(record)
        if not text:
            return
        status = TASK_STATUS_RE.search(text)
        ended = (
            status and status.group(1).lower() in TASK_END_STATUSES
        ) or MONITOR_EXPIRED_RE.search(text)
        if not ended:
            return
        for task_id in TASK_ID_RE.findall(text):
            running.pop(task_id, None)

    @staticmethod
    def _apply_result(running, result, at):
        if result.get("status") == "async_launched" and result.get("agentId"):
            running[str(result["agentId"])] = at + BACKGROUND_STALE_SEC
        elif result.get("backgroundTaskId"):
            running[str(result["backgroundTaskId"])] = at + BACKGROUND_STALE_SEC
        elif result.get("taskId") and "timeoutMs" in result:
            # Monitor。persistent でなければ timeoutMs で切れる
            timeout = result.get("timeoutMs")
            if result.get("persistent") or not isinstance(timeout, (int, float)):
                running[str(result["taskId"])] = at + BACKGROUND_STALE_SEC
            else:
                running[str(result["taskId"])] = at + timeout / 1000.0 + 60
        message = result.get("message")
        if isinstance(message, str):
            stopped = TASK_STOPPED_RE.search(message)
            if stopped:
                running.pop(stopped.group(1), None)
                if result.get("task_id"):
                    running.pop(str(result["task_id"]), None)
        if "scheduledFor" in result:
            scheduled = result.get("scheduledFor")
            if result.get("stopped") or not scheduled:
                running.pop(WAKEUP_ID, None)
            elif isinstance(scheduled, (int, float)):
                running[WAKEUP_ID] = scheduled / 1000.0 + WAKEUP_GRACE_SEC
        if "humanSchedule" in result and result.get("id"):
            # CronCreate。durable（ディスクに残る）は会話を区切っても消えないので数えない
            if not result.get("durable"):
                span = CRON_RECURRING_SEC if result.get("recurring") else CRON_ONCE_SEC
                running[CRON_PREFIX + str(result["id"])] = at + span
        elif set(result) == {"id"}:
            # CronDelete の結果は {"id": …} だけ
            running.pop(CRON_PREFIX + str(result["id"]), None)

    def forget_missing(self, live_paths):
        """もう見ない会話記録の分を捨てる（溜まり続けないように）。"""
        with self._lock:
            for path in [p for p in self._files if p not in live_paths]:
                del self._files[path]


# --- 「残す」印 ---------------------------------------------------------------


def keep_key(session):
    """印の鍵。プロセス（pid と開始時刻）ごとに付ける（/clear で会話の番号が変わっても残る）。"""
    return f"{session.get('pid')}:{session.get('started_at')}"


class KeepMarks:
    """利用者が付けた「残す」印（自動の区切りと起こし直しの対象から外す）。

    印を付けられるのは利用者（画面・REST）だけ。子の AI には選ばせない（利用者の決定）。
    """

    def __init__(self, path=KEEP_FILE):
        self.path = path
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def all(self):
        with self._lock:
            return self._load()

    def is_kept(self, session, marks=None):
        marks = self.all() if marks is None else marks
        return keep_key(session) in marks

    def set(self, session, kept):
        """印を付ける（kept=True）か外す。終わったプロセスの印はついでに掃除する。

        Returns:
            bool: 変えたか
        """
        key = keep_key(session)
        with self._lock:
            data = self._load()
            live = {keep_key(s) for s in sm.list_sessions()}
            data = {k: v for k, v in data.items() if k in live or k == key}
            changed = (key in data) != bool(kept)
            if kept:
                data.setdefault(
                    key,
                    {
                        "name": session.get("name") or "",
                        "cwd": session.get("cwd") or "",
                        "marked_at": _now_text(),
                    },
                )
            else:
                data.pop(key, None)
            self._save(data)
        return changed


# --- 判定 -------------------------------------------------------------------


def idle_seconds(session, now):
    """入力待ちになってからの秒数（入力待ちでなければ None）。

    状態ファイルの statusUpdatedAt はプロセスが起きるたびに今の時刻になる
    （--resume で起こし直した直後も「止まって数秒」に見える・評価役の実測）。
    控えの年齢には使わず、「応答を終えてから落ち着いたか」だけに使う。
    """
    if session.get("status") != "idle":
        return None
    updated = session.get("status_updated_at")
    if not updated:
        return None
    return max(0.0, now - float(updated) / 1000.0)


def exclusions(session, background, kept, reserved):
    """自動の区切り・起こし直しから外す理由の一覧（空なら外さない）。"""
    reasons = []
    if (session.get("kind") or "interactive") != "interactive":
        reasons.append("入力欄の無い実行")
    if session.get("attention") or session.get("status") == "waiting":
        reasons.append("許可待ち・答え待ち")
    if background:
        reasons.append(f"裏の仕事が走っている（{len(background)}件）")
    if kept:
        reasons.append("残す印")
    if reserved:
        reasons.append("区切りの予約が既にある")
    return reasons


def decide(
    session,
    context,
    now,
    config,
    last_response=None,
    last_human=None,
    base_context=None,
):
    """規則に当たるかを返す（除外は呼び手が先に見る）。

    どちらの規則も、最後の応答（＝最後に API を呼んだ時刻）から58分未満、つまり読み直しの
    控え（プロンプトキャッシュ・1時間）が生きている間だけ当てる。控えが切れた会話へ送ると、
    依頼の1往復そのものが大きな文脈の読み直しになり、節約にならない（2026-10-12 実測:
    2日止まった文脈 76万の会話が当たっていた）。控えが切れた会話は、次に仕事が来て止まったときに拾う。
    最後の応答の時刻が分からない会話には当てない。
    どちらの規則も、人の最後の発言から10分は当てない（人と話している最中に区切らない）。

    Args:
        last_response: 会話記録の最後の応答の epoch 秒
        last_human: 会話記録の最後の人の発言の epoch 秒（無ければ None）
        base_context: 会話の最初の応答が読んだ文脈（分からなければ None＝伸びを見ない）

    Returns:
        (rule, detail) か None。rule は "size"（大きさ）/ "idle"（止まった時間）
    """
    idle = idle_seconds(session, now)
    if idle is None or not context or not last_response:
        return None
    age_min = max(0.0, now - last_response) / 60.0
    if age_min >= CACHE_DEADLINE_MINUTES:
        return None
    if base_context and context - base_context < config["min_growth_tokens"]:
        # 区切り直しても縮まない（引き継ぎを読んだだけの会話）
        return None
    if last_human is not None and now - last_human < self_clear.HUMAN_QUIET_SEC:
        # どちらの規則も、人の最後の発言から10分は当てない（次の見回りへ回す・利用者の決定 2026-10-20）
        return None
    human_quiet = last_human is None or (
        now - last_human >= config["human_quiet_minutes"] * 60
    )
    if context > config["size_tokens"] and idle >= SETTLE_SEC and human_quiet:
        return (
            "size",
            f"文脈 {context:,} > {config['size_tokens']:,}・最後の応答から {age_min:.1f} 分",
        )
    if context > config["idle_tokens"] and age_min >= config["idle_minutes"]:
        return (
            "idle",
            f"最後の応答から {age_min:.1f} 分・文脈 {context:,} > {config['idle_tokens']:,}",
        )
    return None


# --- 起こし直し（管理外のセッション） -------------------------------------------

# 手で開いたセッションは人が入力欄を使っているかもしれないので、入力待ちがこれだけ
# 続いているものだけ起こし直す（秒）
RELAUNCH_MIN_IDLE_SEC = 300


class RelaunchError(Exception):
    """起こし直せないとき（理由は文面に入れる）。stopped は元のセッションを止めた後か。"""

    def __init__(self, message, stopped=False):
        super().__init__(message)
        self.stopped = stopped


def relaunch_blockers(session, view, was_relaunched):
    """起こし直せない理由の一覧（空なら起こし直せる）。"""
    blockers = []
    if session.get("launched_by_app"):
        blockers.append("既に管理下")
    if not session.get("session_id"):
        blockers.append("会話の番号が無い")
    elif was_relaunched(session.get("session_id")):
        blockers.append("この会話は一度起こし直した")
    blockers.extend(view["excluded"])
    idle = view["idle_minutes"]
    if session.get("status") != "idle" or idle is None:
        blockers.append("入力待ちでない")
    elif idle * 60 < RELAUNCH_MIN_IDLE_SEC:
        blockers.append(
            f"入力待ちになって {RELAUNCH_MIN_IDLE_SEC / 60:g} 分たっていない（人が使っているかもしれない）"
        )
    return blockers


# --- 本体 -------------------------------------------------------------------


class AutoClear:
    """見回りの本体。tick() を定期的に呼ぶ（claude-web-app の常駐タスク）。"""

    def __init__(
        self,
        tracker,
        transcript_path_for,
        keep_marks=None,
        background=None,
        state_file=STATE_FILE,
        result_log=RESULT_LOG,
        sender=None,
    ):
        """
        Args:
            tracker: usage_tracker.UsageTracker（文脈の大きさを読む）
            transcript_path_for: (cwd, session_id) → 本体の会話記録のパス
            keep_marks: KeepMarks（省略で既定のファイル）
            background: BackgroundWork（省略で新しく作る）
            sender: 送る関数（試験用。省略で sm.send_to_session）
        """
        self.tracker = tracker
        self.transcript_path_for = transcript_path_for
        self.keep_marks = keep_marks or KeepMarks()
        self.background = background or BackgroundWork()
        self.state_file = state_file
        self.result_log = result_log
        self.sender = sender or sm.send_to_session
        # 記録（_recent）と状態（_state）の鍵。見回りの糸と REST の糸の両方から触る
        self._lock = threading.RLock()
        self.config = config_from({})
        self._state = self._load_state()
        # 再起動しても直近の結果が GET で見えるよう、記録の末尾を読み込んでおく
        self._recent = self._load_recent()
        # pid → 送り直してよい時刻（人の書きかけに当たったとき）
        self._retry_after = {}
        # プロセス（keep_key）→ /clear をまたいで引き継ぐ裏の仕事 {番号: 期限}
        self._carry = {}
        self._last_tick = None
        self._last_candidates = []

    # --- 状態ファイル ---

    def _load_state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key in ("sent", "dry_run", "relaunched"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _load_recent(self):
        try:
            with open(self.result_log, "r", encoding="utf-8") as f:
                lines = f.readlines()[-RECENT_LIMIT:]
        except OSError:
            return []
        recent = []
        for line in lines:
            try:
                recent.append(json.loads(line))
            except ValueError:
                continue
        return recent

    def _save_state(self):
        """状態を書き出す（_lock の中で呼ぶ）。"""
        now = time.time()
        for key in ("sent", "dry_run", "relaunched"):
            self._state[key] = {
                k: v
                for k, v in self._state[key].items()
                if now - float(v.get("at_epoch") or 0) < STATE_KEEP_SEC
            }
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as e:
            safe_console.say(f"[AutoClear] 状態を書けませんでした: {e}", flush=True)

    def _put_state(self, key, session_id, value):
        with self._lock:
            self._state[key][session_id] = value
            self._save_state()

    def _has_state(self, key, session_id):
        with self._lock:
            return session_id in self._state[key]

    def _record(self, entry):
        """結果を記録する（ファイルとメモリ）。書けなくても見回りは止めない。"""
        entry.setdefault("at", _now_text())
        with self._lock:
            self._recent.append(entry)
            del self._recent[:-RECENT_LIMIT]
            try:
                os.makedirs(os.path.dirname(self.result_log), exist_ok=True)
                if (
                    os.path.exists(self.result_log)
                    and os.path.getsize(self.result_log) > RESULT_LOG_MAX_BYTES
                ):
                    os.replace(self.result_log, self.result_log + ".1")
                with open(self.result_log, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as e:
                safe_console.say(f"[AutoClear] 記録を書けませんでした: {e}", flush=True)
        # 出力の失敗で例外を出すと、送れた当たりが send_failed に化け、見回りも止まる（2026-10-22）
        safe_console.say(
            f"[AutoClear] {entry.get('outcome')} pid={entry.get('pid')} "
            f"{entry.get('name', '')} {entry.get('rule', '')}: {entry.get('detail', '')}",
            flush=True,
        )

    # --- 見回り ---

    def _scan(self, session, now):
        """会話記録を読み、/clear の前の会話で始まった起床の予約・定期の仕事を足して返す。"""
        session_id = session.get("session_id")
        path = self.transcript_path_for(session.get("cwd"), session_id)
        scanned = self.background.scan(path, now)
        key = keep_key(session)
        with self._lock:
            entry = self._carry.get(key) or {
                "session_id": session_id,
                "current": {},
                "previous": {},
            }
            if entry["session_id"] != session_id:
                # 会話が変わった（/clear）。前の会話で始まった分を引き継ぐ
                entry["previous"].update(entry["current"])
                entry["session_id"] = session_id
            entry["current"] = dict(scanned["carried"])
            entry["previous"] = {k: v for k, v in entry["previous"].items() if v > now}
            self._carry[key] = entry
            previous = set(entry["previous"])
        running = sorted(set(scanned["running"]) | previous)
        return path, dict(scanned, running=running)

    def assess(self, session, now=None, marks=None, reserved_pids=None):
        """1つのセッションの見立て（画面・GET・起こし直しで使う）。

        Returns:
            dict: context_tokens / base_context_tokens（最初の応答の文脈）/
            idle_minutes（入力待ちになってから）/
            response_age_minutes（最後の応答から）/ background（走っている裏の仕事の数）/
            kept / excluded（外す理由の一覧）/ rule・rule_detail（当たる規則。無ければ None）
        """
        now = time.time() if now is None else now
        path, scanned = self._scan(session, now)
        context = self.tracker.context_tokens(path) if path else None
        if reserved_pids is None:
            reserved_pids = {str(r["pid"]) for r in self_clear.status()["pending"]}
        kept = self.keep_marks.is_kept(session, marks)
        excluded = exclusions(
            session, scanned["running"], kept, str(session.get("pid")) in reserved_pids
        )
        idle = idle_seconds(session, now)
        last_response = scanned["last_response"]
        hit = (
            decide(
                session,
                context,
                now,
                self.config,
                last_response=last_response,
                last_human=scanned["last_human"],
                base_context=scanned["base_context"],
            )
            if not excluded
            else None
        )
        return {
            "context_tokens": context,
            "base_context_tokens": scanned["base_context"],
            "idle_minutes": None if idle is None else round(idle / 60.0, 1),
            "response_age_minutes": (
                None if not last_response else round((now - last_response) / 60.0, 1)
            ),
            "background": len(scanned["running"]),
            "kept": kept,
            "excluded": excluded,
            "rule": hit[0] if hit else None,
            "rule_detail": hit[1] if hit else None,
        }

    def tick(self, sessions, settings, now=None):
        """1回の見回り。当たった会話へ送る（dry_run なら記録だけ）。

        Args:
            sessions: list_sessions() に attention を付けたもの（管理外も含めて渡す）
            settings: アプリの設定（auto_clear_* を読む）

        Returns:
            list[dict]: 今回当たったもの（試験・GET 用）
        """
        now = time.time() if now is None else now
        self.config = config_from(settings)
        self._expire_waits(now)
        mode = self.config["mode"]
        self._last_tick = _now_text()
        # 管理外の分も含め、いま動いている会話の読み位置は残す（起こし直しの一覧で読み直さない）
        self.background.forget_missing(
            {
                self.transcript_path_for(s.get("cwd"), s.get("session_id"))
                for s in sessions
            }
        )
        if mode == "off":
            self._last_candidates = []
            return []
        marks = self.keep_marks.all()
        reserved = {str(r["pid"]) for r in self_clear.status()["pending"]}
        seen_key = "sent" if mode == "on" else "dry_run"
        hits = []
        for session in sessions:
            if not session.get("launched_by_app") or sm.is_relogin_session(session):
                continue
            session_id = session.get("session_id")
            if not session_id or self._has_state(seen_key, session_id):
                continue
            view = self.assess(session, now, marks, reserved)
            if not view["rule"]:
                continue
            hits.append({"session": session, **view})
        self._last_candidates = [
            {
                "pid": h["session"].get("pid"),
                "name": h["session"].get("name") or "",
                "rule": h["rule"],
                "detail": h["rule_detail"],
            }
            for h in hits
        ]
        for hit in hits:
            # 1件の失敗で残りの当たりへ送れなくならないよう、当たりごとに受ける
            try:
                if mode == "dry_run":
                    self._note_dry_run(hit, now)
                else:
                    self._send(hit, now)
            except Exception as e:
                session = hit["session"]
                self_clear.drop_auto(session.get("pid"), session.get("session_id"))
                self._put_state(
                    seen_key,
                    session.get("session_id"),
                    {"at_epoch": now, "rule": hit["rule"], "failed": True},
                )
                self._record(
                    self._entry(hit, "send_failed", f"{type(e).__name__}: {e}")
                )
        return hits

    def _entry(self, hit, outcome, detail=""):
        session = hit["session"]
        return {
            "outcome": outcome,
            "rule": hit.get("rule"),
            "pid": session.get("pid"),
            "name": session.get("name") or "",
            "cwd": session.get("cwd") or "",
            "session_id": session.get("session_id"),
            "context_tokens": hit.get("context_tokens"),
            "idle_minutes": hit.get("idle_minutes"),
            "response_age_minutes": hit.get("response_age_minutes"),
            "detail": detail or hit.get("rule_detail") or "",
        }

    def _note_dry_run(self, hit, now):
        """dry_run: 送るはずだった相手と理由を記録する（同じ会話は一度だけ）。"""
        session_id = hit["session"].get("session_id")
        self._put_state("dry_run", session_id, {"at_epoch": now, "rule": hit["rule"]})
        self._record(self._entry(hit, "would_send"))

    def _still_eligible(self, session):
        """送る直前の見直し（当たりを集めてから押された「残す」・入った予約）。"""
        if self.keep_marks.is_kept(session):
            return False
        pending = {str(r["pid"]) for r in self_clear.status()["pending"]}
        return str(session.get("pid")) not in pending

    def _send(self, hit, now):
        """on: 区切りの依頼を送る。送れたら、その会話への予約を待つ印を self_clear に置く。"""
        session = hit["session"]
        pid = session.get("pid")
        session_id = session.get("session_id")
        if now < self._retry_after.get(str(pid), 0) or not self._still_eligible(
            session
        ):
            return
        wait_sec = self.config["handoff_wait_minutes"] * 60
        # 送る前に印を置く（子がすぐ予約しても取りこぼさない）
        self_clear.expect_auto(pid, session_id, now + wait_sec)
        target = send_history.target_of(session, managed=True)
        offsets = sm._transcript_sizes(session_id)
        text = trigger_text(self.config["port"])
        try:
            result = self.sender(pid, text=text, preamble="", protect_draft=True)
            detail = f"{hit.get('rule_detail')}・{result.get('elapsed_sec')}秒"
        except sm.SessionBusyError:
            # 送る直前に作業へ戻った。次の見回りで見直す
            self_clear.drop_auto(pid, session_id)
            return
        except sm.SessionError as e:
            draft = isinstance(e, sm.SessionDraftError)
            if not draft and sm._transcript_has_prompt(session_id, text, offsets):
                # 打鍵は済み、会話記録との照合だけが時間切れだった。子には届いているので
                # 印を残し、送った扱いにする（印を外すと、区切った後に作業を再開させてしまう）
                detail = (
                    f"{hit.get('rule_detail')}・照合は時間切れだったが会話記録に載った"
                )
            else:
                self_clear.drop_auto(pid, session_id)
                if draft:
                    self._retry_after[str(pid)] = now + DRAFT_RETRY_SEC
                else:
                    # 送れない相手へ毎分送り直さないよう、この会話は送った扱いにする
                    self._put_state(
                        "sent",
                        session_id,
                        {"at_epoch": now, "rule": hit["rule"], "failed": True},
                    )
                send_history.record(
                    "auto_clear",
                    ok=False,
                    target=target,
                    text=text,
                    source="auto_clear",
                    error=self_clear._safe_detail(e),
                    code=type(e).__name__,
                )
                self._record(
                    self._entry(
                        hit,
                        "draft_present" if draft else "send_failed",
                        self_clear._safe_detail(e),
                    )
                )
                return
        self._put_state("sent", session_id, {"at_epoch": now, "rule": hit["rule"]})
        send_history.record(
            "auto_clear",
            ok=True,
            target=target,
            text=text,
            source="auto_clear",
            detail={"rule": hit["rule"], "context_tokens": hit.get("context_tokens")},
        )
        self._record(self._entry(hit, "sent", detail))

    def _expire_waits(self, now):
        """予約が来ないまま待ちの上限を過ぎた依頼を「予約なし」として記録する。

        印は残す（遅れて来た予約も自動の区切りとして扱うため。self_clear 側で一定時間後に捨てる）。
        """
        for expired in self_clear.pop_expired_auto(now):
            self._record(
                {
                    "outcome": "no_reservation",
                    "pid": expired["pid"],
                    "session_id": expired["session_id"],
                    "detail": "区切りの依頼を送ったが、待ちの上限までに予約が来なかった",
                }
            )

    # --- 起こし直しの記録 ---

    def was_relaunched(self, session_id):
        return self._has_state("relaunched", session_id)

    def mark_relaunched(self, session_id, info):
        self._put_state("relaunched", session_id, dict(info, at_epoch=time.time()))
        self._record(dict(info, outcome="relaunched", session_id=session_id))

    # --- 管理外のセッション（3） ---

    def unmanaged(self, sessions, now=None):
        """手で開いたセッション（台帳に無いもの）の一覧と、起こし直せるかの見立て。"""
        now = time.time() if now is None else now
        marks = self.keep_marks.all()
        reserved = {str(r["pid"]) for r in self_clear.status()["pending"]}
        items = []
        for session in sessions:
            if session.get("launched_by_app") or sm.is_relogin_session(session):
                continue
            view = self.assess(session, now, marks, reserved)
            blockers = relaunch_blockers(session, view, self.was_relaunched)
            items.append(
                {
                    "pid": session.get("pid"),
                    "name": session.get("name") or "",
                    "cwd": session.get("cwd") or "",
                    "session_id": session.get("session_id"),
                    "status": session.get("status"),
                    "win_session": session.get("win_session"),
                    "elevated": session.get("elevated"),
                    "url": session.get("url"),
                    "context_tokens": view["context_tokens"],
                    "idle_minutes": view["idle_minutes"],
                    "response_age_minutes": view["response_age_minutes"],
                    "background": view["background"],
                    "kept": view["kept"],
                    "relaunched_before": self.was_relaunched(session.get("session_id")),
                    "can_relaunch": not blockers,
                    "blockers": blockers,
                }
            )
        return items

    def relaunch(
        self,
        session,
        claude_exe=None,
        now=None,
        stop=None,
        launch=None,
        options_of=None,
        precheck=None,
        check_input=None,
    ):
        """手で開いたセッションを止め、同じ会話を --resume で管理下に起こし直す（一度だけ）。

        会話の番号（sessionId）は変わらない。元のモデル・モード・思考の深さ（コマンドラインから
        読めたもの）と、Windows セッション・権限（管理者か）を引き継ぐ。
        止める前に、起こし直せること（会話記録・フォルダ・引数・claude の実行ファイル）と、
        入力欄に人の書きかけが無いことを確かめる（止めてから起こせない、を避けるため）。

        Returns:
            dict: old_pid / pid（新しいプロセス）/ session_id / options / dropped_args /
            win_session / elevated

        Raises:
            RelaunchError: 起こし直せない（理由つき）・止めたが起こせなかった
        """
        now = time.time() if now is None else now
        stop = stop or sm.stop_session
        launch = launch or sm.launch_session
        options_of = options_of or sm.launch_options_of
        precheck = precheck or _relaunch_precheck
        check_input = check_input or sm.check_input_area
        view = self.assess(session, now)
        blockers = relaunch_blockers(session, view, self.was_relaunched)
        if blockers:
            raise RelaunchError("起こし直せません: " + "・".join(blockers))
        pid = session.get("pid")
        session_id = session.get("session_id")
        dropped = []
        options = options_of(pid, dropped)
        win = session.get("win_session")
        elevated = session.get("elevated")
        launch_args = dict(options)
        if win is not None:
            launch_args["win_session"] = str(win)
        if elevated is not None:
            launch_args["elevation"] = "admin" if elevated else "standard"
        try:
            precheck(session, options, claude_exe)
        except sm.SessionError as e:
            raise RelaunchError(f"起こし直せません（止める前の確かめ）: {e}")
        try:
            check_input(pid)
        except sm.SessionDraftError as e:
            raise RelaunchError(
                "起こし直せません: 入力欄に人の書きかけがあります"
                f"（{self_clear._safe_detail(e)}）"
            )
        except sm.SessionError as e:
            raise RelaunchError(
                f"起こし直せません: 入力欄を確かめられませんでした（{self_clear._safe_detail(e)}）"
            )
        info = {
            "old_pid": pid,
            "name": session.get("name") or "",
            "cwd": session.get("cwd") or "",
            "options": launch_args,
            "dropped_args": dropped,
        }
        try:
            stop(pid)
        except sm.SessionError as e:
            raise RelaunchError(f"元のセッションを止められませんでした: {e}")
        # 止めた後は、起こせても起こせなくても二度目は断る（止めて起こすを繰り返さない）
        try:
            started = launch(
                cwd=session.get("cwd"),
                resume_session_id=session_id,
                claude_exe=claude_exe,
                **launch_args,
            )
        except Exception as e:
            self.mark_relaunched(session_id, dict(info, failed=True, detail=str(e)))
            raise RelaunchError(
                f"元のセッションは止めましたが、起こし直せませんでした: {e}。"
                f"会話は残っているので、画面の「続きから再開」で {session_id} を選ぶと再開できます",
                stopped=True,
            )
        info.update({"pid": started.get("pid"), "elevated": started.get("elevated")})
        self.mark_relaunched(session_id, info)
        return dict(info, session_id=session_id, win_session=started.get("win_session"))

    def status(self):
        """GET /api/auto-clear の中身。"""
        with self._lock:
            recent = list(self._recent)
        return {
            "config": dict(self.config),
            "last_tick": self._last_tick,
            "candidates": list(self._last_candidates),
            "waiting_for_reservation": self_clear.auto_expectations(),
            "keep": self.keep_marks.all(),
            "recent": recent,
        }


def _relaunch_precheck(session, options, claude_exe):
    """止める前に、起こし直しが通るかを確かめる（launch_session が見るものを先に見る）。

    Raises:
        SessionError: 起こし直せない
    """
    cwd = session.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        raise sm.SessionError(f"作業フォルダが見つかりません: {cwd}")
    sm.validate_resume_target(
        cwd, session.get("session_id"), ignore_pid=session.get("pid")
    )
    sm.build_launch_args(
        options.get("model"), options.get("permission_mode"), options.get("effort")
    )
    sm.resolve_claude_exe(claude_exe)

# -*- coding: utf-8 -*-
"""人の手を待つこと・自動で送れなかったことを、ユーザーではなく管理役へ知らせる（2026-11-01・伝言 49cc8aa92134 の (b)）。

ユーザー（原文・10-31）「bは私に言わないで。管理役とか内部でなんとかするべき」。
本アプリが見つけた次のことを、管理役宛ての伝言（host_pc の送り箱・claude_send.py の put）に置く。
ntfy の人のトピックへは送らない。

    - 許可待ち・答え待ち（状態ファイルで確かめたもの）が permission_wait_minutes 分続いた子
    - 枠切れで止まったのに本アプリが続きを頼まない・頼めなかった子（quota_resume.py が notify を呼ぶ。
      本アプリが頼む形・頼めたときは手が要らないので知らせない＝管理役を起こすとトークンを食う）
    - 自動の送信（自動の区切りの依頼・区切りの予約の /clear・機械からの伝言・起こし直し）が送れなかったこと
      （send_history の聞き手。作業中・書きかけで後回しにしただけのものは数えない）

同じこと（種類と鍵の組）は一度だけ知らせる。知らせは溜めて、MIN_INTERVAL_SEC に1通へまとめる
（管理役の入力欄を埋めないため。本文は claude_send の上限 2000 字に収める）。
送り箱へ置くだけで、ssh も答えの待ちもしない（運ぶのは常駐の mail_follower）。

設定 liaison（受けるたびに読む・再起動は要らない）:
    tool: claude_send.py の絶対パス（空なら知らせない。公開の木・ほかの機械では空のまま）
    from: 送り主の名前（既定 claude-web-app）・to: 宛先の役（既定 管理役）
    permission_wait_minutes: 許可待ちを知らせるまでの分（既定 10）
"""

import importlib.util
import json
import os
import threading
import time
from datetime import datetime

import safe_console
import send_history
import session_manager as sm

STATE_FILE = os.path.join(sm.APP_STATE_DIR, "liaison.json")

DEFAULT_FROM = "claude-web-app"
DEFAULT_TO = "管理役"
DEFAULT_PERMISSION_WAIT_MINUTES = 10

# 1通を送ってから次の1通までの最短の間（秒）
MIN_INTERVAL_SEC = 5 * 60
# 最初の知らせから、まとめて送るまで待つ（秒。続けて起きたものを1通にするため）
BATCH_SEC = 30
# 送り箱へ置けなかったときに置き直すまでの間（秒）
RETRY_SEC = 5 * 60
# 溜める上限（古いものから捨てる）
MAX_PENDING = 200
# 1通の本文の上限（claude_send.MAX_TEXT と同じ）
MAX_TEXT = 2000
# 一度知らせた鍵を覚えておく日数。週の枠は見つけてから頼めなくなるまで最大7日＋6時間空くので、それより長く
# （quota_resume が頼まなかったときに見つけたときと同じ鍵で知らせ直し、覚えていれば二度は送らないため・2026-11-02）
KEEP_DAYS = 8


# 知らせの札。管理役が一読で手の要るものを拾えるよう、要対応か知らせだけかを添える
# （枠切れは、本アプリが頼むかどうかを本文に書く）
KIND_LABELS = {
    "permission": "許可待ち・要対応",
    "input": "答え待ち・要対応",
    "quota_gave_up": "枠切れ・本アプリは頼まない・要対応",
    "quota_resume_failed": "枠が戻ったが送れなかった・要対応",
    "quota_not_idle": "枠が戻ったが入力待ちにならない・要対応",
    "send_failed": "送れなかった・要対応",
    "quota_alert_failed": "週の枠の停止を人へ鳴らせなかった・要対応",
}
# 1件の行の上限（字）
MAX_LINE = 400

# 送れなかった件を知らせる送り元（send_history の source）。人が画面から送ったものは画面に出るので外す。
# quota_resume は自分で諦めたときだけ知らせる（quota_resume.py）
AUTO_SOURCES = ("auto_clear", "self_clear", "relaunch")
AUTO_SOURCE_PREFIXES = ("machine:",)
# 後回しにしただけ（送り直す）の失敗
DEFERRED_CODES = ("SessionBusyError", "SessionDraftError")


def _conf(settings):
    conf = (settings or {}).get("liaison")
    return conf if isinstance(conf, dict) else {}


def _now_text(epoch):
    return datetime.fromtimestamp(epoch).astimezone().strftime("%m-%d %H:%M")


def _label(session):
    return session.get("name") or os.path.basename(session.get("cwd") or "") or "?"


class Liaison:
    """管理役への知らせを溜めて、まとめて送り箱へ置く。"""

    def __init__(self, state_file=None, settings_loader=None, tool_loader=None):
        # None なら使うたびに STATE_FILE を引く（試験が置き場を差し替えられるように・2026-11-02）
        self._state_file = state_file
        # 設定を読む関数（試験で差し替える）
        self.settings_loader = settings_loader or (lambda: {})
        # tool のパス → claude_send のモジュール（試験で差し替える）
        self.tool_loader = tool_loader or _load_tool
        self._lock = threading.Lock()
        self._state = self._load()

    # --- 記録 ---

    @property
    def state_file(self):
        return self._state_file or STATE_FILE

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("seen", {})
                data.setdefault("pending", [])
                data.setdefault("last_sent_epoch", 0)
                return data
        except (OSError, ValueError):
            pass
        return {"seen": {}, "pending": [], "last_sent_epoch": 0}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_file)
        except OSError as e:
            safe_console.say(f"[Liaison] 記録を書けませんでした: {e}", flush=True)

    def enabled(self, settings=None):
        conf = _conf(self.settings_loader() if settings is None else settings)
        tool = conf.get("tool")
        return isinstance(tool, str) and bool(tool.strip())

    # --- 受ける ---

    def notify(self, kind, key, text, now=None, settings=None):
        """知らせを1件溜める。同じ（kind, key）は一度だけ。溜めたら True。"""
        if not self.enabled(settings):
            return False
        now = time.time() if now is None else now
        seen_key = f"{kind}|{key}"
        with self._lock:
            if seen_key in self._state["seen"]:
                return False
            self._state["seen"][seen_key] = now
            pending = self._state["pending"]
            pending.append({"kind": kind, "text": str(text), "at_epoch": now})
            del pending[:-MAX_PENDING]
            self._save()
        return True

    def on_history(self, entry):
        """send_history の聞き手。自動の送信が送れなかった件を溜める。"""
        if entry.get("ok") is not False:
            return
        source = entry.get("source") or ""
        if source not in AUTO_SOURCES and not source.startswith(AUTO_SOURCE_PREFIXES):
            return
        # 自動の区切り・機械の伝言は書きかけでも送り直すので数えない。区切りの予約は書きかけで
        # 止まる（送り直さない）ので知らせる
        if entry.get("code") in DEFERRED_CODES and source != "self_clear":
            return
        target = entry.get("target")
        if isinstance(target, dict):
            who = target.get("name") or os.path.basename(target.get("cwd") or "") or "?"
            who += f"（pid {target.get('pid')}）"
        else:
            who = str(target or "?")
        # 同じ相手・同じ種類の失敗は、送り直しのたびではなく1回だけ（会話が変われば別）
        session_id = target.get("session_id") if isinstance(target, dict) else ""
        key = f"{source}|{entry.get('kind')}|{session_id or who}"
        kind_label = send_history.KIND_LABELS.get(entry.get("kind"), entry.get("kind"))
        # 送信の失敗の文には相手の画面の末尾が付くことがある（self_clear は切らずに残す）。
        # 別の子の画面を管理役へ流さないよう、そこから後ろは捨てる
        reason = str(entry.get("error") or "").split("画面の末尾:")[0].strip()
        self.notify(
            "send_failed",
            key,
            f"{kind_label}（{source}）を {who} へ送れなかった: {reason or entry.get('code') or '理由なし'}",
        )

    def observe_sessions(self, sessions, settings=None, now=None):
        """一覧（attention 付き）から、長く人の手を待っている子を溜める。"""
        settings = self.settings_loader() if settings is None else settings
        if not self.enabled(settings):
            return 0
        now = time.time() if now is None else now
        try:
            minutes = float(
                _conf(settings).get(
                    "permission_wait_minutes", DEFAULT_PERMISSION_WAIT_MINUTES
                )
            )
        except (TypeError, ValueError):
            minutes = DEFAULT_PERMISSION_WAIT_MINUTES
        count = 0
        for session in sessions:
            attention = session.get("attention")
            if not attention or not attention.get("certain"):
                continue
            since = attention.get("since")
            if not isinstance(since, (int, float)):
                continue
            waited = now - since / 1000.0
            if waited < minutes * 60:
                continue
            kind = "input" if attention.get("kind") == "input" else "permission"
            text = (
                f"{_label(session)}（pid {session.get('pid')}・{session.get('account') or '本家'}）が"
                f" {_now_text(since / 1000.0)} から{attention.get('label') or KIND_LABELS[kind]}"
                f"（{int(waited // 60)}分）"
            )
            if self.notify(
                kind, f"{session.get('pid')}|{since}", text, now, settings=settings
            ):
                count += 1
        return count

    # --- 送る ---

    def flush(self, settings=None, now=None):
        """溜めた知らせを1通にまとめて送り箱へ置く。置いた件数を返す（置かなければ 0）。"""
        settings = self.settings_loader() if settings is None else settings
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            pending = list(self._state["pending"])
            if not pending:
                return 0
            if now - self._state.get("last_sent_epoch", 0) < MIN_INTERVAL_SEC:
                return 0
            if now < self._state.get("retry_after_epoch", 0):
                return 0
            if now - min(p["at_epoch"] for p in pending) < BATCH_SEC:
                return 0
        conf = _conf(settings)
        if not self.enabled(settings):
            # 設定から外された・設定を一瞬読めなかった（読めないと既定＝tool が空になる）。
            # 見分けられないので捨てずに残す（溜まりは MAX_PENDING で頭打ち）
            return 0
        text, used = compose(pending)
        try:
            tool = self.tool_loader(conf["tool"].strip())
            sender = conf.get("from") or DEFAULT_FROM
            to = conf.get("to") or DEFAULT_TO
            why = tool.check(sender, to, text)
            if why:
                raise ValueError(why)
            follower = tool.load_follower()
            if follower is None:
                raise RuntimeError("常駐（mail_follower）を読めません")
            path = tool.put(follower.SPOOL, sender, to, text)
        except Exception as e:
            with self._lock:
                self._state["retry_after_epoch"] = now + RETRY_SEC
                self._save()
            safe_console.say(
                f"[Liaison] 管理役への知らせを置けませんでした（{RETRY_SEC // 60}分後に置き直す）: {e}",
                flush=True,
            )
            return 0
        with self._lock:
            # 置いた分だけ外す（置いている間に増えた分は残す）
            sent = {id(p) for p in pending[:used]}
            self._state["pending"] = [
                p for p in self._state["pending"] if id(p) not in sent
            ]
            self._state["last_sent_epoch"] = now
            self._state.pop("retry_after_epoch", None)
            self._save()
        safe_console.say(
            f"[Liaison] 管理役へ {used} 件の知らせを置いた: {os.path.basename(path)}",
            flush=True,
        )
        return used

    def _prune(self, now):
        cutoff = now - KEEP_DAYS * 86400
        seen = self._state["seen"]
        for key in [k for k, v in seen.items() if v < cutoff]:
            del seen[key]

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled(),
                "pending": list(self._state["pending"]),
                "last_sent_epoch": self._state.get("last_sent_epoch") or None,
            }


def compose(pending):
    """溜めた知らせから1通の本文を作る。(本文, 入れた件数)。入りきらない分は次の1通へ回す。"""
    head = "【claude-web-app の自動の知らせ・ユーザーへは知らせていません】管理役で扱ってください。"
    lines = []
    size = len(head)
    used = 0
    for item in pending:
        line = f"\n- [{KIND_LABELS.get(item['kind'], item['kind'])}] {item['text']}"
        if len(line) > MAX_LINE:
            line = line[: MAX_LINE - 1] + "…"
        # 残りの件数の添え書きの分を空けておく
        if size + len(line) > MAX_TEXT - 40:
            break
        lines.append(line)
        size += len(line)
        used += 1
    rest = len(pending) - used
    tail = f"\n（ほか {rest} 件は次の便で）" if rest else ""
    return head + "".join(lines) + tail, used


_tools = {}


def _load_tool(path):
    """claude_send.py をパスから読む（読めたものは使い回す）。"""
    module = _tools.get(path)
    if module is not None:
        return module
    if not os.path.isfile(path):
        raise FileNotFoundError(f"liaison.tool がありません: {path}")
    spec = importlib.util.spec_from_file_location("claude_send_for_liaison", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _tools[path] = module
    return module

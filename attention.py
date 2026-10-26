# -*- coding: utf-8 -*-
"""人の手を待っているセッション（許可待ち・質問への答え待ち）を見分けるモジュール。

判定の材料は2つ。どちらも claude 側が書くものを読むだけで、本アプリは状態を持たない。

1. 状態ファイル（``~/.claude/sessions/<pid>.json``）の ``status``。
   claude は許可の確認・質問のダイアログを出している間 ``status: "waiting"`` と
   ``waitingFor``（``"permission prompt"`` / ``"input needed"`` / ``"worker request"`` /
   ``"sandbox request"`` 等）を書く（claude 2.1.288 の本体の文字列で確認・2026-10-12）。
   これが主な判定で、ここで waiting なら確定として出す。
2. Notification フック（``claude_event_log_hook.py``）の記録
   （``~/.claude/manager/events/events-YYYYMM.jsonl``）の
   「Claude needs your permission」。状態ファイルの書き込みが漏れたときの控え。
   通知のあとに状態ファイルも会話記録も動いていなければ「許可待ちかも」として出す。

出し漏れのほうを避けたい（ユーザーの要望・2026-10-12）ので、控えの判定は
「解消したと言える証拠（状態の更新・会話記録の追記）が無い限り出し続ける」側に倒す。
"""

import glob
import json
import os
import threading
import time
from datetime import datetime

# Notification フックの記録の置き場（claude_event_log_hook.py の EVENTS_DIR と同じ）
EVENTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "manager", "events")

# 読む記録の範囲。月替わりの直後でも前の月の記録を落とさないよう、新しい2つを読む
EVENT_FILES_TO_READ = 2

# 控えの判定で、これより古い通知は使わない（何日も動いていない窓を出し続けないため）
NOTIFICATION_MAX_AGE_SEC = 24 * 3600

# 道具の結果を探すとき、会話記録の末尾から読む大きさ
TRANSCRIPT_TAIL_BYTES = 256 * 1024

# 道具の結果の時刻を覚えておく会話記録の数の上限
TOOL_RESULT_CACHE_MAX = 500

# 通知の時刻は秒単位で丸められて書かれる。状態ファイルの更新（ミリ秒）と比べるときの遊び
NOTIFICATION_SLACK_SEC = 1.0

# waitingFor の値ごとの種類と表示名。知らない値も「応答待ち」として出す（出し漏れを避ける）
WAITING_KINDS = {
    "permission prompt": ("permission", "許可待ち"),
    "sandbox request": ("permission", "許可待ち"),
    "input needed": ("input", "答え待ち"),
}
OTHER_WAITING = ("other", "応答待ち")


def _parse_ts(text):
    """記録の時刻（オフセット付き ISO 8601）を epoch 秒にする。読めなければ None。"""
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def is_permission_message(message):
    """Notification の本文が許可の確認か（"Claude needs your permission[ to use X]"）。"""
    return "permission" in str(message or "").lower()


class NotificationLog:
    """Notification フックの記録を差分だけ読み、会話ごとの最後の許可の通知時刻を持つ。

    記録は追記だけなので、ファイルごとに読み終えた位置を覚え、増えた分だけ読む。
    """

    def __init__(self, events_dir=EVENTS_DIR):
        self.events_dir = events_dir
        self._offsets = {}
        self._last_permission = {}
        self._lock = threading.Lock()

    def refresh(self):
        """増えた記録を読み込む。読めないファイルは飛ばす（判定を止めない）。"""
        paths = sorted(glob.glob(os.path.join(self.events_dir, "events-*.jsonl")))
        with self._lock:
            for path in paths[-EVENT_FILES_TO_READ:]:
                self._read_new(path)

    def _read_new(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        offset = self._offsets.get(path, 0)
        if size < offset:
            # 作り直された（短くなった）ら先頭から読み直す
            offset = 0
        if size == offset:
            return
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(size - offset)
        except OSError:
            return
        # 書きかけの最後の行は次に回す
        end = chunk.rfind(b"\n")
        if end < 0:
            return
        for line in chunk[: end + 1].splitlines():
            if b"permission" not in line:
                continue
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if record.get("event") != "Notification" or not is_permission_message(
                record.get("message")
            ):
                continue
            session_id = record.get("session_id")
            ts = _parse_ts(record.get("ts"))
            if session_id and ts and ts > self._last_permission.get(session_id, 0):
                self._last_permission[session_id] = ts
        self._offsets[path] = offset + end + 1

    def last_permission(self, session_id):
        """その会話の最後の許可の通知の時刻（epoch 秒）。無ければ None。"""
        with self._lock:
            return self._last_permission.get(session_id)


def last_tool_result_time(path, _cache={}):
    """会話記録の末尾から、最後の道具の結果（tool_result）の時刻（epoch 秒）を返す。

    許可に答えが出れば（許可・拒否とも）、その道具の結果が会話記録に書かれる。
    会話記録の更新時刻で見ると、並行して動く別の道具の結果や添付の行でも
    「答えが出た」と早合点するため、道具の結果の行の時刻で見る（評価役の指摘）。
    大きさと更新時刻が変わらなければ前の結果を使う。読めなければ None。
    """
    try:
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    key = (stat.st_size, stat.st_mtime)
    cached = _cache.get(path)
    if cached and cached[0] == key:
        return cached[1]
    value = None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, stat.st_size - TRANSCRIPT_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if b'"tool_result"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("type") != "user":
            continue
        ts = _parse_ts(str(record.get("timestamp") or "").replace("Z", "+00:00"))
        if ts:
            value = ts
            break
    if len(_cache) > TOOL_RESULT_CACHE_MAX:
        # 終わった会話の分が溜まり続けないよう、溢れたら作り直す（1会話1件なので稀）
        _cache.clear()
    _cache[path] = (key, value)
    return value


def assess(session, permission_notified_at=None, answered_at=None, now=None):
    """1つのセッションが人の手を待っているかを判定する。

    Args:
        session: session_manager.list_sessions() の1要素
        permission_notified_at: その会話の最後の許可の通知時刻（epoch 秒）
        answered_at: 会話記録の最後の道具の結果の時刻（epoch 秒。無ければ None）
        now: 現在時刻（試験用）

    Returns:
        dict か None。dict は kind（permission / input / other）/ label（表示名）/
        since（待ち始めの epoch ミリ秒。分からなければ None）/
        certain（状態ファイルで確かめた True・通知だけからの推定 False）
    """
    if session.get("status") == "waiting":
        kind, label = WAITING_KINDS.get(session.get("waiting_for") or "", OTHER_WAITING)
        return {
            "kind": kind,
            "label": label,
            "since": session.get("status_updated_at"),
            "certain": True,
        }

    if not permission_notified_at:
        return None
    now = time.time() if now is None else now
    if now - permission_notified_at > NOTIFICATION_MAX_AGE_SEC:
        return None
    # 通知のあとに状態が変わった・道具の結果が書かれたなら、許可に答えが出ている
    status_updated = session.get("status_updated_at")
    if status_updated and status_updated / 1000.0 > (
        permission_notified_at + NOTIFICATION_SLACK_SEC
    ):
        return None
    if answered_at and answered_at > permission_notified_at + NOTIFICATION_SLACK_SEC:
        return None
    return {
        "kind": "permission",
        # 状態の印（作業中など）と食い違って見えるので、推定の根拠を名前に含める
        "label": "許可待ちかも（通知からの推定）",
        "since": int(permission_notified_at * 1000),
        "certain": False,
    }


def annotate(sessions, notification_log, transcript_path_for):
    """一覧の各セッションに attention を付け、人の手を待つものを先頭へ並べ替える。

    Args:
        sessions: session_manager.list_sessions() の結果（更新の新しい順）
        notification_log: NotificationLog（refresh は呼び手が済ませておく）
        transcript_path_for: (cwd, session_id) → 会話記録のパス を返す関数

    Returns:
        並べ替えた新しいリスト（待ちの確定 → 推定 → その他。同じ組の中は元の順）
    """
    for session in sessions:
        session_id = session.get("session_id")
        notified = notification_log.last_permission(session_id) if session_id else None
        answered = None
        if notified and session.get("status") != "waiting":
            path = transcript_path_for(session.get("cwd") or "", session_id)
            answered = last_tool_result_time(path) if path else None
        session["attention"] = assess(session, notified, answered)

    def rank(session):
        attention = session.get("attention")
        if not attention:
            return 2
        return 0 if attention["certain"] else 1

    # sorted は安定なので、同じ組の中は更新の新しい順のまま
    return sorted(sessions, key=rank)

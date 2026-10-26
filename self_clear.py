# -*- coding: utf-8 -*-
"""セッションが「自分を区切って」と頼める口（/clear の予約）。

セッションは自分のターンの中では作業中（busy）なので、send を自分宛てに呼ぶと
409 busy で止まる。そこで予約だけ受けてすぐ返し、そのセッションがターンを終えて
入力待ち（idle）が続いたところで、サーバ側から /clear と「引き継ぎを読んで続きから」を送る。

頼んだのが誰かは、申告ではなく接続から決める。127.0.0.1 へつないできた接続の
持ち主のプロセスを引き、その祖先をたどって最初に見つかった claude セッションを
「本人」とする（2026-10-12 実測: python ← bash ← … ← claude.exe とたどれ、
net_connections で接続の持ち主の pid が引ける）。サブエージェントのコマンドも同じ
claude.exe の子なので、加えて「本体の会話記録に、この道具を呼ぶ実行中の tool_use が
ある」ことを確かめる（サブエージェントの tool_use は subagents/ の別の記録に載る）。

予約の後に新しい仕事（人の発言・ほかのセッションからの連絡・タスクの通知）が
会話記録に載ったら、引き継ぎが古くなるので送らずに終える（superseded）。
人の最後の発言から HUMAN_QUIET_SEC（10分）たつまでは送らずに待つ（人と話している最中に
区切らない。2026-10-16 ある子が人と話している最中に自分の予約で区切られた・
利用者の決定 2026-10-20）。
人の書きかけは消さない（入力欄に文字があれば送らずに終える）。

予約はメモリにだけ置く（アプリが再起動すると待ち中の予約は消える。消えても
「区切られない」だけで害はない）。結果は RESULT_LOG に1行ずつ残す
（入力欄や画面の中身は残さない）。
"""

import datetime
import json
import os
import re
import threading
import time

import psutil

import safe_console
import send_history
import session_manager as sm

# 入力待ちになってから送るまで、入力待ちが続いていることを確かめる秒数。
# ターンを終えた直後に Stop フックが差し戻して作業を続けることがあるため、
# 一瞬の idle では送らない。
IDLE_STABLE_SEC = 10

# 予約の待ちの上限（秒）。既定 30 分。呼ぶ側が wait_sec で替えられる（範囲内に丸める）
DEFAULT_WAIT_SEC = 1800
MIN_WAIT_SEC = 60
MAX_WAIT_SEC = 3600

# 状態を見に行く間隔（秒）
POLL_SEC = 1.0

# 引き継ぎファイルのパスの長さの上限（文字）。本文が貼り付け扱いになる長さ
# （sm.PASTE_THRESHOLD_CHARS）を超えないように抑える
HANDOFF_PATH_MAX_LEN = 700

# ドライブ文字で始まる Windows の絶対パスだけを受ける（UNC で外の共有へつなぎに行かない）
HANDOFF_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]")

# /clear の後に送る本文。{path} に引き継ぎファイルの絶対パスが入る
RESUME_TEXT = (
    "区切りの /clear が済みました。引き継ぎファイル「{path}」を読んで、"
    "続きから進めてください。"
)

# 自動の区切り（auto_clear）の依頼に応えた予約のときに送る本文。
# 自動の区切りは作業の切れ目とは限らないので、続きを始めさせずに待たせる（利用者の決定・2026-10-12）
AUTO_RESUME_TEXT = (
    "自動の区切りの /clear が済みました。引き継ぎファイル「{path}」を読んで、"
    "次の依頼を待ってください。返事は不要です。"
)

# 人の最後の発言から、これだけたつまでは区切らない（秒）。auto_clear の当たりにも使う
HUMAN_QUIET_SEC = 600
# 人の発言の待ちが明けてから送るまでの余裕（秒）。待ちの上限がこれより先に来ないよう延ばす
HUMAN_QUIET_GRACE_SEC = 300

# 本アプリが打鍵で送る自動の文の頭。会話記録には人の発言（origin.kind=human）として載るので
# （2026-10-20 実測: 全プロジェクトの会話記録で 407件すべて origin.kind=human）、人の発言から外す。
# 自動の区切りの依頼の頭は auto_clear.TRIGGER_TEXT（照合は先頭で行うので頭は変えない）
AUTOMATED_PREFIXES = (
    "区切りの /clear が済みました。",
    "自動の区切りの /clear が済みました。",
    "自動の区切りです。",
)

# /clear だけ済んで本文が届かなかったとき、本文だけを送り直す回数
RESEND_ATTEMPTS = 3

# 本体の会話記録に、この道具を呼ぶ tool_use があるかを探す範囲（末尾のバイト数）と、
# 道具を呼ぶコマンドの目印
TRANSCRIPT_TAIL_BYTES = 512 * 1024
TOOL_MARKER = "request_self_clear"
# 道具を呼ぶコマンドだけを数える（grep などで名前を含むだけのコマンドを数えると、
# サブエージェントの呼び出しが本体の別のコマンドの間にすり抜ける・評価役の指摘）。
# 数えるのは次の2つの形:
#   (a) 道具の名前の後に --handoff か --cancel が続く
#   (b) 行頭かシェルの区切り（; & | ( の後）か代入（$out = の後）で python（python3 も）を起こして
#       道具の .py を渡し、その後が行末・「--」で始まる引数・シェルの区切りやリダイレクト
#       （; & | < > ) 2> *>）・# のコメントだけ
#       （--handoff を省いて既定の引き継ぎ NOW.md / README.md を使う呼び出し。
#       子の約4件に1件（会話記録の60件中16件）が `…; echo "exit=$?"` の形で打つため、区切りの後ろは許す・評価役の指摘）
TOOL_CALL_RE = re.compile(
    re.escape(TOOL_MARKER)
    + r"\S*\s[^\n]*?--(?:handoff|cancel)\b"
    + r"|(?:^|[;&|(=])[ \t]*[\"']?[^\s\"';&|=]*python(?:3[\d.]*)?(?:\.exe)?[\"']?[ \t]+"
    + r"[\"']?(?:[^\s\"']*[\\/])?"
    + re.escape(TOOL_MARKER)
    + r"\.py[\"']?(?=[ \t]*(?:\r?$|--|[;&|<>)#]|\d>|\*>))",
    re.IGNORECASE | re.MULTILINE,
)

# --handoff を省いたときに、会話の cwd の直下から探す引き継ぎファイル（前から順に）。
# NOW.md が本番の役とそろえた置き場（2026-10-17 利用者の依頼）。
# README.md は移行中の子が区切りを断られないための後ろ盾
DEFAULT_HANDOFF_NAMES = (("NOW.md", "now"), ("README.md", "readme"))
COMMAND_TOOLS = ("Bash", "PowerShell")

# 本体の tool_use が記録に書き出されるのを待つ上限と間隔（秒）
MAIN_CHECK_WAIT_SEC = 3.0
MAIN_CHECK_POLL_SEC = 0.3

# 結果の記録（1行1件の JSON）。待ち中の予約の一覧は status() で見る
RESULT_LOG = os.path.join(sm.APP_STATE_DIR, "self_clear.log.jsonl")

# 記録が大きくなりすぎたら、1世代だけ残して作り直す（バイト）
RESULT_LOG_MAX_BYTES = 1024 * 1024

# 直近の結果をメモリにも残す件数（status() で返す）
RECENT_LIMIT = 50

# 記録に残す説明の長さの上限（文字）。入力欄・画面の中身はこの前で切る
DETAIL_MAX_LEN = 300
DETAIL_CUT_MARKERS = ("入力欄: ", "画面の末尾:")

# 祖先をたどる段数の上限（輪になった親子関係で止まらないため）
MAX_ANCESTRY_DEPTH = 64

LOOPBACK_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")

# 応答・記録に出す予約の項目（見張りの内部の値は出さない）
PUBLIC_KEYS = (
    "pid",
    "create_time",
    "session_id",
    "name",
    "cwd",
    "managed",
    "handoff",
    "handoff_source",
    "requested_at",
    "deadline",
    "quiet_until",
    "auto",
)


class SelfClearError(Exception):
    """予約を受けられないとき。code は REST の応答の code に使う。"""

    def __init__(self, message, code="failed", status=400):
        super().__init__(message)
        self.code = code
        self.status = status


_lock = threading.Lock()
# pid（文字列）→ 予約。待ち中のものだけを置く
_pending = {}
_recent = []
# pid（文字列）→ 自動の区切りの依頼を送った会話 {session_id, until, sent_at}。
# この会話から予約が来たら、/clear の後の本文を AUTO_RESUME_TEXT にする
_auto_expected = {}


def expect_auto(pid, session_id, until):
    """自動の区切りの依頼を送る会話に印を置く（送る前に呼ぶ）。"""
    with _lock:
        _auto_expected[str(pid)] = {
            "pid": int(pid),
            "session_id": session_id,
            "until": float(until),
            "sent_at": _now_text(),
        }


def drop_auto(pid, session_id):
    """送れなかったときに印を外す。"""
    with _lock:
        entry = _auto_expected.get(str(pid))
        if entry and entry["session_id"] == session_id:
            _auto_expected.pop(str(pid))


# 待ちの上限を過ぎた印を、遅れて来る予約のためにさらに残しておく秒数
AUTO_LATE_KEEP_SEC = 6 * 3600


def pop_expired_auto(now=None):
    """待ちの上限を過ぎても予約が来なかった印を、一度だけ返す（「予約なし」の記録用）。

    印そのものは AUTO_LATE_KEEP_SEC 残す（上限の後に来た予約も自動の区切りとして扱い、
    /clear の後に「続きから進めて」を送って作業を再開させないため）。
    """
    now = time.time() if now is None else now
    with _lock:
        expired = [
            e
            for e in _auto_expected.values()
            if e["until"] <= now and not e.get("late")
        ]
        for entry in expired:
            entry["late"] = True
        for key in [
            k
            for k, e in _auto_expected.items()
            if e["until"] + AUTO_LATE_KEEP_SEC <= now
        ]:
            _auto_expected.pop(key)
    return [dict(e) for e in expired]


def auto_expectations():
    """予約を待っている自動の区切りの依頼の一覧（GET 用。late は待ちの上限を過ぎたもの）。"""
    with _lock:
        return [
            {k: v for k, v in e.items() if k != "until"}
            for e in _auto_expected.values()
        ]


def _claim_auto(pid, session_id):
    """この予約が自動の区切りの依頼に応えたものか（そうなら印を外して True）。_lock の中で呼ぶ。"""
    entry = _auto_expected.get(str(pid))
    if (
        entry
        and entry["session_id"] == session_id
        and entry["until"] + AUTO_LATE_KEEP_SEC > time.time()
    ):
        _auto_expected.pop(str(pid))
        return True
    return False


def _now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _epoch_text(epoch):
    return datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _public(reservation):
    return {k: reservation.get(k) for k in PUBLIC_KEYS}


def caller_pid(remote_addr, remote_port, server_port):
    """接続の持ち主のプロセスの pid を返す（引けなければ None）。

    127.0.0.1 からの接続だけを扱う。呼んだ側の端点が (remote_addr, remote_port)、
    こちら側の端点のポートが server_port で、両端ともループバックの TCP 接続を探す。
    """
    if remote_addr not in LOOPBACK_ADDRESSES:
        return None
    try:
        remote_port = int(remote_port)
        server_port = int(server_port)
    except (TypeError, ValueError):
        return None
    own = os.getpid()
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.Error:
        return None
    for conn in connections:
        if not conn.laddr or not conn.raddr or not conn.pid or conn.pid == own:
            continue
        if (
            conn.laddr.port == remote_port
            and conn.raddr.port == server_port
            and conn.laddr.ip in LOOPBACK_ADDRESSES
            and conn.raddr.ip in LOOPBACK_ADDRESSES
        ):
            return conn.pid
    return None


def find_session_ancestor(pid):
    """pid の祖先（自分を含む）をたどり、最初に見つかった稼働中の claude セッションを返す。

    pid の使い回しで別のプロセスを親と取り違えないよう、親の開始時刻が子より後なら
    そこで打ち切る。

    Returns:
        dict | None: ~/.claude/sessions/<pid>.json の中身（見つからなければ None）
    """
    try:
        proc = psutil.Process(int(pid))
    except (psutil.Error, TypeError, ValueError):
        return None
    for _ in range(MAX_ANCESTRY_DEPTH):
        state = sm._read_state(proc.pid)
        if state and sm.is_session_alive(state):
            return state
        try:
            parent = proc.parent()
            if parent is None or parent.create_time() > proc.create_time():
                return None
        except psutil.Error:
            return None
        proc = parent
    return None


def _iter_records(path, offset=0):
    """会話記録を offset から読み、(記録, 読み終えた位置) を返す。

    書きかけの最後の行は読まずに残す（次に続きから読むため）。offset が 0 より大きい
    途中の位置なら、最初の改行までは前の行の残りとして読み飛ばす。
    """
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[: end + 1]
    records = []
    for line in chunk.splitlines():
        try:
            records.append(json.loads(line.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            continue
    return records, offset + end + 1


def _tail_records(session_id):
    """本体の会話記録の末尾（TRANSCRIPT_TAIL_BYTES）の記録を返す。"""
    records = []
    for path in sm._transcript_paths(session_id):
        try:
            start = max(0, os.path.getsize(path) - TRANSCRIPT_TAIL_BYTES)
        except OSError:
            continue
        found, _ = _iter_records(path, start)
        records.extend(found)
    return records


def _content(record):
    return (record.get("message") or {}).get("content")


def main_transcript_runs_tool(session_id):
    """本体の会話記録に、この道具を呼んでいて結果がまだ無い tool_use があるか。

    サブエージェントの tool_use は本体の記録には載らないので、サブエージェントから
    打たれた予約はここで見分けられる（2026-10-12 実測: 本体のコマンドの tool_use は
    実行の前に記録に載り、結果の tool_result は実行の後に載る）。
    """
    running = set()
    for record in _tail_records(session_id):
        content = _content(record)
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in COMMAND_TOOLS:
                command = str((block.get("input") or {}).get("command") or "")
                if TOOL_CALL_RE.search(command):
                    running.add(block.get("id"))
            elif block.get("type") == "tool_result":
                running.discard(block.get("tool_use_id"))
    return bool(running)


def is_new_work(record):
    """会話記録の1件が、予約の後に来た新しい仕事（人・ほかのセッション・通知）か。

    実測（2026-10-12）: 人の発言は origin.kind=human、ほかのセッションからの連絡は
    origin.kind=peer、タスクの通知は origin.kind=task-notification の user の記録。
    ツールの結果は origin の無い tool_result だけの記録、スキルの読み込みは isMeta。
    /clear などのローカルのコマンドの記録（<command-name> 等で始まる）と、自動の要約
    （isCompactSummary・isVisibleInTranscriptOnly。origin も isMeta も無い）は数えない
    （区切りたいほど文脈が大きいときにちょうど要約が走るため。2026-10-12 評価役の指摘）。
    作業中に届いてターンの途中に吸い込まれたもの（attachment の queued_command。origin が
    あるもの・タスクの通知）も数える（予約から終わるまでの間に人が話したときに取りやめるため）。
    本アプリが打鍵で送る自動の文（AUTOMATED_PREFIXES）は数えない（/clear の後に送り直した
    本文で取りやめにしないため）。
    """
    queued = _queued_prompt(record)
    if queued is not None:
        prompt = queued.get("prompt")
        if _is_automated(prompt):
            return False
        return bool(queued.get("origin")) or "<task-notification" in _prompt_text(
            prompt
        )
    if record.get("type") != "user":
        return False
    if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
        return False
    content = _content(record)
    if _is_automated(content):
        return False
    if isinstance(content, list) and all(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
    ):
        return False
    if record.get("origin"):
        return True
    if record.get("isMeta"):
        return False
    if isinstance(content, str) and content.lstrip().startswith(
        ("<command-", "<local-command")
    ):
        return False
    return bool(content)


def _new_work_since(session_id, offsets):
    """offsets（パス→位置）から後ろに新しい仕事が載ったか。offsets は読んだ分だけ進める。"""
    found = False
    for path in sm._transcript_paths(session_id):
        records, offsets[path] = _iter_records(path, offsets.get(path, 0))
        if any(is_new_work(r) for r in records):
            found = True
    return found


def _prompt_text(content):
    """user の記録の本文（文字列か、text の部品をつないだもの）。"""
    if isinstance(content, list):
        return " ".join(
            str(c.get("text") or "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return str(content or "")


def _queued_prompt(record):
    """作業中に届いてターンの途中に吸い込まれた発言（attachment の queued_command）なら、その中身。

    実測（2026-10-17）: 子の作業中に人が打った文は user の記録にならず、
    {"type": "attachment", "attachment": {"type": "queued_command", "prompt": …,
    "origin": {"kind": "human"}, "timestamp": …}} の形でだけ載る。違えば None。
    """
    if record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
        return None
    return attachment


def _is_automated(content):
    """本アプリが打鍵で送った自動の文か（頭の文で見分ける）。"""
    return _prompt_text(content).lstrip().startswith(AUTOMATED_PREFIXES)


def is_human_utterance(record):
    """会話記録の1件が人の発言か（origin.kind=human で、本アプリの自動の文でないもの）。

    user の記録と、作業中に吸い込まれた発言（attachment の queued_command）の両方を見る。
    本アプリが打鍵で送る文（/clear の後の本文・自動の区切りの依頼）も origin.kind=human で
    載るので、頭の文（AUTOMATED_PREFIXES）で外す。他のセッションからの指示の貼り付け（前置きつき）と、
    予定や道具が send_to_session で送る文（【定期実行】・【日課 タスクN】など）は数える
    （人の依頼の中継・区切りが最大10分遅れるだけで止まり続けはしないため）。
    """
    if record.get("isSidechain"):
        return False
    queued = _queued_prompt(record)
    if queued is not None:
        origin, content = queued.get("origin"), queued.get("prompt")
    elif record.get("type") == "user":
        origin, content = record.get("origin"), _content(record)
    else:
        return False
    if (origin or {}).get("kind") != "human":
        return False
    return not _is_automated(content)


def human_utterance_epoch(record):
    """人の発言なら、人が打った時刻の epoch 秒（人の発言でない・時刻が読めなければ None）。

    吸い込まれた発言は attachment.timestamp（人が打った時刻）を先に見る。
    """
    if not is_human_utterance(record):
        return None
    queued = _queued_prompt(record)
    if queued is not None and queued.get("timestamp"):
        at = record_epoch(queued)
        if at is not None:
            return at
    return record_epoch(record)


def record_epoch(record):
    """記録の timestamp（ISO 8601）の epoch 秒（読めなければ None）。"""
    try:
        return datetime.datetime.fromisoformat(
            str(record.get("timestamp")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None


def last_human_epoch(session_id):
    """本体の会話記録の最後の人の発言の epoch 秒（無ければ None）。

    末尾だけを見ると、道具の大きな結果が続いたときに10分以内の発言を見落とすので、
    記録を全部読む（予約のときに1回だけ。「human」を含む行だけを JSON として読む）。
    """
    latest = None
    for path in sm._transcript_paths(session_id):
        try:
            f = open(path, "rb")
        except OSError:
            continue
        with f:
            for line in f:
                if b"human" not in line:
                    continue
                try:
                    record = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                at = human_utterance_epoch(record)
                if at is not None and (latest is None or at > latest):
                    latest = at
    return latest


def validate_handoff(path):
    """引き継ぎファイルのパスを確かめ、絶対パスにそろえて返す。

    Raises:
        SelfClearError: 文字列でない・ドライブ文字で始まる絶対パスでない・制御文字を含む・
            長すぎる・ファイルが無い
    """
    if not isinstance(path, str) or not path.strip():
        raise SelfClearError("引き継ぎファイルのパス（handoff）を指定してください")
    path = path.strip()
    if len(path) > HANDOFF_PATH_MAX_LEN:
        raise SelfClearError(
            f"引き継ぎファイルのパスが長すぎます（上限 {HANDOFF_PATH_MAX_LEN} 文字）"
        )
    if any(ord(ch) < 32 for ch in path):
        raise SelfClearError("引き継ぎファイルのパスに改行などの制御文字は使えません")
    if not HANDOFF_PATH_PATTERN.match(path):
        raise SelfClearError(
            "引き継ぎファイルはドライブ文字で始まる絶対パス（例 C:/work/handoff.md）で"
            f"指定してください: {path}"
        )
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise SelfClearError(f"引き継ぎファイルが見つかりません: {path}")
    return path


def default_handoff(cwd):
    """--handoff を省いたときの引き継ぎファイルを、会話の cwd の直下から選ぶ。

    NOW.md があればそれ、無く README.md があればそれ（移行中の後ろ盾）。

    Returns:
        tuple: (絶対パス, 出どころ "now" / "readme")

    Raises:
        SelfClearError: cwd が分からない・どちらも無い
    """
    if not isinstance(cwd, str) or not cwd.strip():
        raise SelfClearError(
            "この会話のフォルダ（cwd）が分からないため、既定の引き継ぎファイルを探せません。"
            "--handoff で引き継ぎファイルを指定してください"
        )
    folder = os.path.normpath(cwd.strip())
    for file_name, source in DEFAULT_HANDOFF_NAMES:
        path = os.path.join(folder, file_name)
        if os.path.isfile(path):
            return path, source
    names = "・".join(name for name, _ in DEFAULT_HANDOFF_NAMES)
    raise SelfClearError(
        f"既定の引き継ぎファイル（{names}）が {folder} の直下にありません。"
        "NOW.md に引き継ぎを書いてから打ち直すか、--handoff で引き継ぎファイルを指定してください"
    )


def _safe_detail(text):
    """記録に残す説明。入力欄・画面の中身（人の書きかけ等）は切り落とす。"""
    text = str(text or "")
    for marker in DETAIL_CUT_MARKERS:
        text = text.split(marker)[0]
    return text.strip()[:DETAIL_MAX_LEN]


def _record(entry):
    """結果を記録する（ファイルとメモリ）。書けなくても予約の処理は止めない。"""
    with _lock:
        _recent.append(entry)
        del _recent[:-RECENT_LIMIT]
    try:
        os.makedirs(sm.APP_STATE_DIR, exist_ok=True)
        if (
            os.path.exists(RESULT_LOG)
            and os.path.getsize(RESULT_LOG) > RESULT_LOG_MAX_BYTES
        ):
            os.replace(RESULT_LOG, RESULT_LOG + ".1")
        with open(RESULT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        safe_console.say(f"[SelfClear] 記録を書けませんでした: {e}", flush=True)
    # 出力の失敗で例外を出すと、見張りが done の後に failed を重ねて黙って死ぬ（2026-10-22）
    safe_console.say(
        f"[SelfClear] pid={entry.get('pid')} {entry.get('outcome')}: "
        f"{entry.get('detail', '')}",
        flush=True,
    )


def _wait_main_transcript(session_id):
    """main_transcript_runs_tool が真になるのを MAIN_CHECK_WAIT_SEC まで待つ。

    本体が並行で複数のコマンドを打つと、先に動いた道具の tool_use が記録に書き出される前に
    呼び出しが届くことがある（2026-10-12 本番で実測: 並行の2本のうち1本が not_main）。
    """
    deadline = time.time() + MAIN_CHECK_WAIT_SEC
    while not main_transcript_runs_tool(session_id):
        if time.time() >= deadline:
            return False
        time.sleep(MAIN_CHECK_POLL_SEC)
    return True


def _identify(caller):
    """接続の持ち主から、本人（本体のセッション）の状態を返す。

    Raises:
        SelfClearError: 本人を確かめられない場合（403）
    """
    if caller is None:
        raise SelfClearError(
            "呼び出し元を確かめられませんでした（同じPCの中から "
            "http://127.0.0.1:<port> へつないでください）",
            code="not_self",
            status=403,
        )
    state = find_session_ancestor(caller)
    if not state:
        raise SelfClearError(
            "呼び出し元が claude セッションの中で動いていません"
            "（セッション自身の Bash / PowerShell から打ってください）",
            code="not_self",
            status=403,
        )
    if sm.is_relogin_session(state) or (state.get("kind") or "interactive") != (
        "interactive"
    ):
        raise SelfClearError(
            "このセッションは区切れません（ログイン更新用・入力欄の無い実行です）",
            code="not_self",
            status=403,
        )
    if not _wait_main_transcript(state.get("sessionId")):
        raise SelfClearError(
            "本体のセッションが自分の Bash / PowerShell からこの道具を打ったことを"
            "確かめられませんでした。サブエージェントや、別のスクリプトの中からは打てません"
            "（打つと本体の会話が区切られるため）",
            code="not_main",
            status=403,
        )
    return state


def request_self_clear(caller, handoff, wait_sec=None, start_worker=True):
    """呼んだプロセス caller の属する claude セッションへ、/clear の予約を入れる。

    Args:
        caller: 接続の持ち主のプロセスの pid（caller_pid で引いたもの。None なら断る）
        handoff: 引き継ぎファイルの絶対パス。None なら会話の cwd 直下の NOW.md
            （無ければ README.md）を使い、どちらを使ったかを handoff_source に残す
        wait_sec: 入力待ちになるのを待つ上限（秒）。None なら DEFAULT_WAIT_SEC
        start_worker: False なら見張りを起こさない（試験用）

    Returns:
        dict: PUBLIC_KEYS の項目（予約の中身）

    Raises:
        SelfClearError: 本人が分からない・引き継ぎファイルが無い・予約が既にある等
    """
    state = _identify(caller)
    # 予約が生きているのに「予約されていません」と取り違えさせないよう、
    # 引き継ぎの確かめより先に重複を断る
    with _lock:
        _raise_if_reserved(_pending.get(str(state["pid"])))
    if handoff is None:
        path, handoff_source = default_handoff(state.get("cwd"))
    else:
        path, handoff_source = validate_handoff(handoff), "explicit"
    try:
        wait = float(DEFAULT_WAIT_SEC if wait_sec is None else wait_sec)
    except (TypeError, ValueError):
        raise SelfClearError("wait_sec は秒数（数値）で指定してください")
    if wait != wait:  # NaN
        wait = MIN_WAIT_SEC
    wait = max(MIN_WAIT_SEC, min(wait, MAX_WAIT_SEC))

    pid = int(state["pid"])
    session_id = state.get("sessionId")
    now = time.time()
    # 取りやめを見る位置を先に取る（記録を読む間に載った発言を、待ちと取りやめのどちらでも落とさない）
    offsets = sm._transcript_sizes(session_id)
    # 人の最後の発言から HUMAN_QUIET_SEC たつまでは送らない。待ちの上限は、明けてから送れるだけ延ばす
    last_human = last_human_epoch(session_id)
    quiet_until = last_human + HUMAN_QUIET_SEC if last_human else 0.0
    deadline_epoch = max(now + wait, quiet_until + HUMAN_QUIET_GRACE_SEC)
    reservation = {
        "pid": pid,
        "create_time": sm._process_create_time(pid),
        "session_id": session_id,
        "name": state.get("name") or "",
        "cwd": state.get("cwd") or "",
        "managed": sm.is_launched_by_app(pid),
        "handoff": path,
        "handoff_source": handoff_source,
        "requested_at": _now_text(),
        "deadline": _epoch_text(deadline_epoch),
        "deadline_epoch": deadline_epoch,
        # 人の発言から10分たつ時刻（それまでは送らない。待たなくてよければ None）
        "quiet_until": _epoch_text(quiet_until) if quiet_until > now else None,
        "quiet_until_epoch": quiet_until,
        # ここより後ろに新しい仕事が載ったら送らない（superseded）
        "offsets": offsets,
    }
    with _lock:
        _raise_if_reserved(_pending.get(str(pid)))
        reservation["auto"] = _claim_auto(pid, session_id)
        _pending[str(pid)] = reservation
    if start_worker:
        try:
            threading.Thread(target=_watch, args=(reservation,), daemon=True).start()
        except Exception as e:
            # 見張りが居ないまま予約が残ると、二度と予約できなくなる
            with _lock:
                if _pending.get(str(pid)) is reservation:
                    _pending.pop(str(pid))
            raise SelfClearError(
                f"見張りを起こせませんでした: {e}", code="error", status=500
            )
    # 予約は登録済み・見張りも起きている。出力の失敗で 500 にすると「断られたのに区切られる」
    safe_console.say(
        f"[SelfClear] reserved pid={pid} name={reservation['name']} "
        f"managed={reservation['managed']} handoff={handoff_source}:{path} "
        f"until {reservation['deadline']}"
        + (
            f" quiet_until {reservation['quiet_until']}（人の発言から10分は送らない）"
            if reservation["quiet_until"]
            else ""
        ),
        flush=True,
    )
    return _public(reservation)


def _raise_if_reserved(previous):
    """予約が既にあれば 409 で断る（_lock の中で呼ぶ）。"""
    if previous:
        raise SelfClearError(
            f"このセッションには既に区切りの予約があります（{previous['requested_at']} に受付・"
            f"引き継ぎ {previous['handoff']}）。このままターンを終えれば、前の予約で区切られます。"
            "引き継ぎを変えたいときは、そのファイルを書き直してください",
            code="already_reserved",
            status=409,
        )


def cancel_self_clear(caller):
    """呼んだプロセスの属するセッションの予約を取り消す。

    見張りが既に送り始めていたら（sending）取り消さない（送信と取り消しが両方
    記録に残り、「区切られません」と言ったのに区切られる、を避けるため）。

    Returns:
        dict: pid / cancelled（予約があって取り消したか）/ sending（送信中で取り消せなかったか）
    """
    state = _identify(caller)
    pid = int(state["pid"])
    with _lock:
        reservation = _pending.get(str(pid))
        if reservation and reservation.get("sending"):
            return {"pid": pid, "cancelled": False, "sending": True}
        if reservation:
            _pending.pop(str(pid))
    if reservation:
        entry = _public(reservation)
        entry.update(
            {
                "outcome": "cancelled",
                "detail": "本人が取り消した",
                "finished_at": _now_text(),
            }
        )
        _record(entry)
    return {"pid": pid, "cancelled": bool(reservation), "sending": False}


def _finish(reservation, outcome, detail=""):
    with _lock:
        key = str(reservation["pid"])
        if _pending.get(key) is reservation:
            _pending.pop(key)
    entry = _public(reservation)
    entry.update(
        {
            "outcome": outcome,
            "detail": _safe_detail(detail),
            "finished_at": _now_text(),
        }
    )
    _record(entry)
    return entry


def _wait_ready(reservation, session_id, sleep, clock, delivered_text=None):
    """入力待ちが IDLE_STABLE_SEC 続くのを待つ。

    delivered_text を渡すと（/clear 済みで本文を送り直す待ちのとき）、毎回の見回りで
    その本文が新しい会話に載ったかも見て、載っていれば "delivered" を返す
    （照合が遅れただけのとき、予約を早く閉じて次の予約を受けられるようにする）。

    Returns:
        None: 送ってよい
        (outcome, detail): 送らずに終えるとき（見張りはそれを記録して終える）
        "cancelled": 取り消された（記録は取り消した側が済ませている）
        "delivered": 本文が既に届いていた
    """
    pid = reservation["pid"]
    idle_since = None
    while True:
        with _lock:
            if _pending.get(str(pid)) is not reservation:
                return "cancelled"
        if delivered_text and sm._transcript_has_prompt(session_id, delivered_text, {}):
            return "delivered"
        if clock() >= reservation["deadline_epoch"]:
            return ("timeout", "入力待ちにならないまま上限を過ぎた")
        state = sm._read_state(pid)
        same_process = (
            state
            and sm.is_session_alive(state)
            and sm._process_create_time(pid) == reservation["create_time"]
        )
        if not same_process:
            return ("process_gone", "セッションが終わった")
        if state.get("sessionId") != session_id:
            return (
                "conversation_changed",
                "予約の後に会話が変わった（ほかから /clear・resume された）",
            )
        if _new_work_since(session_id, reservation["offsets"]):
            return (
                "superseded",
                "予約の後に新しい仕事（人・ほかのセッション・通知）が来たので送らなかった",
            )
        if state.get("status") == "idle":
            if idle_since is None:
                idle_since = clock()
            elif clock() - idle_since >= IDLE_STABLE_SEC and clock() >= reservation.get(
                "quiet_until_epoch", 0
            ):
                # 人の最後の発言から10分たつまでは送らない（その間に人が話せば superseded）
                return None
        else:
            idle_since = None
        sleep(POLL_SEC)


def _history_target(reservation, session_id):
    """送った履歴に残す宛先（予約の中身から作る）。"""
    return {
        "pid": reservation.get("pid"),
        "name": reservation.get("name") or "",
        "cwd": reservation.get("cwd") or "",
        "session_id": session_id or "",
        "managed": bool(reservation.get("managed")),
    }


def _watch(reservation, sleep=time.sleep, clock=time.time):
    """入力待ちが続くのを待って、/clear と引き継ぎの本文を送る。

    結果（outcome）: done / timeout / process_gone / conversation_changed / superseded /
        draft_present（書きかけがあり何もしていない）/ cleared_without_handoff
        （/clear だけ済んで本文を届けられなかった）/ failed / cancelled
    """
    pid = reservation["pid"]
    template = AUTO_RESUME_TEXT if reservation.get("auto") else RESUME_TEXT
    text = template.format(path=reservation["handoff"])
    session_id = reservation["session_id"]
    cleared = False
    resend_failures = 0
    try:
        while True:
            waited = _wait_ready(
                reservation, session_id, sleep, clock, text if cleared else None
            )
            if waited == "cancelled":
                return None
            if waited == "delivered" or (
                cleared and sm._transcript_has_prompt(session_id, text, {})
            ):
                # 照合が遅れただけで本文は届いていた（/clear の直後の会話なので先頭から見る）。
                # 送り直すと引き継ぎの仕事をやり直させてしまう
                return _finish(reservation, "done", "本文は届いていた（照合の遅れ）")
            if waited is not None:
                outcome, detail = waited
                if cleared:
                    outcome = "cleared_without_handoff"
                    detail = f"/clear の後、本文を送る前に止めた（{detail}）"
                return _finish(reservation, outcome, detail)
            with _lock:
                if _pending.get(str(pid)) is not reservation:
                    return None
                # ここから先は取り消させない（cancel_self_clear）
                reservation["sending"] = True
            history_target = _history_target(reservation, session_id)
            history_text = ("" if cleared else "/clear → ") + text
            try:
                try:
                    result = sm.send_to_session(
                        pid,
                        clear=not cleared,
                        text=text,
                        allow_unmanaged=True,
                        protect_draft=True,
                        # 本文はパスを含めても貼り付けにならない長さに抑えてある
                        preamble="",
                    )
                except sm.SessionError as e:
                    # 作業へ戻った（busy）だけのときは待ち直すので、送った履歴には残さない
                    if not isinstance(e, sm.SessionBusyError):
                        send_history.record(
                            "self_clear",
                            ok=False,
                            target=history_target,
                            text=history_text,
                            source="self_clear",
                            error=str(e),
                            code=type(e).__name__,
                            detail={"via": getattr(e, "via", None)},
                        )
                    raise
                send_history.record(
                    "self_clear",
                    ok=True,
                    target=history_target,
                    text=history_text,
                    source="self_clear",
                    detail={
                        "session_id_after": result.get("session_id_after"),
                        "handoff_source": reservation.get("handoff_source"),
                        "via": result.get("via"),
                    },
                )
            except sm.SessionBusyError:
                # 送る直前に作業へ戻った・別の送信が進行中。待ちをやり直す
                # （/clear の前なら、待っている間は取り消せる）
                reservation["sending"] = cleared
                continue
            except sm.SessionError as e:
                if not cleared and not getattr(e, "cleared", False):
                    # /clear の確かめが時間切れでも、実際には会話が変わっていることがある
                    after = sm._read_state(pid) or {}
                    if after.get("sessionId") not in (None, session_id):
                        e.cleared = True
                        e.session_id_after = after.get("sessionId")
                if getattr(e, "cleared", False) or cleared:
                    # /clear は済んだ。新しい会話へ本文だけを送り直す
                    if not cleared:
                        cleared = True
                        session_id = getattr(e, "session_id_after", None) or session_id
                        reservation["offsets"] = sm._transcript_sizes(session_id)
                    resend_failures += 1
                    if (
                        isinstance(e, sm.SessionDraftError)
                        or resend_failures >= RESEND_ATTEMPTS
                    ):
                        return _finish(reservation, "cleared_without_handoff", str(e))
                    continue
                if isinstance(e, sm.SessionDraftError):
                    return _finish(reservation, "draft_present", str(e))
                return _finish(reservation, "failed", str(e))
            if cleared:
                return _finish(reservation, "done", "本文を送り直して届けた")
            return _finish(
                reservation,
                "done",
                f"{result['session_id_before']} -> {result['session_id_after']}",
            )
    except Exception as e:  # 見張りの糸が黙って死なないように、必ず記録して終える
        return _finish(reservation, "failed", f"{type(e).__name__}: {e}")


def status():
    """待ち中の予約と、直近の結果を返す。"""
    with _lock:
        pending = [_public(r) for r in _pending.values()]
        recent = list(_recent)
    return {"pending": pending, "recent": recent}

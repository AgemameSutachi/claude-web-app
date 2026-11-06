# -*- coding: utf-8 -*-
"""管理下のセッションを、適切なときに自動で区切る（引き継ぎを書かせてから /clear）。

文脈が大きいまま会話を続けると、1往復ごとに大きな文脈を読み直す（運用での実測:
前の週の使用量の 41.6% が文脈 5万超のまま続けた分の読み直し、6.9% が1時間以上
止まった後の読み直し）。そこで、本アプリが起こした子（台帳に載っているもの）を
見回り、次の2つの規則に当たったら「自動の区切りです。引き継ぎを書いて予約し、
ターンを終えてください」を送る。子が引き継ぎを書いて request_self_clear.py で予約すると、
あとは self_clear の道（/clear → 「引き継ぎを読み、やりかけがあれば続け、無ければ次の依頼を待って」）で区切られる。

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
    区切りの後の本文（自動の区切りの「引き継ぎを読み、やりかけがあれば続け、無ければ待って」・子が自分で
    区切った「引き継ぎを読んで、続きから進めて」）で始まった会話は、最初のターンの終わり（引き継ぎを
    読み終えた所）から測る（2026-10-27 実測: 最初の API 呼び出しから測ると、読んだだけで伸びが
    2.05〜2.46万になり、61件が空回りしていた。子が自分で区切った後も同じで、11-01〜02 に 15件）。
    本文に書かれた引き継ぎファイルを Read したと分かれば、その直後の応答（読み終えた所）で基準を止める。
    分からなければ、最初の応答から4万を超えて伸びた所で止める（BASE_OPEN_MAX_GROWTH_TOKENS・仕事を続けたとみなす）。

除外: 許可待ち・答え待ちのもの、利用者が「残す」印を付けたもの、区切りの予約が既にあるもの。
裏の仕事（Bash・Monitor・セッションの CronCreate・サブエージェント・ScheduleWakeup の起床）が走っているものは
外さない（2026-11-03・どれも /clear の後も続き、知らせや発火が新しい会話へ届くと実測）。
(1) の大きさだけを当て（(2) は仕事の途中で止まっているだけなので当てない）、送る文の末尾に
「裏の仕事の id・目的・知らせが来たらすることを引き継ぎに書いて」の一文を足す（TRIGGER_BACKGROUND_TEXT）。
裏の仕事を持った入力待ちは Claude Code の状態が shell で、入力待ちに数える（sm.is_input_ready）。
同じ会話（sessionId）へ送るのは、区切りまで届けば一度だけ（送った記録はファイルに残し、再起動しても
送り直さない）。送れなかった・送った後に区切りまで届かなかった（予約が来ない・予約の後の /clear を
打てなかった等）会話は、間をおいて規則に当たれば送り直す（MAX_ATTEMPTS 回までは FAIL_RETRY_SEC、
その後は SLOW_RETRY_SEC おきで、前に送ったときより文脈が SLOW_RETRY_GROWTH_TOKENS 伸びたときだけ）。予約の結果はメモリにしか無いので、送った後に予約の待ち＋その間を
過ぎても同じ番号のまま規則に当たる会話も、区切りまで届かなかったとみなす（アプリの再起動に強い）。

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

import accounts
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
    "--handoff <引き継ぎファイルの絶対パス（/ 区切り）>。"
    "途中の作業は、続けてよいか（人に止められた・返事待ち等）も引き継ぎに書いてください。"
)
# 裏の仕事が走っている子へ足す一文（管理役の承認・2026-11-03 伝言 80e864df88e2・f4b3ddba8058）。
# 裏の Bash・Monitor・セッションの Cron・サブエージェント・起床の予約は /clear の後も続き、終わりの知らせや
# 発火は新しい会話へ届くが、新しい会話はそれが何のための仕事か知らない（捨ての子で実測・何もせず止まった）ので、
# 引き継ぎに書かせる
TRIGGER_BACKGROUND_TEXT = (
    "裏の仕事（Bash・Monitor・定期の仕事・サブエージェント・起床の予約）が{count}件走っているとみています。"
    "区切っても止まらず、終わりの知らせや定期の発火・起床は区切った後の会話に届きます。"
    "実際に走っているものを確かめ、それぞれの id（起床の予約は予定時刻）・目的・知らせが来たらすることを"
    "（出力ファイルがあればその場所も）引き継ぎに書いてから予約してください。"
)
REQUEST_TOOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "request_self_clear.py"
).replace("\\", "/")


def trigger_text(port, python=None, background=0):
    """送る本文（道具の打ち方つき）。python は省略で本アプリの Python。

    background: 走っている裏の仕事の数。1以上なら引き継ぎに裏の仕事を書かせる一文を末尾に足す
    （先頭は照合に使うので変えない）。
    """
    python = (python or sys.executable).replace("\\", "/")
    text = TRIGGER_TEXT.format(python=python, tool=REQUEST_TOOL, port=int(port))
    if background:
        text += TRIGGER_BACKGROUND_TEXT.format(count=int(background))
    return text


# 人の書きかけに当たったときに、同じ相手へ送り直すまでの間（秒）
DRAFT_RETRY_SEC = 600

# 依頼を送れなかった・送った後に区切りまで届かなかった会話へ、送り直すまでの間（秒）。
# MAX_ATTEMPTS 回までは FAIL_RETRY_SEC、それより後は SLOW_RETRY_SEC おきに送り続ける。
# 失敗を「送った扱い」で8日止めると、同じ番号のまま続きから起こし直された会話が自動の圧縮まで
# 膨らむ（2026-10-28: 1a2b3c4d が 13:08 の /clear で画面を読み違えて止まり、そのまま 17:27 に
# 53 万の圧縮まで区切られなかった。01:49〜06:43 の no_input_area の5件も同じ・最大 51.8 万）
FAIL_RETRY_SEC = 15 * 60
MAX_ATTEMPTS = 3
SLOW_RETRY_SEC = 2 * 3600
# MAX_ATTEMPTS 回より後は、前に送ったときより文脈がこれだけ伸びた会話にだけ送り直す。
# 区切らない理由（人や役の返事待ち等）があって止まっている子へ、終わりなく依頼を届けないため
SLOW_RETRY_GROWTH_TOKENS = 50_000
# 区切りの予約がこの結果で終わったら、区切りは済んだ（新しい会話になった）とみなす
RESERVATION_CLEARED = ("done", "cleared_without_handoff")
# 区切りまで届かなかった理由（記録の文面用。self_clear の outcome と no_reservation）
FAILED_REASONS = {
    "draft_present": "入力欄に書きかけがあると見えて /clear を打てなかった",
    "failed": "/clear を打てなかった",
    "timeout": "入力待ちにならないまま予約の待ちの上限を過ぎた",
    "superseded": "予約の後に新しい仕事が来て取りやめた",
    "cancelled": "子が予約を取り消した",
    "process_gone": "予約の後にセッションが終わった",
    "conversation_changed": "予約の後に会話が変わった",
    "no_reservation": "待ちの上限までに予約が来なかった",
}


def retry_wait(attempts):
    """attempts 回送った会話へ、次に送るまでの間（秒）。"""
    return FAIL_RETRY_SEC if attempts < MAX_ATTEMPTS else SLOW_RETRY_SEC


# 状態ファイル（送った会話・起こし直した会話）と結果の記録
STATE_FILE = os.path.join(sm.APP_STATE_DIR, "auto_clear.json")
KEEP_FILE = os.path.join(sm.APP_STATE_DIR, "keep.json")
RESULT_LOG = os.path.join(sm.APP_STATE_DIR, "auto_clear.log.jsonl")
RESULT_LOG_MAX_BYTES = 1024 * 1024
RECENT_LIMIT = 100

# 送った会話の記録を残す日数（会話の番号は使い回されないので、古いものは捨ててよい）
STATE_KEEP_SEC = 8 * 24 * 3600

# 裏の仕事が終わった知らせが来ないまま、これだけたったら走っていないとみなす（秒）。
# 知らせが落ちた（読み取りの取りこぼし等）ものを、いつまでも走っていると数えないため
BACKGROUND_STALE_SEC = 6 * 3600
# セッションの CronCreate（durable でないもの）を走っているとみなす長さ（秒）。
# 繰り返しのものは CronDelete まで、1回きりのものは1日
CRON_RECURRING_SEC = 7 * 24 * 3600
CRON_ONCE_SEC = 24 * 3600
# 一覧から消えたプロセスの、/clear をまたいで運んでいる番号を捨てるまでの間（秒）。
# 状態ファイルを読めなかった1回の欠けで捨てないため
CARRY_FORGET_SEC = 10 * 60
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
# 起床の予約の番号（セッションに1つ）とセッションの定期の仕事の番号の頭
WAKEUP_ID = "wakeup"
CRON_PREFIX = "cron:"


def _now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _parse_ts(text):
    return self_clear.record_epoch({"timestamp": text})


def _retry_note(entry, now):
    """送り直しの印（retry_at・attempts）から、記録に添える一言。"""
    minutes = round((float(entry["retry_at"]) - now) / 60)
    attempts = int(entry.get("attempts") or 1)
    grown = (
        f"文脈が今より {SLOW_RETRY_GROWTH_TOKENS // 10_000} 万伸びて"
        if attempts >= MAX_ATTEMPTS
        else ""
    )
    return f"{attempts} 回送った。{minutes} 分後から、{grown}規則に当たれば送り直す"


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


# 自動の区切りの後に送る本文（引き継ぎを読み、やりかけがあれば続け、無ければ待って）の頭
AUTO_RESUME_HEAD = self_clear.AUTO_RESUME_TEXT.split("{path}")[0]
# 子が自分で区切った後に送る本文（引き継ぎを読んで、続きから進めて）の頭
RESUME_HEAD = self_clear.RESUME_TEXT.split("{path}")[0]
# この頭で始まった会話は、最初のターンの終わり（引き継ぎを読み終えた所）を基準にする
RESUME_HEADS = (AUTO_RESUME_HEAD, RESUME_HEAD)
# 最初のターンの間に基準を伸ばせる幅（トークン・最初の応答から）。引き継ぎを読むだけの最初のターンの伸びは
# 2.05〜2.79万（2026-10-27 実測・自動の区切りの後の162本）。引き継ぎを Read で読んだと分かる会話は、
# その直後の応答で基準を止めるので（下の HANDOFF_PATH_RE）、この幅は読んだと分からないとき（Bash の cat 等）の歯止め。
# 上限を6万に上げる案は、続けた仕事の伸び（11-02 22:37 example-checker で 5.5万）まで基準に吸い込むので採らなかった
BASE_OPEN_MAX_GROWTH_TOKENS = 40_000
# 区切りの後の本文から、引き継ぎファイルのパスを取り出す（「…」の中）
HANDOFF_PATH_RE = re.compile(r"「([^」]+)」")
# 別のプロセスで起こし直した後の最初の応答で、基準に足してよい跳ねの上限（トークン）。
# 前置きの替わりで説明がつくのはこの幅まで（2026-11-01 実測の跳ねは 20,806）
RESTART_MAX_JUMP_TOKENS = 40_000


def turn_prompt_text(record):
    """会話記録の1件が新しいターンを始める入力なら、その本文（違えば None）。

    人の発言・本アプリの自動の文・ほかのセッションからの連絡・タスクの通知・作業中に吸い込まれた
    発言（attachment の queued_command）を入力とみなす。ツールの結果・isMeta（ほかのセッションからの
    連絡を除く）・/clear などのローカルのコマンドの記録・自動の要約は入力ではない（2026-10-27 実記録:
    自動の文は origin.kind=plugin の user の記録、/clear は「<command-name>」で始まる user の記録、
    ほかのセッションからの連絡は origin.kind=peer で isMeta=True の user の記録）。
    起床の予約・定期の仕事の文（origin の無い isMeta）は拾えないので、呼び手はターンの終わりの
    記録（turn_duration）でも区切る。
    """
    queued = self_clear._queued_prompt(record)
    if queued is not None:
        return self_clear._prompt_text(queued.get("prompt"))
    if record.get("type") != "user":
        return None
    if record.get("isMeta") and (record.get("origin") or {}).get("kind") != "peer":
        return None
    if record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
        return None
    content = self_clear._content(record)
    if isinstance(content, list) and all(
        isinstance(c, dict) and c.get("type") == "tool_result" for c in content
    ):
        return None
    text = self_clear._prompt_text(content)
    if not text.strip() or text.lstrip().startswith(("<command-", "<local-command")):
        return None
    return text


def _norm_path(path):
    """パスを比べられる形にする（区切りを / に・大文字小文字をそろえる）。"""
    return str(path or "").strip().replace("\\", "/").lower()


def handoff_path_of(prompt):
    """区切りの後の本文に書かれた引き継ぎファイルのパス（比べる形）。書かれていなければ None。"""
    found = HANDOFF_PATH_RE.search(prompt or "")
    return _norm_path(found.group(1)) if found else None


def reads_file(record, path):
    """応答の記録に、path を Read する道具の呼び出しがあるか。"""
    content = (record.get("message") or {}).get("content")
    if not path or not isinstance(content, list):
        return False
    return any(
        isinstance(c, dict)
        and c.get("type") == "tool_use"
        and c.get("name") == "Read"
        and _norm_path((c.get("input") or {}).get("file_path")) == path
        for c in content
    )


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
        # パス → {"offset", "running": {番号: 走っているとみなす期限}, "started", "ended", "last_response", "last_human" …}
        self._files = {}

    def scan(self, transcript_path, now=None, process_started=None):
        """会話記録を読み進め、見立てに使う値を返す。

        Args:
            process_started: いまこの会話を動かしているプロセスの開始時刻（epoch 秒・分からなければ None）。
                会話の途中で別のプロセスが起こし直した（移し・落ちた子の起こし直し）ときは、
                その後の最初の応答で跳ねた文脈を基準に足す（伸びに数えない）

        Returns:
            dict: running（走っている裏の仕事の番号の一覧）/ carried（/clear をまたいで
            引き継ぐ番号と期限。走っている裏の仕事すべて）/ ended（この会話記録で終わりが分かった番号。
            /clear の前の会話から引き継いだ番号を消すのに使う）/ last_response（最後の応答の epoch 秒）/ last_human
            （最後の人の発言の epoch 秒）/ base_context（伸びを測る基準。ふつうは最初の応答が読んだ文脈、
            区切りの後の本文（自動の区切り・子が自分で区切ったもの）で始まった会話は最初のターンの終わりの文脈
            （引き継ぎを Read した直後の応答。分からなければ最初の応答から4万まで）。別のプロセスが起こし直した後は、
            その最初の応答で跳ねた分（0 超・4万以下）を足した値）。
            読めなければ空の値
        """
        empty = {
            "running": [],
            "carried": {},
            "ended": [],
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
            if process_started is not None and (
                state["process_started"] is None
                or process_started > state["process_started"]
            ):
                # プロセスが替わった（または初めて分かった）。その後の最初の入力を起こし直しとして見る。
                # 古い開始時刻へ戻る向きは受けない（同じ会話を2つのプロセスが動かすと、交互に読むたびに
                # 次の入力を起こし直しとみなし、仕事の伸びを全部基準に吸い込むため・評価1周目の中1）。
                # 開始時刻を知らずに読み進めた後で初めて分かったとき・新しいプロセスの入力を既に読んだ
                # とき（古い行のまま読んでいた）は、替わり目が分からない／過ぎたので見ない（中2）
                passed = state["offset"] > 0 and (
                    state["process_started"] is None
                    or (state["last_prompt_at"] or 0) >= process_started
                )
                state["process_started"] = process_started
                state["restart_seen"] = passed
            if size > state["offset"]:
                records, state["offset"] = self_clear._iter_records(
                    transcript_path, state["offset"]
                )
                for record in records:
                    self._apply(state, record)
            # 裏の仕事はプロセスに付いていて、プロセスと一緒に消える。いまのプロセスより前に始まった
            # 仕事（落ちた・止めたプロセスの残り）は数えない（評価2周目の中2・/clear の後へ運ばないため）
            begun = state["started"]
            live = {
                k: v
                for k, v in state["running"].items()
                if v > now
                and not (
                    state["process_started"] is not None
                    and begun.get(k, now) < state["process_started"]
                )
            }
            return {
                "running": sorted(live),
                # 裏の仕事はどれも /clear の後も続く（2026-11-03 捨ての子で実測）。
                # 新しい会話の記録には始まりが載らないので、プロセスごとに運ぶ（AutoClear._scan）
                "carried": dict(live),
                "ended": sorted(state["ended"]),
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
            # この会話記録で終わりが分かった番号（前の会話から運んだ番号の終わりも、新しい会話に届く）
            "ended": set(),
            # 走っている番号 → 始まった時刻（epoch 秒。いまのプロセスより前のものを数えないため）
            "started": {},
            "last_response": None,
            "last_human": None,
            "base_context": None,
            # 入力（人・自動の文・ほかのセッション）が来たか／基準を最初のターンの終わりまで伸ばすか
            "seen_prompt": False,
            "base_open": False,
            "first_context": None,
            # 区切りの後の本文に書かれた引き継ぎファイル（比べる形）と、それを Read した応答の文脈
            # （None ならまだ読んでいない。次に文脈が増えた応答で基準を止める）
            "handoff_path": None,
            "handoff_read_at": None,
            # 読み終えたとみなして基準を止めた応答の文脈（同じ応答の分割記録が続きを読んだら開き直す）
            "handoff_closed_at": None,
            # 起こし直しの扱い: いまのプロセスの開始時刻・替わり目を決め終えたか（見つけた・見ないと決めた）・
            # 最後に読んだ入力の時刻・最後に読んだ応答の文脈・起こし直しの前の基準
            # （None なら起こし直しの最初の応答を待っていない）
            "process_started": None,
            "restart_seen": False,
            "last_prompt_at": None,
            "last_context": None,
            "restart_base": None,
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
            state["seen_prompt"] = True
            state["base_open"] = False
            state["last_context"] = None
            state["restart_base"] = None
            state["handoff_closed_at"] = None
        if kind == "system" and record.get("subtype") == "turn_duration":
            # ターンの終わり。最初のターンの後に来る入力の形（起床の予約の文など）を
            # 取りこぼしても、ここで基準を止める（直近3日の 162本すべてで次の入力より先に載る）
            state["base_open"] = False
            state["handoff_closed_at"] = None
        prompt = turn_prompt_text(record)
        if prompt is not None:
            started = state["process_started"]
            # 時刻の無い入力は替わり目の判定に使わない（読んだ時の時計で「今のプロセスの後」に見えるため）
            stamped = _parse_ts(record.get("timestamp"))
            in_this_process = (
                started is not None and stamped is not None and stamped >= started
            )
            if stamped is not None:
                state["last_prompt_at"] = max(state["last_prompt_at"] or 0.0, stamped)
            restart = (
                in_this_process and state["seen_prompt"] and not state["restart_seen"]
            )
            if in_this_process or (started is not None and stamped is None):
                # 時刻の無い入力の後は、どこが替わり目か分からないので見ない（評価2周目の低1）
                state["restart_seen"] = True
            state["restart_base"] = None
            state["handoff_closed_at"] = None
            if restart:
                # 別のプロセスが同じ会話を続きから起こし直した後の最初の入力。前置き（システムプロンプト・
                # 道具の一覧・CLAUDE.md）が替わり、仕事をしなくても最初の応答で文脈が跳ねる
                # （2026-11-01 実測: アカウントの移しの1往復だけで 72,928 → 93,734。伸びの下限 2万を越え、
                # /clear の 54 分後にまた区切った）。その跳ねた分だけを基準に足す
                state["base_open"] = False
                if state["base_context"] and state["last_context"]:
                    state["restart_base"] = state["base_context"]
            elif not state["seen_prompt"]:
                # 区切りの後の本文（RESUME_HEADS・自動の区切りも子が自分で区切ったものも）で始まった
                # 会話は、そのターンの終わり（引き継ぎを読み終えた所）を基準にする。最初の API 呼び出しは
                # 引き継ぎを読む前なので、読んだだけで伸びが閾値を越え、約51分後に区切り直していた
                # （2026-10-27 実測: 自動の区切りの後の 61件すべて伸び 2.05〜2.46万・人の発言0。
                # 2026-11-02 実測: 子が自分で区切った後の 11-01〜02 の 15件が、引き継ぎを読んで
                # 待っていただけで伸び 2.09〜4.93万になり idle で送っていた・伝言 2f6b95093ae4）
                # 引き継ぎを Read したと分かれば、その直後の応答（読み終えた所）で止める。続けた仕事の伸びを
                # 基準に入れないため（11-02 22:37 example-checker: 読んだ直後 71,404・ターンの終わり 103,506）
                state["seen_prompt"] = True
                state["base_open"] = prompt.lstrip().startswith(RESUME_HEADS)
                state["first_context"] = None
                state["handoff_path"] = (
                    handoff_path_of(prompt) if state["base_open"] else None
                )
                state["handoff_read_at"] = None
                state["handoff_closed_at"] = None
            else:
                state["base_open"] = False
        if kind == "assistant":
            state["last_response"] = at
            found = usage_tracker.usage_of(record)
            context = usage_tracker.context_of(found[2]) if found else 0
            observed = context
            if context and state["restart_base"] is not None:
                jump = context - state["last_context"]
                if 0 < jump <= RESTART_MAX_JUMP_TOKENS:
                    # 4万を超えて跳ねたなら起こし直しでは説明がつかない。基準は動かさない
                    state["base_context"] = state["restart_base"] + jump
                state["restart_base"] = None
            elif state["base_context"] is None or state["base_open"]:
                if context and state["base_open"]:
                    first = state.get("first_context") or context
                    state["first_context"] = first
                    read_at = state["handoff_read_at"]
                    if read_at is not None and context > read_at:
                        # 引き継ぎを読み終えた所。これより後の伸びは仕事として数える
                        # （同じ応答が道具ごとに分かれた記録は文脈が同じなので、増えた所で止める）
                        state["base_open"] = False
                        state["handoff_closed_at"] = context
                    elif context - first > BASE_OPEN_MAX_GROWTH_TOKENS:
                        # 最初のターンでやりかけの仕事を続けた。その伸びは基準に入れない
                        state["base_open"] = False
                        context = state["base_context"] or first
                if context:
                    state["base_context"] = context
            if reads_file(record, state["handoff_path"]):
                if state["base_open"] and state["handoff_read_at"] is None:
                    state["handoff_read_at"] = observed or state["last_context"] or 0
                elif observed and observed == state["handoff_closed_at"]:
                    # 止めた応答の分割記録が、途中で切れた引き継ぎの続きを読んでいる（offset 付きの Read）。
                    # 読み終えるまで開き直す（2026-11-02 22:43 claude-web-app: 83,146 で止めた後に
                    # 同じ応答で offset=285 を読み、90,509 まで読むだけで伸びていた・評価2周目の中2）
                    state["base_open"] = True
                    state["handoff_read_at"] = observed
                    state["handoff_closed_at"] = None
            if observed:
                state["last_context"] = observed
        human_at = self_clear.human_utterance_epoch(record)
        if human_at is not None:
            # 作業中に吸い込まれた発言も数え、本アプリが送った自動の文
            # （区切りの依頼・/clear の後の本文）は数えない
            state["last_human"] = max(state["last_human"] or 0.0, human_at)
        result = record.get("toolUseResult")
        if isinstance(result, dict):
            before = dict(running)
            BackgroundWork._apply_result(running, result, at, state["ended"])
            for task_id, deadline in running.items():
                if before.get(task_id) != deadline:
                    # 始まった（起床の入れ直しも含む）
                    state["started"][task_id] = at
            for task_id in set(state["started"]) - set(running):
                del state["started"][task_id]
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
            state["ended"].add(task_id)

    @staticmethod
    def _apply_result(running, result, at, ended=None):
        ended = set() if ended is None else ended

        def end(task_id):
            running.pop(task_id, None)
            ended.add(task_id)

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
                end(stopped.group(1))
                if result.get("task_id"):
                    end(str(result["task_id"]))
        if "scheduledFor" in result:
            scheduled = result.get("scheduledFor")
            if result.get("stopped") or not scheduled:
                end(WAKEUP_ID)
            elif isinstance(scheduled, (int, float)):
                running[WAKEUP_ID] = scheduled / 1000.0 + WAKEUP_GRACE_SEC
        if "humanSchedule" in result and result.get("id"):
            # CronCreate。durable（ディスクに残る）は会話を区切っても消えないので数えない
            if not result.get("durable"):
                span = CRON_RECURRING_SEC if result.get("recurring") else CRON_ONCE_SEC
                running[CRON_PREFIX + str(result["id"])] = at + span
        elif set(result) == {"id"}:
            # CronDelete の結果は {"id": …} だけ
            end(CRON_PREFIX + str(result["id"]))

    def forget_missing(self, live_paths):
        """もう見ない会話記録の分を捨てる（溜まり続けないように）。"""
        with self._lock:
            for path in [p for p in self._files if p not in live_paths]:
                del self._files[path]


# --- 「残す」印 ---------------------------------------------------------------


def _process_started(session):
    """一覧の1行のプロセスの開始時刻（epoch 秒）。claude の状態ファイルの startedAt はミリ秒。読めなければ None。"""
    try:
        started = float(session.get("started_at"))
    except (TypeError, ValueError):
        return None
    return started / 1000.0 if started > 0 else None


def keep_key(session):
    """印の鍵。プロセス（pid と開始時刻）ごとに付ける（/clear で会話の番号が変わっても残る）。"""
    return f"{session.get('pid')}:{session.get('started_at')}"


class KeepMarks:
    """利用者が付けた「残す」印（自動の区切りと起こし直しの対象から外す）。

    印を付けられるのは利用者（画面・REST）だけ。子の AI には選ばせない（利用者の決定）。
    """

    def __init__(self, path=None):
        # None なら使うたびに KEEP_FILE を引く（試験が置き場を差し替えられるように・2026-11-02）
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self):
        return self._path or KEEP_FILE

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
    裏の仕事を持った入力待ち（shell）も入力待ちに数える（sm.is_input_ready）。
    """
    if not sm.is_input_ready(session):
        return None
    updated = session.get("status_updated_at")
    if not updated:
        return None
    return max(0.0, now - float(updated) / 1000.0)


def exclusions(session, kept, reserved, quota_stopped=False):
    """自動の区切り・起こし直しから外す理由の一覧（空なら外さない）。

    裏の仕事（Bash・Monitor・セッションの Cron・サブエージェント・ScheduleWakeup の起床）が走っていることは
    外す理由にしない（2026-11-03 管理役の承認・伝言 80e864df88e2・f4b3ddba8058）。どれも /clear の後も続くと
    実測したので、size の規則で区切りを頼み、引き継ぎに書かせる（decide・trigger_text）。前面で考え続けている
    （状態が busy の）間は入力待ちでないので、decide がもともと当てない。
    起こし直し（プロセスを止める）は裏の仕事を切るので、relaunch_blockers が別に止める。

    quota_stopped: 会話記録の最後の応答が枠切れで、その後に人の発言が無い（accounts.quota_stop）。
    枠切れの間に区切りを頼んでも動けず、戻った直後は続きの依頼（quota_resume）と並ぶので外す
    （2026-11-02・5.31節の評価で残した低）。印は続きの依頼（人の発言として載る）が会話記録に載った時点で消え、
    その後は作業中であることと「人の最後の発言から10分は当てない」（decide）で守られる
    （続きの依頼の文が self_clear.AUTOMATED_PREFIXES で始まると10分の守りが効かなくなる。試験で固定）。
    """
    reasons = []
    if quota_stopped:
        reasons.append("枠切れで止まっている（人か続きの依頼の再開待ち）")
    if (session.get("kind") or "interactive") != "interactive":
        reasons.append("入力欄の無い実行")
    if session.get("attention") or session.get("status") == "waiting":
        reasons.append("許可待ち・答え待ち")
    if kept:
        reasons.append("残す印")
    if reserved:
        reasons.append("区切りの予約が既にある")
    return reasons


def _quota_stopped(session, path):
    """会話が枠切れで止まっているか。一覧が quota_stop を付けていればそれを、無ければ会話記録を読む。

    読めないときは止まっていない扱い（今までどおり規則で決める）。
    """
    if "quota_stop" in session:
        return bool(session["quota_stop"])
    if not path:
        return False
    try:
        return bool(accounts.quota_stop(path))
    except Exception as e:
        safe_console.say(
            f"[AutoClear] 枠切れの印を読めませんでした {path}: {e}", flush=True
        )
        return False


def decide(
    session,
    context,
    now,
    config,
    last_response=None,
    last_human=None,
    base_context=None,
    background=0,
):
    """規則に当たるかを返す（除外は呼び手が先に見る）。

    裏の仕事が走っている会話（background が1以上）には size の規則だけを当てる。idle の規則は
    「止まっている」ことを根拠にするが、裏の仕事を待って止まっている子は仕事の途中なので当てない。

    どちらの規則も、最後の応答（＝最後に API を呼んだ時刻）から58分未満、つまり読み直しの
    控え（プロンプトキャッシュ・1時間）が生きている間だけ当てる。控えが切れた会話へ送ると、
    依頼の1往復そのものが大きな文脈の読み直しになり、節約にならない（2026-10-12 実測:
    2日止まった文脈 76万の会話が当たっていた）。控えが切れた会話は、次に仕事が来て止まったときに拾う。
    最後の応答の時刻が分からない会話には当てない。
    どちらの規則も、人の最後の発言から10分は当てない（人と話している最中に区切らない）。

    Args:
        last_response: 会話記録の最後の応答の epoch 秒
        last_human: 会話記録の最後の人の発言の epoch 秒（無ければ None）
        base_context: 伸びを測る基準（BackgroundWork.scan の base_context。分からなければ None＝伸びを見ない）
        background: 走っている裏の仕事の数

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
    # 状態が shell なら、会話記録の読み取りが取りこぼしていても裏の仕事を持っている
    waiting_on_background = background or session.get("status") == "shell"
    if (
        not waiting_on_background
        and context > config["idle_tokens"]
        and age_min >= config["idle_minutes"]
    ):
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
    if view.get("background"):
        # 起こし直しはプロセスを止めるので、裏の仕事が切れる（/clear と違って生き残らない）
        blockers.append(f"裏の仕事が走っている（{view['background']}件）")
    idle = view["idle_minutes"]
    # shell（裏の仕事を持った入力待ち）は起こし直すと裏の仕事が切れるので、ここでは idle だけを起こし直す
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
        state_file=None,
        result_log=None,
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
        # None なら使うたびに STATE_FILE・RESULT_LOG を引く（試験が置き場を差し替えられるように）
        self._state_file = state_file
        self._result_log = result_log
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

    @property
    def state_file(self):
        return self._state_file or STATE_FILE

    @property
    def result_log(self):
        return self._result_log or RESULT_LOG

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

    def _done_with(self, key, session_id, now):
        """この会話へ今は送らないか。

        dry_run は一度だけ。on で送った会話は、区切れれば番号が変わるのでふつうは二度と来ない。
        送った後も同じ番号のまま規則に当たり続け、予約の待ち＋送り直しの間を過ぎたら、
        区切りまで届かなかったとみなして送り直す（予約の結果はメモリにしか無く、
        アプリの再起動で失われても送り直せるように）。失敗の印は retry_at で送り直す。
        """
        with self._lock:
            entry = self._state[key].get(session_id)
        if entry is None:
            return False
        if key != "sent":
            return True
        attempts = int(entry.get("attempts") or 1)
        retry_at = entry.get("retry_at")
        if retry_at is None:
            retry_at = (
                float(entry.get("at_epoch") or 0)
                + self.config["handoff_wait_minutes"] * 60
                + retry_wait(attempts)
            )
        return now < float(retry_at)

    def _grown_enough(self, session_id, context):
        """MAX_ATTEMPTS 回送った会話へ、もう一度送ってよいほど文脈が伸びたか。"""
        with self._lock:
            entry = self._state["sent"].get(session_id) or {}
        if int(entry.get("attempts") or 1) < MAX_ATTEMPTS:
            return True
        before = entry.get("context_tokens")
        # 今回の文脈が測れなければ次の見回りへ回す（前の値が無い古い印は通す）
        return before is None or (context or 0) - before >= SLOW_RETRY_GROWTH_TOKENS

    def _mark_attempt(self, session_id, rule, now, failed, context=None):
        """送った（failed なら、送れなかった）印を置き、その印を返す。

        送るのは印が無いか送り直しの番が来たときだけ（_done_with）なので、前の回数に1を足す。
        失敗は retry_wait(回数) 後に送り直す。context はそのときの文脈（_grown_enough で使う）。
        """
        with self._lock:
            before = self._state["sent"].get(session_id)
            attempts = int(before.get("attempts") or 1) + 1 if before else 1
            entry = {
                "at_epoch": now,
                "rule": rule,
                "attempts": attempts,
                "context_tokens": context,
            }
            if failed:
                entry["failed"] = True
                entry["retry_at"] = now + retry_wait(attempts)
            self._state["sent"][session_id] = entry
            self._save_state()
            return entry

    def _fail_after_send(self, session_id, now, outcome, at_epoch=None):
        """送れた依頼が区切りまで届かなかった（予約なし・/clear を打てなかった等）。

        Returns: 送り直しの印（置き済み・送った記録が無い・古い依頼の結果なら None）
        """
        with self._lock:
            entry = self._state["sent"].get(session_id)
            if (
                not entry
                or entry.get("failed")
                or (at_epoch is not None and at_epoch + 1 < float(entry["at_epoch"]))
            ):
                return None
            failed = dict(entry, failed=True, reason=outcome)
            failed["retry_at"] = now + retry_wait(int(entry.get("attempts") or 1))
            self._state["sent"][session_id] = failed
            self._save_state()
            return failed

    def _settle_reservations(self, now):
        """自動の区切りの予約が /clear まで届かずに終わった会話を、送り直せるようにする。"""
        for done in self_clear.status()["recent"]:
            if not done.get("auto") or done.get("outcome") in RESERVATION_CLEARED:
                continue
            finished = _parse_ts(done.get("finished_at"))
            if finished is None or not done.get("session_id"):
                continue
            failed = self._fail_after_send(
                done["session_id"], now, done["outcome"], at_epoch=finished
            )
            if failed:
                self._record(
                    {
                        "outcome": "retry_scheduled",
                        "pid": done.get("pid"),
                        "name": done.get("name") or "",
                        "session_id": done["session_id"],
                        "detail": FAILED_REASONS.get(
                            done["outcome"], f"予約が {done['outcome']} で終わった"
                        )
                        + "。"
                        + _retry_note(failed, now),
                    }
                )

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
        """会話記録を読み、/clear の前の会話で始まった裏の仕事（まだ走っているもの）を足して返す。

        前の会話から運んだ番号は、期限を過ぎるか、新しい会話の記録に終わり（終わりの知らせ・TaskStop・
        CronDelete・起床の取り消し）が載ったら消す（終わりの知らせは /clear の後の会話に届く・2026-11-03 実測）。
        """
        session_id = session.get("session_id")
        path = self.transcript_path_for(session.get("cwd"), session_id)
        started = _process_started(session)
        scanned = self.background.scan(path, now, started)
        key = keep_key(session)
        with self._lock:
            known = self._carry.get(key)
            old_path = (
                known.get("path")
                if known and known["session_id"] != session_id
                else None
            )
        # 会話が変わった（/clear）。前の会話の記録は読み切ってから運ぶ（最後の見回りから /clear までの間に
        # 前の会話へ載った終わりの知らせを落とすと、終わった仕事を6時間走っていると数え続ける・評価2周目の中1）。
        # 読み位置が捨てられていれば最初から読むので、ロックの外で読む（評価3周目の低3）
        old = self.background.scan(old_path, now, started) if old_path else None
        with self._lock:
            entry = self._carry.get(key) or {
                "session_id": session_id,
                "path": path,
                "current": {},
                "previous": {},
            }
            if entry["session_id"] != session_id:
                current = entry["current"]
                if old is not None and entry.get("path") == old_path:
                    # 前の会話で終わった番号は、さらに前から運んでいた分からも消す（評価3周目の中1）
                    old_ended = set(old["ended"])
                    entry["previous"] = {
                        k: v for k, v in entry["previous"].items() if k not in old_ended
                    }
                    current = old["carried"]
                entry["previous"].update(current)
                entry["session_id"] = session_id
            entry["path"] = path
            entry["seen"] = now
            entry["current"] = dict(scanned["carried"])
            ended = set(scanned["ended"])
            entry["previous"] = {
                k: v for k, v in entry["previous"].items() if v > now and k not in ended
            }
            self._carry[key] = entry
            previous = set(entry["previous"])
        running = sorted(set(scanned["running"]) | previous)
        return path, dict(scanned, running=running)

    def assess(self, session, now=None, marks=None, reserved_pids=None):
        """1つのセッションの見立て（画面・GET・起こし直しで使う）。

        Returns:
            dict: context_tokens / base_context_tokens（伸びを測る基準。BackgroundWork.scan の base_context）/
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
            session,
            kept,
            str(session.get("pid")) in reserved_pids,
            quota_stopped=_quota_stopped(session, path),
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
                background=len(scanned["running"]),
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
        self._settle_reservations(now)
        mode = self.config["mode"]
        self._last_tick = _now_text()
        # 管理外の分も含め、いま動いている会話の読み位置は残す（起こし直しの一覧で読み直さない）
        self.background.forget_missing(
            {
                self.transcript_path_for(s.get("cwd"), s.get("session_id"))
                for s in sessions
            }
        )
        with self._lock:
            # 終わったプロセスの運んでいる番号を捨てる（溜まり続けないように）。一覧は状態ファイルを
            # 読めなかった子を黙って外すので、1回欠けただけでは捨てない（運んだ番号は戻せない・評価3周目の中2）
            live_keys = {keep_key(s) for s in sessions}
            for key in [
                k
                for k, entry in self._carry.items()
                if k not in live_keys
                and now - entry.get("seen", now) >= CARRY_FORGET_SEC
            ]:
                del self._carry[key]
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
            if not session_id or self._done_with(seen_key, session_id, now):
                continue
            view = self.assess(session, now, marks, reserved)
            if not view["rule"]:
                continue
            if seen_key == "sent" and not self._grown_enough(
                session_id, view["context_tokens"]
            ):
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
                session_id = session.get("session_id")
                detail = f"{type(e).__name__}: {e}"
                with self._lock:
                    marked = self._state[seen_key].get(session_id) or {}
                if marked.get("at_epoch") == now:
                    # この回の印は _send が置き済み（送れた後の記録で落ちた等）。二重に数えない。
                    # 送れていれば予約待ちの印は残す（外すと、予約しても自動の区切りとして扱われない）
                    failed = marked.get("failed")
                    self._record(
                        self._entry(
                            hit,
                            "send_failed" if failed else "sent",
                            f"{'送れなかった' if failed else '送った'}後の記録で落ちた（{detail}）",
                        )
                    )
                    continue
                self_clear.drop_auto(session.get("pid"), session_id)
                if seen_key == "sent":
                    entry = self._mark_attempt(
                        session_id,
                        hit["rule"],
                        now,
                        failed=True,
                        context=hit.get("context_tokens"),
                    )
                    detail += "。" + _retry_note(entry, now)
                else:
                    self._put_state(
                        seen_key,
                        session_id,
                        {"at_epoch": now, "rule": hit["rule"], "failed": True},
                    )
                self._record(self._entry(hit, "send_failed", detail))
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
        text = trigger_text(self.config["port"], background=hit.get("background"))
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
                note = ""
                if draft:
                    self._retry_after[str(pid)] = now + DRAFT_RETRY_SEC
                else:
                    # 送れない相手へ毎分は送らない。間をおいて送り直す
                    entry = self._mark_attempt(
                        session_id,
                        hit["rule"],
                        now,
                        failed=True,
                        context=hit.get("context_tokens"),
                    )
                    note = "。" + _retry_note(entry, now)
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
                        self_clear._safe_detail(e) + note,
                    )
                )
                return
        entry = self._mark_attempt(
            session_id,
            hit["rule"],
            now,
            failed=False,
            context=hit.get("context_tokens"),
        )
        if entry["attempts"] > 1:
            detail += f"・送り直し（{entry['attempts']} 回目）"
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
            failed = self._fail_after_send(expired["session_id"], now, "no_reservation")
            self._record(
                {
                    "outcome": "no_reservation",
                    "pid": expired["pid"],
                    "session_id": expired["session_id"],
                    "detail": "区切りの依頼を送ったが、待ちの上限までに予約が来なかった"
                    + ("。" + _retry_note(failed, now) if failed else ""),
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
        options.get("model"),
        options.get("permission_mode"),
        options.get("effort"),
        options.get("chrome"),
    )
    sm.resolve_claude_exe(claude_exe)

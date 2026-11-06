# -*- coding: utf-8 -*-
"""機械（管理側の役など）から子のセッションへ伝言を入れる口（POST /api/machine-send）。

今の送信（/api/sessions/<pid>/send）は入力欄へ打つだけなので、子からは人の発言と
見分けられない（2026-10-29 ユーザーの指摘）。この口は次のようにする:
  - 本文の頭に見出し「【機械からの伝言・id=…・from=…・ユーザーの発言ではありません】」を
    本アプリが必ず付ける。from は送り手の申告ではなく、合言葉から決める（省けない・偽れない）
  - 送り手は設定 machine_send.senders に書いた役名だけ。役名ごとに合言葉を持つ
    （合言葉は設定ファイルに置き、コードには書かない）
  - 子が作業中・入力欄に書きかけ・入力欄が読めない（no_input_area）ときは預かり、
    入力待ちが IDLE_STABLE_SEC 続いたところで入れる。期限（ttl_sec）を過ぎたら expired
  - 受けた時刻・入れた時刻・結果を id ごとに残し、GET で引ける（伝わるまでの時間を示すため）
  - 宛先は本アプリの台帳の子に限らず、一覧に出るセッション（ほかのプロジェクトの下の子・
    ほかのアカウントのもの）すべて。人の書きかけは消さない（protect_draft）

預かりはファイル（~/.claude-web-app/machine_send.json）にも置き、本アプリが再起動しても
続きから入れる。本文は入れ終わったら捨て、先頭だけ（秘密を伏せて）残す。

会話記録の上では、この伝言も人の発言（origin.kind=human）として載る。自分を区切る予約
（self_clear）は「新しい仕事」として取りやめる。定期実行の文などと同じ扱いで、そのままにする。
"""

import contextlib
import hmac
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime

import access_guard
import safe_console
import send_history
import session_manager as sm

# 口の道（本体の before_request が、この道だけを機械の接続元に開ける）
PATH = "/api/machine-send"

# 本文の頭の見出し。id を先に置く（会話記録の照合は先頭の数十字で行うため）
HEADING = "【機械からの伝言・id={id}・from={sender}・ユーザーの発言ではありません】"

# 見出しの直後に必ず付ける、伝言の立場の一文（受け取った子が、従うかを判断できるように）
STANCE = (
    "claude-web-app（このセッションを起こし・管理している道具）が送り手を確かめた伝言です。"
    "ユーザーの指示と食い違うときはユーザーの指示を優先してください。"
    "返し方は本文に従ってください（本文に無ければ返事は要りません）。"
)

# 本文が貼り付け扱いになる長さのとき、先に素の打鍵で打つ前置き（見出しを貼り付けの外にも出す）
PASTE_PREAMBLE = (
    "{heading}" + STANCE + "下の貼り付けがこの伝言の本文です。読んで進めてください。"
)

# id の形（送り手が付ける。同じ送り手の同じ id は二度入れない）
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")

# 役名に使えない文字（見出しの区切りと紛れるもの・改行）
SENDER_FORBIDDEN = "【】・=\r\n\t"
SENDER_MAX_LEN = 32

# 本文の上限（字）。見出しを足しても sm.SEND_TEXT_MAX_LEN に収まるように
TEXT_MAX_LEN = 8000

# 期限（秒）。送り手が ttl_sec で替えられる（範囲内に丸める）
DEFAULT_TTL_SEC = 3600
MIN_TTL_SEC = 60
MAX_TTL_SEC = 24 * 3600

# 預かりの上限（入れる前のものの件数）
MAX_PENDING_TOTAL = 100
MAX_PENDING_PER_TARGET = 20

# 入力待ちがこれだけ続いてから入れる（ターンの終わりに Stop フックが差し戻すことがあるため）
IDLE_STABLE_SEC = 5

# 見回りの間隔（秒）
POLL_SEC = 1.0

# 入力待ちを見た間がこれより開いたら、続いているかを見直す（秒）
IDLE_GAP_SEC = 10

# 作業中・書きかけ以外の失敗（入力欄が読めない等）が続いたら、間をおく（秒・回数に比例）
RETRY_BACKOFF_SEC = 15
# その失敗がこれだけ続いたら failed で終える
MAX_FAILURES = 6

# 送る直前に作業へ戻った・別の送信が進行中だったときの間（秒）
BUSY_BACKOFF_SEC = 10
# 人の書きかけがあったときの間（秒・回数に比例・上限あり）
DRAFT_BACKOFF_SEC = 30
DRAFT_BACKOFF_MAX_SEC = 300

# 終わったものを残す件数と日数
KEEP_FINISHED = 1000
KEEP_FINISHED_DAYS = 14

# 本文の先頭として残す字数（終わった後）
TEXT_HEAD_CHARS = 120

STATE_FILE = os.path.join(sm.APP_STATE_DIR, "machine_send.json")

# 状態（status）。pending は入れる前、それ以外は終わったもの
PENDING = "pending"
FINISHED = ("delivered", "expired", "target_gone", "failed")

# 入れずに待っている理由（wait_reason）の表示
WAIT_REASONS = {
    "not_idle": "相手が作業中・入力待ち以外",
    "settling": "入力待ちが続くのを確かめている",
    "busy": "送る直前に作業へ戻った・別の送信が進行中（10秒あけて見直す）",
    "draft": "相手の入力欄に書きかけがある（消さずに、30秒×回数・最長5分あけて見直す）",
    "error": "送れなかった（入力欄が読めない等）。間をおいて送り直す",
    "target_missing": "宛先のセッションが見つからない（起こし直しを待つ）",
}

# project_key → 作業場の絶対パス（本体が resolve_project_path を入れる。試験では差し替える）
project_path_resolver = None

_lock = threading.RLock()
_messages = None  # 読み込んだ伝言の並び（受けた順）
_worker = None
_idle_since = {}  # pid → 入力待ちを最初に見た時刻（見回りの中だけで使う）


class MachineSendError(Exception):
    """口で断るとき。status・code は REST の応答に使う。"""

    # 既定の code は invalid（入力の不備）。伝言の状態の failed と紛れないように
    def __init__(self, message, code="invalid", status=400, **extra):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


# --- 設定（送り手と合言葉） ----------------------------------------------------


def _conf(settings):
    """設定の machine_send（書いていなければ空。[] 等の書き違いはそのまま返して断らせる）。"""
    conf = (settings or {}).get("machine_send")
    return {} if conf is None else conf


def senders_of(settings):
    """設定の machine_send.senders を {役名: 合言葉} にする（読めなければ MachineSendError）。

    送り手を足す・合言葉を替えるのに再起動は要らない（受けるたびに読む）。
    """
    conf = _conf(settings)
    if not isinstance(conf, dict):
        raise MachineSendError(
            "設定の machine_send はオブジェクトで書いてください", "config", 503
        )
    senders = conf.get("senders")
    senders = {} if senders is None else senders
    if not isinstance(senders, dict):
        raise MachineSendError(
            '設定の machine_send.senders は {"役名": "合言葉"} の形で書いてください',
            "config",
            503,
        )
    result = {}
    for name, token in senders.items():
        if not valid_sender_name(name):
            raise MachineSendError(
                f"設定の machine_send.senders の役名が使えません: {name!r}"
                f"（{SENDER_MAX_LEN}字まで・【】・= と改行を含めない）",
                "config",
                503,
            )
        if (
            not isinstance(token, str)
            or len(token.strip()) < access_guard.MIN_TOKEN_LENGTH
        ):
            raise MachineSendError(
                f"設定の machine_send.senders の「{name}」の合言葉が短すぎます"
                f"（{access_guard.MIN_TOKEN_LENGTH}文字以上）",
                "config",
                503,
            )
        if token.strip() in result.values():
            # 同じ合言葉の送り手が2人いると、どちらからの伝言か決められない
            raise MachineSendError(
                f"設定の machine_send.senders の「{name}」の合言葉が、ほかの送り手と同じです",
                "config",
                503,
            )
        result[name] = token.strip()
    return result


# sources にこれより広い範囲を書いたら起動時に注意を出す（範囲の中は画面も開けなくなるため）
NARROW_PREFIX = {4: 24, 6: 64}


def broad_sources(networks):
    """sources のうち広すぎる範囲（送り手の機械1台ずつでなさそうなもの）。"""
    return [net for net in networks if net.prefixlen < NARROW_PREFIX[net.version]]


def sources_of(settings):
    """設定の machine_send.sources（この口しか使わせない接続元の範囲）を読む。

    Raises:
        access_guard.AccessConfigError: 読めない範囲がある（本体は起動を止める）
    """
    conf = _conf(settings)
    if not isinstance(conf, dict):
        raise access_guard.AccessConfigError(
            "設定の machine_send はオブジェクトで書いてください"
        )
    try:
        return access_guard.parse_networks(conf.get("sources"))
    except access_guard.AccessConfigError as e:
        raise access_guard.AccessConfigError(
            f"machine_send.sources: {e}".replace("allowed_networks", "sources").replace(
                "100.64.0.0/10", "192.0.2.10/32"
            )
            + "（送り手の機械を1台ずつ /32 で書く。範囲の中の接続元は画面もほかの API も使えなくなる）"
        )


def from_source(networks, addr):
    """接続元が sources の範囲にあるか（ループバックは含めない＝同じ機械の道具は今までどおり）。"""
    if not networks:
        return False
    ip = access_guard._parse_address(addr)
    if ip is None or ip.is_loopback:
        return False
    return any(ip.version == net.version and ip in net for net in networks)


def valid_sender_name(name):
    return (
        isinstance(name, str)
        and name.strip() == name
        and 0 < len(name) <= SENDER_MAX_LEN
        and not any(c in SENDER_FORBIDDEN for c in name)
    )


def authenticate(senders, token):
    """合言葉に当たる役名を返す（当たらなければ None）。時間差で漏れない比べ方で全部と比べる。"""
    found = None
    if not isinstance(token, str) or not token.strip():
        return None
    presented = token.strip().encode()
    for name, expected in senders.items():
        if hmac.compare_digest(presented, expected.encode()):
            found = name
    return found


def is_machine_path(path):
    return path == PATH or (path or "").startswith(PATH + "/")


# --- 宛先 ---------------------------------------------------------------------


def parse_target(value):
    """宛先を {"kind": pid|project_key|name, "value": …} にする。

    {"pid": 123} / {"project_key": "developing/a/projects/b"} / {"name": "my-app"} の
    どれか1つ。文字列なら、数字だけは pid・/ を含めば project_key・それ以外は name。
    """
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            value = {"pid": int(text)}
        elif "/" in text:
            value = {"project_key": text}
        else:
            value = {"name": text}
    if isinstance(value, int) and not isinstance(value, bool):
        value = {"pid": value}
    if not isinstance(value, dict):
        raise MachineSendError(
            'to は {"pid": 数}・{"project_key": "…"}・{"name": "…"} のどれかで指定してください'
        )
    keys = [k for k in ("pid", "project_key", "name") if value.get(k) not in (None, "")]
    if len(keys) != 1:
        raise MachineSendError(
            "to には pid・project_key・name のどれか1つだけを書いてください"
        )
    kind = keys[0]
    raw = value[kind]
    if kind == "pid":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise MachineSendError("to.pid は数で指定してください")
        try:
            pid = int(raw)
        except ValueError:
            raise MachineSendError("to.pid は数で指定してください")
        if pid <= 0:
            raise MachineSendError("to.pid は正の数で指定してください")
        return {"kind": "pid", "value": pid}
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        raise MachineSendError(f"to.{kind} は 300字までの文字列で指定してください")
    return {"kind": kind, "value": raw.strip()}


def _norm_path(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path))) if path else ""


def _receivable(session):
    """入力欄のあるセッションか（-p の実行・ログイン更新用は外す。後者は一覧に出ない）。"""
    return (session.get("kind") or "interactive") == "interactive"


def find_targets(selector, sessions):
    """宛先に当たる生きたセッションの並び（入力欄のあるものだけ）。"""
    kind, value = selector["kind"], selector["value"]
    sessions = [s for s in sessions if _receivable(s)]
    if kind == "pid":
        return [s for s in sessions if str(s.get("pid")) == str(value)]
    if kind == "project_key":
        resolver = project_path_resolver
        path = resolver(value) if resolver else None
        if not path:
            raise MachineSendError(
                f"project_key が読めません: {value}"
                "（developing/… か working/… の形。置き場が設定に無い機械では使えません）",
                "bad_target",
                400,
            )
        want = _norm_path(path)
        return [s for s in sessions if _norm_path(s.get("cwd")) == want]
    named = [s for s in sessions if (s.get("name") or "") == value]
    if named:
        return named
    # 名前（-n）の無いセッションは作業場のフォルダ名で当てる
    return [
        s
        for s in sessions
        if not s.get("name")
        and os.path.basename(_norm_path(s.get("cwd"))) == os.path.normcase(value)
    ]


def _candidate(session):
    return {
        "pid": session.get("pid"),
        "name": session.get("name") or "",
        "cwd": session.get("cwd") or "",
        "session_id": session.get("session_id") or "",
        "account": session.get("account") or "",
    }


def _target_of(session):
    pid = session.get("pid")
    return {
        "pid": pid,
        "create_time": sm._process_create_time(pid),
        "session_id": session.get("session_id") or "",
        "name": session.get("name") or "",
        "cwd": session.get("cwd") or "",
        "account": session.get("account") or "",
        "managed": bool(session.get("launched_by_app")),
    }


# --- 本文 ---------------------------------------------------------------------


def heading(sender, msg_id):
    return HEADING.format(id=msg_id, sender=sender)


# 見出しの頭（これを含む本文は、この口が付けたもの以外は偽物）
HEADING_MARK = "【機械からの伝言"
# 送り手の本文の中に見出しの頭があれば、これに置き換える（ほかの送り手を名乗れないように）
HEADING_MARK_DEFUSED = "［機械からの伝言"
# 見出しの頭に見える形。似た括弧・字の間の空白を許す（目に見えない字は先に除く）
_HEADING_LIKE = re.compile(r"[【〖〘]\s*機\s*械\s*か\s*ら\s*の\s*伝\s*言")
FORGED_ERROR = (
    "本文に「【機械からの伝言」の形は使えません（claude-web-app の機械の口が送り手を確かめたときだけ付ける見出しです）。"
    "引用するときは【 を「 などの別の字に替えてください。機械から伝えるときは /api/machine-send を使ってください"
)


def _invisible(char):
    """目に見えない字か（書式文字・制御文字。タブ・改行は本文の区切りなので残す）。"""
    return unicodedata.category(char) in ("Cf", "Cc") and char not in "\t\n\r"


def _visible(text):
    """目に見えない字を除き、互換の字をそろえる（見出しの判定用）。"""
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if not _invisible(c))


def forged(text):
    """ほかの口（普通の送信・一斉指示・起動の最初の指示）の本文が、機械の口の見出しに見える形を含むか。"""
    return isinstance(text, str) and bool(_HEADING_LIKE.search(_visible(text)))


# 互換の字（NFKC で別の字にそろうもの）を探す範囲。基本の面と CJK 互換漢字の補助
_COMPAT_RANGES = (range(0x10000), range(0x2F800, 0x2FA20))


def _lookalike_classes(targets):
    """targets の各字について、NFKC でそろえるとその字になる字の組（正規表現の字の組）。"""
    found = {t: {t} for t in targets}
    for block in _COMPAT_RANGES:
        for code in block:
            char = chr(code)
            normal = unicodedata.normalize("NFKC", char)
            if normal in found:
                found[normal].add(char)
    return {
        t: "[" + "".join(re.escape(c) for c in sorted(chars)) + "]"
        for t, chars in found.items()
    }


# 元の本文に当てる、互換の字も含めた見出しの形（本文全体は書き換えず、当たった所だけ置き換える。
# 字の組は初めて使うときに作る）
_raw_heading_like = None


def _raw_pattern():
    global _raw_heading_like
    if _raw_heading_like is None:
        classes = _lookalike_classes("【〖〘機械からの伝言")
        brackets = "|".join(classes[c] for c in "【〖〘")
        word = r"\s*".join(classes[c] for c in "機械からの伝言")
        _raw_heading_like = re.compile(f"(?:{brackets})\\s*{word}")
    return _raw_heading_like


def _defuse(text):
    """送り手の本文の中の、見出しに見える形を無害な形にする（目に見えない字も除く）。"""
    text = "".join(c for c in text if not _invisible(c))
    return _raw_pattern().sub(HEADING_MARK_DEFUSED, text)


def build_body(sender, msg_id, text):
    """見出しつきの1行の本文（送り手の本文は1行に整える）。"""
    if not isinstance(text, str) or not text.strip():
        raise MachineSendError("text（本文）を文字列で指定してください")
    single = " ".join(line.strip() for line in text.splitlines() if line.strip())
    single = _defuse(single)
    if len(single) > TEXT_MAX_LEN:
        raise MachineSendError(
            f"本文が長すぎます（{len(single)} 字。上限 {TEXT_MAX_LEN}）", "too_long"
        )
    return heading(sender, msg_id) + STANCE + single


# --- 記録 ---------------------------------------------------------------------


def _now():
    return time.time()


def _text(epoch):
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _load():
    """預かりと記録を読む（1回だけ。壊れていれば空から始め、壊れた物は脇へ残す）。"""
    global _messages
    if _messages is not None:
        return _messages
    messages = []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            messages = [m for m in data["messages"] if isinstance(m, dict)]
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        safe_console.say(
            f"[MachineSend] 記録が読めないので空から始めます（{STATE_FILE}）: {e}",
            flush=True,
        )
        try:
            os.replace(STATE_FILE, STATE_FILE + f".broken-{int(_now())}")
        except OSError:
            pass
    _messages = messages
    return _messages


def _prune(messages, now):
    """終わったものを KEEP_FINISHED 件・KEEP_FINISHED_DAYS 日に抑える（入れる前のものは残す）。"""
    limit = now - KEEP_FINISHED_DAYS * 86400
    finished = [
        m
        for m in messages
        if m.get("status") != PENDING and (m.get("finished_epoch") or now) >= limit
    ]
    keep = set(id(m) for m in finished[-KEEP_FINISHED:])
    messages[:] = [m for m in messages if m.get("status") == PENDING or id(m) in keep]


def _save():
    """ファイルへ書く（一時ファイルから置き換える）。書けなくても口は止めない。"""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                {"version": 1, "messages": _messages}, f, ensure_ascii=False, indent=1
            )
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        safe_console.say(f"[MachineSend] 記録を書けませんでした: {e}", flush=True)


def public(message):
    """応答に出す形（本文・照合の位置などの内部の値は出さない）。"""
    received = message.get("received_epoch")
    delivered = message.get("delivered_epoch")
    return {
        "id": message.get("id"),
        "from": message.get("from"),
        "status": message.get("status"),
        "to": message.get("to"),
        "target": {
            k: v for k, v in (message.get("target") or {}).items() if k != "create_time"
        },
        "received_at": _text(received),
        "delivered_at": _text(delivered),
        "finished_at": _text(message.get("finished_epoch")),
        "expires_at": _text(message.get("expires_epoch")),
        # 受けてから子の会話に載るまでの秒数（伝達にかかった時間）
        "latency_sec": (
            round(delivered - received, 1) if delivered and received else None
        ),
        "attempts": message.get("attempts", 0),
        "wait_reason": message.get("wait_reason"),
        "wait_reason_text": WAIT_REASONS.get(message.get("wait_reason")),
        "detail": message.get("detail") or "",
        # いちばん新しい送れなかった理由（送り直しを待つ間も引ける。失敗が無ければ空）
        "last_error": message.get("last_error") or "",
        "via": message.get("via"),
        "session_id_after": message.get("session_id_after"),
        "text_len": message.get("text_len"),
        "text_head": message.get("text_head") or "",
    }


def _find(sender, msg_id):
    for message in _load():
        if message.get("from") == sender and message.get("id") == msg_id:
            return message
    return None


def _pending():
    return [m for m in _load() if m.get("status") == PENDING]


# --- 受ける -------------------------------------------------------------------


def submit(sender, payload, sessions=None, now=None, start_worker=True):
    """伝言を受けて預かる。返り値は (HTTP 状態, 応答)。

    Args:
        sender: 合言葉から決めた役名（payload の from と一致していること）
        payload: {"from", "to", "text", "id", "ttl_sec"?}
        sessions: 生きたセッションの並び（省略で sm.list_sessions()）
    """
    now = now or _now()
    if not isinstance(payload, dict):
        raise MachineSendError("ボディは JSON のオブジェクトにしてください")
    claimed = payload.get("from")
    if claimed is not None and claimed != sender:
        raise MachineSendError(
            f"from（{claimed!r}）が合言葉の役名と違います。from には自分の役名を書いてください",
            "forbidden",
            403,
        )
    msg_id = payload.get("id")
    if not isinstance(msg_id, str) or not ID_PATTERN.fullmatch(msg_id):
        raise MachineSendError(
            "id は英数字で始まる英数字・_.- の40字までで指定してください"
            "（同じ id は二度入れません。応答を受け取れなかったときの送り直しには同じ id を、"
            "期限切れ・失敗の後に送り直すときは新しい id を使ってください）"
        )
    selector = parse_target(payload.get("to"))
    body = build_body(sender, msg_id, payload.get("text"))
    # 同じ id の送り直しかを見分ける値（宛先も含める。宛先だけ替えた送り直しを黙って重ねない）
    fingerprint = _hash(json.dumps(selector, ensure_ascii=False) + "\n" + body)
    ttl = payload.get("ttl_sec")
    ttl = DEFAULT_TTL_SEC if ttl is None else ttl
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl != ttl:
        raise MachineSendError("ttl_sec は秒数（数）で指定してください")
    ttl = max(MIN_TTL_SEC, min(float(ttl), MAX_TTL_SEC))

    with _lock:
        previous = _find(sender, msg_id)
        if previous is not None:
            # 送り直し（応答を受け取れなかった等）。同じ伝言を二度入れない
            if previous.get("body_hash") != fingerprint:
                raise MachineSendError(
                    f"id {msg_id} は別の本文か別の宛先で受け付け済みです。新しい伝言には新しい id を使ってください",
                    "duplicate_id",
                    409,
                    accepted=public(previous),
                )
            return 200, _duplicate(previous)

    live = sm.list_sessions() if sessions is None else sessions
    matches = find_targets(selector, live)
    if not matches:
        raise MachineSendError(
            f"宛先のセッションが見つかりません（{selector['kind']}={selector['value']}）。"
            "動いていて入力欄のあるセッションだけに送れます。"
            "起こし直しの最中なら、少しあけて送り直してください",
            "not_found",
            404,
        )
    if len(matches) > 1:
        raise MachineSendError(
            f"宛先に当たるセッションが {len(matches)} 本あります。candidates を見て、"
            "名前（-n）か project_key で1本に絞るか、pid で指定し直してください"
            "（pid は起こし直すと外れます）",
            "ambiguous",
            409,
            candidates=[_candidate(s) for s in matches],
        )
    target = _target_of(matches[0])

    with _lock:
        # 受けるまでの間に同じ id が来た場合も二度入れない
        previous = _find(sender, msg_id)
        if previous is not None:
            if previous.get("body_hash") != fingerprint:
                raise MachineSendError(
                    f"id {msg_id} は別の本文か別の宛先で受け付け済みです。新しい伝言には新しい id を使ってください",
                    "duplicate_id",
                    409,
                    accepted=public(previous),
                )
            return 200, _duplicate(previous)
        pending = _pending()
        if len(pending) >= MAX_PENDING_TOTAL:
            raise MachineSendError(
                f"預かりが上限（{MAX_PENDING_TOTAL}件）です。入るのを待ってから送ってください",
                "too_many",
                429,
            )
        same = [
            m for m in pending if (m.get("target") or {}).get("pid") == target["pid"]
        ]
        if len(same) >= MAX_PENDING_PER_TARGET:
            raise MachineSendError(
                f"この宛先への預かりが上限（{MAX_PENDING_PER_TARGET}件）です。"
                "入るのを待ってから送ってください",
                "too_many",
                429,
            )
        message = {
            "id": msg_id,
            "from": sender,
            "status": PENDING,
            "to": selector,
            "target": target,
            "received_epoch": now,
            "expires_epoch": now + ttl,
            "attempts": 0,
            "failures": 0,
            "next_try_epoch": 0,
            "wait_reason": None,
            "detail": "",
            "body": body,
            "body_hash": fingerprint,
            "text_len": len(body),
            # 見出し・立場の文を除いた、送り手の本文の先頭
            "text_head": send_history.text_head(
                body[len(heading(sender, msg_id)) + len(STANCE) :]
            )[:TEXT_HEAD_CHARS],
            # 送った先の会話記録の位置（送った後の確かめが外れても二度入れないため）
            "tried": [],
        }
        _load().append(message)
        _prune(_load(), now)
        _save()
    safe_console.say(
        f"[MachineSend] 受けた id={msg_id} from={sender} pid={target['pid']} "
        f"name={target['name']} 字数={len(body)}",
        flush=True,
    )
    if start_worker:
        ensure_worker()
    return 202, {"ok": True, "duplicate": False, "message": public(message)}


def _duplicate(previous):
    """同じ id の送り直しへの応答。終わったものなら、入らないことと次の手を添える。"""
    body = {"ok": True, "duplicate": True, "message": public(previous)}
    status = previous.get("status")
    if status in ("expired", "failed", "target_gone"):
        body["hint"] = (
            f"この id は終わっています（{status}）。同じ id では二度と入れません。"
            "入れ直すときは新しい id で送ってください"
        )
    return body


def _hash(text):
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get(sender, msg_id):
    """送り手自身の伝言を1件返す（他の送り手のものは見せない）。"""
    with _lock:
        message = _find(sender, msg_id)
        return public(message) if message else None


def list_for(sender, limit=50):
    """送り手自身の伝言を新しい順に返す。"""
    with _lock:
        mine = [public(m) for m in _load() if m.get("from") == sender]
    return list(reversed(mine))[: max(1, min(int(limit), 500))]


# --- 入れる（見回り） ---------------------------------------------------------


def _finish(message, status, detail, now):
    message["status"] = status
    message["detail"] = str(detail or "")[:300]
    message["finished_epoch"] = now
    message["wait_reason"] = None
    message.pop("body", None)
    message.pop("tried", None)
    safe_console.say(
        f"[MachineSend] {status} id={message['id']} from={message['from']} "
        f"pid={(message.get('target') or {}).get('pid')}: {message['detail']}",
        flush=True,
    )


def _landed(message):
    """前に送ったとき（tried）の後ろに、この伝言の見出しが会話記録に載っているか。"""
    needle = json.dumps(heading(message["from"], message["id"]), ensure_ascii=False)[
        1:-1
    ].encode("utf-8")
    for tried in message.get("tried") or []:
        offsets = tried.get("offsets") or {}
        for path in sm._transcript_paths(tried.get("session_id")):
            try:
                with open(path, "rb") as f:
                    f.seek(offsets.get(path, 0))
                    if needle in f.read():
                        return True
            except OSError:
                continue
    return False


def _live_target(message):
    """預かった宛先の、いまの状態（sessions/<pid>.json）。居なければ None。

    pid で指定したものは同じプロセス（開始時刻）のときだけ。project_key・名前で
    指定したものは、プロセスが替わっていたら（起こし直し）当て直す。
    """
    target = message["target"]
    pid = target.get("pid")
    state = sm._read_state(pid) if pid else None
    if (
        state
        and sm.is_session_alive(state)
        and sm._process_create_time(pid) == target.get("create_time")
    ):
        return state
    if message["to"]["kind"] == "pid":
        return None
    try:
        matches = find_targets(message["to"], sm.list_sessions())
    except MachineSendError:
        return None
    # 起こし直しは同じ作業場で起きる。別の作業場の同じ名前の子へは当て直さない
    want = _norm_path(target.get("cwd"))
    matches = [s for s in matches if _norm_path(s.get("cwd")) == want]
    if len(matches) != 1:
        return None
    message["target"] = _target_of(matches[0])
    return sm._read_state(message["target"]["pid"])


@contextlib.contextmanager
def _released():
    """子へ送る間（確かめまで数十秒かかりうる）だけ錠を外す。その間も新しい伝言を受けられるように。

    送っている伝言は見回りの糸しか書き換えないので、外している間に受け手が記録を
    書き出しても食い違わない。錠を持たずに呼ばれたとき（試験）は何もしない。
    RLock を1段だけ外す作り。tick を別の錠の中や _lock を重ねて持った所から呼ぶと、
    錠を持ったまま送ることになる（今は見回りの糸からだけ呼ぶ）。
    """
    try:
        _lock.release()
    except RuntimeError:
        yield
        return
    try:
        yield
    finally:
        _lock.acquire()


def _deliver_one(message, now):
    """1件を入れてみる。入れた・終えたら True（同じ宛先の次へ進める）。"""
    # 載っていたかを期限より先に見る（載っていたのに expired と答えると、送り手が新しい id で送り直して二重に入る）
    if message.get("tried") and _landed(message):
        message["delivered_epoch"] = message.get("delivered_epoch") or now
        _finish(message, "delivered", "会話記録に載っていた（確かめの遅れ）", now)
        return True
    if now >= message["expires_epoch"]:
        reason = (
            WAIT_REASONS.get(message.get("wait_reason")) or "入力待ちにならなかった"
        )
        _finish(message, "expired", f"期限までに入れられなかった（{reason}）", now)
        return True
    state = _live_target(message)
    if not state:
        if message["to"]["kind"] == "pid":
            _finish(message, "target_gone", "宛先のセッションが終わった", now)
            return True
        _wait(message, "target_missing")
        return False
    if now < message.get("next_try_epoch", 0):
        return False
    pid = message["target"]["pid"]
    if not sm.is_input_ready(state):
        # 裏の仕事を持った入力待ち（shell）にも入れる（持続する Monitor 等の子へ届かなくならないように）
        _idle_since.pop(pid, None)
        _wait(message, "not_idle")
        return False
    # [最初に入力待ちを見た時刻, 最後に見た時刻]。見ていない間が開いた（ほかの宛先へ長く送っていた）なら
    # その間に作業へ戻ったかもしれないので、続いているかを見直す
    seen = _idle_since.get(pid)
    if seen is None or now - seen[1] > IDLE_GAP_SEC:
        seen = [now, now]
    seen[1] = now
    _idle_since[pid] = seen
    if now - seen[0] < IDLE_STABLE_SEC:
        _wait(message, "settling")
        return False

    session_id = state.get("sessionId")
    message["attempts"] = message.get("attempts", 0) + 1
    message.setdefault("tried", []).append(
        {"session_id": session_id, "offsets": sm._transcript_sizes(session_id)}
    )
    # 送る前の位置をファイルへ書いてから送る（送っている途中で本アプリが落ちても、
    # 起き直した後に載っていたかを見てから送るので二重に入らない）
    _save()
    body = message["body"]
    history_target = dict(message["target"])
    history_target.pop("create_time", None)
    history_target["session_id"] = session_id
    try:
        with _released():
            result = sm.send_to_session(
                pid,
                text=body,
                preamble=PASTE_PREAMBLE.format(
                    heading=heading(message["from"], message["id"])
                ),
                allow_unmanaged=True,
                protect_draft=True,
            )
    except sm.SessionError as e:
        _idle_since.pop(pid, None)
        if _landed(message):
            # 本文は入ったが確かめが時間切れになっただけ（送り直すと二度入る）
            message["delivered_epoch"] = _now()
            message["via"] = getattr(e, "via", None)
            _record_history(message, history_target, True, None)
            _finish(message, "delivered", "会話記録に載っていた（確かめの遅れ）", now)
            return True
        if isinstance(e, (sm.SessionBusyError, sm.SessionDraftError)):
            # どちらも本文を打つ前に断っている。打った記録から外し、間をおいて見直す
            # （書きかけの確かめは1文字打って消すので、人が打っている間に何度も挟まない）
            message["tried"].pop()
            message["attempts"] -= 1
            message["waits"] = message.get("waits", 0) + 1
            if isinstance(e, sm.SessionBusyError):
                _wait(message, "busy")
                pause = BUSY_BACKOFF_SEC
            else:
                _wait(message, "draft")
                pause = min(DRAFT_BACKOFF_SEC * message["waits"], DRAFT_BACKOFF_MAX_SEC)
            message["next_try_epoch"] = now + pause
        else:
            message["failures"] = message.get("failures", 0) + 1
            message["wait_reason"] = "error"
            message["detail"] = str(e).split("画面の末尾:")[0][:300]
            message["last_error"] = message["detail"]
            if message["failures"] >= MAX_FAILURES:
                _record_history(message, history_target, False, e)
                _finish(message, "failed", message["detail"], now)
                return True
            message["next_try_epoch"] = now + RETRY_BACKOFF_SEC * message["failures"]
        return False
    except Exception as e:  # 見回りを止めない。予期しない失敗は1件の失敗として数える
        _idle_since.pop(pid, None)
        message["failures"] = message.get("failures", 0) + 1
        message["wait_reason"] = "error"
        message["detail"] = f"{type(e).__name__}: {e}".split("画面の末尾:")[0][:300]
        message["last_error"] = message["detail"]
        if message["failures"] >= MAX_FAILURES:
            _finish(message, "failed", message["detail"], now)
            return True
        message["next_try_epoch"] = now + RETRY_BACKOFF_SEC * message["failures"]
        return False
    _idle_since.pop(pid, None)
    message["delivered_epoch"] = _now()
    message["via"] = result.get("via")
    message["session_id_after"] = result.get("session_id_after")
    _record_history(message, history_target, True, None)
    _finish(message, "delivered", VIA_TEXT.get(result.get("via"), "入れた"), now)
    return True


# 入れた口の言い換え（detail に出す）
VIA_TEXT = {
    "inbox": "受け箱（mod web-app-inbox）で入れた（inbox）",
    "console": "入力欄に打って入れた（console）",
}


def _wait(message, reason):
    """入れずに待つ理由を付ける。理由が替わったら、前の失敗の文は消す（今の理由と食い違わないように）。"""
    if message.get("wait_reason") != reason:
        message["detail"] = ""
    message["wait_reason"] = reason


def _record_history(message, target, ok, error):
    send_history.record(
        "machine",
        ok=ok,
        target=target,
        text=message.get("body") or message.get("text_head"),
        source=f"machine:{message['from']}",
        # 画面の末尾（相手の画面の中身）は残さない
        error=str(error).split("画面の末尾:")[0] if error else None,
        code=type(error).__name__ if error else None,
        detail={"id": message["id"], "via": message.get("via")},
    )


def tick(now=None):
    """預かりを1巡する。宛先ごとに古いものから1件ずつ（1巡で同じ宛先へは1件だけ入れる）。

    now を省くと、伝言ごとに時刻を取り直す（ほかの宛先へ送るのに数十秒かかっても、
    入力待ちの続き・送り直しの間を古い時刻で判定しないように）。試験では now を渡す。
    """
    fixed = now
    with _lock:
        pending = _pending()
        if not pending:
            return 0
        seen = set()
        changed = False
        for message in pending:
            key = (message.get("target") or {}).get("pid")
            if key in seen:
                continue
            seen.add(key)
            before = (message.get("status"), message.get("wait_reason"))
            done = _deliver_one(message, _now() if fixed is None else fixed)
            if done or before != (message.get("status"), message.get("wait_reason")):
                changed = True
        if changed:
            _prune(_load(), _now() if fixed is None else fixed)
            _save()
        return len(_pending())


def _run():
    global _worker
    while True:
        try:
            left = tick()
        except Exception as e:  # 見回りの糸が黙って死なないように
            safe_console.say(
                f"[MachineSend] 見回りで失敗: {type(e).__name__}: {e}", flush=True
            )
            left = 1
        if not left:
            with _lock:
                # 預かりが空になったら終える（次に受けたら起こし直す）
                if not _pending():
                    _worker = None
                    return
        time.sleep(POLL_SEC)


def ensure_worker():
    """見回りが居なければ起こす（預かりがあるときだけ）。"""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return False
        if not _pending():
            return False
        _worker = threading.Thread(target=_run, name="machine-send", daemon=True)
        _worker.start()
        return True

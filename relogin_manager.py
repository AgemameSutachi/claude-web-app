"""本アプリが動く機械の Claude のログインを、画面から人が更新するための仕組み。

流れ（作者の別の管理ポータルと同じ形）:
    1. 開始: 信頼済みの専用フォルダで、窓を出さずに claude を起こし、/login を打つ。
       方式の選択が出たら既定（Claude のサブスクリプション）で進め、画面から OAuth の
       URL を取り出して返す。本アプリはセッション0で動くので、ブラウザは開かない。
       「開始」はいつも新しく起こす（前の手続きが残っていれば閉じてから）。
    2. コード: 人が画面に貼った認証コードを、ログイン用の claude の**貼り付け欄に**打って確定する。
       打つ前に画面に貼り付け欄の目印があることを確かめ、無ければ打たない（普通の入力欄へ
       打って会話として送ってしまわないため）。コードを打つのは1つの手続きにつき1回だけ。
       コードは送り手へ標準入力で渡し、ディスク・コマンドライン・ログ・応答に残さない。
       打った後は成功でも失敗でもログイン用の claude を閉じる（画面にコードを残さない）。
    3. 成否: 画面の文言ではなく、資格情報の「更新に使う鍵の期限」（refreshTokenExpiresAt）が
       開始前より延びたかで決める（画面の文言は誤判定しうる・他のセッションの自動更新でも
       ファイルの更新時刻は進むため）。
    4. 確かめ: 期限（時刻だけ）と、別の `claude -p` が1回応答するかを見る。
    5. 取り消し: ログイン用の claude を閉じる。放置しても 15 分で自動的に閉じる。

資格情報（~/.claude/.credentials.json）は期限の時刻だけを読み、トークンの値は読み出さない・
返さない。2026-10-11 時点の形: claudeAiOauth.expiresAt（短期の鍵・約8時間で自動更新）と
claudeAiOauth.refreshTokenExpiresAt（更新に使う鍵の期限。これが切れると再ログインが要る）。

前提: 本アプリは Tailscale の内側からしか開けない。その内側にいる人なら誰でも、自分の
アカウントのコードでこの機械のログインを差し替えられる。外から開く機械では、
設定の auth_token（access_guard.py）で合言葉を要るようにし、使わないなら relogin_enabled: false で切る。
"""

import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse

import psutil

import session_manager as sm

CREDENTIALS_PATH = os.path.join(sm.CLAUDE_HOME, ".credentials.json")

# ログイン用の claude を起こす専用フォルダ（session_manager が一覧・送信の対象から外す）
RELOGIN_DIR = sm.RELOGIN_DIR
RELOGIN_NAME = "relogin"
# 確かめ（claude -p）を動かすフォルダ
CHECK_DIR = os.path.join(sm.APP_STATE_DIR, "relogin-check")

# 起動・画面の変化を待つ時間（秒）
START_IDLE_TIMEOUT_SEC = 60
URL_TIMEOUT_SEC = 40
CODE_TIMEOUT_SEC = 45
# 失敗の文言が出てから、期限が延びないかをもう少し見る時間（秒）
FAILURE_GRACE_SEC = 5
CHECK_TIMEOUT_SEC = 120
POLL_SEC = 1.0

# 放置されたログイン手続きを自動で閉じるまでの時間（秒）
IDLE_CLOSE_SEC = 15 * 60

# 認証コードの上限（文字数）。実際は百数十字。
CODE_MAX_LEN = 2000

# 画面からコードを伏せるときに照合する断片の長さ
REDACT_CHUNK = 4

# 画面の見分け（Claude Code の /login の画面。小文字で照合する）。
# 文言は推定を含むので、実際の画面を見て直す。
LOGIN_METHOD_MARKERS = ("select login method", "claude account with subscription")
PASTE_MARKERS = ("paste code",)
FAILURE_MARKERS = ("invalid", "login failed", "oauth error", "authentication failed")
# コードが通らなかったときの画面（2026-10-12 本番で、無効なコードを1回送って実測）。
# 貼り付け欄の行は消えるので、この文言は画面全体から見る
RETRY_MARKERS = ("press enter to retry",)

# URL に使える文字だけの行（折り返しの続きの行の見分け）
URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
# 枠の線（URL が枠の中に描かれたとき取り除く）
BOX_CHARS = "│┃|║"

_lock = threading.RLock()
# いま開いているログイン用の claude（pid と開始時刻で pid の再利用を見分ける）
_state = {
    "pid": None,
    "create_time": None,
    "started_at": None,
    "url": None,
    "code_sent": False,
}


class ReloginError(Exception):
    """ログイン更新の操作に失敗したことを表す例外（文面にコードは入れない）。

    screen には、利用者に見せてよい画面の末尾（コードを打つ前のもの）を添えられる。
    """

    def __init__(self, message, screen=None):
        super().__init__(message)
        self.screen = screen or []


# --- 期限 -------------------------------------------------------------------


def read_expiry():
    """資格情報の期限（時刻だけ）を返す。トークンの値は返さない。

    Returns:
        dict: exists / access_expires_at / refresh_expires_at（いずれもミリ秒）/ updated_at
    """
    try:
        with open(CREDENTIALS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        updated_at = int(os.path.getmtime(CREDENTIALS_PATH) * 1000)
    except FileNotFoundError:
        return {"exists": False}
    except (OSError, ValueError) as e:
        return {
            "exists": True,
            "error": f"資格情報のファイルを読めませんでした: {type(e).__name__}",
        }
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    oauth = oauth if isinstance(oauth, dict) else {}

    def number(key):
        value = oauth.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    result = {
        "exists": True,
        "access_expires_at": number("expiresAt"),
        "refresh_expires_at": number("refreshTokenExpiresAt"),
        "updated_at": updated_at,
    }
    del data, oauth
    return result


# --- ログイン用の claude ------------------------------------------------------


def _login_pid():
    """いま開いているログイン用の claude の pid（無い・別のプロセスに変わっていれば None）。"""
    pid = _state.get("pid")
    if not pid:
        return None
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - (_state.get("create_time") or 0)) > 2.0:
            return None
        return pid
    except psutil.Error:
        return None


def _screen(pid):
    """ログイン用の claude の画面の行（読むだけ）。"""
    return sm.read_console_screen_isolated(pid, max_lines=80)


def _type(pid, text, enter=True):
    """ログイン用の claude へ text（と Enter）を打つ。text は標準入力で渡す（残さない）。"""
    try:
        completed = subprocess.run(
            [sys.executable, sm.INPUT_SENDER_SCRIPT, "--stdin"],
            input=json.dumps({"pid": int(pid), "raw": text, "enter": enter}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            # 子の標準エラー（cp932 の traceback など）で読み込みが落ちないように
            errors="replace",
            timeout=30,
            creationflags=sm.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise ReloginError(f"打ち込みの送り手を起こせませんでした: {type(e).__name__}")
    try:
        response = json.loads(completed.stdout or "{}")
    except ValueError:
        response = {}
    if not response.get("ok"):
        raise ReloginError(
            "ログイン用の claude へ打ち込めませんでした"
            f"（{response.get('code') or completed.returncode}）"
        )


def _valid_url(url):
    parsed = urllib.parse.urlparse(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and ("oauth" in url or "authorize" in url)
    )


def extract_url(lines):
    """画面の行から OAuth の URL を取り出す（幅で折り返した続きの行もつなぐ）。

    枠の線は取り除き、続きの行は URL に使える文字だけの行に限る（罫線などをつながない）。
    """
    cleaned = [line.strip().strip(BOX_CHARS).strip() for line in lines]
    for i, line in enumerate(cleaned):
        start = line.find("https://")
        if start < 0:
            continue
        head = line[start:].split()[0]
        parts = [head]
        for following in cleaned[i + 1 :]:
            if not following or not URL_CHARS.fullmatch(following):
                break
            parts.append(following)
        url = "".join(parts)
        if _valid_url(url):
            return url
    return None


def _tail(lines, count=20):
    return [line for line in lines if line.strip()][-count:]


def _close(pid):
    """ログイン用の claude を閉じる（子のプロセスも）。"""
    try:
        proc = psutil.Process(pid)
        children = proc.children(recursive=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


def _close_orphans():
    """専用フォルダで動いている claude（本アプリの再起動で取り残されたもの）を閉じる。"""
    for proc in psutil.process_iter(["name"]):
        try:
            if "claude" not in (proc.info.get("name") or "").lower():
                continue
            if sm._same_path(proc.cwd(), RELOGIN_DIR):
                _close(proc.pid)
        except psutil.Error:
            continue


def _clear_state():
    _state.update(
        {
            "pid": None,
            "create_time": None,
            "started_at": None,
            "url": None,
            "code_sent": False,
        }
    )


def _close_if_idle(pid):
    """タイマーから呼ばれる。コードを送って交換を待っている最中なら閉じない（後始末は送信側が行う）。"""
    if _state.get("pid") == pid and _state.get("code_sent"):
        return
    cancel(expected_pid=pid)


def _expire_if_idle():
    """放置されたログイン手続き（IDLE_CLOSE_SEC を過ぎたもの）を閉じる。"""
    started = _state.get("started_at")
    if started and time.time() * 1000 - started > IDLE_CLOSE_SEC * 1000:
        _close_if_idle(_state.get("pid"))


def status():
    """期限と、開いているログイン手続きの状態（画面は返さない）。"""
    _expire_if_idle()
    pid = _login_pid()
    return {
        "expiry": read_expiry(),
        "login": (
            {
                "active": True,
                "url": _state.get("url"),
                "started_at": _state.get("started_at"),
                "code_sent": _state.get("code_sent"),
            }
            if pid
            else {"active": False}
        ),
    }


def start(claude_exe=None):
    """ログイン用の claude を新しく起こして /login を打ち、OAuth の URL を返す。

    前の手続きが残っていれば閉じてから起こす（使い回すと、コードを打った後の画面や
    使用済みの URL を返してしまうため）。失敗したら起こしたものを閉じる。

    Returns:
        dict: url（取り出せなければ None）/ screen（URL が取れないときだけ。コードを打つ前の画面）

    Raises:
        ReloginError: 起動・打ち込みに失敗した場合
    """
    with _lock:
        previous = _login_pid()
        _clear_state()
        if previous:
            _close(previous)
        _close_orphans()

        os.makedirs(RELOGIN_DIR, exist_ok=True)
        sm.ensure_trusted(RELOGIN_DIR)
        exe = sm.resolve_claude_exe(claude_exe)
        try:
            pid = sm._spawn_detached(
                [exe, "-n", RELOGIN_NAME], RELOGIN_DIR, sm.sanitize_env()
            )
            create_time = psutil.Process(pid).create_time()
        except sm.SessionError as e:
            raise ReloginError(f"ログイン用の claude を起こせませんでした: {e}")
        except psutil.Error:
            raise ReloginError("ログイン用の claude がすぐに終了しました")
        _state.update(
            {
                "pid": pid,
                "create_time": create_time,
                "started_at": int(time.time() * 1000),
                "url": None,
                "code_sent": False,
            }
        )
    # 誰も画面を開かなくても、放置された手続きは IDLE_CLOSE_SEC で閉じる
    timer = threading.Timer(IDLE_CLOSE_SEC, _close_if_idle, args=(pid,))
    timer.daemon = True
    timer.start()

    try:
        # 入力待ちになるのを待ってから /login を打つ
        if not sm._wait_state(
            pid, lambda s: s.get("status") == "idle", START_IDLE_TIMEOUT_SEC
        ):
            raise ReloginError(
                "ログイン用の claude が入力待ちになりませんでした。画面の末尾: "
                + " / ".join(_tail(_screen(pid), 8))
            )
        _type(pid, "/login", enter=True)

        pressed_method = False
        lines = []
        deadline = time.time() + URL_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            lines = _screen(pid)
            url = extract_url(lines)
            if url:
                # 別のタブの「開始」で入れ替わっていたら、古い URL を書き込まない
                with _lock:
                    if _state.get("pid") == pid:
                        _state["url"] = url
                return {"url": url, "screen": []}
            text = "\n".join(lines).lower()
            if not pressed_method and any(m in text for m in LOGIN_METHOD_MARKERS):
                # 方式の選択は既定（先頭＝Claude のサブスクリプション）のまま Enter で進める
                _type(pid, "", enter=True)
                pressed_method = True
        # URL は取れなかったが、手続きは残して画面を見せる（コードを打つ前なので画面にコードは無い）
        return {"url": None, "screen": _tail(lines)}
    except Exception:
        cancel(expected_pid=pid)
        raise


def _redact(lines, code):
    """画面の行を伏せて返す。貼り付け欄の行から下はすべて伏せ、上もコードの断片を含む行は伏せる。"""
    chunks = {
        code[i : i + REDACT_CHUNK]
        for i in range(0, max(1, len(code) - REDACT_CHUNK + 1))
    }
    result = []
    below_paste = False
    for line in lines:
        lower = line.lower()
        if below_paste or any(m in lower for m in PASTE_MARKERS):
            below_paste = True
            result.append("（貼り付け欄から下は伏せました）")
        elif any(c and c in line for c in chunks):
            result.append("（認証コードを含む行を伏せました）")
        else:
            result.append(line)
    # 同じ伏せ字の行が続くときは1行にまとめる
    compact = []
    for line in result:
        if not (compact and compact[-1] == line and line.startswith("（")):
            compact.append(line)
    return compact


def _refresh_extended(before):
    """更新に使う鍵の期限が before より延びたか。

    自動更新（約8時間ごと）ではこの期限は延びない（2026-10-11 実測: 19:21 の自動更新の後も
    10-14 18:17 のまま）ので、他のセッションの自動更新と取り違えない。
    """
    return (read_expiry().get("refresh_expires_at") or 0) > before


def _failure_shown(lines):
    """失敗の文言が出たか。

    「Press Enter to retry」は画面全体で見る（通らないと貼り付け欄の行が消えるため）。
    そのほかの失敗の文言は、貼り付け欄の行より下だけで見る（上の起動時の警告などは見ない）。
    """
    lower = [line.lower() for line in lines]
    if any(m in line for line in lower for m in RETRY_MARKERS):
        return True
    start = next(
        (i for i, line in enumerate(lower) if any(m in line for m in PASTE_MARKERS)),
        None,
    )
    if start is None:
        return False
    return any(m in line for line in lower[start + 1 :] for m in FAILURE_MARKERS)


def submit_code(code):
    """人が貼った認証コードを、ログイン用の claude の貼り付け欄に打って確定する。

    コードは標準入力で送り手へ渡し、この関数を出たら持たない。応答・例外の文面にも入れない。
    打った後は、成功でも失敗でもログイン用の claude を閉じる。

    Returns:
        dict: ok（期限が延びたか）/ message / screen（伏せたもの）/ expiry / before_refresh_expires_at

    Raises:
        ReloginError: 入力の不備・手続きが開いていない・貼り付け欄が見えない・打ち込みに失敗した場合
    """
    if not isinstance(code, str):
        raise ReloginError("認証コードを文字列で入れてください")
    code = code.strip()
    if not code:
        raise ReloginError("認証コードが空です")
    if len(code) > CODE_MAX_LEN or any(ord(c) < 0x20 for c in code):
        raise ReloginError(
            "認証コードの形が正しくありません（改行や制御文字は入れられません）"
        )

    with _lock:
        pid = _login_pid()
        if not pid:
            raise ReloginError(
                "ログインの手続きが開いていません。先に「開始」を押してください"
            )
        if _state.get("code_sent"):
            raise ReloginError(
                "この手続きにはもうコードを送りました。「開始」からやり直してください"
            )
        # 貼り付け欄が出ていなければ打たない（普通の入力欄に打つと会話として送られてしまう）
        before_screen = _screen(pid)
        if not any(m in "\n".join(before_screen).lower() for m in PASTE_MARKERS):
            # コードを打つ前の画面なのでコードは映っていない。何が出ていたかを添える
            tail = _tail(before_screen, 12)
            print(f"[Relogin] no paste field. screen: {' / '.join(tail)}", flush=True)
            raise ReloginError(
                "ログイン用の claude に、認証コードの貼り付け欄が見えません（コードは送っていません）。"
                "「開始」からやり直してください。繰り返すなら、画面の文言が想定と違う可能性があります"
                "（下の画面と、本アプリのログを見てください）",
                screen=tail,
            )
        _state["code_sent"] = True

    before = read_expiry().get("refresh_expires_at") or 0
    try:
        _type(pid, code, enter=True)
        lines = []
        outcome = None
        deadline = time.time() + CODE_TIMEOUT_SEC
        while time.time() < deadline:
            time.sleep(POLL_SEC)
            if _refresh_extended(before):
                outcome = "ok"
                lines = _screen(pid)
                break
            lines = _screen(pid)
            if _failure_shown(lines):
                # 失敗の文言が出ても、交換が遅れて終わることがあるので期限を少し読み直す
                grace = time.time() + FAILURE_GRACE_SEC
                while time.time() < grace and not _refresh_extended(before):
                    time.sleep(POLL_SEC)
                outcome = "ok" if _refresh_extended(before) else "failed"
                break
        # 伏せてから末尾に絞る（絞ってからだと、貼り付け欄の行が切れて伏せが効かない）
        screen = _tail(_redact(lines, code))
    finally:
        del code
        # 成功でも失敗でも閉じる（画面にコードを残さない・使用済みの手続きを使い回させない）
        cancel(expected_pid=pid)

    expiry = read_expiry()
    if outcome == "ok":
        return {
            "ok": True,
            "message": "ログインを更新しました（期限が延びました）。「確かめる」で応答するかも見られます",
            "screen": [],
            "expiry": expiry,
            "before_refresh_expires_at": before or None,
        }
    return {
        "ok": False,
        "message": (
            "ログインに失敗しました。もう一度「開始」から、新しい URL でやり直してください"
            if outcome == "failed"
            else f"{CODE_TIMEOUT_SEC} 秒待っても期限が延びませんでした。"
            "「確かめる」で期限を見て、延びていなければ「開始」から新しい URL でやり直してください"
        ),
        "screen": screen,
        "expiry": expiry,
        "before_refresh_expires_at": before or None,
    }


def cancel(expected_pid=None):
    """ログイン用の claude を閉じる（開いていなければ何もしない）。

    expected_pid を渡したときは、いまの手続きがその pid のときだけ閉じる
    （遅れて届いた後始末が、別のタブから始めた新しい手続きを閉じないように）。

    Returns:
        bool: 閉じたか
    """
    with _lock:
        pid = _login_pid()
        if not pid or (expected_pid is not None and pid != expected_pid):
            return False
        _clear_state()
    _close(pid)
    return True


def check(claude_exe=None):
    """資格情報の期限と、別の `claude -p` が1回応答するかを確かめる。

    Returns:
        dict: ok / reply（応答の先頭）/ error / elapsed_sec / expiry / checked_at
    """
    # 確かめの claude -p は別のフォルダで動かす（「開始」が専用フォルダの取り残しを閉じるとき、
    # 確かめの最中のものまで閉じないように）
    os.makedirs(CHECK_DIR, exist_ok=True)
    started = time.time()
    try:
        exe = sm.resolve_claude_exe(claude_exe)
        completed = subprocess.run(
            [
                exe,
                "-p",
                "OK とだけ返してください",
                "--model",
                "haiku",
                "--max-turns",
                "1",
                "--output-format",
                "json",
            ],
            cwd=CHECK_DIR,
            env=sm.sanitize_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SEC,
            creationflags=sm.CREATE_NO_WINDOW,
        )
        try:
            result = json.loads(completed.stdout or "{}")
        except ValueError:
            result = {}
        reply = str(result.get("result") or "").strip()
        ok = (
            completed.returncode == 0
            and not result.get("is_error")
            and re.search(r"\bOK\b", reply.upper()) is not None
        )
        error = None
        if not ok:
            error = (
                reply
                or (completed.stderr or "").strip()
                or f"終了コード {completed.returncode}"
            )[-300:]
    except subprocess.TimeoutExpired:
        ok, reply, error = (
            False,
            "",
            f"{CHECK_TIMEOUT_SEC} 秒以内に応答がありませんでした",
        )
    except (OSError, sm.SessionError) as e:
        ok, reply, error = False, "", f"claude を起こせませんでした: {e}"
    return {
        "ok": ok,
        "reply": reply[:200],
        "error": error,
        "elapsed_sec": round(time.time() - started, 1),
        "expiry": read_expiry(),
        "checked_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }

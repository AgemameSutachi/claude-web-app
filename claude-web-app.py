# -*- coding: utf-8 -*-
import glob
import html
import ipaddress
import os
import re
import socket
import sys
import uuid
import threading
import time
import json
from urllib.parse import urlencode, urlsplit
from flask import Flask, Response, render_template, abort, redirect, request, jsonify
from flask_socketio import SocketIO, emit
import access_guard
import accounts
import project_manager as pm
import session_manager as sm
import broadcaster
import relogin_manager as rl
import self_clear
import attention
import send_history
import usage_tracker
import auto_clear
import machine_send
import quota_resume
import quota_alert
import liaison
import transcript_tail
import safe_console

# 常駐の管理アプリ配下では PYTHONIOENCODING が無く、標準出力・標準エラーが cp932 になる。
# 画面の行（❯ など cp932 に無い文字）を print すると UnicodeEncodeError で落ちるため、
# 書けない文字はバックスラッシュと u で始まる16進の形で書く（日本語は cp932 で書けるのでそのまま。
# 2026-10-12 評価役の指摘）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError):
        pass

app = Flask(__name__)
app.config["SECRET_KEY"] = str(uuid.uuid4())

# 接続中のクライアント数。0 のときはセッション一覧の定期配信を止める。
connected_clients = 0
clients_lock = threading.Lock()

# セッション一覧を定期配信する間隔（秒）。claude 側の状態（idle/busy）を追う。
SESSION_POLL_INTERVAL_SEC = 4

# 本アプリの画面からの呼び出しに付ける目印（送った履歴の「どこから」を分けるだけ。認証ではない）
SCREEN_HEADER = "X-Claude-Web-App-Screen"

# 使用量（今週の推定額）を数え直す間隔（秒）。会話記録の増えた分だけを読む
USAGE_REFRESH_INTERVAL_SEC = 60

# 許可待ちの控えの判定に使う通知の記録・使用量の集計・プロセスごとの会話の番号
notification_log = attention.NotificationLog()
# 会話記録は、設定にあるすべてのアカウントの置き場の projects を数える（accounts.py）
usage = usage_tracker.UsageTracker(
    lambda: [
        (name, os.path.join(home, "projects")) for name, home in sm._account_homes()
    ]
)
# 置き場は使うたびに sm.APP_STATE_DIR から組み立てる（試験が一時の置き場へ向けられるように・2026-11-02）
lineage = usage_tracker.SessionLineage(
    lambda: os.path.join(sm.APP_STATE_DIR, "session_lineage.json")
)
# 管理下のセッションを適切なときに自動で区切る見回り（auto_clear.py）
auto = auto_clear.AutoClear(usage, lambda cwd, sid: transcript_path_for(cwd, sid))
# 人の手を待つこと・自動で送れなかったことの、管理役への知らせ（liaison.py。設定 liaison.tool が空なら何もしない）
notices = liaison.Liaison(settings_loader=lambda: load_settings())
# 枠切れで止まった子へ、枠が戻ったら続きを頼む見回り（quota_resume.py）
quota = quota_resume.QuotaResume(
    lambda cwd, sid: transcript_path_for(cwd, sid),
    notifier=lambda kind, key, text: notices.notify(kind, key, text),
)
# 週の枠で止まった子を ntfy で人へ知らせる見回り（quota_alert.py。config/ntfy.ini が無ければ鳴らさない）
weekly_alert = quota_alert.QuotaAlert(
    lambda cwd, sid: transcript_path_for(cwd, sid),
    notifier=lambda kind, key, text: notices.notify(kind, key, text),
)

# プロジェクトの親ディレクトリ絶対パスは config/settings.json の developing_dir・working_dir
# （起動時に読む。例は config/settings.example.json）。
# 既定は空（未設定）。新しく入れた機械で別の機械の道をそのまま使うと、
# 同期専用の写し（実行禁止）等を指してしまうため、未設定なら置き場を使わない（一覧は空・作成と移動は断る）

# プロジェクト使用回数記録用ファイルの絶対パス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USAGE_FILE = os.path.join(BASE_DIR, "config", "project_usage.json")
SNIPPETS_FILE = os.path.join(BASE_DIR, "config", "snippets.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "config", "settings.json")

# プロジェクトの置き場（作成先・移動先）の識別子。
# temp（一時フォルダで試す）→ developing（育った）→ working（運用開始）と移していく想定。
PROJECT_LOCATIONS = ("temp", "developing", "working")

# 新規作成で選べる置き場。運用中のものを置く working へいきなり作ることはしない。
CREATE_LOCATIONS = ("temp", "developing")

# 移動の再試行回数。終了直後はフォルダの掴みが残ることがあるため、間を空けて試す。
MOVE_RETRY_COUNT = 3

# アプリ動作設定のデフォルト値。
# port はGPU TweakやAGY等、他アプリとの競合を避けて自由に変更できるよう設定ファイル化している。
DEFAULT_SETTINGS = {
    # 待ち受けるアドレス。設定ファイルが無い（新しく入れた）機械では同じ機械からだけ開ける
    # 127.0.0.1 にする。外（Tailscale）から開くときは 0.0.0.0 にし、allowed_networks と
    # auth_token で絞る（設定ファイルに 0.0.0.0 と書いてある機械は今までどおり）。
    # Tailscale の IP だけで待ち受けると、127.0.0.1 から呼ぶ区切りの予約・resumer が繋がらない
    "host": "127.0.0.1",
    "port": 5002,  # 他アプリと競合する場合は config/settings.json の port を変更する
    # 接続を受け付ける相手の範囲（CIDR の並び。空なら絞らない。ループバックはいつも通す）
    "allowed_networks": [],
    # 外（ループバック以外）から使うときの合言葉（16文字以上。空なら合言葉なし）。access_guard.py
    "auth_token": "",
    # ループバック以外に、Host として受け付ける名前・IP（DNS rebinding 対策・host_refused）。
    # 外から開く機械は、開くときの名前（機械名・tailnet の名前）と IP をここに書く
    "allowed_hosts": [],
    # Socket.IO の接続元（Origin）の許可。null なら同じオリジンだけ。
    # "*" や ["http://100.x.y.z:5002"] のように明示もできる（"*" は別サイトから繋がれるので勧めない）
    "cors_allowed_origins": None,
    # 画面・定型文に出す機械の名前（空ならホスト名）
    "machine_name": "",
    # ログイン更新（/relogin）を使えるか。false で画面も API も 404 にする
    "relogin_enabled": True,
    # プロジェクトの置き場（起動時に読む。変えたら再起動）
    "developing_dir": "",
    "working_dir": "",
    # プロジェクト名の自動生成（ollama）を使うか。false なら ollama を呼ばず日時の名前にする
    "auto_naming_enabled": True,
    # 新規プロジェクトの作成先（この直下の projects/ フォルダに集約される）。
    # 開いているプロジェクトごとに散らばらせず、常にここへ作る。
    "project_root": "developing/claude-web-app",
    # プロジェクト名の自動生成に使うローカルLLM（ollama）の設定。
    # 外部APIを使わずローカル完結させるため、既定でホストの ollama を参照する。
    "ollama_url": "http://localhost:11434",
    "ollama_model": "qwen3.5:9b",
    "ollama_timeout": 60,
    # claude 実行ファイルのパス。空なら PATH から自動で探す。
    "claude_exe": "",
    # 自動の区切り（auto_clear.py）: off / dry_run（当たりを記録するだけ）/ on（送る）
    "auto_clear_mode": auto_clear.DEFAULT_MODE,
    # 子に mod web-app-inbox が入っていれば受け箱で送るか（false なら必ず打鍵。送るたびに読む・再起動は要らない）
    "inbox_enabled": True,
    # claude のアカウント（accounts.py。置き場の無い1件が本家。空なら本家だけ＝選ぶ画面を出さない）。
    # 呼ぶたびに読む（再起動は要らない）
    "accounts": [],
    # 割り当てを trial_roles.json の形で書き出す先（claude-session-resumer が読む写し。空なら書かない）
    "trial_roles_mirror": "",
    # 写しに書くアカウント（空なら本家以外の唯一のもの）
    "mirror_account": "",
    # 5時間枠の使用%がこれ以上のアカウントは新しいセッションの既定にしない（accounts.py の policy。
    # アカウントごとの five_hour_max_pct が優先。枠の大きさの比はアカウントの capacity（週）・
    # five_hour_capacity（5時間）、5時間枠の無いアカウントは five_hour_unlimited）
    "five_hour_max_pct": 80,
    # 割り当ての無い作業場で起こすアカウント（空なら本家。本家に会話の記録がある作業場は本家のまま・accounts.effective_name）
    "unassigned_account": "",
    # 機械から子へ伝言を入れる口（POST /api/machine-send・machine_send.py）。
    # senders: {役名: 合言葉（16文字以上）}（受けるたびに読む）。空なら口は使えない。
    # sources: この口しか使わせない接続元の範囲（CIDR。起動時に読む・変えたら再起動）
    "machine_send": {"senders": {}, "sources": []},
    # 枠切れで止まった子へ、枠が戻ったら続きを頼むか（quota_resume.py）: off / dry_run（記録だけ）/ on（送る）
    "quota_resume_mode": quota_resume.DEFAULT_MODE,
    # 週の枠で止まった子を ntfy で人へ1通知らせるか（quota_alert.py・送り先は config/ntfy.ini）: on / off
    "quota_alert_mode": quota_alert.DEFAULT_MODE,
    # 許可待ち・枠切れ・自動で送れなかったことを管理役へ知らせる（liaison.py。受けるたびに読む）。
    # tool: claude_send.py の絶対パス（空なら知らせない）
    "liaison": {
        "tool": "",
        "from": liaison.DEFAULT_FROM,
        "to": liaison.DEFAULT_TO,
        "permission_wait_minutes": liaison.DEFAULT_PERMISSION_WAIT_MINUTES,
    },
}

# デフォルトスニペット
DEFAULT_SNIPPETS = [
    {"label": "git status", "command": "git status"},
    {"label": "git diff", "command": "git diff"},
    {"label": "pytest", "command": "python -m pytest"},
    {"label": "claude", "command": "claude"},
    {"label": "exit", "command": "exit"},
]


def load_settings():
    """アプリ動作設定をJSONファイルから読み込む。

    存在しない場合はデフォルト設定でファイルを新規作成する。
    既存ファイルにキーが欠けている場合はデフォルト値で補完し、
    後からポート等を追記しても安全に動作するようにしている。
    """
    settings = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                settings.update(loaded)
            return settings
        except Exception as e:
            print(
                f"設定ファイルの読み込みに失敗しました（デフォルト値を使用します）: {e}"
            )
            return settings

    # ファイルが無い場合はデフォルトを書き出してから返す
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"設定ファイルの作成に失敗しました: {e}")
    return settings


def _setting_dir(settings, key, default=""):
    """置き場の設定を絶対パスにする（空・文字列でないときは既定＝未設定の空文字）。"""
    value = settings.get(key)
    if not isinstance(value, str) or not value.strip():
        return default
    return os.path.abspath(os.path.expandvars(os.path.expanduser(value.strip())))


def machine_name(settings=None):
    """画面・定型文に出す機械の名前（設定の machine_name、空ならホスト名）。"""
    settings = settings if settings is not None else load_settings()
    name = settings.get("machine_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return socket.gethostname() or "この機械"


LOOPBACK_HOST_NAMES = ("127.0.0.1", "localhost", "::1")


def _is_loopback_listen(listen_host):
    """待ち受けがループバックだけか（localhost・127.0.0.0/8・::1。[::1] の括弧も外して見る）。"""
    value = (listen_host or "").strip().lower().strip("[]")
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _host_name(value):
    """Host（や設定の名前）から、ポート・IPv6 の括弧・末尾のドットを外して小文字にした名前。"""
    host = (value or "").strip().lower()
    if host.startswith("["):
        name = host[1 : host.find("]")] if "]" in host else host[1:]
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    try:
        # IP は書き方の違い（IPv6 の省略等）をそろえる
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name


def parse_allowed_hosts(value):
    """設定の allowed_hosts（名前・IP の並び）を、比べやすい形の集合にする。

    Raises:
        ValueError: 並びでない・文字列でない・空の要素がある
    """
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(
            'allowed_hosts は名前・IP の並びで書いてください（例: ["my-pc", "100.64.1.2"]）'
        )
    names = set()
    for item in value:
        raw = item.strip() if isinstance(item, str) else ""
        # 括弧は外側の1組だけ（"[fd7a::1]junk]" の後ろを黙って捨てない）
        bracketed = raw.startswith("[") and raw.endswith("]") and raw.count("]") == 1
        # URL の形（http://…）・ポート付き・空白は、黙って違う名前にならないよう断る
        if (
            not raw
            or any(c in raw for c in "/@ \t")
            or (":" in raw and raw.count(":") == 1)
            or (raw.startswith("[") and not bracketed)
        ):
            raise ValueError(
                f"allowed_hosts の要素が読めません: {item!r}"
                "（名前か IP だけを書く。http:// やポートは付けない）"
            )
        name = _host_name(raw)
        # 名前は英数・-・_・. だけ（* や , を黙って通さない）。括弧は IPv6 にだけ付ける。空になる "." や "[]" も断る。
        # 続くドット（a..b）と、数字とドットだけで IP として読めない形（999.1.1.1）は書き損じとして断る
        if not (
            _is_ipv6(name)
            if bracketed
            else _is_ip(name)
            or (
                _HOST_NAME_RE.fullmatch(name)
                and ".." not in name
                and not _NUMERIC_DOTTED_RE.fullmatch(name)
            )
        ):
            raise ValueError(
                f"allowed_hosts の要素が読めません: {item!r}"
                "（名前は英数字・-・_・. だけ。角括弧（[ と ]）は IPv6 にだけ付ける。"
                "IP は 0〜255 の4つの数（先頭に 0 を付けない）か IPv6 で書く）"
            )
        # どこでもない宛先（0.0.0.0・::）は Host として通すと別サイトからの手口に使われうるので断る
        if _is_ip(name) and ipaddress.ip_address(name).is_unspecified:
            raise ValueError(
                f"allowed_hosts に {item!r} は書けません"
                "（0.0.0.0・:: は待ち受けの書き方で、開く名前ではありません。この機械の IP か名前を書く）"
            )
        names.add(name)
    return frozenset(names)


_HOST_NAME_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_.-]*[a-z0-9_])?")
# 数字とドットだけ（IP として読めなかったものは書き損じ）
_NUMERIC_DOTTED_RE = re.compile(r"[0-9.]+")


def _is_ip(value):
    """IP（v4・v6）として読めるか。"""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_ipv6(value):
    """IPv6 として読めるか（括弧つきで書けるのは IPv6 だけ）。"""
    return _is_ip(value) and ":" in value


def host_refused(allowed_hosts, request_host):
    """Host が、ループバックの名前・IP でも allowed_hosts でもない要求を断るか。

    悪いサイトの名前をこの機械の IP へ向け直す手口（DNS rebinding）では、ブラウザから見て
    同じオリジンになり Origin の確かめを抜ける。Host は向け直した名前のままなので、ここで断る。
    外（0.0.0.0）で待ち受けて Tailscale の名前や IP で開く機械は、その名前を allowed_hosts に書く。
    """
    name = _host_name(request_host)
    # ループバックの字のまま（127.0.0.2 等で待ち受けたときの 127.0.0.2 を含む）と localhost はいつも通す
    if _is_loopback_listen(name):
        return False
    return name not in allowed_hosts


def socketio_cors_origins(settings, guard):
    """Socket.IO の Origin の許可。明示が無ければ同じオリジンだけ（None）。

    WebSocket は CORS に縛られないので、"*" にすると同じ PC（や範囲の中のスマホ）の
    ブラウザで開いた別サイトのページの JS が繋ぎ、セッションの起動・送信まで打てる。
    守り（範囲・合言葉）の有無にかかわらず、明示が無ければ他のサイトから繋がせない
    （以前は守りが無いとき "*" だった・2026-10-25 公開前の評価で高）。
    Origin を付けない道具（ブラウザ以外）は今までどおり繋がる。guard は呼び出しの形を保つため残す。
    """
    value = settings.get("cors_allowed_origins")
    if value is not None:
        return value
    return None


# 起動時に1回だけ読む設定（置き場・入口の守り・Origin の許可。変えたら再起動）
STARTUP_SETTINGS = load_settings()
# 受け箱（mod web-app-inbox）で送るかは、session_manager が送るたびに設定の inbox_enabled を見る
DEVELOPING_DIR = _setting_dir(STARTUP_SETTINGS, "developing_dir")
WORKING_DIR = _setting_dir(STARTUP_SETTINGS, "working_dir")
# 設定が読めない形なら起動を止める（読めた分だけで動くと、思ったより広く通してしまう）
try:
    guard = access_guard.from_settings(STARTUP_SETTINGS)
    ALLOWED_HOSTS = parse_allowed_hosts(STARTUP_SETTINGS.get("allowed_hosts"))
    # 機械の口（/api/machine-send）だけを開ける接続元（管理側の役の容器など）
    MACHINE_SOURCES = machine_send.sources_of(STARTUP_SETTINGS)
except (access_guard.AccessConfigError, ValueError) as e:
    raise SystemExit(f"設定ファイル {SETTINGS_FILE} を直してから起動してください: {e}")
# URL で渡した合言葉（?token=）をアクセスログに残さない
access_guard.install_log_redaction()

# 標準の threading モードで起動する（eventlet の monkey_patch は使わない）
socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=socketio_cors_origins(STARTUP_SETTINGS, guard),
)


# --- 入口の守り（access_guard） -------------------------------------------------
# 合言葉を入れてもらう画面。合言葉は表示・ログに出さない
_LOGIN_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-web-app（{machine}）</title>
<style>body{font-family:sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem}
h1{font-size:1.4rem;overflow-wrap:anywhere}
input,button{font-size:1rem;padding:.5rem;width:100%;box-sizing:border-box;margin:.3rem 0}
.err{color:#b00020}</style></head><body>
<h1>claude-web-app（{machine}）</h1>
<p>{message}</p>
{error}
{form}
</body></html>"""

_LOGIN_FORM = """<form method="post" action="/auth">
<input type="hidden" name="next" value="{next}">
<input type="password" name="token" autocomplete="current-password" placeholder="合言葉" autofocus>
<button type="submit">開く</button>
</form>"""

_LOGIN_MESSAGE = (
    "このアプリを開くには合言葉が要ります。この機械の設定（auth_token）に書いた合言葉を"
    "入れてください。一度入れると、このブラウザは覚えます。"
)

_BAD_TOKEN = (
    "合言葉が違います。前後の空白や大文字・小文字を確かめて、もう一度入れてください。"
)

# 合言葉なしで開ける道（入口の画面と、その送り先）
_AUTH_PATH = "/auth"


def _safe_next(value):
    """合言葉を入れた後に戻る先。このサイトの中の道だけ（それ以外は /）。

    / で始まり、// で始まらず、バックスラッシュ・空白・制御文字を含まず（Werkzeug は
    タブ等を落とすので /<TAB>/evil が //evil になる）、スキームもホストも持たないもの。
    """
    if not isinstance(value, str) or not value.startswith("/"):
        return "/"
    if value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    if parts.path == _AUTH_PATH:
        return "/"
    return value


def _current_path_without_token():
    """いまの道（合言葉の問い合わせを除いたもの）。"""
    rest = [(k, v) for k, v in request.args.items(multi=True) if k != "token"]
    return (request.path or "/") + ("?" + urlencode(rest) if rest else "")


def _page(message, error="", form="", status=401):
    """入口・断りの画面を作る（中身はすべてエスケープする）。"""
    body = (
        _LOGIN_PAGE.replace("{machine}", html.escape(machine_name()))
        .replace("{message}", html.escape(message))
        .replace("{error}", f'<p class="err">{html.escape(error)}</p>' if error else "")
        .replace("{form}", form)
    )
    return Response(body, status=status, mimetype="text/html")


def _login_page(error="", status=401, next_path=None):
    """合言葉の入口の画面を返す。"""
    if next_path is None:
        next_path = _current_path_without_token()
    form = _LOGIN_FORM.replace("{next}", html.escape(_safe_next(next_path)))
    return _page(_LOGIN_MESSAGE, error=error, form=form, status=status)


def _remember_token(response):
    """ブラウザに合言葉の印（合言葉そのものではない）を覚えさせる。"""
    response.set_cookie(
        access_guard.COOKIE_NAME,
        guard.cookie_value(),
        max_age=access_guard.COOKIE_MAX_AGE_SEC,
        httponly=True,
        # Lax: 他のサイトのページからの POST・fetch・WebSocket では送られない。
        # 通知などのリンクから開いたとき（トップの移動）には送られる
        samesite="Lax",
        path="/",
    )
    return response


def _relogin_path(path):
    return path == "/relogin" or path.startswith("/api/relogin")


@app.before_request
def _guard_request():
    """接続元・合言葉・ログイン更新の可否を、どの道より先に確かめる。"""
    path = request.path or "/"
    if _relogin_path(path) and not load_settings().get("relogin_enabled", True):
        abort(404)
    addr = request.remote_addr
    if host_refused(ALLOWED_HOSTS, request.host):
        # Host がループバックでも allowed_hosts でもない＝悪いサイトの名前をこの機械へ
        # 向け直して（DNS rebinding）同じオリジンに見せかけた要求か、設定に書いていない名前で開いた
        safe_console.say(
            f"[Access] refused (host) addr={addr} path={path} host={request.host}",
            flush=True,
        )
        port = STARTUP_SETTINGS.get("port", DEFAULT_SETTINGS["port"])
        error = (
            f"この名前（{_host_name(request.host)}）では開けません。"
            f"http://127.0.0.1:{port}/ で開くか、この名前を設定の allowed_hosts に足して起動し直してください"
        )
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": error, "code": "forbidden"}), 403
        return _page(error, status=403)
    # 機械の口: sources の接続元には、この口のほかは何も開けない。口そのものは
    # 送り手ごとの合言葉で口の中が確かめる（画面用の合言葉・別ページ対策の検査は掛けない。
    # Authorization ヘッダーは別サイトのページからは事前の問い合わせなしに付けられない）
    machine_path = machine_send.is_machine_path(path)
    machine_source = machine_send.from_source(MACHINE_SOURCES, addr)
    if machine_source and not machine_path:
        safe_console.say(
            f"[Access] refused (machine only) addr={addr} path={path}", flush=True
        )
        error = f"このアドレス（{addr}）からは {machine_send.PATH} だけが使えます"
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": error, "code": "machine_only"}), 403
        return _page(error, status=403)
    if machine_path and (machine_source or guard.network_allowed(addr)):
        return None
    if guard.active and not guard.network_allowed(addr):
        safe_console.say(
            f"[Access] refused (network) addr={addr} path={path}", flush=True
        )
        error = (
            f"このアドレス（{addr}）からは使えません。"
            "許可した範囲の網（allowed_networks。Tailscale 等）から、この機械のその網の IP で開いてください"
        )
        if path.startswith("/api/"):
            return jsonify({"ok": False, "error": error, "code": "forbidden"}), 403
        return _page(error, status=403)
    # 状態を変える要求は、別のページ（別ポート・別サイト）から来たものを断る。
    # 範囲だけの守り（合言葉なし）でも、範囲の中のスマホで開いた悪いページから POST できるため。
    # 守りの無い設定（既定の 127.0.0.1）でも、同じ PC のブラウザで開いた別サイトのページから
    # 127.0.0.1 へ POST されうるので掛ける（守りが無ければ kind は "open"）
    kind = (
        guard.auth_kind(addr, request.headers, request.cookies)
        if guard.active
        else "open"
    )
    if (
        # 同じ機械の直の接続（local）も、そのブラウザで開いた別サイトのページから 127.0.0.1 へ
        # POST されうるので掛ける（urllib・resumer は Origin も Sec-Fetch-Site も付けないので通る）
        kind in ("open", "cookie", "local")
        # 入口（/auth）は合言葉を確かめて印を作るだけで、セッションには触らない
        and path != _AUTH_PATH
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and access_guard.cross_origin_refused(
            kind,
            request.headers,
            access_guard.own_origin(request.headers, request.scheme, request.host),
        )
    ):
        safe_console.say(
            f"[Access] refused (cross origin) addr={addr} path={path} "
            f"origin={request.headers.get('Origin')}",
            flush=True,
        )
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "別のページからの操作は受け付けません。このアプリの画面から操作してください",
                    "code": "cross_origin",
                }
            ),
            403,
        )
    if not guard.token_required:
        return None
    if path == _AUTH_PATH:
        return None
    # ``/?token=<合言葉>`` で開いたら、印を覚えさせてから合言葉を外した URL へ移す
    if request.method == "GET" and "token" in request.args:
        target = _current_path_without_token()
        if guard.token_matches(request.args.get("token")):
            return _remember_token(redirect(target, code=303))
        safe_console.say(f"[Access] refused (bad token in url) addr={addr}", flush=True)
        if path.startswith("/api/"):
            return (
                jsonify({"ok": False, "error": _BAD_TOKEN, "code": "unauthorized"}),
                401,
            )
        return _login_page(_BAD_TOKEN, next_path=target)
    if kind:
        return None
    if path.startswith("/api/"):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "合言葉が要ります（Authorization: Bearer <合言葉>）",
                    "code": "unauthorized",
                }
            ),
            401,
        )
    return _login_page()


@app.route(_AUTH_PATH, methods=["GET", "POST"])
def auth_page():
    """合言葉の入口。POST で合っていれば印を覚えさせて元の道（next）へ移す。"""
    next_path = _safe_next(request.values.get("next"))
    if not guard.token_required:
        return redirect(next_path, code=303)
    if request.method == "GET":
        return _login_page(status=200, next_path=next_path)
    if guard.token_matches(request.form.get("token")):
        return _remember_token(redirect(next_path, code=303))
    safe_console.say(
        f"[Access] refused (bad token) addr={request.remote_addr}", flush=True
    )
    return _login_page(_BAD_TOKEN, next_path=next_path)


def load_snippets():
    """スニペット一覧をJSONファイルから読み込む。存在しない場合はデフォルトを作成する。"""
    if os.path.exists(SNIPPETS_FILE):
        try:
            with open(SNIPPETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading snippets file: {e}")

    # デフォルトを保存して返す
    save_snippets(DEFAULT_SNIPPETS)
    return DEFAULT_SNIPPETS


def save_snippets(snippets):
    """スニペット一覧をJSONファイルに保存する。"""
    try:
        os.makedirs(os.path.dirname(SNIPPETS_FILE), exist_ok=True)
        with open(SNIPPETS_FILE, "w", encoding="utf-8") as f:
            json.dump(snippets, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving snippets file: {e}")


def load_project_usage():
    """プロジェクトの使用回数をJSONファイルから読み込む。"""
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading usage file: {e}")
    return {}


def save_project_usage(usage):
    """プロジェクトの使用回数をJSONファイルに保存する。"""
    try:
        os.makedirs(os.path.dirname(USAGE_FILE), exist_ok=True)
        with open(USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(usage, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving usage file: {e}")


def rename_project_usage(old_key, new_key):
    """プロジェクト移動時に、起動回数の記録を新しいキーへ引き継ぐ。

    配下のサブプロジェクト（``<キー>/projects/...``）の記録もまとめて付け替える。
    移動しただけで「よく使うプロジェクト」の並び順が初期化されないようにするため。
    アカウントの割り当ても付け替える（付け替えないと、移した先は本家で起きてしまう）。
    """
    rename_project_account(old_key, new_key)
    usage = load_project_usage()
    if not usage:
        return

    changed = False
    for key in list(usage.keys()):
        if key == old_key:
            suffix = ""
        elif key.startswith(old_key + "/"):
            suffix = key[len(old_key) :]
        else:
            continue
        # 付け替え先に以前の同名の回数が残っていれば、上書きせず足し合わせる
        count = usage.pop(key)
        usage[new_key + suffix] = usage.get(new_key + suffix, 0) + count
        changed = True

    if changed:
        save_project_usage(usage)


def rename_project_account(old_key, new_key):
    """プロジェクトの移動・改名で、アカウントの割り当てを新しいフォルダへ付け替える（配下も）。"""
    old_path = resolve_project_path(old_key, must_exist=False)
    new_path = resolve_project_path(new_key, must_exist=False)
    if not old_path or not new_path:
        return
    try:
        moved = accounts.rename_path(old_path, new_path, new_key)
        if moved:
            print(
                f"[Accounts] {old_key} -> {new_key}: 割り当て {moved} 件を付け替え・固定しました",
                flush=True,
            )
    except (accounts.AccountError, OSError) as e:
        print(
            f"[Accounts] 割り当てを付け替えられませんでした {old_key} -> {new_key}: {e}",
            flush=True,
        )


def increment_project_usage(project_key):
    """指定されたプロジェクトの使用回数をインクリメントして保存する。"""
    usage = load_project_usage()
    usage[project_key] = usage.get(project_key, 0) + 1
    save_project_usage(usage)


def get_sorted_projects():
    """developingとworkingディレクトリ下のプロジェクト一覧をスキャンし、使用頻度順にソートしたフラットなリストとして取得する"""
    projects = []
    usage = load_project_usage()
    # 各親プロジェクトが抱えるサブプロジェクトの件数（一覧のバッジ表示用）
    try:
        sub_counts = pm.count_sub_projects_by_parent()
    except Exception as e:
        print(f"Error counting sub projects: {e}")
        sub_counts = {}

    # developingの走査
    try:
        if os.path.exists(DEVELOPING_DIR):
            entries = os.listdir(DEVELOPING_DIR)
            for entry in entries:
                full_path = os.path.join(DEVELOPING_DIR, entry)
                if (
                    os.path.isdir(full_path)
                    and not entry.startswith((".", "0_"))
                    and entry not in ["test"]
                ):
                    key = f"developing/{entry}"
                    projects.append(
                        {
                            "value": key,
                            "name": entry,
                            "group": "developing",
                            "count": usage.get(key, 0),
                            "sub_count": sub_counts.get(key, 0),
                        }
                    )
    except Exception as e:
        print(f"Error listing developing projects: {e}")

    # workingの走査
    try:
        if os.path.exists(WORKING_DIR):
            entries = os.listdir(WORKING_DIR)
            for entry in entries:
                full_path = os.path.join(WORKING_DIR, entry)
                if (
                    os.path.isdir(full_path)
                    and not entry.startswith((".", "0_"))
                    and entry not in ["test"]
                ):
                    key = f"working/{entry}"
                    projects.append(
                        {
                            "value": key,
                            "name": entry,
                            "group": "working",
                            "count": usage.get(key, 0),
                            "sub_count": sub_counts.get(key, 0),
                        }
                    )
    except Exception as e:
        print(f"Error listing working projects: {e}")

    # ソート: 使用回数(count)の降順、プロジェクト名(name)の昇順、グループ(group)の昇順
    projects.sort(key=lambda x: (-x["count"], x["name"].lower(), x["group"]))
    return projects


def resolve_project_path(project_key, must_exist=True):
    """プロジェクトキーから作業ディレクトリの絶対パスを解決する。

    サブプロジェクト（``developing/親/projects/子``）のような多階層キーにも対応する。
    クライアントから渡された文字列をそのままパス結合すると、``..`` を含むキーで
    想定外のディレクトリを開けてしまうため、ここで厳密に検証する。

    Args:
        project_key: ``グループ名/フォルダ名[/projects/フォルダ名]`` 形式のキー
        must_exist: False なら実在を確かめない（移す前・改名前のパスを組み立てるとき）

    Returns:
        検証済みの絶対パス。キーが不正または実在しない場合は None。
    """
    if not project_key or "/" not in project_key:
        return None

    group, relative_path = project_key.split("/", 1)
    base_dir = {"developing": DEVELOPING_DIR, "working": WORKING_DIR}.get(group)
    if not base_dir:
        return None

    parts = relative_path.split("/")
    # 空要素・カレント/親ディレクトリ参照・ドライブ指定を含むキーは受け付けない
    for part in parts:
        if not part or part in (".", "..") or "\\" in part or ":" in part:
            return None

    target_path = os.path.normpath(os.path.join(base_dir, *parts))

    # 正規化後もベースディレクトリの配下に留まっていることを検証する
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(target_path)
    if os.path.commonpath([base_abs, target_abs]) != base_abs:
        return None

    if must_exist and not os.path.isdir(target_abs):
        return None
    return target_abs


# 機械の口が宛先の project_key を作業場のパスへ直すのに使う（会話の cwd と比べるだけ）
machine_send.project_path_resolver = lambda key: resolve_project_path(
    key, must_exist=False
)


def sync_sub_projects_of(parent_key, parent_dir, prune=True):
    """親の projects/ 配下を実フォルダと同期する。

    手で改名されたフォルダは記録が付け替わるので、起動回数の記録も新しいキーへ移す。
    """
    for old_key, new_key in pm.sync_sub_projects(parent_key, parent_dir, prune=prune):
        rename_project_usage(old_key, new_key)


def sync_all_sub_projects():
    """すべての置き場を実フォルダと同期する（横断検索の前に使う）。

    先に developing / working 直下の改名を付け替え、次に projects/ を持つ親を
    削除なしで同期し、最後に実体の消えた記録をまとめて消す。親ごとに消すと、
    別の親へ移ったフォルダや、改名された親の子が記録を引き継げないため。
    """
    for group_key, group_dir in (
        ("developing", DEVELOPING_DIR),
        ("working", WORKING_DIR),
    ):
        if not os.path.isdir(group_dir):
            continue
        try:
            for old_key, new_key in pm.sync_top_level(group_key, group_dir):
                rename_project_usage(old_key, new_key)
        except Exception as e:
            print(f"Error syncing top level projects of {group_key}: {e}")

    for project in get_sorted_projects():
        parent_dir = resolve_project_path(project["value"])
        if not parent_dir or not os.path.isdir(
            os.path.join(parent_dir, pm.PROJECTS_SUBDIR)
        ):
            continue
        try:
            sync_sub_projects_of(project["value"], parent_dir, prune=False)
        except Exception as e:
            # 1つの親の失敗で検索全体を止めない
            print(f"Error syncing sub projects of {project['value']}: {e}")

    try:
        pm.prune_vanished_sub_projects()
    except Exception as e:
        print(f"Error pruning vanished sub projects: {e}")


def get_project_root_key():
    """新規プロジェクトの作成先（親）となるプロジェクトキーを返す。

    どのセッションを開いていても作成先は常に一箇所へ集約する。開いている
    プロジェクトごとに新規プロジェクトが散らばると、後から探せなくなるため。
    作成先は config/settings.json の ``project_root`` で変更できる。
    """
    settings = load_settings()
    return settings.get("project_root", DEFAULT_SETTINGS["project_root"])


def resolve_location(location):
    """プロジェクトの「置き場」の指定を、実際のパスとキーへ解決する。

    本アプリでは、作った直後で試行錯誤中のものと、育って独立したもの、
    運用が始まったものを次の3か所へ置き分ける。

        temp        一時フォルダ（settings の project_root 配下の projects/）
        developing  開発中プロジェクトの置き場（DEVELOPING_DIR 直下）
        working     運用中プロジェクトの置き場（WORKING_DIR 直下）

    Args:
        location: 置き場の識別子（``temp`` / ``developing`` / ``working``）

    Returns:
        dict: ``dir``（絶対パス）、``key_prefix``（プロジェクトキーの前半分）、
        ``parent_key``（DBへ記録する所属）、``label``（画面表示用）。
        識別子が未知、または一時フォルダの親が実在しない場合は None。
    """
    if location == "temp":
        root_key = get_project_root_key()
        root_path = resolve_project_path(root_key)
        if not root_path:
            return None
        return {
            "id": "temp",
            "dir": os.path.join(root_path, pm.PROJECTS_SUBDIR),
            "key_prefix": f"{root_key}/{pm.PROJECTS_SUBDIR}",
            "parent_key": root_key,
            "label": f"{root_key}/{pm.PROJECTS_SUBDIR}/",
        }
    if location in ("developing", "working"):
        base_dir = DEVELOPING_DIR if location == "developing" else WORKING_DIR
        if not base_dir:
            # 置き場が未設定の機械では作成先・移動先にしない（相対パスで作業フォルダへ作らない）
            return None
        return {
            "id": location,
            "dir": base_dir,
            "key_prefix": location,
            "parent_key": location,
            "label": f"{location}/",
        }
    return None


def get_location_labels():
    """クライアントへ渡す置き場の一覧（識別子・表示名・作成可否）を作る。"""
    labels = []
    for location in PROJECT_LOCATIONS:
        info = resolve_location(location)
        if info:
            labels.append(
                {
                    "id": info["id"],
                    "label": info["label"],
                    "can_create": info["id"] in CREATE_LOCATIONS,
                }
            )
    return labels


def is_protected_path(target_path):
    """本アプリ自身を巻き込む移動かどうかを判定する。

    本アプリは自分自身のフォルダ（BASE_DIR）から動いており、その中には
    一時フォルダ（projects/）もある。自分を含むフォルダを移動すると
    稼働中のプロセスごと足元が消えるため、これは禁止する。

    Args:
        target_path: 移動しようとしているフォルダの絶対パス

    Returns:
        True なら移動してはいけない
    """
    target = os.path.abspath(target_path)
    base = os.path.abspath(BASE_DIR)
    try:
        return os.path.commonpath([target, base]) == target
    except ValueError:
        # ドライブが異なる場合は commonpath が例外になる（＝含まれていない）
        return False


def build_session_display_name(project_key):
    """PTY 上で起動する `claude` に渡すセッション表示名を、プロジェクトキーから作る。

    `claude -n <name>` で指定した名前は、claude のプロンプトボックス・`/resume` の
    セッション一覧・ターミナルタイトルに表示される。プロジェクトキーは
    ``developing/claude-web-app/projects/invoice-ocr-batch`` のように階層を含んで
    長くなるため、末尾のフォルダ名（＝自動命名で得たプロジェクト名）だけを使う。

    Args:
        project_key: プロジェクトキー

    Returns:
        表示名。``default`` や空の場合は空文字（この場合は名前を指定しない）
    """
    if not project_key or project_key == "default":
        return ""
    name = pm.to_single_line(project_key.strip("/").split("/")[-1])
    return name


@app.route("/")
def index():
    """メイン画面のレンダリング。プロジェクト一覧をソート済みのフラットなリストとしてHTMLテンプレートへ渡す。"""
    projects = get_sorted_projects()
    return render_template(
        "index.html",
        projects=projects,
        launch_choices=sm.launch_option_choices(),
    )


def _notify_broadcast_done(job):
    """ブロードキャスト完了時に、接続中の全クライアントへ結果を配り、送った履歴に残す。"""
    try:
        job = job or {}
        results = job.get("results") or {}
        # 配送係が終わっても、宛先ごとに FAILED・UNKNOWN があれば成功とは書かない
        all_sent = bool(results) and all(
            str(v).startswith("SENT") for v in results.values()
        )
        send_history.record(
            "broadcast",
            ok=job.get("status") == "done" and all_sent,
            target=list(job.get("targets") or []),
            text=job.get("message") or "",
            source="broadcast",
            error=job.get("error"),
            detail={"job": job.get("id"), "stage": "finished", "results": results},
        )
    except Exception as e:
        print(f"Error recording broadcast history: {e}")
    try:
        socketio.emit("broadcast_result", job)
    except Exception as e:
        print(f"Error emitting broadcast result: {e}")


# --- ログイン更新（relogin_manager） ------------------------------------------
# 認証コードは、受け取ったら打ち込んで捨てる。ログ・応答・例外の文面に入れない
# （Flask のアクセスログはボディを書かない。ここの print にもコードを渡さない）。


def _relogin_call(label, action):
    """ログイン更新の操作を呼び、(HTTP 状態コード, 応答) を返す。"""
    try:
        return 200, {"ok": True, **action()}
    except rl.ReloginError as e:
        print(f"[Relogin] {label} failed: {e}", flush=True)
        return 400, {
            "ok": False,
            "error": str(e),
            "screen": e.screen,
            "status": rl.status(),
        }
    except Exception as e:
        # 想定外の例外の文面は、念のため型の名前だけにする（引数を含みうるため）
        print(f"[Relogin] {label} error: {type(e).__name__}", flush=True)
        return 500, {"ok": False, "error": f"想定外のエラー: {type(e).__name__}"}


@app.route("/history")
def history_page():
    """本アプリから送ったもの（起動・/clear・本文・一斉指示・区切りの予約）の履歴の画面。"""
    return render_template("history.html", kind_labels=send_history.KIND_LABELS)


@app.route("/api/history")
def api_history():
    """送った履歴を新しい順に返す。
    ``?limit=200&kind=<種類>&q=<宛先・本文の部分一致>&result=failed``（failed で失敗だけ）

    応答: 200 ``{"ok": true, "items": [...], "kinds": {識別子: 表示名}, "limit": 件数の上限}``
    """
    kind = request.args.get("kind") or None
    if kind and kind not in send_history.KIND_LABELS:
        return jsonify({"ok": False, "error": f"未知の種類です: {kind}"}), 400
    try:
        limit = max(
            1, min(int(request.args.get("limit") or 200), send_history.MAX_LIST)
        )
    except ValueError:
        return jsonify({"ok": False, "error": "limit は数で指定してください"}), 400
    items = send_history.list_entries(
        limit=limit,
        kind=kind,
        query=request.args.get("q"),
        failed_only=request.args.get("result") == "failed",
    )
    return jsonify(
        {
            "ok": True,
            "items": items,
            "kinds": send_history.KIND_LABELS,
            "limit": limit,
        }
    )


@app.route("/relogin")
def relogin_page():
    """この機械の Claude のログインを更新する画面（設定 relogin_enabled で切れる）。"""
    return render_template("relogin.html", machine_name=machine_name())


@app.route("/api/relogin")
def api_relogin_status():
    """資格情報の期限（時刻だけ）と、開いているログイン手続きの状態。"""
    return jsonify({"ok": True, **rl.status()})


def _relogin_account():
    """ボディの account（ログインし直すアカウントの名前。省略は本家）。文字列でなければ None。"""
    payload = request.get_json(silent=True)
    value = payload.get("account") if isinstance(payload, dict) else None
    return value if isinstance(value, str) and value else None


@app.route("/api/relogin/start", methods=["POST"])
def api_relogin_start():
    """ログイン用の claude を起こして /login を打ち、OAuth の URL を返す。
    ボディ: ``{"account": "<アカウントの名前>"}``（省略は本家）"""
    account = _relogin_account()
    status, body = _relogin_call(
        "start", lambda: rl.start(load_settings().get("claude_exe"), account)
    )
    if status == 200:
        print(
            f"[Relogin] started url={'yes' if body.get('url') else 'no'}",
            flush=True,
        )
    return jsonify(body), status


@app.route("/api/relogin/code", methods=["POST"])
def api_relogin_code():
    """人が貼った認証コードを打って確定する。ボディ: ``{"code": "..."}``（ログには残さない）。"""
    payload = request.get_json(silent=True)
    code = payload.get("code") if isinstance(payload, dict) else None
    status, body = _relogin_call("code", lambda: rl.submit_code(code))
    del code, payload
    if status == 200:
        print(f"[Relogin] code submitted (ok={body.get('ok')})", flush=True)
    return jsonify(body), status


@app.route("/api/relogin/check", methods=["POST"])
def api_relogin_check():
    """資格情報の期限と、別の claude -p が1回応答するかを確かめる。
    ボディ: ``{"account": "<アカウントの名前>"}``（省略は本家）"""
    account = _relogin_account()
    status, body = _relogin_call(
        "check", lambda: rl.check(load_settings().get("claude_exe"), account)
    )
    print(f"[Relogin] check ok={body.get('ok')}", flush=True)
    return jsonify(body), status


@app.route("/api/relogin/cancel", methods=["POST"])
def api_relogin_cancel():
    """ログイン用の claude を閉じる。"""
    return jsonify({"ok": True, "closed": rl.cancel()})


@app.route("/api/broadcast", methods=["POST"])
def api_broadcast():
    """全稼働セッションへの一斉指示（ブロードキャスト）を開始する。

    ボディ: ``{"message": "...", "exclude": ["名前", ...], "preset": "reboot"}``
    ``message`` があればそれを使い、無ければ ``preset`` の定型文を使う。

    実行には宛先数×数秒＋起動十数秒かかるため、配送はバックグラウンドで行い、
    この応答は即座に返す。進行状況は ``GET /api/broadcast/status`` と
    Socket.IO の ``broadcast_result`` イベントで確認できる。
    """
    data = request.get_json(silent=True) or {}

    message = (data.get("message") or "").strip()
    preset = data.get("preset")
    if not message and preset:
        message = broadcaster.preset_message(preset, machine_name())
        if not message:
            return jsonify({"ok": False, "error": f"未知の定型文です: {preset}"}), 400
    if not message:
        return (
            jsonify({"ok": False, "error": "message か preset を指定してください"}),
            400,
        )
    if machine_send.forged(message):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": machine_send.FORGED_ERROR,
                    "code": "forged_heading",
                }
            ),
            400,
        )

    exclude = data.get("exclude") or []
    if not isinstance(exclude, list):
        return (
            jsonify({"ok": False, "error": "exclude は名前のリストで指定してください"}),
            400,
        )

    settings = load_settings()

    def refused(status, error, code):
        # 開始の時点で断られた一斉指示も、送った履歴に残す（個別の送信と揃える）
        send_history.record(
            "broadcast",
            ok=False,
            target=[],
            text=message,
            source=_request_source(),
            error=error,
            code=code,
            detail={"stage": "refused", "exclude": exclude},
        )
        return jsonify({"ok": False, "error": error}), status

    try:
        job = broadcaster.start_broadcast(
            message,
            exclude=exclude,
            claude_exe=settings.get("claude_exe"),
            on_complete=_notify_broadcast_done,
        )
    except broadcaster.BroadcastBusyError as e:
        return refused(409, str(e), "busy")
    except broadcaster.BroadcastError as e:
        return refused(400, str(e), "failed")
    except Exception as e:
        print(f"Error starting broadcast: {e}")
        return refused(500, f"一斉指示を開始できませんでした: {e}", "error")

    print(f"[Broadcast] Started job={job['id']} targets={job['targets']}")
    # 結果は配り終えたとき（_notify_broadcast_done）にもう1件残す
    send_history.record(
        "broadcast",
        ok=None,
        target=list(job["targets"]),
        text=message,
        source=_request_source(),
        detail={"job": job["id"], "stage": "started"},
    )
    return jsonify({"ok": True, "targets": job["targets"], "job": job["id"]})


@app.route("/api/broadcast/status")
def api_broadcast_status():
    """最後に実行した（または実行中の）一斉指示の状態を返す。"""
    job = broadcaster.get_status()
    return jsonify({"ok": True, "job": job})


def _history_target_for_pid(pid):
    """送る前の宛先を履歴用に引く（送った後は /clear で会話の番号が変わるため先に引く）。"""
    try:
        session = sm.find_session_by_pid(pid)
    except Exception:
        session = None
    if session:
        return send_history.target_of(session)
    return {"pid": pid}


def send_to_session_request(pid, payload, source="api"):
    """/clear と本文の送信要求を処理し、(HTTP 状態コード, 応答) を返す。

    REST と Socket.IO の両方から使う。検証と送信の本体は sm.send_to_session。
    既定で送れる相手は本アプリが起こした子だけ。allow_unmanaged: true を明示した
    ときだけ台帳に無い相手にも送る。中身は /clear と本文だけ。
    送った・断った・失敗したものは送った履歴（send_history）に残す。
    """
    status, body = _send_to_session(pid, payload)
    # ボディの形の不備で送る前に断ったもの（宛先を引く前）は、送った履歴に残さない
    target = body.pop("_history_target", None)
    if target is not None:
        payload = payload if isinstance(payload, dict) else {}
        clear = payload.get("clear") is True
        text = payload.get("text") or ""
        result = body.get("result") or {}
        send_history.record(
            send_history.send_kind(clear, text),
            ok=bool(body.get("ok")),
            target=target,
            text=text,
            source=source,
            error=None if body.get("ok") else body.get("error"),
            code=None if body.get("ok") else body.get("code"),
            detail=(
                {
                    "session_id_after": result.get("session_id_after"),
                    "allow_unmanaged": payload.get("allow_unmanaged") is True,
                    "via": result.get("via"),
                    "inbox_fallback": result.get("inbox_fallback"),
                }
                if body.get("ok")
                else {
                    "allow_unmanaged": payload.get("allow_unmanaged") is True,
                    "via": body.get("via"),
                    "cleared": body.get("cleared"),
                }
            ),
        )
    return status, body


def _send_to_session(pid, payload):
    """send_to_session_request の本体（検証と送信）。"""
    payload = payload or {}
    if not isinstance(payload, dict):
        return 400, {
            "ok": False,
            "error": "ボディは JSON のオブジェクトにしてください",
            "code": "failed",
        }
    for key in ("text", "preamble"):
        if payload.get(key) is not None and not isinstance(payload.get(key), str):
            return 400, {
                "ok": False,
                "error": f"{key} は文字列で指定してください",
                "code": "failed",
            }
    # 長い本文は前置きの直後に貼り付けられ1つの発言になるので、つなげた形でも見る
    joined = (payload.get("preamble") or "") + (payload.get("text") or "")
    for value in (payload.get("text"), payload.get("preamble"), joined):
        if machine_send.forged(value):
            # 機械の口の見出しは本アプリが送り手を確かめたときだけ付ける（ほかの口から偽らせない）
            return 400, {
                "ok": False,
                "error": machine_send.FORGED_ERROR,
                "code": "forged_heading",
            }
    target = _history_target_for_pid(pid)
    status, body = _send_checked(pid, payload)
    body["_history_target"] = target
    return status, body


def _send_checked(pid, payload):
    """形を確かめたボディで送り、(HTTP 状態コード, 応答) を返す。"""
    try:
        result = sm.send_to_session(
            pid,
            # "false" のような文字列を真として扱わないよう、JSON の true だけを受け付ける
            clear=payload.get("clear") is True,
            text=payload.get("text"),
            wait_idle_sec=payload.get("wait_idle_sec") or 0,
            # 省略時（キーが無い）は既定の前置き。空文字なら前置きを付けない
            # 省略（None）なら既定文（設定 paste_preamble・無ければ sm.DEFAULT_PASTE_PREAMBLE）
            preamble=payload.get("preamble"),
            # 台帳に無い相手へは、相手を明示して確かめたときだけ送る（真偽値の true だけ受け付ける）
            allow_unmanaged=payload.get("allow_unmanaged") is True,
            discard_draft=payload.get("discard_draft") is True,
        )
    except sm.SessionNotManagedError as e:
        # 標準出力はパイプでバッファされ、ログに後からまとめて出る（2026-10-02 実測）。
        # 送信の記録はその場で読めるよう flush する
        safe_console.say(
            f"[Session] Send refused pid={pid} (not_managed): {e}", flush=True
        )
        return 403, {"ok": False, "error": str(e), "code": "not_managed"}
    except sm.SessionBusyError as e:
        safe_console.say(
            f"[Session] Send refused pid={pid} (busy, via={getattr(e, 'via', None)}): {e}",
            flush=True,
        )
        return 409, _send_error(e, "busy")
    except sm.SessionDraftError as e:
        safe_console.say(
            f"[Session] Send refused pid={pid} (draft_present, via={getattr(e, 'via', None)}): {e}",
            flush=True,
        )
        return 409, _send_error(e, "draft_present")
    except sm.SessionError as e:
        # 応答だけでは途中で切れて読めないことがあるため、全文をログにも残す
        safe_console.say(
            f"[Session] Send failed pid={pid} (via={getattr(e, 'via', None)}): {e}",
            flush=True,
        )
        return 400, _send_error(e, "failed")
    except Exception as e:
        safe_console.say(f"Error sending to session pid={pid}: {e}", flush=True)
        return 500, {"ok": False, "error": f"送信に失敗しました: {e}", "code": "error"}

    # 送れた後の出力の失敗で 500 にすると「送れなかった」と誤って伝わる（2026-10-22）
    safe_console.say(
        f"[Session] Sent to pid={pid} (clear={result['cleared']}, "
        f"text={result['text_sent']}, {result['session_id_before']} -> "
        f"{result['session_id_after']}, {result['elapsed_sec']}s, via={result.get('via')}"
        + (
            f", inbox_fallback={result['inbox_fallback']}"
            if result.get("inbox_fallback")
            else ""
        )
        + ")",
        flush=True,
    )
    return 200, {"ok": True, "result": result}


def _send_error(e, code):
    """送信の失敗の応答。/clear が済んだか（cleared）と口（via）も返す。

    画面は cleared を見て「/clear」のチェックを外す（送り直しで二度 /clear しないため）。
    """
    return {
        "ok": False,
        "error": str(e),
        "code": code,
        "cleared": bool(getattr(e, "cleared", False)),
        "session_id_after": getattr(e, "session_id_after", None),
        "via": getattr(e, "via", None),
    }


@app.route("/api/sessions/<int:pid>/send", methods=["POST"])
def api_send_to_session(pid):
    """稼働中のセッションへ /clear と本文を送る（プロセスは止めない）。

    ボディ: ``{"clear": true, "text": "次の指示", "wait_idle_sec": 0, "preamble": "…",
    "allow_unmanaged": false, "discard_draft": false}``
    （preamble は本文が貼り付け扱いになる長さのときだけ先に素で打つ前置き。省略で既定文、
    空文字で付けない。allow_unmanaged: true で台帳に無い相手にも送る。その相手の入力欄に
    文字があれば 409 draft_present で止まり、discard_draft: true のときだけ消して送る）
    応答: 200 ``{"ok": true, "result": {...}}`` / 400 入力不備・送信失敗 /
    403 台帳に無い相手（allow_unmanaged なし）/ 409 作業中・別の送信が進行中・入力欄に文字
    """
    status, body = send_to_session_request(
        pid, request.get_json(silent=True), source=_request_source()
    )
    return jsonify(body), status


def _request_source():
    """送った履歴の「どこから」。本アプリの画面は目印のヘッダーを付けて呼ぶ。"""
    return "screen" if request.headers.get(SCREEN_HEADER) else "api"


@app.route("/api/sessions/request-clear", methods=["POST"])
def api_request_self_clear():
    """セッションが自分の区切り（/clear と引き継ぎの読み直し）を予約する。

    呼んだのが誰かは申告ではなく接続から決める（127.0.0.1 からの接続の持ち主の祖先の
    claude セッションで、本体の会話記録にこの道具を呼ぶ実行中の tool_use があること）。
    そのセッションがターンを終えて入力待ちが続いたら、サーバ側から /clear と
    「…引き継ぎファイル「<handoff>」を読んで、続きから進めてください。」を送る。
    ボディ: ``{"handoff": "<引き継ぎファイルの絶対パス>", "wait_sec": 1800}``
    （handoff を省くと会話の cwd 直下の NOW.md、無いか空なら README.md）
    応答: 202 ``{"ok": true, "result": {pid, create_time, session_id, name, cwd, managed,
    handoff, handoff_source（explicit / now / readme）, requested_at, deadline}}`` / 400 failed（ボディ・handoff・wait_sec の不備）/
    403 not_self（セッションの中からの呼び出しでない）・not_main（サブエージェント等から）/
    409 already_reserved（同じセッションに予約が既にある）/ 500 error
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "ボディは JSON のオブジェクトにしてください",
                    "code": "failed",
                }
            ),
            400,
        )
    return _self_clear_call(
        "reserve",
        lambda caller: self_clear.request_self_clear(
            caller, payload.get("handoff"), payload.get("wait_sec")
        ),
        202,
    )


@app.route("/api/sessions/request-clear", methods=["DELETE"])
def api_cancel_self_clear():
    """呼んだセッション自身の区切りの予約を取り消す（本人の確かめは予約と同じ）。

    応答: 200 ``{"ok": true, "result": {pid, cancelled, sending}}``（予約が無ければ cancelled: false。
    既に送り始めていれば取り消さず sending: true）/
    403 not_self・not_main / 500 error
    """
    return _self_clear_call("cancel", self_clear.cancel_self_clear, 200)


def _self_clear_call(label, action, ok_status):
    """接続の持ち主を引いて self_clear の処理を呼び、応答を作る（予約・取り消しで共通）。"""
    caller = self_clear.caller_pid(
        request.environ.get("REMOTE_ADDR"),
        request.environ.get("REMOTE_PORT"),
        request.environ.get("SERVER_PORT"),
    )
    try:
        result = action(caller)
    except self_clear.SelfClearError as e:
        safe_console.say(
            f"[SelfClear] {label} refused caller={caller} ({e.code}): {e}", flush=True
        )
        return jsonify({"ok": False, "error": str(e), "code": e.code}), e.status
    except Exception as e:
        safe_console.say(
            f"Error in self clear {label} caller={caller}: {e}", flush=True
        )
        return (
            jsonify(
                {"ok": False, "error": f"処理に失敗しました: {e}", "code": "error"}
            ),
            500,
        )
    return jsonify({"ok": True, "result": result}), ok_status


@app.route("/api/sessions/request-clear")
def api_self_clear_status():
    """区切りの予約のうち待ち中のものと、直近の結果を返す。"""
    return jsonify({"ok": True, **self_clear.status()})


def _machine_error(e):
    return (
        jsonify({"ok": False, "error": str(e), "code": e.code, **e.extra}),
        e.status,
    )


def _machine_sender():
    """合言葉（Authorization: Bearer）から送り手の役名を決める。

    Returns:
        (役名, None) か、断るとき (None, 応答)
    """
    try:
        senders = machine_send.senders_of(load_settings())
    except machine_send.MachineSendError as e:
        # 合言葉を確かめる前なので、役名を含む詳しい文は記録にだけ出す
        safe_console.say(f"[MachineSend] 設定が読めません: {e}", flush=True)
        return None, _machine_error(
            machine_send.MachineSendError(
                "機械の口の設定（machine_send.senders）が読めません。"
                "この機械の server.log に理由を書きました",
                e.code,
                e.status,
            )
        )
    if guard.token and guard.token in senders.values():
        # 画面の合言葉を知る人が送り手を名乗れてしまう
        safe_console.say(
            "[MachineSend] 設定が読めません: machine_send.senders に画面の合言葉（auth_token）と同じ合言葉があります",
            flush=True,
        )
        return None, _machine_error(
            machine_send.MachineSendError(
                "機械の口の設定（machine_send.senders）が読めません。"
                "この機械の server.log に理由を書きました",
                "config",
                503,
            )
        )
    if not senders:
        return None, _machine_error(
            machine_send.MachineSendError(
                "機械の口は使えません（設定の machine_send.senders に送り手がありません）",
                "disabled",
                503,
            )
        )
    sender = machine_send.authenticate(senders, guard.presented_token(request.headers))
    if not sender:
        safe_console.say(
            f"[MachineSend] refused (token) addr={request.remote_addr} path={request.path}",
            flush=True,
        )
        return None, _machine_error(
            machine_send.MachineSendError(
                "送り手の合言葉が要ります（Authorization: Bearer <合言葉>）",
                "unauthorized",
                401,
            )
        )
    return sender, None


@app.route(machine_send.PATH, methods=["POST"])
def api_machine_send():
    """機械（管理側の役など）から子のセッションへ伝言を入れる。

    本文の頭に「【機械からの伝言・id=…・from=…・ユーザーの発言ではありません】」を付け、
    子が入力待ちになったところで入れる（作業中・書きかけ・入力欄が読めないときは預かる）。
    ボディ: ``{"from": "役名", "to": {"pid": 123} | {"project_key": "…"} | {"name": "…"},
    "id": "送り手が付ける id", "text": "本文", "ttl_sec": 3600}``
    応答: 202 受けた（``message.status`` は pending）/ 200 同じ id を受け付け済み（duplicate）/
    400 入力不備 / 401 合言葉 / 403 from が合言葉の役名と違う / 404 宛先が無い /
    409 宛先が曖昧（candidates）・同じ id で別の本文 / 429 預かりが上限 / 503 口が使えない
    """
    sender, refused = _machine_sender()
    if refused:
        return refused
    try:
        status, body = machine_send.submit(sender, request.get_json(silent=True))
    except machine_send.MachineSendError as e:
        safe_console.say(
            f"[MachineSend] refused from={sender} ({e.code}): {e}", flush=True
        )
        return _machine_error(e)
    except Exception as e:
        safe_console.say(f"Error in machine send from={sender}: {e}", flush=True)
        return (
            jsonify(
                {"ok": False, "error": f"受け付けに失敗しました: {e}", "code": "error"}
            ),
            500,
        )
    return jsonify(body), status


@app.route(machine_send.PATH + "/<msg_id>")
def api_machine_send_status(msg_id):
    """送り手自身の伝言1件の状態（受けた・入れた時刻と結果）。"""
    sender, refused = _machine_sender()
    if refused:
        return refused
    message = machine_send.get(sender, msg_id)
    if not message:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"id {msg_id} の伝言はありません（自分が送ったものだけ引けます）",
                    "code": "not_found",
                }
            ),
            404,
        )
    return jsonify({"ok": True, "message": message})


@app.route(machine_send.PATH)
def api_machine_send_list():
    """送り手自身の伝言の一覧（新しい順・?limit=50）。"""
    sender, refused = _machine_sender()
    if refused:
        return refused
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    return jsonify({"ok": True, "messages": machine_send.list_for(sender, limit)})


@app.route("/api/sessions/<int:pid>/adopt", methods=["POST"])
def api_adopt_session(pid):
    """台帳に無い稼働中の子を、本アプリの子として台帳に取り込む（以後 send できる）。

    台帳の機能より前に起こした子の救済用。親プロセスが生きている相手（ターミナルから
    起こされたもの）は 400 で断る。
    応答: 200 ``{"ok": true, "result": {pid, name, cwd, adopted}}`` / 400 取り込めない
    """
    try:
        result = sm.adopt_session(pid)
    except sm.SessionError as e:
        return jsonify({"ok": False, "error": str(e), "code": "failed"}), 400
    except Exception as e:
        print(f"Error adopting session pid={pid}: {e}", flush=True)
        return (
            jsonify(
                {"ok": False, "error": f"取り込みに失敗しました: {e}", "code": "error"}
            ),
            500,
        )
    print(
        f"[Session] Adopted pid={pid} ({result['name']}, adopted={result['adopted']})",
        flush=True,
    )
    return jsonify({"ok": True, "result": result}), 200


@app.route("/api/auto-clear")
def api_auto_clear_status():
    """自動の区切りの状態（設定・今回の当たり・予約待ち・残す印・直近の結果）。"""
    return jsonify({"ok": True, **auto.status()})


@app.route("/api/sessions/<int:pid>/tail")
def api_session_tail(pid):
    """会話の末尾（人の発言と応答の文だけ・道具は名前だけ）を返す。会話記録を読むだけ（Claude は呼ばない）。

    クエリ: limit（既定 20・上限 100）。応答: 200 ``{"ok", "pid", "name", "session_id", "items"}``
    / 404 稼働中でない・会話記録が無い
    """
    session = sm.find_session_by_pid(pid)
    if not session or sm.is_relogin_session(session):
        return (
            jsonify(
                {"ok": False, "error": f"稼働中のセッションが見つかりません: pid={pid}"}
            ),
            404,
        )
    path = transcript_path_for(session.get("cwd"), session.get("session_id"))
    try:
        limit = int(request.args.get("limit") or transcript_tail.DEFAULT_LIMIT)
    except ValueError:
        return jsonify({"ok": False, "error": "limit は数で指定してください"}), 400
    try:
        # 起こしたばかりで会話記録がまだ無い子はよくある場面なので、失敗ではなく空で返す
        items = transcript_tail.read_tail(path, limit) if path else []
    except FileNotFoundError:
        items = []
    except OSError as e:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"会話記録を読めませんでした: {e.strerror or type(e).__name__}",
                }
            ),
            500,
        )
    return jsonify(
        {
            "ok": True,
            "pid": session.get("pid"),
            "name": session.get("name") or "",
            "session_id": session.get("session_id"),
            "items": items,
        }
    )


@app.route("/api/quota-resume")
def api_quota_resume_status():
    """枠が戻った子への続きの依頼の状態（設定・見た枠切れ・直近の結果）と、管理役への知らせの溜まり・週の枠の ntfy の知らせ。"""
    return jsonify(
        {
            "ok": True,
            **quota.status(),
            "liaison": notices.status(),
            "weekly_alert": weekly_alert.status(),
        }
    )


def keep_request(pid, kept):
    """「残す」印を付け外しし、(HTTP 状態コード, 応答) を返す。"""
    session = sm.find_session_by_pid(pid)
    if not session:
        return 404, {
            "ok": False,
            "error": f"稼働中のセッションが見つかりません: pid={pid}",
        }
    try:
        changed = auto.keep_marks.set(session, kept)
    except OSError as e:
        return 500, {"ok": False, "error": f"印を書けませんでした: {e}"}
    print(f"[AutoClear] keep pid={pid} {session.get('name')} -> {kept}", flush=True)
    return 200, {
        "ok": True,
        "pid": session.get("pid"),
        "kept": kept,
        "changed": changed,
    }


@app.route("/api/sessions/<int:pid>/keep", methods=["PUT", "DELETE"])
def api_keep_session(pid):
    """「残す」印を付ける（PUT）・外す（DELETE）。印の付いたセッションは自動で区切らない。

    印はプロセスごと（/clear で会話が変わっても残る。プロセスが終われば消える）。
    応答: 200 ``{"ok": true, "pid", "kept", "changed"}`` / 404 稼働中でない
    """
    status, body = keep_request(pid, request.method == "PUT")
    if status == 200:
        broadcast_sessions()
    return jsonify(body), status


@socketio.on("set_keep")
def handle_set_keep(message):
    """画面の「残す」ボタン。``{"pid", "kept": bool}``"""
    message = message if isinstance(message, dict) else {}
    status, body = keep_request(message.get("pid"), message.get("kept") is True)
    emit("keep_result", body, room=request.sid)
    if status == 200:
        broadcast_sessions()


@app.route("/api/sessions/unmanaged")
def api_unmanaged_sessions():
    """手で開いたセッション（台帳に無いもの）の一覧と、起こし直せるかの見立て。"""
    try:
        items = auto.unmanaged(annotated_sessions())
    except Exception as e:
        print(f"Error listing unmanaged sessions: {e}", flush=True)
        return jsonify({"ok": False, "error": f"一覧を作れませんでした: {e}"}), 500
    return jsonify({"ok": True, "sessions": items})


@app.route("/api/sessions/<int:pid>/relaunch", methods=["POST"])
def api_relaunch_session(pid):
    """手で開いたセッションを止め、同じ会話を --resume で管理下に起こし直す（会話ごとに一度だけ）。

    入力待ちが5分以上続き、許可待ち・裏の仕事・残す印・区切りの予約が無いものに限る。
    応答: 200 ``{"ok": true, "result": {old_pid, pid, session_id, options, ...}, "session": {...}}`` /
    400 起こし直せない（理由つき）/ 404 稼働中でない / 502 止めたが起こし直せなかった・登録されない
    """
    session = next(
        (s for s in annotated_sessions() if str(s.get("pid")) == str(pid)), None
    )
    if not session:
        return (
            jsonify(
                {"ok": False, "error": f"稼働中のセッションが見つかりません: pid={pid}"}
            ),
            404,
        )
    target = send_history.target_of(session, managed=False)
    try:
        result = auto.relaunch(session, claude_exe=load_settings().get("claude_exe"))
    except auto_clear.RelaunchError as e:
        # 止める前に断っただけ（何もしていない）なら、送った履歴には残さない
        if e.stopped:
            send_history.record(
                "resume",
                ok=False,
                target=target,
                source="relaunch",
                error=str(e),
                code="relaunch",
            )
            broadcast_sessions()
        return jsonify({"ok": False, "error": str(e)}), (502 if e.stopped else 400)
    registered = wait_for_session(result["pid"])
    send_history.record(
        "resume",
        ok=bool(registered),
        target=dict(target, pid=result["pid"], managed=True),
        source="relaunch",
        error=None if registered else "起こし直したが claude 側に登録されなかった",
        detail={"old_pid": pid, "options": result.get("options")},
    )
    broadcast_sessions()
    if not registered:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "起こし直しましたが、claude 側に登録されませんでした",
                    "result": result,
                }
            ),
            502,
        )
    return jsonify({"ok": True, "result": result, "session": registered})


@socketio.on("send_to_session")
def handle_send_to_session(message):
    """Socket.IO 版の送信。``{"pid", "clear", "text", "wait_idle_sec", "preamble",
    "allow_unmanaged", "discard_draft"}`` を受け取る（意味は REST と同じ）。"""
    message = message if isinstance(message, dict) else {}
    status, body = send_to_session_request(message.get("pid"), message, source="socket")
    body["status"] = status
    emit("session_send_result", body, room=request.sid)


@socketio.on("connect")
def handle_connect():
    """接続時に、初期表示に必要な一式（セッション一覧・プロジェクト一覧）を送る。"""
    global connected_clients
    # Socket.IO の道は before_request を通らないので、ここで同じ確かめをする
    if host_refused(ALLOWED_HOSTS, request.host):
        safe_console.say(
            f"[WebSocket] refused (host) addr={request.remote_addr} host={request.host}",
            flush=True,
        )
        return False
    # 機械の口の接続元（sources）には画面の操作を開けない（before_request の machine_only と同じ）
    if machine_send.from_source(MACHINE_SOURCES, request.remote_addr):
        safe_console.say(
            f"[WebSocket] refused (machine only) addr={request.remote_addr}",
            flush=True,
        )
        return False
    if guard.active and not guard.authorized(
        request.remote_addr, request.headers, request.cookies
    ):
        safe_console.say(
            f"[WebSocket] refused (access) addr={request.remote_addr}", flush=True
        )
        return False
    sid = request.sid
    print(f"[WebSocket] Client connected: {sid}")

    with clients_lock:
        connected_clients += 1

    # 最初の指示の定型文（旧スニペット機能を、初期指示のテンプレートとして流用）
    try:
        emit("snippets_list", {"snippets": load_snippets()}, room=sid)
    except Exception as e:
        print(f"Error sending snippets: {e}")

    # プロジェクトブラウザ（第1階層）用に親プロジェクト一覧を送信
    try:
        emit(
            "parent_projects_list",
            {
                "projects": get_sorted_projects(),
                "project_root": get_project_root_key(),
                "locations": get_location_labels(),
            },
            room=sid,
        )
    except Exception as e:
        print(f"Error sending parent projects: {e}")

    # 稼働中のセッション一覧
    emit("sessions_list", build_sessions_payload(), room=sid)


@socketio.on("disconnect")
def handle_disconnect():
    """切断時の後片付け。接続数が 0 になれば定期配信は止まる。"""
    global connected_clients
    print(f"[WebSocket] Client disconnected: {request.sid}")
    with clients_lock:
        connected_clients = max(0, connected_clients - 1)


@socketio.on("add_snippet")
def handle_add_snippet(message):
    """新しいスニペットを追加する"""
    label = message.get("label")
    command = message.get("command")
    if label and command:
        try:
            snippets = load_snippets()
            if not any(s["label"] == label for s in snippets):
                snippets.append({"label": label, "command": command})
                save_snippets(snippets)
                socketio.emit("snippets_list", {"snippets": snippets})
        except Exception as e:
            print(f"Error adding snippet: {e}")


@socketio.on("delete_snippet")
def handle_delete_snippet(message):
    """指定されたスニペットを削除する"""
    label = message.get("label")
    if label:
        try:
            snippets = load_snippets()
            snippets = [s for s in snippets if s["label"] != label]
            save_snippets(snippets)
            socketio.emit("snippets_list", {"snippets": snippets})
        except Exception as e:
            print(f"Error deleting snippet: {e}")


@socketio.on("list_parent_projects")
def handle_list_parent_projects():
    """親プロジェクト（第1階層）の一覧をクライアントへ送信する。"""
    sid = request.sid
    try:
        emit(
            "parent_projects_list",
            {
                "projects": get_sorted_projects(),
                "project_root": get_project_root_key(),
                "locations": get_location_labels(),
            },
            room=sid,
        )
    except Exception as e:
        print(f"Error listing parent projects: {e}")


@socketio.on("list_sub_projects")
def handle_list_sub_projects(message):
    """指定した親プロジェクト配下のサブプロジェクト（第2階層）一覧を送信する。

    検索文字列とソート順を受け取り、作成日順・更新日順・名前順での並べ替えと
    内容検索に対応する。
    """
    sid = request.sid
    parent_key = message.get("parent")
    query = (message.get("query") or "").strip()
    sort = message.get("sort") or "updated"

    parent_path = resolve_project_path(parent_key)
    if not parent_path:
        emit(
            "sub_projects_list",
            {
                "parent": parent_key,
                "projects": [],
                "error": f"親プロジェクトが見つかりません: {parent_key}",
            },
            room=sid,
        )
        return

    try:
        sync_sub_projects_of(parent_key, parent_path)
        sub_projects = pm.list_sub_projects(parent_key, query=query, sort=sort)
        emit(
            "sub_projects_list",
            {
                "parent": parent_key,
                "projects": sub_projects,
                "query": query,
                "sort": sort,
            },
            room=sid,
        )
    except Exception as e:
        print(f"Error listing sub projects for {parent_key}: {e}")
        emit(
            "sub_projects_list",
            {
                "parent": parent_key,
                "projects": [],
                "error": f"一覧の取得に失敗しました: {e}",
            },
            room=sid,
        )


@socketio.on("search_projects")
def handle_search_projects(message):
    """作成済みプロジェクトを、親を問わず横断して検索する。

    プロジェクト名だけでなく、作成時に書いた指示内容そのものを対象とするため、
    「あのとき何を作ろうとしたか」からプロジェクトを探し出せる。
    """
    sid = request.sid
    message = message or {}
    query = (message.get("query") or "").strip()

    if not query:
        emit("projects_search_result", {"query": "", "projects": []}, room=sid)
        return

    try:
        # 検索は DB だけを引くため、先に実フォルダと突き合わせる
        # （手で改名・削除したフォルダが古い名前のまま出続けないように）
        sync_all_sub_projects()
        # 実体の無い行（消えた直下のプロジェクト）を外してから50件に切る
        results = [
            row
            for row in pm.list_sub_projects(query=query, sort="updated", limit=200)
            if os.path.isdir(row.get("abs_path") or "")
        ][:50]
        emit("projects_search_result", {"query": query, "projects": results}, room=sid)
    except Exception as e:
        print(f"Error searching projects: {e}")
        emit(
            "projects_search_result",
            {"query": query, "projects": [], "error": f"検索に失敗しました: {e}"},
            room=sid,
        )


@socketio.on("suggest_project_name")
def handle_suggest_project_name(message):
    """利用者が書いた最初の指示文からプロジェクト名を自動生成して返す。

    ローカルの ollama に指示文を渡して kebab-case のプロジェクト名を生成する。
    生成に失敗した場合も日時ベースの名前を返し、エラー内容を併せて通知する。
    """
    sid = request.sid
    message = message or {}
    text = (message.get("text") or "").strip()

    if not text:
        emit(
            "project_name_suggested",
            {
                "name": "",
                "parent": get_project_root_key(),
                "error": "やりたいことを入力してください",
            },
            room=sid,
        )
        return

    settings = load_settings()
    if settings.get("auto_naming_enabled", True) is False:
        # ollama の無い機械では呼ばずに日時の名前にする（待たせない）
        name, error = pm.fallback_project_name(), None
    else:
        name, error = pm.generate_project_name(text, settings)

    emit(
        "project_name_suggested",
        {
            "name": name,
            "parent": get_project_root_key(),
            "error": error,
        },
        room=sid,
    )


@socketio.on("create_project")
def handle_create_project(message):
    """親プロジェクトの projects/ 配下に新規プロジェクトフォルダを作成する。

    作成後は該当プロジェクトのPTYセッションへ自動的に切り替える。
    """
    sid = request.sid
    message = message or {}
    name = message.get("name")
    source_chat = message.get("chat") or ""

    # 作成先は「一時フォルダ」か「developing 直下」から選べる。
    # 指定が無い場合は従来どおり一時フォルダへ作る。
    location = message.get("location") or "temp"
    if location not in CREATE_LOCATIONS:
        emit(
            "project_create_result",
            {"ok": False, "error": f"作成先の指定が不正です: {location}"},
            room=sid,
        )
        return

    # Claude in Chrome の道具（起動時にしか付かない）。起動・移しの口と同じく true は --chrome・
    # false は --no-chrome・省けば claude の設定に従う。フォルダを作る前に形を確かめる
    # （作ってから起動で断ると、空の作業場だけが残るため）
    chrome = message.get("chrome")
    if chrome is not None and not isinstance(chrome, bool):
        emit(
            "project_create_result",
            {"ok": False, "error": "chrome は true か false で指定してください"},
            room=sid,
        )
        return

    info = resolve_location(location)
    if not info:
        emit(
            "project_create_result",
            {
                "ok": False,
                "error": "作成先フォルダが見つかりません"
                "（config/settings.json の project_root と developing_dir・working_dir を確認してください。README の「設定」）",
            },
            room=sid,
        )
        return

    # どのアカウントで起こすか（省略は割り当ての無い作業場の既定＝unassigned_account・無ければ本家）。
    # フォルダを作る前に、起こせるアカウントかを確かめる
    try:
        account_name = message.get("account") or accounts.unassigned_account()["name"]
        if not isinstance(account_name, str):
            raise accounts.AccountError("account は文字列で指定してください")
        account = accounts.require_usable(account_name)
        pending = accounts.pending_import_reason()
        if pending and not accounts.unassigned_account()["main"]:
            # 既定が本家でない間は、本家で作る作業場も明示の本家として台帳に書く必要がある（書けないので先に断る）
            raise accounts.AccountError(
                f"いまは作業場を作れません（trial_roles.json の役をまだ取り込めていません: {pending}）。"
                "config/settings.json の accounts と trial_roles_mirror を直してから作ってください"
            )
        if pending and not account["main"]:
            # 取り込みを待つ間は割り当てを書けない。フォルダを作ってから断ると行き止まりになるので先に断る
            raise accounts.AccountError(
                f"いまは {account['label']} を選べません（trial_roles.json の役をまだ取り込めていません: {pending}）。"
                f"{accounts.main_account()['label']} で作るか、設定を直してから作ってください"
            )
    except accounts.AccountError as e:
        emit("project_create_result", {"ok": False, "error": str(e)}, room=sid)
        return

    try:
        created = pm.create_project(
            dest_dir=info["dir"],
            key_prefix=info["key_prefix"],
            name=name,
            source_chat=source_chat,
            parent_key=info["parent_key"],
        )
    except ValueError as e:
        emit("project_create_result", {"ok": False, "error": str(e)}, room=sid)
        return
    except OSError as e:
        emit(
            "project_create_result",
            {"ok": False, "error": f"フォルダの作成に失敗しました: {e}"},
            room=sid,
        )
        return
    except Exception as e:
        print(f"Error creating project under {info['key_prefix']}: {e}")
        emit(
            "project_create_result",
            {"ok": False, "error": f"プロジェクトの作成に失敗しました: {e}"},
            room=sid,
        )
        return

    print(f"[Project] Created new project: {created['project_key']}")

    # 作った時点で作業場にアカウントを覚える（以後は同じアカウントで起こす）。
    # 本家なら台帳に載せない（載っていない作業場は本家）。ただし、フォルダ名がほかのアカウントの
    # 印（取り込みで作業場へ解けなかった役の名前）に当たるときは、この作業場だけ本家と書く
    created_path = resolve_project_path(created["project_key"])
    try:
        needs_record = not account["main"] or bool(
            created_path and accounts.effective_name(created_path) != account["name"]
        )
    except accounts.AccountError:
        needs_record = True
    if needs_record:
        try:
            if not created_path:
                raise accounts.AccountError("作ったフォルダが見つかりません")
            result = accounts.assign(
                created_path, created["project_key"], account["name"], "create"
            )
            if _mirror_warning(result):
                print(f"[Accounts] {_mirror_warning(result)}", flush=True)
                # 作成時の警告（git・NOW.md）があれば消さずに並べる
                created["warning"] = "\n".join(
                    w for w in (created.get("warning"), _mirror_warning(result)) if w
                )
        except (accounts.AccountError, OSError) as e:
            # 割り当てを書けないまま起こすと本家で起きる。起こさずに知らせる
            emit(
                "project_create_result",
                {
                    "ok": False,
                    "error": f"作業場（{created['project_key']}）はできましたが、{account['label']} への割り当てを"
                    "記録できなかったので、セッションは起こしていません。"
                    + (
                        # 取り込み待ちの間は「移す」も断られるので、待ちが解けてからと案内する
                        "trial_roles.json の役の取り込みが済んでから、"
                        if "まだ取り込めていない" in str(e)
                        else "この画面を閉じ、"
                    )
                    + f"プロジェクトの一覧からこの作業場を開いて「アカウントを移す」で {account['label']} を選んでください（詳細: {e}）",
                    "project": created,
                },
                room=sid,
            )
            return
    created["account"] = account["name"]
    emit("project_create_result", {"ok": True, "project": created}, room=sid)

    # 作成したプロジェクトで、そのままセッションを起動する。
    # 利用者が書いた指示は起動時の引数として渡るため、TUI への流し込みは不要。
    # モデル・モード等の起動オプションも、そのまま引き継ぐ。
    handle_start_session(
        {
            **{key: message.get(key) for key in LAUNCH_OPTION_KEYS},
            "project": created["project_key"],
            "prompt": source_chat,
            "account": account["name"],
            "chrome": chrome,
        }
    )


def stop_sessions_under(folder):
    """指定フォルダ（配下を含む）を作業ディレクトリにしている claude を終了させる。

    Windows では、プロセスの作業ディレクトリになっているフォルダは掴まれた
    ままで移動できない。フォルダを移す前に、その中で動いているセッションを
    終了させる必要がある。

    Args:
        folder: 対象フォルダの絶対パス

    Returns:
        終了させたセッションの情報リスト
    """
    base = os.path.abspath(folder)
    closed = []
    for session in sm.list_sessions():
        cwd = session.get("cwd")
        if not cwd:
            continue
        try:
            if os.path.commonpath([base, os.path.abspath(cwd)]) != base:
                continue
        except ValueError:
            continue
        try:
            sm.stop_session(session["pid"])
            closed.append(session)
            print(
                f"[Session] Stopped {session.get('name')} (pid={session['pid']}) for move"
            )
        except sm.SessionError as e:
            print(f"[Session] Failed to stop pid={session.get('pid')}: {e}")
    return closed


@socketio.on("move_project")
def handle_move_project(message):
    """プロジェクトフォルダを別の置き場（一時／developing／working）へ移動する。

    一時フォルダで作って試したものが育ってきたら developing へ、運用が
    始まったら working へ、という移し替えを画面から行えるようにする。

    移動の前に、対象プロジェクト（と配下）のPTYセッションを終了させる。
    Windows ではプロセスの作業ディレクトリになっているフォルダは掴まれた
    ままで移動できないためで、これを黙って行うと「移動できません」だけが
    返る分かりにくい失敗になる。
    """
    sid = request.sid
    message = message or {}
    project_key = message.get("project")
    location = message.get("location")

    def fail(reason):
        emit("project_move_result", {"ok": False, "error": reason}, room=sid)

    if not project_key or project_key == "default":
        return fail("移動するプロジェクトを指定してください。")

    if location not in PROJECT_LOCATIONS:
        return fail(f"移動先の指定が不正です: {location}")

    src_path = resolve_project_path(project_key)
    if not src_path:
        emit("project_missing", {"project": project_key}, room=sid)
        return fail(f"プロジェクトが見つかりません: {project_key}")

    if is_protected_path(src_path):
        return fail("このプロジェクトは本アプリ自身を含んでいるため移動できません。")

    info = resolve_location(location)
    if not info:
        return fail("移動先フォルダが見つかりません。")

    # 移動先のフォルダを掴んでいるセッションを先に終了させる
    closed = stop_sessions_under(src_path)
    if closed:
        # プロセス終了がOSへ反映されるのを待つ（掴みが外れる前に移動すると失敗する）
        time.sleep(1.0)

    # 終了させた直後は子プロセス（claude 等）がフォルダを掴んだままのことがあり、
    # 1回目の移動だけが失敗することがある。掴みが外れるのを待って数回試す。
    moved = None
    last_error = None
    for attempt in range(MOVE_RETRY_COUNT):
        try:
            moved = pm.move_project(
                project_key=project_key,
                src_path=src_path,
                dest_dir=info["dir"],
                key_prefix=info["key_prefix"],
                parent_key=info["parent_key"],
            )
            break
        except ValueError as e:
            # 置き場の指定そのものが誤っている場合は、待っても解決しない
            return fail(str(e))
        except OSError as e:
            last_error = e
            print(f"[Project] Move attempt {attempt + 1} failed for {project_key}: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"Error moving project {project_key}: {e}")
            return fail(f"移動に失敗しました: {e}")

    if moved is None:
        return fail(
            f"移動に失敗しました: {last_error}"
            "（フォルダを掴んでいるプロセスがあります。"
            "エクスプローラやエディタで開いていないか確認してください）"
        )

    # 起動回数の記録を新しいキーへ引き継ぐ
    rename_project_usage(project_key, moved["project_key"])

    print(
        f"[Project] Moved {project_key} -> {moved['project_key']} "
        f"(closed sessions: {len(closed)})"
    )
    emit(
        "project_move_result",
        {
            "ok": True,
            "project": moved,
            "location": location,
            "location_label": info["label"],
            "closed_sessions": [s.get("name") or s.get("pid") for s in closed],
        },
        room=sid,
    )

    # フォルダ構成が変わったため、全クライアントの一覧を更新する
    try:
        socketio.emit(
            "parent_projects_list",
            {
                "projects": get_sorted_projects(),
                "project_root": get_project_root_key(),
                "locations": get_location_labels(),
            },
        )
    except Exception as e:
        print(f"Error broadcasting project list after move: {e}")


def project_key_for_path(path):
    """作業ディレクトリの絶対パスから、本アプリのプロジェクトキーを逆引きする。

    セッション一覧には、本アプリ以外（PCのターミナル等）で起動されたものも
    含まれる。プロジェクトとして辿れるものにはキーを付けて、一覧から
    そのまま操作できるようにする。

    Args:
        path: セッションの作業ディレクトリ

    Returns:
        プロジェクトキー。管理対象の外なら None
    """
    if not path:
        return None
    target = os.path.abspath(path)
    for group, base_dir in (("developing", DEVELOPING_DIR), ("working", WORKING_DIR)):
        if not base_dir:
            # 未設定の置き場（abspath("") は作業フォルダになるので比べない）
            continue
        base = os.path.abspath(base_dir)
        try:
            if os.path.commonpath([base, target]) != base:
                continue
        except ValueError:
            # ドライブが異なる場合（commonpath が例外を投げる）
            continue
        relative = os.path.relpath(target, base).replace("\\", "/")
        if relative in (".", ""):
            return None
        return f"{group}/{relative}"
    return None


def build_sessions_payload():
    """クライアントへ送るセッション一覧を組み立てる。"""
    try:
        sessions = sm.list_sessions()
    except Exception as e:
        print(f"Error listing sessions: {e}")
        return {"sessions": [], "error": f"セッション一覧の取得に失敗しました: {e}"}

    for session in sessions:
        session["project_key"] = project_key_for_path(session.get("cwd"))

    # アカウント（account は動いている置き場＝実際の値。割り当てと違えば account_expected に割り当てを出す）
    account_list = []
    try:
        account_list = [account_brief(a, sessions) for a in accounts.definitions()]
    except Exception as e:
        print(f"Error reading accounts: {e}", flush=True)
    try:
        current = accounts.state()
        for session in sessions:
            try:
                expected = accounts.effective_name(session.get("cwd"), current)
            except accounts.AccountError as e:
                # 既定の設定の誤りは行ごとに受ける（1行の誤りで残りの行の照合まで落とさない）
                print(
                    f"Error resolving account for {session.get('cwd')}: {e}", flush=True
                )
                continue
            session["account_expected"] = (
                expected if expected != session.get("account") else None
            )
    except Exception as e:
        print(f"Error reading account assignments: {e}", flush=True)

    # 枠を使い切って止まったセッション（会話記録の最後の応答が rate_limit）。どのアカウントの何の枠か・戻る時刻
    for session in sessions:
        try:
            stop = accounts.quota_stop(
                transcript_path_for(session.get("cwd"), session.get("session_id"))
            )
        except Exception as e:
            print(f"Error reading quota stop: {e}", flush=True)
            stop = None
        session["quota_stop"] = stop
        # 枠が戻ったときの続きの依頼の状態（画面で「自動で頼みます／頼めませんでした」を分ける）
        session["quota_resume"] = quota.state_for(session.get("session_id"), stop)
        # again_wait のときの、もう1回頼む時刻（HH:MM）
        session["quota_resume_at"] = (
            quota.again_wait_text(session.get("session_id"), stop) if stop else None
        )

    # 許可待ち・答え待ち（人の手を待つもの）を判定して先頭へ並べる。
    # 判定に失敗しても一覧は出す（印が付かないだけ。理由はログへ）
    try:
        notification_log.refresh()
        sessions = attention.annotate(sessions, notification_log, transcript_path_for)
    except Exception as e:
        print(f"Error assessing attention: {e}", flush=True)

    # 今週の使用量と会話の長さ（集計は裏で回しているものの写しを読むだけ）
    settings = load_settings()
    usage_summary = None
    try:
        lineage.observe(sessions)
        for session in sessions:
            session["usage"] = usage_tracker.describe(
                session,
                usage,
                lineage,
                transcript_path_for(session.get("cwd"), session.get("session_id")),
                settings,
            )
        usage_summary = usage_tracker.summary(usage, settings)
    except Exception as e:
        print(f"Error describing usage: {e}", flush=True)

    # 「残す」印（自動の区切り・起こし直しから外す。利用者だけが付け外しする）
    try:
        marks = auto.keep_marks.all()
        waiting = {str(e["pid"]) for e in self_clear.auto_expectations()}
        pending = {str(r["pid"]) for r in self_clear.status()["pending"]}
        for session in sessions:
            session["kept"] = auto.keep_marks.is_kept(session, marks)
            # 自動の区切りの依頼を送って予約を待っている・区切りの予約が入っている
            session["auto_waiting"] = str(session.get("pid")) in waiting
            session["clear_reserved"] = str(session.get("pid")) in pending
    except Exception as e:
        print(f"Error reading keep marks: {e}", flush=True)
    return {
        "sessions": sessions,
        "usage_summary": usage_summary,
        "auto_clear_mode": auto_clear.config_from(settings)["mode"],
        "quota_resume_mode": quota_resume.mode_of(settings),
        # 管理役への知らせが有効か（画面で「管理役が扱います」と「手で送って」を分ける）
        "liaison_enabled": notices.enabled(settings),
        "accounts": account_list,
        "error": None,
    }


def account_usage_view(account, usage_info):
    """画面に出すアカウントの使用%の形（一覧と /api/accounts で共通）。

    回復の時刻は accounts.format_reset の1通りで書いた文字列で渡す（画面では時刻を組み立てない）。
    """
    rule = accounts.policy(account)
    over = accounts.over_detail(account, usage_info)
    return {
        "five_hour": usage_info["five_hour"],
        "weekly": usage_info["weekly"],
        "five_hour_max_pct": rule["five_hour_max_pct"],
        "capacity": rule["capacity"],
        "five_hour_capacity": rule["five_hour_capacity"],
        "five_hour_unlimited": rule["five_hour_unlimited"],
        "over_line": over["text"] if over else None,
        "over_window": over["window"] if over else None,
        "five_hour_resets": accounts.format_reset(
            usage_info["five_hour"].get("resets_at")
        ),
        "weekly_resets": accounts.format_reset(usage_info["weekly"].get("resets_at")),
    }


def account_brief(account, sessions):
    """一覧に載せるアカウントの要約（5時間枠・週の枠の使用%と回復・目安・枠の大きさ・動いている本数）。

    account_b のような枠の小さいアカウントに何本まで載せられるかを、人が一覧で見て決められるように。
    使用%が読めなくても、名前と本数は出す（1つのアカウントの失敗で一覧全体を落とさない）。
    """
    brief = {
        "name": account["name"],
        "label": account["label"],
        "main": account["main"],
        "running": sum(1 for s in sessions if s.get("account") == account["name"]),
        "busy": sum(
            1
            for s in sessions
            if s.get("account") == account["name"] and s.get("status") == "busy"
        ),
    }
    try:
        brief.update(account_usage_view(account, accounts.usage(account)))
    except Exception as e:
        print(f"Error reading usage of {account['name']}: {e}", flush=True)
    return brief


def transcript_path_for(cwd, session_id):
    """作業フォルダと会話の番号から、本体の会話記録のパスを作る（無ければ None）。"""
    if not cwd or not session_id:
        return None
    return os.path.join(sm.transcript_dir_for(cwd), f"{session_id}.jsonl")


def usage_refresh_loop():
    """使用量を定期的に数え直す常駐タスク（会話記録の増えた分だけを読む）。

    誰も見ていないときも回す。止めると、次に開いたときの1回目が重くなるため
    （週の始まりからの分を一度に読むことになる）。
    """
    while True:
        try:
            started = time.time()
            snapshot = usage.refresh()
            elapsed = time.time() - started
            if elapsed > 10:
                print(
                    f"[Usage] refreshed in {elapsed:.1f}s "
                    f"(total ${snapshot['total_cost']:.2f})",
                    flush=True,
                )
        except Exception as e:
            print(f"Error refreshing usage: {e}", flush=True)
        socketio.sleep(USAGE_REFRESH_INTERVAL_SEC)


def annotated_sessions():
    """一覧に許可待ちの印（attention）を付けたもの（自動の区切り・起こし直しの判定用）。"""
    sessions = sm.list_sessions()
    notification_log.refresh()
    return attention.annotate(sessions, notification_log, transcript_path_for)


def auto_clear_loop():
    """管理下のセッションを見回り、規則に当たったら区切りの依頼を送る常駐タスク。

    誰も見ていないときも回す。設定 auto_clear_mode が off なら何もしない、
    dry_run なら当たりを記録するだけ。
    """
    while True:
        socketio.sleep(auto_clear.POLL_SEC)
        try:
            auto.tick(annotated_sessions(), load_settings())
        except Exception as e:
            # ここで例外を出すと見回りが止まったままになる（出力の失敗でも死なない）
            safe_console.say(f"Error in auto clear: {e}", flush=True)


def watch_loop():
    """枠が戻った子へ続きを頼み（quota_resume）、管理役への知らせを溜めて送る（liaison）常駐タスク。

    誰も見ていないときも回す。どちらかの失敗でもう片方を止めない。
    """
    while True:
        socketio.sleep(quota_resume.POLL_SEC)
        settings = load_settings()
        try:
            sessions = annotated_sessions()
        except Exception as e:
            safe_console.say(f"Error listing sessions for watch: {e}", flush=True)
            continue
        try:
            quota.tick(sessions, settings)
        except Exception as e:
            safe_console.say(f"Error in quota resume: {e}", flush=True)
        try:
            weekly_alert.tick(sessions, settings)
        except Exception as e:
            safe_console.say(f"Error in weekly quota alert: {e}", flush=True)
        try:
            notices.observe_sessions(sessions, settings)
            notices.flush(settings)
        except Exception as e:
            safe_console.say(f"Error in liaison: {e}", flush=True)


def broadcast_sessions():
    """接続中の全クライアントへセッション一覧を配信する。"""
    socketio.emit("sessions_list", build_sessions_payload())


def sessions_poll_loop():
    """セッションの状態（idle/busy）を定期的に配信し続ける常駐タスク。

    claude 自身が書き出す状態ファイルを読むだけなので、アプリ側に状態は持たない。
    誰も見ていないときは配信しない（接続数が 0 のあいだは休む）。
    """
    while True:
        socketio.sleep(SESSION_POLL_INTERVAL_SEC)
        with clients_lock:
            has_clients = connected_clients > 0
        if not has_clients:
            # 誰も見ていなくても、/clear で変わった会話の番号は覚えておく
            # （行の今週の使用量に、/clear より前の会話の分も含めるため）
            try:
                lineage.observe(sm.list_sessions())
            except Exception as e:
                print(f"Error observing session lineage: {e}")
            continue
        try:
            broadcast_sessions()
        except Exception as e:
            print(f"Error broadcasting sessions: {e}")


@socketio.on("list_sessions")
def handle_list_sessions():
    """セッション一覧の再取得要求。"""
    emit("sessions_list", build_sessions_payload(), room=request.sid)


@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    """セッション一覧（画面の一覧と同じ中身。各行に account＝動いているアカウント）。"""
    body = build_sessions_payload()
    return jsonify({"ok": not body.get("error"), **body})


# 起動時に画面から受け取るオプション（sm.launch_session の引数名と同じ）
LAUNCH_OPTION_KEYS = ("model", "permission_mode", "effort", "win_session", "elevation")


def start_session_request(message, source="api"):
    """起動要求を処理し、起動・再開を送った履歴に残す（形の不備で断ったものは残さない）。"""
    status, body = _start_session(message)
    if body.get("code") != "invalid_body":
        message = message if isinstance(message, dict) else {}
        session = body.get("session") or {}
        target = {
            "pid": session.get("pid") or body.get("pid"),
            "name": session.get("name") or "",
            "cwd": session.get("cwd") or "",
            "session_id": session.get("session_id") or "",
            "project": message.get("project") or "",
            "managed": True,
        }
        options = {k: message.get(k) for k in LAUNCH_OPTION_KEYS if message.get(k)}
        if isinstance(message.get("chrome"), bool):
            options["chrome"] = message["chrome"]
        if message.get("resume"):
            options["resume"] = message.get("resume")
        elif body.get("auto_resumed"):
            options.update({"resume": body.get("resumed"), "auto_resumed": True})
        if body.get("account"):
            options["account"] = body.get("account")
        send_history.record(
            "resume" if message.get("resume") or body.get("auto_resumed") else "launch",
            ok=bool(body.get("ok")),
            target=target,
            text=message.get("prompt") or "",
            source=source,
            error=None if body.get("ok") else body.get("error"),
            code=None if body.get("ok") else body.get("code"),
            detail=options or None,
        )
    else:
        body["code"] = "failed"
    return status, body


def _start_session(message):
    """セッションの起動要求を処理し、(HTTP 状態コード, 応答) を返す。

    Socket.IO の start_session と REST の POST /api/sessions の両方から使う。
    起動したセッションは本アプリの子プロセスではないため、アプリを停止・
    再起動しても生き残る。モデル・権限モード・思考の深さ・起動先の Windows
    セッション・権限（管理者／通常）を選べ、resume に過去の会話の sessionId を
    渡すと続きから再開する（値の検証は sm.launch_session 側で行う）。
    """
    message = message or {}
    # 画面からは来ない形（配列のボディ・文字列でない値）も、500 にせず 400 で断る
    if not isinstance(message, dict):
        return 400, {
            "ok": False,
            "error": "ボディは JSON のオブジェクトにしてください",
            "code": "invalid_body",
        }
    for key in ("project", "prompt", "resume", "account") + LAUNCH_OPTION_KEYS:
        if message.get(key) is not None and not isinstance(message.get(key), str):
            return 400, {
                "ok": False,
                "error": f"{key} は文字列で指定してください",
                "code": "invalid_body",
            }
    for key in ("new", "remote_control", "chrome"):
        if message.get(key) is not None and not isinstance(message.get(key), bool):
            return 400, {
                "ok": False,
                "error": f"{key} は true か false で指定してください",
                "code": "invalid_body",
            }
    if machine_send.forged(message.get("prompt")):
        return 400, {
            "ok": False,
            "error": machine_send.FORGED_ERROR,
            "code": "forged_heading",
        }
    project_key = message.get("project")
    prompt = (message.get("prompt") or "").strip()
    resume_id = (message.get("resume") or "").strip() or None

    target_path = resolve_project_path(project_key)
    if not target_path:
        return 404, {
            "ok": False,
            "error": f"プロジェクトが見つかりません: {project_key}",
            "code": "project_missing",
        }

    auto_resumed = False
    with _launching_lock:
        if not resume_id and not message.get("new"):
            # 既定は前の会話の続き（起こし直すたびに新しい会話になり、claude.ai の一覧に
            # 同じ名前の行が並んだため・2026-10-28 管理役の依頼）。新しい会話は new: true のときだけ
            resume_id = latest_resumable_id(target_path)
            auto_resumed = bool(resume_id)
        if resume_id and _is_launching(resume_id):
            # 二度押し・再送。既定の選択でも新しい会話へ逃がさず断る（同じ作業場に2本並べない）
            return 409, {
                "ok": False,
                "error": f"その会話はいま起こしている途中です: {resume_id}。"
                "同じ会話を2つで動かすと記録が壊れるため、起こしません"
                "（別の会話で起こすなら new: true）",
                "code": "failed",
            }
        if resume_id:
            _launching[resume_id] = None
    status, body = 500, {}
    try:
        status, body = _launch_and_wait(
            message, project_key, target_path, prompt, resume_id, auto_resumed
        )
        return status, body
    finally:
        if resume_id:
            with _launching_lock:
                if body.get("code") == "not_registered" and body.get("pid"):
                    # 登録を待ちきれなかっただけで、起こした claude は動いているかもしれない。
                    # その pid が生きている間は控えたままにする（押し直しで2本目を起こさない）。
                    # 開始時刻も控え、pid が別のプロセスに再利用されたら外れるようにする。
                    # 開始時刻は起こした直後に台帳（record_launched）が取ったものを使う（登録を待つ最大45秒の間に
                    # 死んで再利用された pid の時刻を控えないため・評価役の低）。台帳に無ければ None＝pid が生きている間控える
                    entry = sm._load_registry().get(str(body["pid"])) or {}
                    _launching[resume_id] = (body["pid"], entry.get("create_time"))
                else:
                    _launching.pop(resume_id, None)


# 起こしている途中（状態ファイルが書かれる前）の会話の番号 → 起動中は None、登録を待ちきれなかったら
# (起こした pid, その開始時刻)。validate_resume_target は状態ファイルで「動いている」を見るため、
# その前は二重に起こせてしまう（評価役の指摘）
_launching = {}
_launching_lock = threading.Lock()


def _is_launching(session_id):
    """その会話を起こしている途中か（_launching_lock の中で呼ぶ）。

    pid が死んでいる・別のプロセスに再利用された（開始時刻が違う）なら控えから外す
    （再利用された pid が生きている間、既定の起動が 409 で断られ続けないように・2026-11-02）。
    開始時刻を控えられなかったときは、今までどおり pid が生きている間は控える。
    """
    if session_id not in _launching:
        return False
    entry = _launching[session_id]
    if entry is None:
        return True
    pid, created = entry if isinstance(entry, tuple) else (entry, None)
    alive = sm.psutil.pid_exists(int(pid))
    if alive and created is not None:
        # 今の開始時刻が読めないときは外す側に倒す（控えの古い pid で起動を断り続けないため）
        actual = sm._process_create_time(pid)
        alive = (
            actual is not None
            and abs(actual - created) <= sm.LAUNCHED_TIME_TOLERANCE_SEC
        )
    if alive:
        return True
    _launching.pop(session_id, None)
    return False


def _launch_and_wait(
    message, project_key, target_path, prompt, resume_id, auto_resumed
):
    """起こして claude 側の登録を待ち、(状態コード, 応答) を返す（_start_session の後半）。"""
    settings = load_settings()
    display_name = build_session_display_name(project_key)
    try:
        started = sm.launch_session(
            cwd=target_path,
            name=display_name,
            prompt=pm.to_single_line(prompt) or None,
            claude_exe=settings.get("claude_exe"),
            resume_session_id=resume_id,
            account=message.get("account") or None,
            # claude.ai の Remote Control の行の題名。指定なしは既定（再開なら付ける・session_manager）、
            # true なら新しい会話でも付ける、false なら付けない
            remote_control_name={True: display_name, False: ""}.get(
                message.get("remote_control")
            ),
            # true は --chrome・false は --no-chrome（Claude in Chrome の道具は起動時にしか付かない）。
            # 省けば何も足さない（設定の claudeInChromeDefaultEnabled に従う＝今までどおり）
            chrome=message.get("chrome"),
            **{key: message.get(key) or None for key in LAUNCH_OPTION_KEYS},
        )
    except sm.SessionError as e:
        return 400, {"ok": False, "error": str(e), "code": "failed"}
    except Exception as e:
        print(f"Error starting session for {project_key}: {e}", flush=True)
        return 500, {
            "ok": False,
            "error": f"セッションの起動に失敗しました: {e}",
            "code": "error",
        }

    increment_project_usage(project_key)
    options = {k: message.get(k) for k in LAUNCH_OPTION_KEYS if message.get(k)}
    if started.get("chrome") is not None:
        options["chrome"] = started["chrome"]
    print(
        f"[Session] Started {project_key} (pid={started['pid']}, "
        f"trusted_now={started['trusted']}, win_session={started['win_session']}, "
        f"elevated={started['elevated']}, resumed={started['resumed']}, "
        f"account={started.get('account')}, options={options})",
        flush=True,
    )

    # 管理者を指定しても、利用者が管理者でなければ通常権限で動く。黙らずに知らせる
    warning = None
    if message.get("elevation") == "admin" and started["elevated"] is False:
        warning = "管理者を指定しましたが、通常権限で起動されました（利用者が管理者でない可能性があります）"
    elif message.get("elevation") == "standard" and started["elevated"]:
        warning = "通常を指定しましたが、管理者として起動されました"

    # 起動を待って、claude 側に登録されたかを確認する
    session = wait_for_session(started["pid"])
    if not session:
        # 画面に何が出ているかをそのまま見せる（信頼確認の残り・エラー等）
        screen = sm.read_console_screen_isolated(started["pid"], max_lines=40)
        return 502, {
            "ok": False,
            "error": "セッションを起動しましたが、claude 側に登録されませんでした。",
            "code": "not_registered",
            "screen": [line for line in screen if line.strip()][-15:],
            "pid": started["pid"],
            "account": started.get("account"),
        }

    # 状態ファイルを見つけた置き場（実際のアカウント）が、起こしたアカウントと違えば知らせる
    if started.get("account") and session.get("account") != started["account"]:
        mismatch = f"{started['account']} で起こしましたが、{session.get('account')} の置き場に登録されました"
        warning = f"{warning} / {mismatch}" if warning else mismatch

    return 200, {
        "ok": True,
        "session": session,
        "project": project_key,
        "elevated": started["elevated"],
        "resumed": started["resumed"],
        "auto_resumed": auto_resumed,
        "account": started.get("account"),
        "warning": warning,
    }


def latest_resumable_id(cwd):
    """作業場のいちばん新しい対話の会話の sessionId（動いていなければ）。無ければ None。

    いちばん新しい対話の会話が動いていれば、古い会話へは戻らず新しい会話にする
    （同じ作業場で2本目を起こすときに、ずっと前の会話を掘り起こさないため）。
    無人実行（`claude -p`・headless）の会話は続きの候補にしない（定期の無人実行が同じ作業場で走ると、
    対話の続きのつもりがその使い捨ての会話を起こしてしまう・2026-11-02）。手で選べば再開はできる。
    """
    try:
        # 画面の一覧（list_resumable の既定の数）と同じ範囲を、同じ順で見る（画面が既定に選ぶ会話と食い違わないように）。
        # 最初の対話の会話で止める（起動のたびに _launching_lock の中で30本を読まないため・評価役の中）
        items = sm.list_resumable(cwd, stop_at_interactive=True)
    except OSError:
        return None
    interactive = [item for item in items if not item.get("headless")]
    if not interactive or interactive[0].get("live"):
        return None
    return interactive[0]["session_id"]


@socketio.on("start_session")
def handle_start_session(message):
    """指定プロジェクトで claude セッションを、本アプリから切り離して起動する。"""
    sid = request.sid
    status, body = start_session_request(message, source="screen")
    if body.get("code") == "project_missing":
        emit("project_missing", {"project": message.get("project")}, room=sid)
    emit("session_start_result", body, room=sid)
    if status == 200:
        broadcast_sessions()


@app.route("/api/sessions", methods=["POST"])
def api_start_session():
    """セッションを起動する（Socket.IO の start_session と同じ処理）。

    ボディ: ``{"project": "<プロジェクトキー>", "prompt": "最初の指示（省略可）",
    "model", "permission_mode", "effort", "win_session", "elevation": "admin|standard",
    "resume": "<過去の会話の sessionId>", "account": "<アカウントの名前>", "new": false,
    "remote_control": true, "chrome": true}``
    （省略した項目は既定。account は作業場の割り当てと同じときだけ通る＝確かめ用。省略は割り当てどおり。
    resume を省くと、いちばん新しい対話の会話（無人実行 claude -p を除く）が動いていなければその続きで起こす。
    新しい会話は new: true のときだけ。
    chrome は true で --chrome・false で --no-chrome、省けば claude の設定に従う）
    応答: 200 ``{"ok": true, "session": {...}, "elevated", "resumed", "auto_resumed", "account", "warning"}`` /
    400 指定の不備・起動失敗 / 404 プロジェクトが無い / 502 claude 側に登録されない
    """
    status, body = start_session_request(
        request.get_json(silent=True), source=_request_source()
    )
    if status == 200:
        broadcast_sessions()
    return jsonify(body), status


# 起動の要求を「起こす前の検証」で断ったと分かる文（400 の error に含まれる）。
# 起こした後のエラー（時間切れ・別のセッションで起きた等）も 400 で返るため、外の道具
# （claude-session-resumer）はこの文で「起こしていない＝別の開き方へ戻してよい」を見分ける。
# 文を変えたら tests/test_integration_spec.py が出どころとの食い違いを知らせる
PRE_LAUNCH_REFUSALS = (
    "指定が不正",
    "記録が見つかりません",
    "別のフォルダ",
    "作業ディレクトリが見つかりません",
    "通常権限で起動できません",
    "実行ファイルが見つかりません",
    "設定ファイルを読めませんでした",
    "設定ファイルを更新できませんでした",
    "JSON のオブジェクト",
    "文字列で指定",
)

# アカウントで断ったときの 400 の error に含まれる文（accounts.py・session_manager.launch_session）。
# 起こしていないが、PRE_LAUNCH_REFUSALS と違い「ほかの道で起こしてよい」ではない
# （外の道具が自分の道で起こすと、割り当てと違うアカウント＝多くは本家で起きてしまう）
ACCOUNT_REFUSALS = (
    "に割り当てられています",
    "の設定が使えません",
    "は設定にありません",
    "割り当ての台帳",
    "アカウントの設定ファイル",
    "まだ取り込めていない",
)

# 再開を頼んだ会話がすでに動いていたときの 400 の error に含まれる文（起こしていない・動いている）
ALREADY_RUNNING_PHRASE = "その会話はいま動いています"

# 下の仕様の版。項目を消す・意味を変えるときに上げる（足すだけなら上げない）
# 2: POST /api/sessions で resume を省いたときの既定が「新しい会話」から「続き」に変わった（2026-10-28）
INTEGRATION_SPEC_VERSION = 2


def integration_spec():
    """外の道具が本アプリから写している仕様（台帳・置き場・起動の選択肢・断り文）を返す。

    claude-session-resumer はこれらを自分のコードに写して持っている。ここから読めば、
    本アプリが変わっても写しを直さずに済む（第2段で共有のモジュールへ寄せるまでのつなぎ）。
    置き場は起動時に読んだ値（設定ファイルを書き換えても再起動まではこちらが正しい）。
    """
    choices = sm.launch_option_choices()
    values = lambda key: [c["value"] for c in choices[key] if c["value"]]
    return {
        "ok": True,
        "spec_version": INTEGRATION_SPEC_VERSION,
        "app": "claude-web-app",
        "port": STARTUP_SETTINGS.get("port", DEFAULT_SETTINGS["port"]),
        "settings_file": SETTINGS_FILE,
        "project_roots": {
            "developing": DEVELOPING_DIR or None,
            "working": WORKING_DIR or None,
        },
        "project_key": {
            "format": "<group>/<folder>[/" + pm.PROJECTS_SUBDIR + "/<folder>]",
            "groups": ["developing", "working"],
            "sub_projects_dir": pm.PROJECTS_SUBDIR,
        },
        "registry": {
            "path": sm.LAUNCHED_REGISTRY,
            "create_time_tolerance_sec": sm.LAUNCHED_TIME_TOLERANCE_SEC,
            # 台帳は {"<pid>": {...}}。adopted は後から取り込んだ記録にだけ付く
            "entry_keys": ["create_time", "name", "cwd", "launched_at", "adopted"],
        },
        "launch": {
            "endpoint": "POST /api/sessions",
            "option_keys": list(LAUNCH_OPTION_KEYS),
            # resume を省くと、いちばん新しい対話の会話が動いていなければその続き。新しい会話は new: true。
            # 無人実行（claude -p）の会話は除く（値は版 2 のまま。除くものは resume_default_excludes）
            "resume_default": "latest_not_running",
            "resume_default_excludes": ["headless"],
            "new_conversation_key": "new",
            # true で claude.ai の Remote Control の行の題名（フォルダ名）を --remote-control で渡す
            "remote_control_key": "remote_control",
            # true で --chrome・false で --no-chrome（Claude in Chrome の道具）。省けば claude の設定に従う
            "chrome_key": "chrome",
            "model": values("model"),
            "permission_mode": values("permission_mode"),
            "effort": values("effort"),
            "win_session": values("win_session"),
            "elevation": values("elevation"),
            "refused_before_launch": {
                "statuses": [404],
                "status_400_phrases": list(PRE_LAUNCH_REFUSALS),
            },
            "already_running": {"status": 400, "phrase": ALREADY_RUNNING_PHRASE},
            # 起こしていない・ほかの道で起こさない（割り当てたアカウントでしか起こせない）
            "refused_account": {
                "status": 400,
                "phrases": list(ACCOUNT_REFUSALS),
            },
        },
        "adopt": {"endpoint": "POST /api/sessions/<pid>/adopt"},
        "resumable": {"endpoint": "GET /api/projects/resumable?project=<key>"},
        "accounts": {
            "endpoint": "GET /api/accounts",
            "move": "POST /api/accounts/move",
            "move_carry_key": "carry_session",
            "assignments_path": accounts.STATE_PATH,
            "launch_key": "account",
        },
    }


@app.route("/api/integration/spec")
def api_integration_spec():
    """外の道具（claude-session-resumer）が写している仕様を JSON で返す（読むだけ）。"""
    return jsonify(integration_spec())


# --- アカウント（accounts.py） -------------------------------------------------

# アカウントを移した後の最初の一言の既定（会話を持ち越さない移し。新しい会話なので引き継ぎを読ませる）
MOVE_DEFAULT_PROMPT = (
    "アカウントを {label} へ移して起こし直しました。前の会話は持ち越していません。"
    "このフォルダの NOW.md（無いか空なら README.md）を読んで、続きから進めてください。"
)

# 会話を持ち越したとき（carry_session）の最初の一言
MOVE_CARRY_PROMPT = (
    "アカウントを {label} へ移して、同じ会話を続きから起こし直しました。"
    "途中だった作業があれば続けてください。返事は不要です。"
)

_move_lock = threading.Lock()


def accounts_overview():
    """アカウントの一覧・使用%（週の枠・5時間枠）・新規作成の既定・割り当て・写しの状態。"""
    listed = []
    usages = {}
    for account in accounts.definitions():
        usage_info = accounts.usage(account)
        usages[account["name"]] = usage_info
        listed.append(
            {
                "name": account["name"],
                "label": account["label"],
                "main": account["main"],
                "usable": not account["error"],
                "error": account["error"],
                **account_usage_view(account, usage_info),
            }
        )
    default_name, default_reason = accounts.default_choice(usages)
    try:
        current = accounts.state()
        assignments = [
            {
                "project_key": e.get("project_key")
                or project_key_for_path(e.get("path")),
                "path": e.get("path"),
                "account": e.get("account"),
                "updated_at": e.get("updated_at"),
                "by": e.get("by"),
            }
            for e in current["assignments"].values()
        ]
        legacy = current.get("legacy_names") or {}
        mirror = accounts.mirror_status(current)
        pending = current.get("pending_import") or None
        state_error = None
    except accounts.AccountError as e:
        assignments, legacy, mirror, pending, state_error = [], {}, None, None, str(e)
    return {
        "ok": state_error is None,
        # trial_roles.json の役を取り込めずに待っている理由（この間は本家以外へ割り当てられない）
        "pending_import": pending,
        "accounts": listed,
        "default": default_name,
        "default_reason": default_reason,
        "assignments": sorted(assignments, key=lambda a: a["project_key"] or ""),
        "legacy_names": legacy,
        "mirror": mirror,
        "error": state_error,
    }


@app.route("/api/accounts")
def api_accounts():
    """アカウントの一覧と週次の使用%（取れなければ known: false と理由）・既定・割り当て。

    ``?project=<キー>`` を付けると、その作業場の割り当て（``project``）も返す。
    """
    body = accounts_overview()
    project_key = request.args.get("project")
    if project_key:
        path = resolve_project_path(project_key)
        if not path:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"プロジェクトが見つかりません: {project_key}",
                    }
                ),
                404,
            )
        try:
            name, reason = accounts.resolve(path)
            body["project"] = {
                "project_key": project_key,
                "account": name,
                # assigned: 台帳に行がある（本家と明示した行も含む）。reason は accounts.resolve の理由
                "assigned": reason in ("assigned", "explicit_main", "legacy"),
                "reason": reason,
            }
        except accounts.AccountError as e:
            # 既定の設定の誤りなどで行き先を決められない。一覧ごと落とさず、作業場の欄に理由を載せる
            # （開始画面に「なぜ起こせないか」を押す前に出すため。起動は session_manager が同じ理由で断る）
            body["project"] = {
                "project_key": project_key,
                "account": None,
                "assigned": False,
                "reason": "error",
                "error": str(e),
            }
    return jsonify(body), 200 if body["ok"] else 500


def find_role_folders(name):
    """役の名前（フォルダ名・小文字）に当たる作業場 [(パス, プロジェクトキー)]（trial_roles の取り込み用）。

    developing・working の直下と、その下の projects/ の直下を探す。
    """
    found = []
    for group, base_dir in (("developing", DEVELOPING_DIR), ("working", WORKING_DIR)):
        if not base_dir or not os.path.isdir(base_dir):
            continue
        for pattern in ("*", os.path.join("*", pm.PROJECTS_SUBDIR, "*")):
            for folder in glob.glob(os.path.join(base_dir, pattern)):
                if os.path.isdir(folder) and accounts.folder_name(folder) == name:
                    key = project_key_for_path(folder)
                    if key:
                        found.append((os.path.abspath(folder), key))
    return found


# 台帳が無いまま割り当てを引かれたら、その場で写しの役を取り込めるようにする
accounts.set_role_resolver(find_role_folders)


def _mirror_warning(result):
    """割り当ての結果から、写し（trial_roles.json）を書けなかった警告の文（無ければ None）。"""
    error = (result or {}).get("mirror", {}).get("error")
    return f"claude-session-resumer 用の trial_roles.json: {error}" if error else None


def _sessions_in(path):
    """作業場（そのフォルダちょうど）で動いているセッション。"""
    return [s for s in sm.list_sessions() if sm._same_path(s.get("cwd") or "", path)]


def move_account_request(message, source="api"):
    """作業場のアカウントを移す: 割り当てを書き換え → 動いているものを止める → 移す先で起こす。

    ボディ: ``{"project", "account": "<移す先>", "prompt": "起こし直しの最初の一言（省略で既定の文）",
    "launch": true（false なら起こさない）, "force": false（作業中でも止める）,
    "carry_session": false（true なら会話を持ち越す）, "model" ほか起動オプション}``

    carry_session が真なら、動いている会話（無ければいちばん新しい会話）の記録を移す先の置き場の
    同じ名前のフォルダへ新しく写し（元は残す）、同じ sessionId で --resume して起こす。
    移す先に同じ名前の記録が既にあれば、何も変えずに断る。ただしそれが同じ会話の前の版
    （前に移す先から写し出したときの残り）なら、``.before-carry-<日時>`` へよけてから写す（set_aside）。

    応答: 200 ``{"ok": true, "previous", "account", "stopped": [pid...], "session", "mirror",
    "pid", "session_id", "carried", "copied_files"}`` /
    400 指定の不備・同じアカウント・持ち越す会話が無い / 404 プロジェクトが無い /
    409 作業中（force で押し切れる）・移し中・持ち越す相手が2本以上・移す先に同じ記録 /
    502 止めたが写せなかった（割り当ては前に戻す）・止めたが起こせなかった（割り当ては移したまま）
    """
    if not isinstance(message, dict):
        return 400, {"ok": False, "error": "ボディは JSON のオブジェクトにしてください"}
    for key in ("project", "account", "prompt") + LAUNCH_OPTION_KEYS:
        if message.get(key) is not None and not isinstance(message.get(key), str):
            return 400, {"ok": False, "error": f"{key} は文字列で指定してください"}
    if machine_send.forged(message.get("prompt")):
        return 400, {
            "ok": False,
            "error": machine_send.FORGED_ERROR,
            "code": "forged_heading",
        }
    for key in ("force", "launch", "carry_session", "remote_control", "chrome"):
        # "false" のような文字列を真と取り違えて止める・起こすことがないように
        if message.get(key) is not None and not isinstance(message.get(key), bool):
            return 400, {
                "ok": False,
                "error": f"{key} は true か false で指定してください",
            }
    project_key = message.get("project")
    path = resolve_project_path(project_key)
    if not path:
        return 404, {
            "ok": False,
            "error": f"プロジェクトが見つかりません: {project_key}",
        }
    try:
        target = accounts.require_usable(message.get("account") or "")
        previous, previous_reason = accounts.resolve(path)
    except accounts.AccountError as e:
        return 400, {"ok": False, "error": str(e)}
    if previous == target["name"]:
        if previous_reason == "default":
            return 400, {
                "ok": False,
                "error": f"{project_key} は割り当てが無く、既定（unassigned_account）でもう {target['label']} で起きます"
                "（最初に起こしたときに割り当てとして書きます）",
            }
        return 400, {
            "ok": False,
            "error": f"{project_key} はもう {target['label']} に割り当てられています",
        }
    if not _move_lock.acquire(blocking=False):
        return 409, {
            "ok": False,
            "error": "ほかのアカウントの移しが進行中です。終わってから送り直してください",
        }
    try:
        running = _sessions_in(path)
        busy = [s for s in running if s.get("status") == "busy"]
        if busy and not message.get("force"):
            return 409, {
                "ok": False,
                "code": "busy",
                "error": f"作業中のセッションがあります（PID {', '.join(str(s['pid']) for s in busy)}）。"
                "終わってから移すか、作業中でも止めて移してください（API では force: true）",
            }
        carry = bool(message.get("carry_session"))
        carried_id = None
        if carry:
            # 写せるかは、何も変える前に確かめる（割り当て・停止の前）
            if len(running) > 1:
                return 409, {
                    "ok": False,
                    "code": "ambiguous",
                    "error": f"この作業場で {len(running)} 本動いているため、どの会話を持ち越すか決められません"
                    f"（PID {', '.join(str(s['pid']) for s in running)}）。1本にしてから送り直してください",
                }
            # 割り当てを書き換える前なので、前のアカウントの置き場から探す
            carried_id = (
                running[0].get("session_id") if running else latest_resumable_id(path)
            )
            if not carried_id:
                return 400, {
                    "ok": False,
                    "error": f"{project_key} に持ち越せる会話がありません（何も変えていません。"
                    "無人実行 claude -p の会話は持ち越しの候補にしません）。"
                    "carry_session を外せば新しい会話で移せます",
                }
            try:
                src_home = accounts.claude_home(
                    accounts.require_usable(previous), sm.CLAUDE_HOME
                )
                dst_home = accounts.claude_home(target, sm.CLAUDE_HOME)
                sm.check_transcript_copy(path, carried_id, src_home, dst_home)
            except (sm.SessionError, accounts.AccountError, OSError) as e:
                return 409, {
                    "ok": False,
                    "code": "carry_refused",
                    "error": f"会話を持ち越せません（何も変えていません）: {e}。"
                    "会話を持ち越さずに移すなら carry_session を外してください"
                    "（/clear の直後でまだ記録が無いときもこれになります）",
                }
        # 1. 割り当てを書き換える（ここから先は移す先で起きる）
        try:
            assigned = accounts.assign(path, project_key, target["name"], "move")
        except accounts.AccountError as e:
            # 取り込み待ち・設定の誤りなど、直すまで何度送っても同じ（何も止めていない）
            return 400, {"ok": False, "error": f"移せません（何も止めていません）: {e}"}
        except OSError as e:
            return 500, {
                "ok": False,
                "error": f"割り当てを書けませんでした（何も止めていません）: {e}",
            }
        # 持ち越すときは、元と同じモデル・モード・思考の深さで起こす（止める前に読む）
        carried_options = (
            sm.launch_options_of(running[0]["pid"]) if carry and running else {}
        )
        # 2. 動いているものを止める
        stopped, stop_errors = [], []
        for s in running:
            try:
                sm.stop_session(s["pid"])
                stopped.append(s["pid"])
            except sm.SessionError as e:
                # 一覧を取ってから止めるまでに自分で終わったものは、止まったものとして扱う
                if sm.find_session_by_pid(s["pid"]) is None:
                    stopped.append(s["pid"])
                    continue
                stop_errors.append(f"PID {s['pid']}: {e}")
        if stop_errors:
            # 止められないまま起こすと、同じ作業場で2つのアカウントが動く。割り当ては前へ戻す
            try:
                accounts.assign(path, project_key, previous, "move-rollback")
            except (accounts.AccountError, OSError) as e:
                stop_errors.append(f"割り当ても戻せませんでした: {e}")
            return 502, {
                "ok": False,
                "error": "動いているセッションを止められなかったので、移すのをやめました（割り当ては前に戻しました）: "
                + " / ".join(stop_errors),
                "stopped": stopped,
            }
        result = {
            "ok": True,
            "project": project_key,
            "previous": previous,
            "account": target["name"],
            "stopped": stopped,
            "mirror": assigned["mirror"],
            "session": None,
            "warning": _mirror_warning(assigned),
            "pid": None,
            "session_id": None,
            "carried": None,
            "copied_files": 0,
            "set_aside": [],
        }
        if carry:
            # 3a. 会話の記録を移す先の置き場へ新しく写す（止めた後なので記録はもう伸びない）
            try:
                copied = sm.copy_transcript(path, carried_id, src_home, dst_home)
                result["copied_files"] = copied["copied"]
                result["set_aside"] = copied["set_aside"]
            except sm.SessionError as e:
                # 写せないまま移す先で起こすと白紙の会話になる。割り当てを前へ戻し、前で続けられるようにする
                rollback = ""
                try:
                    accounts.assign(path, project_key, previous, "move-rollback")
                except (accounts.AccountError, OSError) as e2:
                    rollback = f"（割り当ても戻せませんでした: {e2}）"
                result.update(
                    {
                        "ok": False,
                        "error": f"前のセッションは止めましたが、会話の記録を写せませんでした: {e}。"
                        f"割り当ては前に戻しました{rollback}。resume: {carried_id} で起こせば前のアカウントで続きから動きます",
                    }
                )
                return 502, result
            result["carried"] = carried_id
        if message.get("launch", True) is False:
            return 200, result
        # 3. 移す先で起こす。持ち越すなら同じ会話の続き、持ち越さないなら新しい会話
        # （移す先に前の会話が残っていても、黙ってそれを続けない）。前の起動先セッションは引き継ぐ
        default_prompt = MOVE_CARRY_PROMPT if carry else MOVE_DEFAULT_PROMPT
        prompt = (message.get("prompt") or "").strip() or default_prompt.format(
            label=target["label"]
        )
        launch = {
            **carried_options,
            **{k: message.get(k) for k in LAUNCH_OPTION_KEYS if message.get(k)},
            "project": project_key,
            "prompt": prompt,
            "account": target["name"],
        }
        if message.get("chrome") is not None:
            # 明示した chrome は持ち越した値（元の --chrome）より勝つ
            launch["chrome"] = message.get("chrome")
        if carry:
            launch["resume"] = carried_id
            # 別アカウントの resume は claude.ai に新しい行を作る。題名を渡さないと自動の名前になる
            launch["remote_control"] = message.get("remote_control", True)
        else:
            launch["new"] = True
        if (
            not launch.get("win_session")
            and running
            and running[0].get("win_session") is not None
        ):
            launch["win_session"] = str(running[0]["win_session"])
        status, body = start_session_request(launch, source=source)
        result["session"] = body.get("session")
        session = body.get("session") or {}
        result["pid"] = session.get("pid") or body.get("pid")
        result["session_id"] = session.get("session_id")
        warnings = [w for w in (result["warning"], body.get("warning")) if w]
        result["warning"] = " / ".join(warnings) or None
        if status != 200:
            result.update(
                {
                    "ok": False,
                    "error": f"割り当ては {target['label']} へ移し、前のセッションも止めましたが、"
                    f"起こせませんでした: {body.get('error')}。原因を直してから「セッションを開始」で起こせば、"
                    f"{target['label']} で起きます（割り当ては移っています）"
                    + (
                        f"。claude.ai の行の題名を渡す --remote-control が原因かもしれません。外すなら "
                        f"POST /api/sessions に resume: {carried_id}・remote_control: false を付けて起こせます"
                        f"（そのときは一覧の名前が自動の名前になります）"
                        if launch.get("remote_control")
                        else ""
                    ),
                    "screen": body.get("screen"),
                }
            )
            return 502, result
        return 200, result
    finally:
        _move_lock.release()


@app.route("/api/accounts/move", methods=["POST"])
def api_move_account():
    """作業場のアカウントを移す（管理役・画面の両方から。管理者の札は要らない）。"""
    status, body = move_account_request(
        request.get_json(silent=True), source=_request_source()
    )
    print(
        f"[Accounts] move {body.get('project')} {body.get('previous')} -> "
        f"{body.get('account')} ok={body.get('ok')} stopped={body.get('stopped')}",
        flush=True,
    )
    broadcast_sessions()
    return jsonify(body), status


def resumable_request(project_key):
    """プロジェクトの過去の会話（resume の候補）を返す。(状態コード, 応答)"""
    if not isinstance(project_key, str) or not project_key:
        return 400, {
            "ok": False,
            "error": "project（プロジェクトキー）を指定してください",
            "project": project_key if isinstance(project_key, str) else None,
        }
    target_path = resolve_project_path(project_key)
    if not target_path:
        return 404, {
            "ok": False,
            "error": f"プロジェクトが見つかりません: {project_key}",
            "project": project_key,
        }
    try:
        items = sm.list_resumable(target_path)
    except Exception as e:
        print(f"Error listing resumable for {project_key}: {e}", flush=True)
        return 500, {
            "ok": False,
            "error": f"過去の会話を読めませんでした: {e}",
            "project": project_key,
        }
    return 200, {"ok": True, "project": project_key, "items": items}


@socketio.on("list_resumable")
def handle_list_resumable(message):
    """プロジェクトの過去の会話の一覧（開始モーダルの「続きから再開」用）。"""
    message = message if isinstance(message, dict) else {}
    _, body = resumable_request(message.get("project"))
    emit("resumable_list", body, room=request.sid)


@app.route("/api/projects/resumable/hide", methods=["POST"])
def api_hide_resumable():
    """過去の会話を resume の一覧から外す（試験で起こした会話など。記録は消さない）。

    ボディ: ``{"project": "<プロジェクトキー>", "session_id": "<会話の番号>"}``
    応答: 200 ``{"ok": true, "hidden": 今回外したか}`` / 400 指定の不備 / 404 プロジェクトが無い
    """
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    project_key = payload.get("project")
    target_path = (
        resolve_project_path(project_key) if isinstance(project_key, str) else None
    )
    if not target_path:
        return (
            jsonify(
                {"ok": False, "error": f"プロジェクトが見つかりません: {project_key}"}
            ),
            404,
        )
    try:
        hidden = sm.hide_resumable(target_path, payload.get("session_id"))
    except sm.SessionError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "hidden": hidden})


@app.route("/api/projects/resumable")
def api_list_resumable():
    """プロジェクトの過去の会話の一覧。``?project=<プロジェクトキー>``"""
    status, body = resumable_request(request.args.get("project"))
    return jsonify(body), status


def wait_for_session(pid, timeout=45):
    """起動した claude が自身のセッションを登録するまで待つ。

    claude.ai から操作するには、ブリッジ（bridgeSessionId）の登録まで
    済んでいる必要がある。登録が終わったセッションの情報を返す。

    Args:
        pid: 起動したプロセスID
        timeout: 待つ最大秒数

    Returns:
        セッション情報の辞書。時間内に登録されなければ None
    """
    deadline = time.time() + timeout
    latest = None
    while time.time() < deadline:
        socketio.sleep(1.5)
        for session in sm.list_sessions():
            if str(session.get("pid")) == str(pid):
                latest = session
                if session.get("url"):
                    session["project_key"] = project_key_for_path(session.get("cwd"))
                    return session
    if latest:
        latest["project_key"] = project_key_for_path(latest.get("cwd"))
    return latest


def stop_session_request(pid):
    """セッションの終了要求を処理し、(HTTP 状態コード, 応答) を返す。

    Socket.IO の stop_session と REST の POST /api/sessions/<pid>/stop の両方から使う。
    管理者として動く子は通常権限のプロセスからは止められないため、昇格して動く
    本アプリに止めさせる口として REST も用意する。
    """
    try:
        result = sm.stop_session(pid)
    except sm.SessionError as e:
        return 400, {"ok": False, "error": str(e)}
    except Exception as e:
        print(f"Error stopping session pid={pid}: {e}", flush=True)
        return 500, {"ok": False, "error": f"セッションを終了できませんでした: {e}"}

    print(f"[Session] Stopped pid={pid} (forced={result['forced']})", flush=True)
    return 200, {"ok": True, "pid": pid, "forced": result["forced"]}


@socketio.on("stop_session")
def handle_stop_session(message):
    """稼働中の claude セッションを終了させる。"""
    message = message if isinstance(message, dict) else {}
    status, body = stop_session_request(message.get("pid"))
    emit("session_stop_result", body, room=request.sid)
    if status == 200:
        broadcast_sessions()


@app.route("/api/sessions/<int:pid>/stop", methods=["POST"])
def api_stop_session(pid):
    """稼働中の claude セッションを終了させる（Socket.IO の stop_session と同じ処理）。

    応答: 200 ``{"ok": true, "pid", "forced"}`` / 400 稼働中の claude セッションでない・
    終了できない（権限不足など）/ 500 想定外のエラー
    """
    status, body = stop_session_request(pid)
    if status == 200:
        broadcast_sessions()
    return jsonify(body), status


@socketio.on("restart_system")
def handle_restart_system():
    """Webサーバー自身のプロセスを最新コードで再起動する。

    セッションは本アプリの子プロセスではないため、**再起動しても
    稼働中の claude セッションは巻き添えにならない**。
    """
    print("[System] Received restart request from client. Executing self-restart...")
    socketio.emit("system_restarting", {})
    socketio.sleep(0.5)

    # プロセスの置換による自己再起動
    os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    # 標準出力・標準エラーへ書けなくなっても落ちないようにし、同じ内容をファイルにも残す
    # （常駐の管理アプリが再起動すると出力のパイプの読み手が消え、print が OSError を出す。
    # 2026-10-22 に予約が 500 を返し、自動の区切りの見回りが止まった）
    safe_console.install(os.path.join(sm.APP_STATE_DIR, "server.log"))

    # プロジェクトのメタデータを保存するSQLiteテーブルを初期化する
    try:
        pm.init_db()
    except Exception as e:
        print(f"Failed to initialize project database: {e}")

    # アカウントの割り当ての台帳が無ければ、写し（trial_roles.json）から一度だけ作る
    try:
        report = accounts.import_trial_roles(find_role_folders)
        print(f"[Accounts] import: {report}", flush=True)
        note = accounts.refresh_mirror_note()
        if note.get("written") or note.get("error"):
            print(f"[Accounts] 写しの注記: {note}", flush=True)
        mirror = accounts.mirror_status()
        if mirror.get("in_sync") is False:
            print(f"[Accounts] trial_roles.json が台帳と違います: {mirror}", flush=True)
    except Exception as e:
        print(f"[Accounts] 台帳の準備に失敗しました: {e}", flush=True)

    # セッション一覧の定期配信を開始する（状態 idle/busy の追従用）
    socketio.start_background_task(sessions_poll_loop)

    # 今週の使用量の集計を回す（1回目は週の始まりからの分を読むので数秒かかる）
    socketio.start_background_task(usage_refresh_loop)

    # 管理下のセッションの自動の区切り（設定 auto_clear_mode。既定は dry_run＝記録だけ）
    socketio.start_background_task(auto_clear_loop)

    # 枠が戻った子への続きの依頼・管理役への知らせ（自動の送信の失敗は送った履歴から拾う）
    send_history.listeners.append(notices.on_history)
    socketio.start_background_task(watch_loop)

    # 機械の口の預かり（再起動の前に受けて、まだ入れていないもの）の続きを入れる
    try:
        if machine_send.ensure_worker():
            print("[MachineSend] 預かりの続きを入れ始めました", flush=True)
    except Exception as e:
        print(f"[MachineSend] 預かりを読めませんでした: {e}", flush=True)

    # ホスト・ポートは config/settings.json から読み込む（他アプリとのポート競合時に編集可能）。
    # 外（Tailscale）から開くときは host を 0.0.0.0 にし、allowed_networks・auth_token で絞る。
    settings = STARTUP_SETTINGS
    host = settings.get("host", DEFAULT_SETTINGS["host"])
    port = settings.get("port", DEFAULT_SETTINGS["port"])
    print("\n" + "=" * 80)
    print(" [Claude Code Session Manager] Server Started!")
    print(f" Settings File: {SETTINGS_FILE}")
    print(f" Machine: {machine_name(settings)}  Listen: {host}:{port}")
    print(f" Projects: {DEVELOPING_DIR} / {WORKING_DIR}")
    print(f" Access URL: http://localhost:{port}/")
    print(
        f" Allowed networks: {[str(n) for n in guard.networks] or 'all'}"
        f"  Token: {'required' if guard.token_required else 'none'}"
    )
    listen_local = host == "localhost" or access_guard.is_loopback(host)
    if not listen_local and not guard.token_required:
        reach = (
            "allowed_networks の範囲の人は誰でも"
            if guard.networks
            else "届く人は誰でも（Tailscale 等の手前の守りだけが頼り）"
        )
        print(
            f" [注意] ループバック以外で待ち受けていて、合言葉（auth_token）がありません。"
            f"{reach}セッションを操作できます（README の「安全のための注意」）"
        )
    if MACHINE_SOURCES:
        print(f" Machine-send sources: {[str(n) for n in MACHINE_SOURCES]}")
    broad = machine_send.broad_sources(MACHINE_SOURCES)
    if broad:
        print(
            f" [注意] machine_send.sources に広い範囲があります: {[str(n) for n in broad]}。"
            "この範囲の接続元は画面もほかの API も使えません（送り手の機械を1台ずつ /32 で書く）"
        )
    for label, path in (
        ("developing_dir", DEVELOPING_DIR),
        ("working_dir", WORKING_DIR),
    ):
        if not path:
            print(
                f" [警告] {label} が未設定です（{SETTINGS_FILE} に書くまで、その置き場の"
                "プロジェクトは一覧に出ず、作成・移動もできません。README の「設定」）"
            )
        elif not os.path.isdir(path):
            print(f" [警告] {label} のフォルダがありません: {path}")
    print("=" * 80 + "\n")

    # 標準スレッドベースでWebサーバーを起動
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)

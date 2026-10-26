# -*- coding: utf-8 -*-
"""外から開くときの入口の守り（接続元の絞り込みとトークン認証）。

本アプリはセッションの起動・送信・ログインの差し替えまでできるため、開発に使う PC 以外の機械
（公開の側の機械等）で動かすときは「誰でも開ける」状態にしない。設定（config/settings.json）で
次の2つを使えるようにする。どちらも空なら今までどおり（設定ファイルが守りを書かない機械の動きは変えない）。

    allowed_networks  接続を受け付ける相手のアドレスの範囲（CIDR の並び）。
                      例: ["100.64.0.0/10"]（Tailscale）。ループバックはいつも通す。
    auth_token        外（ループバック以外）から使うときに要る合言葉。16文字以上。
                      ブラウザは一度 ``/?token=<合言葉>`` か入口の画面で入れると、
                      合言葉から作った印（クッキー）を覚える。API は
                      ``Authorization: Bearer <合言葉>`` か ``X-Claude-Web-App-Token`` で渡す。

ループバック（127.0.0.1・::1）からの接続は合言葉なしで通す。区切りの予約
（request_self_clear.py）や claude-session-resumer は同じ機械の 127.0.0.1 から呼び、
予約の本人確認も接続の持ち主で行う（self_clear.caller_pid）ため。
ただし中継（tailscale serve・リバースプロキシ）を通った接続は、相手がループバックに見えても
中継の印（X-Forwarded-For 等のヘッダー）があれば外からの接続として扱う。

URL で渡した合言葉（``?token=``）は Werkzeug のアクセスログに URL ごと書かれるため、
``install_log_redaction`` でログの中の ``token=`` の値を伏せる。
"""

import hashlib
import hmac
import ipaddress
import logging
import re

# ブラウザに覚えさせる印のクッキーの名前と、API で合言葉を渡すヘッダー
COOKIE_NAME = "claude_web_app_auth"
TOKEN_HEADER = "X-Claude-Web-App-Token"

# 合言葉の最短の長さ。短い合言葉は総当たりで破られうるため、設定の時点で断る
MIN_TOKEN_LENGTH = 16

# 印のクッキーの有効期間（秒）。ブラウザの上限（400日）に合わせる
COOKIE_MAX_AGE_SEC = 400 * 24 * 60 * 60


# 中継を通ったことを示すヘッダー（どれか1つでもあればループバックとして扱わない）
PROXY_HEADERS = (
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Real-IP",
    "Forwarded",
    "Tailscale-User-Login",
)

# ログの中の合言葉（URL の token= の値）
_TOKEN_IN_URL_RE = re.compile(r"([?&]token=)[^&\s\"']*", re.IGNORECASE)


class AccessConfigError(ValueError):
    """入口の守りの設定が不正（起動時に止めて直してもらう）。"""


def _parse_address(addr):
    """接続元の文字列をアドレスにする（IPv4 射影の IPv6 は IPv4 に直す）。読めなければ None。"""
    if not addr:
        return None
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def is_loopback(addr):
    """ループバック（同じ機械）からの接続か。"""
    ip = _parse_address(addr)
    return bool(ip and ip.is_loopback)


def is_proxied(headers):
    """中継（tailscale serve・リバースプロキシ）を通った接続か。"""
    return any(headers.get(name) for name in PROXY_HEADERS)


# 同じ機械からの直の接続として認める Host の名前（DNS リバインディング対策）
LOCAL_HOST_NAMES = ("127.0.0.1", "localhost", "[::1]", "::1")


def _host_name(host):
    """Host ヘッダーからポートを除いた名前（小文字）。"""
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_local(addr, headers):
    """同じ機械からの直の接続か（ループバックで、中継の印が無く、Host もループバックの名前）。

    Host を見るのは、悪いページが自分の名前を 127.0.0.1 へ向け直して（DNS リバインディング）
    同じ機械のブラウザから呼ぶのを、合言葉なしで通さないため。
    """
    if not is_loopback(addr) or is_proxied(headers):
        return False
    return _host_name(headers.get("Host")) in LOCAL_HOST_NAMES


def redact_token(text):
    """文字列の中の URL の ``token=`` の値を伏せる。"""
    return _TOKEN_IN_URL_RE.sub(r"\1***", text)


class TokenRedactFilter(logging.Filter):
    """ログの1行から URL の合言葉を伏せる（Werkzeug のアクセスログ用）。"""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_token(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_log_redaction(logger_name="werkzeug"):
    """指定のロガーに伏せ字のフィルタを付ける（二重には付けない）。"""
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, TokenRedactFilter) for f in logger.filters):
        logger.addFilter(TokenRedactFilter())
    return logger


def parse_networks(values):
    """設定の allowed_networks を範囲の並びにする。

    Args:
        values: None・空・CIDR 文字列の並び（"100.64.0.0/10" や "192.168.1.0/24"）

    Raises:
        AccessConfigError: 並びでない・読めない範囲が混じっている場合（読めたものだけで
            動くと、思ったより広く・狭く通してしまうため、起動の時点で断る）
    """
    if values in (None, "", []):
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise AccessConfigError(
            'allowed_networks は CIDR の並びで書いてください（例: ["100.64.0.0/10"]）'
        )
    networks = []
    for value in values:
        try:
            networks.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            raise AccessConfigError(
                f'allowed_networks の範囲が読めません: {value!r}（例: "100.64.0.0/10"）'
            )
    return networks


class AccessGuard:
    """接続元の絞り込みと合言葉の照合をまとめたもの。設定は起動時に1回読む。"""

    def __init__(self, token="", networks=None):
        token = token or ""
        if not isinstance(token, str):
            raise AccessConfigError("auth_token は文字列で書いてください")
        token = token.strip()
        if token and len(token) < MIN_TOKEN_LENGTH:
            raise AccessConfigError(
                f"auth_token が短すぎます（{MIN_TOKEN_LENGTH}文字以上にしてください）"
            )
        self.token = token
        self.networks = parse_networks(networks)

    @property
    def token_required(self):
        """外からの接続に合言葉が要るか。"""
        return bool(self.token)

    @property
    def active(self):
        """何か1つでも守りが効いているか（どちらも空なら今までどおり素通し）。"""
        return bool(self.token or self.networks)

    def network_allowed(self, addr):
        """接続元が受け付ける範囲にあるか。範囲の指定が無ければ常に真。"""
        if not self.networks:
            return True
        ip = _parse_address(addr)
        if ip is None:
            return False
        if ip.is_loopback:
            return True
        return any(ip.version == net.version and ip in net for net in self.networks)

    def token_matches(self, value):
        """渡された合言葉が設定と一致するか（時間差で漏れない比べ方）。"""
        if not self.token or not isinstance(value, str) or not value:
            return False
        return hmac.compare_digest(value.strip().encode(), self.token.encode())

    def cookie_value(self):
        """ブラウザに覚えさせる印。合言葉そのものはクッキーに置かない。"""
        return hmac.new(
            self.token.encode(), b"claude-web-app-cookie-v1", hashlib.sha256
        ).hexdigest()

    def cookie_matches(self, value):
        if not self.token or not isinstance(value, str) or not value:
            return False
        return hmac.compare_digest(value.encode(), self.cookie_value().encode())

    def presented_token(self, headers):
        """ヘッダーから渡された合言葉を取り出す（無ければ空文字）。"""
        auth = headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return (headers.get(TOKEN_HEADER) or "").strip()

    def auth_kind(self, addr, headers, cookies):
        """何で通したかを返す（通せないなら None）。

        "open"（合言葉なしの設定）・"local"（同じ機械の直の接続）・
        "header"（ヘッダーの合言葉）・"cookie"（ブラウザの印）。
        """
        if not self.network_allowed(addr):
            return None
        if not self.token:
            return "open"
        if is_local(addr, headers):
            return "local"
        if self.token_matches(self.presented_token(headers)):
            return "header"
        if self.cookie_matches(cookies.get(COOKIE_NAME)):
            return "cookie"
        return None

    def authorized(self, addr, headers, cookies):
        """この接続を通してよいか（範囲と合言葉の両方）。"""
        return self.auth_kind(addr, headers, cookies) is not None


def _first(value):
    """カンマ区切りのヘッダーの最初の値。"""
    return (value or "").split(",", 1)[0].strip()


def own_origin(headers, scheme, host):
    """この要求から見た自分のオリジン（中継越しなら中継の外側の名前）。

    tailscale serve 等の中継は X-Forwarded-Proto・X-Forwarded-Host を付けるので、
    中継の印があるときはそれを使う（Socket.IO の engineio と同じ考え方）。
    """
    if is_proxied(headers):
        scheme = _first(headers.get("X-Forwarded-Proto")) or scheme
        host = _first(headers.get("X-Forwarded-Host")) or host
    return f"{scheme}://{host}".lower()


def cross_origin_refused(kind, headers, origin):
    """状態を変える要求（GET 以外）を、別のページから来たとして断るか。

    クッキーはポートで分かれず、SameSite もポートを区別しないため、同じ IP の別ポートの
    アプリのページからの POST にも印が付いて届く。範囲だけで守るときも、範囲の中の
    スマホで開いた悪いページが単純なフォームで POST できる。そこで:
      - Origin があれば、自分のオリジン（origin）と同じときだけ通す
      - Origin が無く Sec-Fetch-Site が cross-site・same-site なら断る
      - 印（kind == "cookie"）で通したものは、どちらも無ければ断る
    ヘッダーの合言葉・同じ機械の直の接続はここに来ない（呼ぶ側で除く）。
    """
    sent = (headers.get("Origin") or "").strip().rstrip("/").lower()
    if sent:
        return sent != (origin or "").strip().rstrip("/").lower()
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in ("cross-site", "same-site"):
        return True
    if kind == "cookie":
        return fetch_site not in ("same-origin", "none")
    return False


def from_settings(settings):
    """設定の dict から AccessGuard を作る（不正なら AccessConfigError）。"""
    settings = settings or {}
    return AccessGuard(
        token=settings.get("auth_token") or "",
        networks=settings.get("allowed_networks"),
    )

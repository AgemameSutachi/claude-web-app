# -*- coding: utf-8 -*-
"""自分のセッションの区切り（/clear と引き継ぎの読み直し）を claude-web-app に予約する道具。

本体の claude セッションが、自分の Bash / PowerShell ツールから直接打つ:

    <python> <claude-web-app のフォルダ>/request_self_clear.py
    <python> <claude-web-app のフォルダ>/request_self_clear.py --handoff C:/.../handoff.md

--handoff を省くと、その会話のフォルダ（cwd）直下の NOW.md を引き継ぎにする。NOW.md が無く
README.md があれば README.md（移行中の後ろ盾）。どちらを使ったかは claude-web-app の記録に残る。
予約を入れたらすぐ終わる（待たない）。セッションはそのまま追加の作業をせずにターンを終える。
ターンが終わって入力待ちが10秒続くと、claude-web-app が /clear を打ち、続けて
「区切りの /clear が済みました。引き継ぎファイル「<パス>」を読んで、続きから進めてください。」を送る。

- どのセッションかは claude-web-app が接続から決める。pid や sessionId を渡す必要はなく、
  ほかのセッションは区切れない。
- サブエージェントからは打てない（403 not_main。打つと本体の会話が区切られるため）。
- 予約の後に人・ほかのセッション・タスクの通知から新しい仕事が来たら、区切りは取りやめになる。
- 取り消すときは --cancel。
標準ライブラリだけで動く（どのプロジェクトの Python からでも打てる）。

終了コード: 0 予約できた・取り消した / 1 断られた・つながらない（この呼び出しでは予約していない。
前に予約していればそれは生きている。応答が無かったときは「分からない」と出る）/
2 引数の誤り / 3 既に予約がある（前の予約が生きている）
"""

import argparse
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request

DEFAULT_PORT = 5002
SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "settings.json"
)
REQUEST_TIMEOUT_SEC = 15

# ドライブ文字の直後に区切りが無いパス（Bash が \ を消した形）
DRIVE_RELATIVE_PATTERN = re.compile(r"[A-Za-z]:[^\\/]")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ALREADY_RESERVED = 3

# Git Bash 形式のパス（/c/Users/…）。Windows の Python では別の場所を指す
GIT_BASH_PATH_PATTERN = re.compile(r"/[A-Za-z]/")

# 断られた・つながらないときに添える次の一手（前の予約が生きている場合もあるので言い切らない）
NOT_RESERVED_HINT = (
    "この呼び出しでは区切りを予約していません（前に予約していなければ /clear はされません）。"
    "区切らずにこのまま作業を続けてかまいません。"
)


def app_port():
    """claude-web-app の待ち受けポート（設定が読めなければ既定の 5002）。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port") or DEFAULT_PORT)
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_PORT


def _call(base, method, body=None):
    """claude-web-app を呼び、(HTTP 状態コード, 応答の JSON) を返す。"""
    request = urllib.request.Request(
        base + "/api/sessions/request-clear",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    # 手元のアプリへ直接つなぐ（プロキシを通すと接続の持ち主を確かめられない）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SEC) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"error": str(e), "code": None}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        description=(
            "自分のセッションの /clear と引き継ぎの読み直しを予約する（すぐ終わる）。"
            "本体のセッションの Bash / PowerShell から直接打つ（サブエージェントからは打てない）。"
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--handoff",
        help="引き継ぎファイルのパス（Windows 形式。例 C:/work/handoff.md。"
        "相対なら今のフォルダから。/c/... の形は使えない）。"
        "省くと、この会話のフォルダ直下の NOW.md（無ければ README.md）",
    )
    action.add_argument("--cancel", action="store_true", help="予約を取り消す")
    parser.add_argument(
        "--wait-sec",
        type=int,
        default=None,
        help="ターンが終わるのを待つ上限（秒。既定 1800。60 未満は 60、3600 超は 3600 に丸める）",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="claude-web-app の URL（既定 http://127.0.0.1:<config/settings.json の port>）",
    )
    args = parser.parse_args(argv)
    base = (args.url or f"http://127.0.0.1:{app_port()}").rstrip("/")

    if args.cancel:
        method, body = "DELETE", None
    elif args.handoff is None:
        # 既定の引き継ぎ（会話の cwd 直下の NOW.md、無ければ README.md）はサーバーが選ぶ。
        # シェルの今のフォルダは cd で動いていることがあるため、ここでは探さない
        method, body = "POST", {}
        if args.wait_sec is not None:
            body["wait_sec"] = args.wait_sec
    else:
        if DRIVE_RELATIVE_PATTERN.match(args.handoff):
            # 例「C:Usersname…」: 引用符なしの Bash で C:\Users\… の \ が消えた形
            print(
                f"引き継ぎファイルのパスの区切りが消えています: {args.handoff}"
                "（Bash では \\ が消えます）。C:/work/handoff.md のように / 区切りで渡すか、"
                f"引用符で囲んで打ち直してください。{NOT_RESERVED_HINT}",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if GIT_BASH_PATH_PATTERN.match(args.handoff):
            print(
                f"引き継ぎファイルのパスが Git Bash の形です: {args.handoff}。"
                "C:/work/handoff.md のような Windows の形で打ち直してください。"
                + NOT_RESERVED_HINT,
                file=sys.stderr,
            )
            return EXIT_REFUSED
        handoff = os.path.abspath(args.handoff)
        if not os.path.isfile(handoff):
            print(
                f"引き継ぎファイルが見つかりません: {handoff}。{NOT_RESERVED_HINT}"
                "区切るなら、引き継ぎを書いてから打ち直してください。",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        method, body = "POST", {"handoff": handoff}
        if args.wait_sec is not None:
            body["wait_sec"] = args.wait_sec

    try:
        status, reply = _call(base, method, body)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Python 3.9 以前では socket.timeout が TimeoutError の仲間でないので両方見る
        timeouts = (TimeoutError, socket.timeout)
        if isinstance(e, timeouts) or isinstance(getattr(e, "reason", None), timeouts):
            # 送った後に応答だけ来なかった。サーバーでは済んでいるかもしれない
            print(
                f"claude-web-app から {REQUEST_TIMEOUT_SEC} 秒以内に応答がなく、"
                f"{'取り消せた' if args.cancel else '予約できた'}かは分かりません。"
                "少し待って同じコマンドを打ち直すと分かります"
                "（予約なら終了コード 3＝予約済み、取り消しなら「予約はありません」）。",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print(
            f"claude-web-app へつなげませんでした（{base}）: {e}。{NOT_RESERVED_HINT}"
            "（claude-web-app が止まっている可能性があります）",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    code = reply.get("code")
    if status == 404:
        print(
            "claude-web-app がこの機能より古い版です（/api/sessions/request-clear が無い）。"
            + NOT_RESERVED_HINT,
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if code == "already_reserved":
        print(f"{reply.get('error')}（already_reserved）", file=sys.stderr)
        return EXIT_ALREADY_RESERVED
    if status >= 300 or not reply.get("ok"):
        hint = (
            "取り消せていません（予約していたなら、それは生きています）。"
            if args.cancel
            else NOT_RESERVED_HINT
        )
        if code == "not_main" and not args.cancel and args.handoff is None:
            # 再起動前の古い本体は --handoff を省いた呼び出しを本人と認めない（not_main になる）
            hint += (
                "サブエージェントから打っていないなら、claude-web-app が再起動前の古い版で、"
                "--handoff を省いた呼び出しを認めていない可能性があります。"
                "--handoff <NOW.md か README.md の絶対パス（/ 区切り）> を付けて打ち直してください。"
            )
        print(
            f"断られました（HTTP {status}・{code}）: {reply.get('error')}。{hint}",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    result = reply.get("result") or {}
    if args.cancel:
        if result.get("sending"):
            print(
                "既に /clear を送り始めていて取り消せませんでした（このあと区切られます）。",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        print(
            "予約を取り消しました。区切られません。"
            if result.get("cancelled")
            else "取り消す予約はありませんでした。"
        )
        return EXIT_OK
    source_note = {
        "now": "（--handoff を省いたので NOW.md を使います）",
        "readme": "（--handoff を省き、NOW.md が無いので README.md を使います）",
    }.get(result.get("handoff_source"), "")
    quiet_until = result.get("quiet_until")
    if quiet_until:
        # 人の最後の発言から10分たつまでは区切らない（self_clear.HUMAN_QUIET_SEC）
        when = f"ターンが終わっても、人の最後の発言から10分たつ {quiet_until} までは待ってから /clear され"
        cancel_window = "それまでに"
    else:
        when = "ターンが終わって入力待ちが10秒続くと /clear され"
        cancel_window = "ターンを終える前に"
    print(
        f"区切りを予約しました{source_note}"
        f"（{result.get('name') or '名前なし'}・pid {result.get('pid')}・"
        f"{result.get('deadline')} まで待つ）。"
        "このあと追加の作業をせずに、短い完了報告でターンを終えてください"
        "（「続けます」「進めます」で終えない。継続のフックに差し戻されて作業が増えるため）。"
        f"{when}、新しい会話に"
        f"「{result.get('handoff')}」を読む指示が届きます。"
        "予約の後に作業を足したら、終える前に引き継ぎファイルを書き直してください。"
        f"{cancel_window}人・ほかのセッション・通知から新しい依頼が来たら区切りは取りやめになります"
        "（区切りたければ、その仕事を終えてから打ち直してください）。"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

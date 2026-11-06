"""動いている子を止め、同じアカウント・同じ会話のまま、claude.ai の行の題名を付けて起こし直す台本。

別のアカウントへ持ち越した会話は claude.ai の Remote Control の行が新しく作られ、題名が自動の名前
（<機械名>-<ランダム2語>）になる（2026-10-28 実測）。この台本は本アプリ（5002）の口だけを使い、
  1. 本アプリが remote_control（題名を --remote-control で渡す）を受け付ける版かを確かめる（古い版なら何もしない）
  2. 一覧で相手の pid・会話・アカウントが合っていて、入力待ち（idle）かを確かめる（作業中なら何もしない）
  3. 元のモデル・権限モード・思考の深さと起動先を読んでから止める
  4. 同じ会話を resume・remote_control: true で起こし、最初の一言を渡す
  5. 一覧に同じ会話の行が出て claude.ai の url が付くまで待ち、名前と url を出す

使い方（claude-web-app の venv で・5002 と同じ機械）:
  python resume_with_name.py --project <キー> --pid <pid> --session-id <会話の番号> --account <アカウント名> [--dry-run]

--dry-run は 1〜3 の確かめと読み取りだけをして、止めも起こしもしない。
結果は標準出力に JSON で1行ずつ。終了コード:
  0 起こし、claude.ai の行が新しくなった（url が替わった）
  1 起こせなかった（止めた後なら、題名なしで起こし直す1行を出す）
  2 確かめで止めた（何も変えていない）
  3 起こしたが、claude.ai の行は前と同じ（同じアカウントの resume は前の行につなぎ直す。題名は変わっていない見込み）
  4 起こしたが、url の付くのを確かめられなかった（新しい pid は出す）
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_manager as sm  # noqa: E402

BASE = "http://127.0.0.1:5002"
DEFAULT_PROMPT = "名前を直すための起こし直しです。返事は不要です。"

# 起こした子が一覧に出て claude.ai の url が付くまで待つ上限（秒）と間隔
REGISTER_TIMEOUT_SEC = 120
POLL_SEC = 3


def call(method, path, body=None, timeout=300):
    """5002 を呼び、(状態コード, JSON) を返す。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {"error": str(e)}
    except (urllib.error.URLError, OSError) as e:
        # 繋がらない・時間切れ。止めた後でも戻し方の1行を出せるよう、例外にせず返す
        return 0, {"error": f"{type(e).__name__}: {e}"}


def say(step, **fields):
    print(json.dumps({"step": step, **fields}, ensure_ascii=False), flush=True)


def rows():
    _, body = call("GET", "/api/sessions")
    return body.get("sessions", [])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="プロジェクトキー")
    parser.add_argument("--pid", type=int, required=True, help="いま動いている子の pid")
    parser.add_argument("--session-id", required=True, help="続ける会話の番号")
    parser.add_argument(
        "--account", required=True, help="いまのアカウント名（変えない）"
    )
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="起こした後の最初の一言"
    )
    parser.add_argument("--dry-run", action="store_true", help="確かめと読み取りだけ")
    args = parser.parse_args()

    # 1. 古い版の本アプリは remote_control を黙って無視する（題名の無いまま起こしてしまう）
    status, spec = call("GET", "/api/integration/spec")
    if (spec.get("launch") or {}).get("remote_control_key") != "remote_control":
        say(
            "refused",
            reason="本アプリが remote_control を受け付ける版ではありません（再起動が要る）。何も変えていません",
            spec_status=status,
            spec_version=spec.get("spec_version"),
        )
        return 2

    # 2. 相手を確かめる
    row = next((s for s in rows() if s.get("pid") == args.pid), None)
    problems = []
    if not row:
        problems.append(f"pid {args.pid} が一覧にありません")
    else:
        if row.get("session_id") != args.session_id:
            problems.append(
                f"会話が {row.get('session_id')} です（{args.session_id} のはず）"
            )
        if row.get("account") != args.account:
            problems.append(
                f"アカウントが {row.get('account')} です（{args.account} のはず）"
            )
        if row.get("win_session") is not None and str(row["win_session"]) not in {
            v for v, _ in sm.WIN_SESSION_CHOICES if v
        }:
            # 本アプリが起こせるのはセッション0と1だけ。止めた後に断られるので先に断る
            problems.append(
                f"起動先がセッション {row['win_session']} です（0 か 1 だけ起こせる）"
            )
        if row.get("status") != "idle":
            problems.append(
                f"状態が {row.get('status')} です（入力待ちのときだけ起こし直す）"
            )
    if not problems:
        # 止めた後に断られないよう、作業場の会話と割り当てのアカウントも止める前に照らす
        _, resumable = call("GET", f"/api/projects/resumable?project={args.project}")
        ids = {it.get("session_id") for it in resumable.get("items") or []}
        if args.session_id not in ids:
            problems.append(
                f"{args.project} の会話の一覧に {args.session_id} がありません（project の取り違え？）"
            )
        _, info = call("GET", f"/api/accounts?project={args.project}")
        assigned = (info.get("project") or {}).get("account")
        if assigned != args.account:
            problems.append(
                f"{args.project} の割り当ては {assigned} です（{args.account} のはず）"
            )
    if problems:
        say("refused", reason=" / ".join(problems) + "。何も変えていません")
        return 2

    # 3. 元の起動オプションと起動先・権限（止める前に読む）
    dropped = []
    options = sm.launch_options_of(args.pid, dropped)
    if row.get("win_session") is not None:
        options["win_session"] = str(row["win_session"])
    if row.get("elevated") is not None:
        options["elevation"] = "admin" if row["elevated"] else "standard"
    before = {k: row.get(k) for k in ("pid", "name", "url", "account", "session_id")}
    say("checked", before=before, options=options, not_carried=dropped)
    if args.dry_run:
        say("dry_run", note="止めも起こしもしていません")
        return 0

    status, body = call("POST", f"/api/sessions/{args.pid}/stop", {})
    if status != 200 or not body.get("ok"):
        say(
            "refused",
            reason=f"止められませんでした（{status}）: {body}。何も変えていません",
        )
        return 2
    say("stopped", pid=args.pid)

    # 4. 同じ会話を題名つきで起こす
    request = {
        **options,
        "project": args.project,
        "resume": args.session_id,
        "account": args.account,
        "remote_control": True,
        "prompt": args.prompt,
    }
    status, body = call("POST", "/api/sessions", request)
    if status != 200 or not body.get("ok"):
        # 題名なし＝remote_control: false（キーを外すだけでは、resume の既定で題名がまた付く）
        fallback = dict(request, remote_control=False)
        say(
            "failed",
            status=status,
            error=body.get("error"),
            pid=body.get("pid"),
            screen=body.get("screen"),
            restore=f"題名なしで起こし直すなら POST {BASE}/api/sessions に "
            + json.dumps(fallback, ensure_ascii=False)
            + "（502 や時間切れのときは子が動いていることがある。先に GET /api/sessions で会話の番号を探す）",
        )
        return 1

    # 5. 一覧に出て url が付くまで待つ
    new_pid = (body.get("session") or {}).get("pid")
    deadline = time.time() + REGISTER_TIMEOUT_SEC
    found = None
    while time.time() < deadline:
        found = next(
            (s for s in rows() if s.get("pid") == new_pid and s.get("url")), None
        )
        if found:
            break
        time.sleep(POLL_SEC)
    if not found:
        say(
            "started_unconfirmed",
            pid=new_pid,
            session=body.get("session"),
            note=f"{REGISTER_TIMEOUT_SEC} 秒待っても claude.ai の url が付きません（子は起きている）",
        )
        return 4
    after = {k: found.get(k) for k in ("pid", "name", "url", "account", "session_id")}
    if after["url"] == before["url"]:
        say(
            "started_same_row",
            after=after,
            note="claude.ai の行は前と同じ（前の行につなぎ直した）。題名は変わっていない見込み。一覧で確かめる",
        )
        return 3
    say(
        "started",
        after=after,
        note="claude.ai の一覧でこの url の行の名前を人が見る（機械で読む公式の手段は無い）",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

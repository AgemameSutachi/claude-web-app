"""動いている子を、同じ会話のまま claude.ai の新しい行（題名＝手元の名前）で同じアカウントへ戻す台本。

名前を明示せずに作られた claude.ai の Remote Control の行は、発言から題名が付け替わり、同じアカウントで
起こし直しても前の行へつなぎ直すだけで題名は直らない（2026-10-28 実測・resume_with_name.py の終了 3）。
別のアカウントの resume は新しい行を作り、本アプリはそのとき題名を渡す（54339f0・b379c1b。練習の
names 通しで確かめた）。そこで本アプリ（5002）の口だけを使い、
  1. 本アプリが題名（remote_control）と持ち越し（carry_session）を受け付ける版かを確かめる
  2. 一覧で相手の pid・会話・アカウントが合っていて、入力待ち（idle）かを確かめる。割り当て・会話の一覧・
     経由するアカウントへ会話記録を写せるかも、変える前に確かめる（作業中・食い違いなら何もしない）
  3. 経由するアカウント（--via）へ会話を持ち越す（新しい行・題名つき）
  4. 元のアカウント（--account）へ持ち帰る（もう一度新しい行・題名つき）
各段で、一覧の url と会話記録の最後の bridge-session がそろい、前の段と違う行で持ち主が替わったかを確かめる。

古い行（元の行・経由した行）は claude.ai の一覧に切れた行として残る（機械で消す公式の手段は無い）。

使い方（claude-web-app の venv で・5002 と同じ機械）:
  python fresh_named_row.py --project <キー> --pid <pid> --session-id <会話の番号> --account <いまのアカウント>
      --via <経由するアカウント> [--dry-run]

--dry-run は 1〜2 の確かめと読み取りだけをして、止めも写しもしない。
結果は標準出力に JSON で1行ずつ。終了コード:
  0 戻し終え、claude.ai の行が新しくなった（最後の url を出す）
  1 途中で外れた（どこで・いまどのアカウントで動いているか・戻し方を出す）
  2 確かめで止めた（何も変えていない）
"""

import argparse
import json
import os
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts  # noqa: E402
import migration_drill as drill  # noqa: E402
import session_manager as sm  # noqa: E402

DEFAULT_PROMPT = "名前を直すための起こし直しです。返事は不要です。"


def say(step, **fields):
    print(json.dumps({"step": step, **fields}, ensure_ascii=False), flush=True)


class Unreachable(drill.DrillError):
    """5002 に繋がらない・応答を待ちきれなかった（5002 の側はまだ処理中かもしれない）。"""


def call(method, path, body=None, timeout=300):
    """drill.call と同じ。繋がらない・時間切れも DrillError にそろえる（止めた後でも戻し方を出すため）。"""
    try:
        return drill.call(method, path, body, timeout)
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(
            f"5002 に繋がりません（{method} {path}）: {type(e).__name__}: {e}"
        )


def carry(account, args):
    try:
        return drill.carry(args.project, account, args.prompt)
    except (urllib.error.URLError, OSError) as e:
        raise Unreachable(
            f"{account} への持ち越しの応答を受け取れません（子は止まった後かもしれない）: "
            f"{type(e).__name__}: {e}"
        )


def rows():
    _, body = call("GET", "/api/sessions")
    return body.get("sessions", [])


def answers(records):
    """assistant の記録の件数（文の無い返答も数える。「返事は不要」の一言には文を出さないことがある）。"""
    return sum(1 for r in records if r.get("type") == "assistant")


def wait_registered(account, cwd, session_id, pid, before):
    """新しい pid の行が一言への返答を終えて入力待ちになり、url を持ち、会話記録の bridge-session と
    url がそろうまで待つ。before は持ち越す前の返答の件数（一言を読む前の入力待ちで次へ進まないため）。
    """
    deadline = time.time() + drill.IDLE_TIMEOUT_SEC
    row, records = None, []
    while time.time() < deadline:
        row = next((s for s in rows() if s.get("pid") == pid), None)
        try:
            records = drill.read_records(account, cwd, session_id)
        except drill.DrillError:
            records = []
        if (
            row
            and answers(records) > before
            and row.get("status") == "idle"
            and row.get("url")
            and drill.last_bridge(records)[0] == drill.url_body(row.get("url"))
        ):
            return row, records
        time.sleep(drill.POLL_SEC)
    raise drill.DrillError(
        f"{drill.IDLE_TIMEOUT_SEC} 秒待っても pid {pid} が返答・入力待ち・claude.ai の登録にそろいません。"
        f"最後に見た行: {row}・返答 {answers(records)} 件（{before} 件より増えるはず）・"
        f"記録の最後の bridge {drill.last_bridge(records)[0]}"
    )


def preflight(args):
    """変える前の確かめ。(一覧の行, 作業場のパス, 元の行の持ち主, 問題の並び) を返す。

    5002 に繋がらないときは DrillError（呼び手が「何も変えていない」で止める）。
    """
    problems = []
    status, spec = call("GET", "/api/integration/spec")
    # 持ち越しの鍵は spec の accounts の値に carry_session があるかで見る
    if (spec.get("launch") or {}).get(
        "remote_control_key"
    ) != "remote_control" or "carry_session" not in (
        spec.get("accounts") or {}
    ).values():
        problems.append(
            f"本アプリが題名と持ち越しを受け付ける版ではありません（spec {status}・版 {spec.get('spec_version')}）"
        )
    listed = rows()
    row = next((s for s in listed if s.get("pid") == args.pid), None)
    if not row:
        return None, None, None, problems + [f"pid {args.pid} が一覧にありません"]
    cwd = row.get("cwd") or ""
    same_place = [s for s in listed if sm._same_path(s.get("cwd") or "", cwd)]
    if len(same_place) > 1:
        # 5002 も 409 で断るが、変える前に「何も変えていない」で止める
        problems.append(
            f"同じ作業場で {len(same_place)} 本動いています（pid {[s.get('pid') for s in same_place]}）"
        )
    if row.get("win_session") is not None and str(row["win_session"]) not in {
        v for v, _ in sm.WIN_SESSION_CHOICES if v
    }:
        # 5002 は元の起動先を引き継いで起こす。0 と 1 以外は、止めて写した後に起動で断られる
        problems.append(
            f"起動先がセッション {row['win_session']} です（0 か 1 だけ起こせる）"
        )
    if row.get("session_id") != args.session_id:
        problems.append(
            f"会話が {row.get('session_id')} です（{args.session_id} のはず）"
        )
    if row.get("account") != args.account:
        problems.append(
            f"アカウントが {row.get('account')} です（{args.account} のはず）"
        )
    if row.get("status") != "idle":
        problems.append(f"状態が {row.get('status')} です（入力待ちのときだけ打つ）")
    if args.via == args.account:
        problems.append("--via は --account と別のアカウントにしてください")
    _, resumable = call("GET", f"/api/projects/resumable?project={args.project}")
    if args.session_id not in {
        it.get("session_id") for it in resumable.get("items") or []
    }:
        problems.append(
            f"{args.project} の会話の一覧に {args.session_id} がありません（project の取り違え？）"
        )
    assigned = assigned_account(args)
    if assigned != args.account:
        problems.append(
            f"{args.project} の割り当ては {assigned} です（{args.account} のはず）"
        )
    owner = None
    try:
        home = accounts.claude_home(
            accounts.require_usable(args.account), sm.CLAUDE_HOME
        )
        via_home = accounts.claude_home(
            accounts.require_usable(args.via), sm.CLAUDE_HOME
        )
        # 行きの写しで断られないか（経由先に同じ名前で違う中身の記録が無いか）
        sm.check_transcript_copy(cwd, args.session_id, home, via_home)
        bridge, owner = drill.last_bridge(
            drill.read_records(args.account, cwd, args.session_id)
        )
        # 元の行が読めないと、1段目で「新しい行になったか」を比べられない
        if not row.get("url") or bridge != drill.url_body(row.get("url")):
            problems.append(
                f"一覧の url {row.get('url')} と会話記録の最後の bridge-session（{bridge}）が合いません"
            )
    except (accounts.AccountError, sm.SessionError, drill.DrillError) as e:
        problems.append(str(e))
    return row, cwd, owner, problems


def assigned_account(args):
    """いまの割り当てのアカウント（読めなければ None）。"""
    _, info = call("GET", f"/api/accounts?project={args.project}")
    return (info.get("project") or {}).get("account")


def leg(args, cwd, name, source, account, previous, step):
    """source から account へ持ち越して、新しい行を確かめる。(url, 持ち主) を返す。"""
    started = time.time()
    before = answers(drill.read_records(source, cwd, args.session_id))
    status, body = carry(account, args)
    if status != 200 or not body.get("ok"):
        raise drill.DrillError(
            f"{account} への持ち越しが {status} で返りました: {body}"
        )
    if body.get("session_id") != args.session_id or body.get("account") != account:
        raise drill.DrillError(f"持ち越した後の会話・アカウントが違います: {body}")
    row, records = wait_registered(account, cwd, args.session_id, body["pid"], before)
    url, owner = drill.check_row(row, records, account, name, previous)
    say(
        step,
        sec=round(time.time() - started, 1),
        pid=row.get("pid"),
        account=account,
        session_id=row.get("session_id"),
        name=row.get("name"),
        url=url,
        owner=owner,
        new_row=True,
        set_aside=body.get("set_aside"),
    )
    return url, owner


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="プロジェクトキー")
    parser.add_argument("--pid", type=int, required=True, help="いま動いている子の pid")
    parser.add_argument("--session-id", required=True, help="続ける会話の番号")
    parser.add_argument(
        "--account", required=True, help="いまのアカウント名（最後にここへ戻す）"
    )
    parser.add_argument("--via", required=True, help="経由するアカウント名")
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="起こすたびの最初の一言"
    )
    parser.add_argument("--dry-run", action="store_true", help="確かめと読み取りだけ")
    args = parser.parse_args()

    try:
        row, cwd, first_owner, problems = preflight(args)
    except drill.DrillError as e:
        row, cwd, first_owner, problems = None, None, None, [str(e)]
    if problems:
        say("refused", reason=" / ".join(problems) + "。何も変えていません")
        return 2
    name = row.get("name")
    first_url = row.get("url")
    say(
        "checked",
        before={k: row.get(k) for k in ("pid", "name", "url", "account", "session_id")},
        owner=first_owner,
        via=args.via,
    )
    if args.dry_run:
        say("dry_run", note="止めも写しもしていません")
        return 0

    stage = "via"
    try:
        via_url, via_owner = leg(
            args,
            cwd,
            name,
            args.account,
            args.via,
            (first_url, first_owner),
            f"carried->{args.via}",
        )
        stage = "back"
        last_url, _ = leg(
            args,
            cwd,
            name,
            args.via,
            args.account,
            (via_url, via_owner),
            f"carried->{args.account}",
        )
    except drill.DrillError as e:
        plan = restore_plan(args)
        if isinstance(e, Unreachable) and not plan.pop("settled", False):
            # 応答を待ちきれなくても 5002 は写し・起動を続けていることがある。すぐ起こすと二重になる
            plan["restore"] = (
                "5002 がまだ処理中かもしれない。1〜2 分おいて GET /api/sessions で会話 "
                f"{args.session_id} の行を読み直し、無いときだけ次を打つ: "
                + plan["restore"]
            )
        say("failed", stage=stage, error=str(e), **plan)
        return 1
    say(
        "done",
        name=name,
        new_url=last_url,
        husks=[first_url, via_url],
        note=f"claude.ai の {args.account} の一覧で new_url の行の名前が {name} かを人が見る。"
        "husks の行は切れた行として残る（claude.ai の画面でアーカイブする）。"
        f"{args.via} の置き場には行きの写し（{args.session_id}.jsonl）が残り、{args.account} の置き場の"
        "前の版は .before-carry-<日時> へよけてある（どちらも消していない）",
    )
    return 0


def restore_plan(args):
    """外れたときに、いまの状態（動いている行・割り当て）と戻し方を返す。読めなくても例外にしない。

    5002 は写しや起動に失敗すると子を止めたまま割り当てを前のアカウントへ戻すことがある。
    そのときは持ち越しではなく、割り当て先での resume で起こし直す。
    """
    try:
        now = next((s for s in rows() if s.get("session_id") == args.session_id), None)
        assigned = assigned_account(args)
    except drill.DrillError as e:
        return {
            "now": None,
            "restore": f"5002 の状態を読めません（{e}）。GET /api/sessions と "
            f"GET /api/accounts?project={args.project} で、会話 {args.session_id} の行と割り当てを先に確かめる",
        }
    state = {
        "now": {k: (now or {}).get(k) for k in ("pid", "account", "status", "url")},
        "assigned": assigned,
    }
    if now and now.get("account") == args.account:
        return dict(
            state,
            settled=True,
            restore=f"戻しは不要（{args.account} で動いている）。now.url が最初の行と同じなら何も変わって"
            "いないので、入力待ちのときに打ち直せばよい。違えば新しい行ができているが、名前は確かめきれて"
            f"いないので、claude.ai の {args.account} の一覧で now.url の行の名前を人が見る",
        )
    if not assigned:
        return dict(
            state,
            restore=f"割り当てを読めません。GET /api/accounts?project={args.project} で割り当てを確かめてから、"
            f"会話 {args.session_id} をそのアカウントで resume する",
        )
    if now:
        return dict(
            state,
            restore=f"{args.account} へ戻すなら、claude-web-app の venv で migration_drill.carry("
            f"{args.project!r}, {args.account!r}, {args.prompt!r}) を呼ぶ",
        )
    request = {
        "project": args.project,
        "resume": args.session_id,
        "account": assigned,
        "prompt": args.prompt,
    }
    return dict(
        state,
        restore=f"子が動いていない。まず割り当て先（{assigned}）で起こす: POST {drill.BASE}/api/sessions に "
        + json.dumps(request, ensure_ascii=False)
        + (
            ""
            if assigned == args.account
            else f"。その後 {args.account} へ戻すなら migration_drill.carry("
            f"{args.project!r}, {args.account!r}, {args.prompt!r})"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())

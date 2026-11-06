"""アカウントの移し（POST /api/accounts/move の carry_session）の往復を本物の 5002 で通す台本。

捨てプロジェクト（既定 developing/_migration-drill）で、次を1回ずつ打ち、所要時間と結果を出す。
  1. 移す前のアカウントで新しい会話を起こし、合言葉を覚えさせる（haiku・dontAsk・セッション0）
  2. carry_session で移す先へ移し、合言葉を答えさせる（同じ会話の続きか）
  3. carry_session で元へ戻し、もう一度答えさせる
  4. 子を止める（割り当ては元のアカウントに戻っている）

各段で確かめること（手元のプロセス一覧だけで合格にしない）:
  - 同じ会話の続きか（段ごとに違う合言葉の答え・返答の件数が増えたこと）
  - 手元の名前（一覧の name）がフォルダ名か
  - claude.ai の Remote Control の行が、そのアカウントで新しく登録されたか
    （一覧の url が前の段と違い、会話記録の最後の bridge-session が同じ id でそのアカウントの持ち主に替わったこと）
  claude.ai の一覧に**その名前で**出たかは、機械で確かめる公式の手段が無い（2026-10-28 に資料で確かめた）。
  そこで url を出し、--phase out で移す先に止めておけるようにした（人が一覧で名前を見てから --phase back で戻す）。

使い方（claude-web-app の venv で）:
  python migration_drill.py --from <元> --to <移す先>                 往復を通す
  python migration_drill.py --from <元> --to <移す先> --phase out     起こして移す先へ移したまま止める
  python migration_drill.py --from <元> --to <移す先> --phase back    out の続き（元へ戻して止める）
  python migration_drill.py --from <元> --to <移す先> --phase names   名前が保たれるかの通し（移し→/clear→
      区切りと同じ形の送信→同じアカウントの続き→持ち帰り）。最後に、人が claude.ai の一覧で見る url の表を出す

結果は標準出力と、``~/.claude-web-app/migration_drill.log.jsonl`` に1行ずつ残す。
終了コード: 0 通った / 1 どこかで外れた（どこで・何が返ったかを出す）。
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import accounts  # noqa: E402
import session_manager as sm  # noqa: E402

BASE = "http://127.0.0.1:5002"
STATE_DIR = os.path.join(accounts.USER_DIR, ".claude-web-app")
LOG_PATH = os.path.join(STATE_DIR, "migration_drill.log.jsonl")
# --phase out から back へ渡す（会話の番号・合言葉・移す先の url）
PHASE_PATH = os.path.join(STATE_DIR, "migration_drill.phase.json")

# 入力待ち（idle）と Remote Control の登録を待つ上限（秒）と、見に行く間隔
IDLE_TIMEOUT_SEC = 240
POLL_SEC = 3


class DrillError(Exception):
    """台本のどこかで期待が外れたことを表す例外。"""


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


def find_row(project_cwd):
    """一覧から作業場の行（無ければ None）。2行以上あれば DrillError（同じ名前が増えた）。"""
    _, body = call("GET", "/api/sessions")
    rows = [
        s
        for s in body.get("sessions", [])
        if sm._same_path(s.get("cwd") or "", project_cwd)
    ]
    if len(rows) > 1:
        raise DrillError(f"一覧に同じ作業場の行が {len(rows)} 本あります: {rows}")
    return rows[0] if rows else None


def transcript_path(account_name, project_cwd, session_id):
    account = accounts.require_usable(account_name)
    folder = sm.transcript_dir_in(
        accounts.claude_home(account, sm.CLAUDE_HOME), project_cwd
    )
    return os.path.join(folder, f"{session_id}.jsonl")


def read_records(account_name, project_cwd, session_id):
    """そのアカウントの置き場にある会話記録の行（読めない行は飛ばす）。"""
    path = transcript_path(account_name, project_cwd, session_id)
    if not os.path.isfile(path):
        raise DrillError(f"{account_name} の置き場に会話の記録がありません: {path}")
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def replies(records):
    """claude の返答の文の並び（古い順）。"""
    texts = []
    for record in records:
        if record.get("type") != "assistant":
            continue
        for part in (record.get("message") or {}).get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(part.get("text") or "")
    return texts


def last_bridge(records):
    """最後の bridge-session（claude.ai の Remote Control の行）の (id の本体, 持ち主) 。無ければ (None, None)。

    会話記録の id は ``cse_<本体>``、一覧の url は ``…/code/session_<本体>``（本体が同じ。2026-10-28 実測）。
    記録の形は claude の内部のもので版で変わりうる（公式資料の注意）。読めなければ None で返す。
    """
    for record in reversed(records):
        if record.get("type") == "bridge-session":
            raw = record.get("bridgeSessionId") or ""
            return raw.split("_", 1)[-1] or None, record.get("ownerAccountUuid")
    return None, None


def url_body(url):
    """一覧の url（…/session_<本体>）の本体。無ければ None。"""
    tail = (url or "").rstrip("/").rsplit("/", 1)[-1]
    return tail.split("_", 1)[-1] if tail else None


def wait_settled(account_name, project_cwd, session_id, pid, before):
    """返答が before 件より増え、子が入力待ちで url を持つまで待ち、(記録, 一覧の行) を返す。

    移す前からある返答（写した記録に入っている）や、指示を読む前の入力待ちを拾わないため。
    url（claude.ai への登録）は起動の後しばらくして付くので、それも待つ。
    """
    deadline = time.time() + IDLE_TIMEOUT_SEC
    records, row = [], None
    while time.time() < deadline:
        try:
            records = read_records(account_name, project_cwd, session_id)
        except DrillError:
            records = []  # 起こした直後は記録がまだ無いことがある
        row = find_row(project_cwd)
        if (
            len(replies(records)) > before
            and row
            and row.get("pid") == pid
            and row.get("status") == "idle"
            and row.get("url")
            # 状態ファイルの url と会話記録の bridge-session は別々に書かれる。そろうまで待つ（評価役の指摘）
            and last_bridge(records)[0] == url_body(row.get("url"))
        ):
            return records, row
        time.sleep(POLL_SEC)
    # どの条件が合わなかったかを切り分けられるよう、最後に見た値を添える
    row = row or {}
    raise DrillError(
        f"{IDLE_TIMEOUT_SEC} 秒待っても pid {pid} が新しい返答・入力待ち・claude.ai の登録（url と会話記録の"
        f"bridge-session）にそろいません。最後に見た値: 返答 {len(replies(records))} 件（{before} 件より増えるはず）・"
        f"一覧の pid {row.get('pid')}・状態 {row.get('status')}・url {row.get('url')}・"
        f"記録の最後の bridge {last_bridge(records)[0]}"
    )


def log(step, started, **fields):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "step": step,
        "sec": round(time.time() - started, 1),
        **fields,
    }
    print(json.dumps(entry, ensure_ascii=False), flush=True)
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_row(row, records, account, name, previous):
    """手元の名前と claude.ai の行の登録を確かめ、(url, 持ち主) を返す。

    previous は前の段の (url, 持ち主)。移しの後は url も持ち主も替わっているはず
    （別のアカウントの resume は前の行につなぎ直せず、新しい行が作られる）。
    """
    if row.get("account") != account:
        raise DrillError(
            f"一覧の account が {row.get('account')} です（{account} のはず）"
        )
    if row.get("name") != name:
        raise DrillError(f"手元の名前が {row.get('name')!r} です（{name!r} のはず）")
    url = row.get("url")
    bridge, owner = last_bridge(records)
    # 読めないまま黙って飛ばすと、新しい行が登録されたかを確かめずに合格になる（評価役の指摘）
    if not bridge or not owner:
        raise DrillError(
            "会話記録に bridge-session（claude.ai の行の登録）と持ち主が読めません"
            "（claude の版で記録の形が変わったかもしれません）"
        )
    if bridge != url_body(url):
        raise DrillError(
            f"一覧の url {url} と会話記録の最後の bridge-session（{bridge}）が違います"
        )
    if previous:
        if url == previous[0]:
            raise DrillError(
                f"claude.ai の行が前の段と同じです（{url}）。新しい行が登録されていません"
            )
        if owner == previous[1]:
            raise DrillError(f"claude.ai の行の持ち主が前の段と同じです（{owner}）")
    return url, owner


def carry(project, account, prompt):
    """動いている子を止め、会話を account の置き場へ写して同じ会話の続きで起こす（carry_session）。

    claude.ai の行の題名は既定で手元の名前（remote_control）。(状態コード, JSON) を返す。
    """
    return call(
        "POST",
        "/api/accounts/move",
        {
            "project": project,
            "account": account,
            "carry_session": True,
            "prompt": prompt,
        },
    )


def move(
    project, project_cwd, name, source, account, word, session_id, stage, previous
):
    """carry_session で移し、続き・名前・claude.ai の登録を確かめて、(新しい pid, url, 持ち主) を返す。"""
    started = time.time()
    before = len(replies(read_records(source, project_cwd, session_id)))
    expected = f"{word}-{stage}"
    status, body = carry(
        project,
        account,
        f"練習です。最初に覚えた合言葉の後ろに -{stage} を付けて、その1語だけで答えてください。",
    )
    if status != 200 or not body.get("ok"):
        raise DrillError(f"{account} への移しが {status} で返りました: {body}")
    if body.get("session_id") != session_id or body.get("account") != account:
        raise DrillError(f"移した後の会話・アカウントが違います: {body}")
    records, row = wait_settled(account, project_cwd, session_id, body["pid"], before)
    reply = replies(records)[-1]
    url, owner = check_row(row, records, account, name, previous)
    log(
        f"move->{account}",
        started,
        pid=body["pid"],
        account=row.get("account"),
        session_id=row.get("session_id"),
        name=row.get("name"),
        url=url,
        owner=owner,
        new_row_registered=True,
        copied_files=body.get("copied_files"),
        set_aside=body.get("set_aside"),
        reply=reply[:80],
        continued=expected in reply,
        title_check=f"claude.ai の {account} の一覧で、この url の行の名前が {name} かを人が見る（機械で読む公式の手段が無い）",
    )
    if expected not in reply:
        raise DrillError(
            f"{account} で合言葉が返りません（続きから読めていない）: {reply!r}"
        )
    return body["pid"], url, owner


def stop_and_restore(args, project_cwd):
    """練習の子を止め、割り当てが元のアカウントでなければ戻す（次に打てるように）。

    後始末の失敗で上の結果を隠さないよう、ここの例外は記録だけにする。
    """
    try:
        # 止めるのは作業場でいま動いている行（移しの後に失敗すると pid の変数は前の子のまま）
        row = find_row(project_cwd)
        if row:
            status, body = call("POST", f"/api/sessions/{row['pid']}/stop", {})
            log("stop", time.time(), pid=row["pid"], status=status, ok=body.get("ok"))
        _, info = call("GET", f"/api/accounts?project={args.project}")
        now = ((info or {}).get("project") or {}).get("account")
        if now and now != args.source:
            status, body = call(
                "POST",
                "/api/accounts/move",
                {
                    "project": args.project,
                    "account": args.source,
                    "launch": False,
                    "force": True,
                },
            )
            log("restore", time.time(), account=args.source, status=status)
    except Exception as e:  # 後始末は最善の努力（結果の報告を優先する）
        log("cleanup_failed", time.time(), error=f"{type(e).__name__}: {e}")


def phase_out(args, project_cwd, name):
    """起こして移す先へ移すまで。(会話の番号, 合言葉, 移す先の url, 持ち主, 元の url) を返す。"""
    if find_row(project_cwd):
        raise DrillError("練習の作業場で子がもう動いています。止めてから打ってください")
    word = "drill-" + secrets.token_hex(3)
    # 1. 元のアカウントで新しい会話を起こして合言葉を覚えさせる
    started = time.time()
    status, body = call(
        "POST",
        "/api/sessions",
        {
            "project": args.project,
            "new": True,
            "model": "haiku",
            "permission_mode": "dontAsk",
            "win_session": "0",
            "prompt": f"移しの練習です。合言葉は {word} です。覚えて「了解」とだけ返してください。",
        },
    )
    if status != 200 or not body.get("ok"):
        raise DrillError(f"起動が {status} で返りました: {body}")
    session = body["session"]
    pid, session_id = session["pid"], session["session_id"]
    records, row = wait_settled(args.source, project_cwd, session_id, pid, 0)
    first = check_row(row, records, args.source, name, None)
    log(
        "start",
        started,
        pid=pid,
        account=row.get("account"),
        session_id=session_id,
        name=row.get("name"),
        url=first[0],
        reply=replies(records)[-1][:80],
    )
    # 2. 移す先へ持ち越す
    _, url, owner = move(
        args.project,
        project_cwd,
        name,
        args.source,
        args.target,
        word,
        session_id,
        2,
        first,
    )
    return session_id, word, url, owner, first[0]


def phase_back(args, project_cwd, name, session_id, word, url, owner):
    """移す先から元へ持ち帰る。"""
    move(
        args.project,
        project_cwd,
        name,
        args.target,
        args.source,
        word,
        session_id,
        3,
        (url, owner),
    )


# 自動の区切りが /clear の後に送る本文と同じ形（題名を明示しない行は、この発言から題名が付け替わった）
HANDOFF_LIKE_TEXT = (
    "自動の区切りの /clear が済みました。引き継ぎファイル「{readme}」を読み、"
    "やりかけの作業（人に止められたもの・返事待ちは除く）があれば続きから進めてください。"
    "無ければ次の依頼を待ってください。"
)


def wait_row(project_cwd, pid=None, session_id=None):
    """作業場の行が入力待ちで url を持つまで待つ（pid・会話の番号を指定すればそれも合うまで）。"""
    deadline = time.time() + IDLE_TIMEOUT_SEC
    row = None
    while time.time() < deadline:
        row = find_row(project_cwd)
        if (
            row
            and row.get("status") == "idle"
            and row.get("url")
            and (pid is None or row.get("pid") == pid)
            and (session_id is None or row.get("session_id") == session_id)
        ):
            return row
        time.sleep(POLL_SEC)
    raise DrillError(
        f"{IDLE_TIMEOUT_SEC} 秒待っても行がそろいません。最後に見た行: {row}"
    )


def send(pid, payload):
    """子へ送る（本アプリの送信の口）。応答の result を返す。"""
    status, body = call("POST", f"/api/sessions/{pid}/send", payload)
    if status != 200 or not body.get("ok"):
        raise DrillError(f"送信が {status} で返りました: {body}")
    return body.get("result") or {}


def names_scenario(args, project_cwd, name):
    """claude.ai の行の名前が、移し・/clear・区切り・同じアカウントの resume・戻しの後も保たれるかを通しで見る。

    題名は機械で読めないので、段ごとの url を表に出す（人が一覧でまとめて1回見る）。
    url の替わり方（新しい行か・同じ行か）と手元の名前は機械で確かめる。
    """
    table = []
    # 練習の起動の設定（1段目と同じ。5段目の起こし直しにも付ける）
    launch_options = {
        "model": "haiku",
        "permission_mode": "dontAsk",
        "win_session": "0",
    }

    def note(stage, row, same_as=None, differ_from=None):
        """段の行を表に足し、手元の名前と行の替わり方を確かめる（外れたら落とす）。"""
        url = row.get("url")
        entry = {
            "stage": stage,
            "account": row.get("account"),
            "pid": row.get("pid"),
            "session_id": row.get("session_id"),
            "name": row.get("name"),
            "url": url,
        }
        if same_as is not None:
            entry["same_row_as_before"] = url == same_as
        if differ_from is not None:
            entry["new_row"] = url != differ_from
        table.append(entry)
        log(f"names:{stage}", time.time(), **entry)
        if row.get("name") != name:
            raise DrillError(
                f"{stage}: 手元の名前が {row.get('name')!r} です（{name!r} のはず）"
            )
        if same_as is not None and url != same_as:
            raise DrillError(
                f"{stage}: claude.ai の行が替わりました（{same_as} → {url}）。同じ行のはず"
            )
        if differ_from is not None and url == differ_from:
            raise DrillError(
                f"{stage}: claude.ai の行が前と同じです（{url}）。新しい行のはず"
            )
        return url

    def after_id(result):
        """送信の後の会話の番号。送る前と同じ（受け箱経由で古い値）なら None（番号では待たない）。"""
        after = result.get("session_id_after")
        return None if after == result.get("session_id_before") else after

    # 1. 元のアカウントで新しい会話（-n の名前で行が作られる）・2. 移す先へ持ち越す（新しい行）
    session_id, _, url_b, _, url_a = phase_out(args, project_cwd, name)
    table.append({"stage": "start", "account": args.source, "name": name, "url": url_a})
    row = wait_row(project_cwd, session_id=session_id)
    # 3段目の比べる相手は、この段で一覧に見えた行の url
    url_b = note(f"carried->{args.target}", row, differ_from=url_a)
    # 3. /clear だけ（会話の番号は替わる・行は同じはず）
    result = send(row["pid"], {"clear": True, "wait_idle_sec": 60})
    row = wait_row(project_cwd, pid=row["pid"], session_id=after_id(result))
    url_c = note("clear", row, same_as=url_b)
    # 4. 自動の区切りと同じ形（/clear と引き継ぎの本文）。題名を明示しない行はここで題名が付け替わった
    readme = os.path.join(project_cwd, "README.md")
    result = send(
        row["pid"],
        {
            "clear": True,
            "text": HANDOFF_LIKE_TEXT.format(readme=readme),
            "wait_idle_sec": 60,
        },
    )
    time.sleep(POLL_SEC)  # 本文を読み始めてから入力待ちに戻るのを待つ
    row = wait_row(project_cwd, pid=row["pid"], session_id=after_id(result))
    url_d = note("handoff_like", row, same_as=url_c)
    last_session = row.get("session_id")
    # 5. 止めて、同じアカウントで既定の起動（いちばん新しい会話の続き・題名を渡す）
    status, body = call("POST", f"/api/sessions/{row['pid']}/stop", {})
    if status != 200 or not body.get("ok"):
        raise DrillError(f"止められませんでした（{status}）: {body}")
    status, body = call(
        "POST",
        "/api/sessions",
        {
            **launch_options,
            "project": args.project,
            "prompt": "練習の起こし直しです。返事は不要です。",
        },
    )
    if status != 200 or not body.get("ok") or not body.get("auto_resumed"):
        raise DrillError(
            f"同じアカウントの続きで起こせませんでした（{status}）: {body}"
        )
    if body.get("resumed") != last_session:
        raise DrillError(
            f"続けた会話が {body.get('resumed')} です（4段目の {last_session} のはず）"
        )
    row = wait_row(project_cwd, pid=(body.get("session") or {}).get("pid"))
    note("resume_same_account", row, same_as=url_d)
    url_e = row.get("url")
    # 6. 元のアカウントへ持ち帰る（新しい行）
    status, body = carry(
        args.project, args.source, "練習の持ち帰りです。返事は不要です。"
    )
    if status != 200 or not body.get("ok"):
        raise DrillError(f"{args.source} への持ち帰りが {status} で返りました: {body}")
    row = wait_row(project_cwd, pid=body.get("pid"))
    note(f"carried->{args.source}", row, differ_from=url_e)

    # 人が見る表: url ごとに、どのアカウントの一覧で見るか・期待する名前
    rows_to_check = {}
    for entry in table:
        rows_to_check.setdefault(entry["url"], entry["account"])
    log(
        "names:check_list",
        time.time(),
        expected_name=name,
        rows=[{"url": u, "account_list": a} for u, a in rows_to_check.items()],
        note="claude.ai の各アカウントの一覧で、これらの行の名前がすべて expected_name かを人が見る",
    )
    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default="developing/_migration-drill")
    parser.add_argument("--from", dest="source", required=True, help="元のアカウント名")
    parser.add_argument(
        "--to", dest="target", required=True, help="移す先のアカウント名"
    )
    parser.add_argument(
        "--phase",
        choices=("all", "out", "back", "names"),
        default="all",
        help="all=往復 / out=移す先へ移したまま止める（人が claude.ai の一覧で名前を見る）/ back=out の続き / "
        "names=移し→/clear→区切りと同じ形の送信→同じアカウントの続き→持ち帰りの各段の url を表にする",
    )
    parser.add_argument(
        "--cwd",
        help="作業場のパス（省略時は developing/ を本アプリの親フォルダとして組み立てる）",
    )
    args = parser.parse_args()
    group, _, rest = args.project.partition("/")
    project_cwd = args.cwd or (
        os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", *rest.split("/")
            )
        )
        if group == "developing"
        else ""
    )
    # 手元の名前（-n）はフォルダ名（build_session_display_name と同じ）
    name = os.path.basename(project_cwd)

    whole = time.time()
    status, info = call("GET", f"/api/accounts?project={args.project}")
    project = (info or {}).get("project") or {}
    if status != 200 or not project:
        print(
            f"プロジェクトの割り当てを読めません（{status}）: {info}", file=sys.stderr
        )
        return 1
    expected_now = args.target if args.phase == "back" else args.source
    if project.get("account") != expected_now:
        print(
            f"{args.project} はいま {project.get('account')} です（{expected_now} のはず）",
            file=sys.stderr,
        )
        return 1
    if not os.path.isdir(project_cwd):
        print(
            f"作業場が見つかりません（--cwd で指定できます）: {project_cwd}",
            file=sys.stderr,
        )
        return 1
    keep_running = False
    try:
        if args.phase == "names":
            names_scenario(args, project_cwd, name)
            log("names_done", whole, ok=True)
            return 0
        if args.phase == "back":
            with open(PHASE_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            # 前の out が途中で落ちると古い状態ファイルが残る。動いている会話と同じときだけ戻す
            current = find_row(project_cwd)
            if not current or current.get("session_id") != saved["session_id"]:
                raise DrillError(
                    f"--phase out の記録（会話 {saved['session_id']}）と、いま動いている行"
                    f"（{(current or {}).get('session_id')}）が違います。out からやり直してください"
                )
            phase_back(
                args,
                project_cwd,
                name,
                saved["session_id"],
                saved["word"],
                saved["url"],
                saved["owner"],
            )
        else:
            session_id, word, url, owner, _ = phase_out(args, project_cwd, name)
            if args.phase == "out":
                with open(PHASE_PATH, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "session_id": session_id,
                            "word": word,
                            "url": url,
                            "owner": owner,
                        },
                        f,
                    )
                keep_running = True
                log(
                    "out_done",
                    whole,
                    ok=True,
                    url=url,
                    next=f"claude.ai の {args.target} の一覧で {name} の行を見てから --phase back を打つ",
                )
                return 0
            phase_back(args, project_cwd, name, session_id, word, url, owner)
        log("round_trip", whole, ok=True)
        return 0
    except (DrillError, OSError, KeyError, ValueError) as e:
        log("failed", whole, ok=False, error=f"{type(e).__name__}: {e}")
        return 1
    finally:
        if not keep_running:
            stop_and_restore(args, project_cwd)


if __name__ == "__main__":
    sys.exit(main())

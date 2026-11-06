"""練習の台本（migration_drill.py）の判定の部品の試験（5002 には繋がない）。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import migration_drill as drill  # noqa: E402

ACCOUNT_A = "0eae-owner"
ACCOUNT_B = "45ee-owner"


def bridge(body, owner):
    return {
        "type": "bridge-session",
        "bridgeSessionId": f"cse_{body}",
        "ownerAccountUuid": owner,
    }


def test_last_bridge_and_url_body():
    """会話記録の cse_<本体> と一覧の url の session_<本体> を突き合わせる（2026-10-28 の実物の形）。"""
    records = [bridge("012A", ACCOUNT_A), {"type": "assistant"}, bridge("019B", ACCOUNT_B)]
    assert drill.last_bridge(records) == ("019B", ACCOUNT_B)
    assert drill.last_bridge([{"type": "user"}]) == (None, None)
    assert drill.url_body("https://claude.ai/code/session_019B") == "019B"
    assert drill.url_body(None) is None


def row(
    account="account_b",
    name="_migration-drill",
    url="https://claude.ai/code/session_019B",
):
    return {"account": account, "name": name, "url": url}


def test_check_row_accepts_new_row_of_new_owner():
    records = [bridge("012A", ACCOUNT_A), bridge("019B", ACCOUNT_B)]
    previous = ("https://claude.ai/code/session_012A", ACCOUNT_A)
    assert drill.check_row(row(), records, "account_b", "_migration-drill", previous) == (
        "https://claude.ai/code/session_019B",
        ACCOUNT_B,
    )


@pytest.mark.parametrize(
    "the_row, records, previous, message",
    [
        # 手元の一覧だけ合っていても、claude.ai の行が前の段のままなら落とす
        (
            row(url="https://claude.ai/code/session_012A"),
            [bridge("012A", ACCOUNT_A)],
            ("https://claude.ai/code/session_012A", ACCOUNT_A),
            "前の段と同じ",
        ),
        # url と会話記録の最後の bridge が食い違う
        (row(), [bridge("019C", ACCOUNT_B)], None, "違います"),
        # 新しい行でも持ち主が替わっていない
        (
            row(),
            [bridge("019B", ACCOUNT_A)],
            ("https://claude.ai/code/session_012A", ACCOUNT_A),
            "持ち主",
        ),
        # 手元の名前が自動の名前
        (
            row(name="yk-host_pc-velvet-dongarra"),
            [bridge("019B", ACCOUNT_B)],
            None,
            "手元の名前",
        ),
        # アカウントが違う
        (row(account="account_a"), [bridge("019B", ACCOUNT_B)], None, "account"),
        # bridge-session が読めない（記録の形が変わった）なら黙って通さない
        (row(), [{"type": "assistant"}], None, "読めません"),
        (row(), [bridge("019B", None)], None, "読めません"),
    ],
)
def test_check_row_refuses(the_row, records, previous, message):
    with pytest.raises(drill.DrillError) as e:
        drill.check_row(the_row, records, "account_b", "_migration-drill", previous)
    assert message in str(e.value)


def test_wait_settled_waits_until_url_and_bridge_agree(monkeypatch):
    """一覧の url が付いても、会話記録の bridge-session がまだ前の段のままなら待つ（そろってから返す）。"""
    reply = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x"}]},
    }
    seen = [
        [bridge("012A", ACCOUNT_A), reply],  # 1回目: 記録はまだ前の段の行
        [bridge("012A", ACCOUNT_A), reply, bridge("019B", ACCOUNT_B)],  # 2回目: そろった
    ]
    monkeypatch.setattr(drill, "read_records", lambda *a: seen.pop(0))
    monkeypatch.setattr(
        drill, "find_row", lambda cwd: dict(row(), pid=5, status="idle")
    )
    monkeypatch.setattr(drill.time, "sleep", lambda sec: None)
    records, the_row = drill.wait_settled("account_b", "cwd", "sid", 5, 0)
    assert drill.last_bridge(records) == ("019B", ACCOUNT_B) and seen == []


class FakeNames:
    """names の通しの代役。段ごとに行（pid・会話・url）を差し替える。"""

    def __init__(self, same_row=True, resumed=None, stale_after=False):
        self.row = {
            "pid": 2,
            "session_id": "s1",
            "account": "account_b",
            "name": "_migration-drill",
            "status": "idle",
            "url": "U_B",
        }
        self.same_row, self.resumed, self.stale_after = same_row, resumed, stale_after
        self.posts = []

    def call(self, method, path, body=None, timeout=300):
        if method == "POST":
            self.posts.append((path, body))
        if path.endswith("/send"):
            before = self.row["session_id"]
            sid = f"s{len(self.posts) + 1}"
            self.row = dict(self.row, session_id=sid)
            if not self.same_row:
                self.row["url"] = f"U_{sid}"
            # 受け箱経由で状態ファイルの更新が遅れると、送る前と同じ番号が返りうる
            after = before if self.stale_after else sid
            return 200, {
                "ok": True,
                "result": {"session_id_before": before, "session_id_after": after},
            }
        if path.endswith("/stop"):
            return 200, {"ok": True}
        if path == "/api/sessions" and method == "POST":
            resumed = self.resumed or self.row["session_id"]
            self.row = dict(self.row, pid=3)
            return 200, {
                "ok": True,
                "auto_resumed": True,
                "resumed": resumed,
                "session": {"pid": 3},
            }
        if path == "/api/accounts/move":
            self.row = dict(self.row, pid=4, account="account_a", url="U_C")
            return 200, {"ok": True, "pid": 4}
        raise AssertionError(path)


def run_names(monkeypatch, fake):
    monkeypatch.setattr(drill, "call", fake.call)
    monkeypatch.setattr(drill, "find_row", lambda cwd: dict(fake.row))
    monkeypatch.setattr(
        drill, "phase_out", lambda *a: ("s1", "w", "U_B", "owner", "U_A")
    )
    monkeypatch.setattr(drill.time, "sleep", lambda sec: None)
    logged = []
    monkeypatch.setattr(
        drill, "log", lambda step, started, **f: logged.append((step, f))
    )

    class Args:
        project, source, target = "developing/_migration-drill", "account_a", "account_b"

    table = drill.names_scenario(Args, "F:/x/_migration-drill", "_migration-drill")
    return table, logged


def test_names_scenario_lists_urls_per_stage(monkeypatch):
    fake = FakeNames()
    table, logged = run_names(monkeypatch, fake)
    stages = [t["stage"] for t in table]
    assert stages == [
        "start",
        "carried->account_b",
        "clear",
        "handoff_like",
        "resume_same_account",
        "carried->account_a",
    ]
    # /clear・区切り・同じアカウントの続きは同じ行（前の段と同じ url）
    assert [t.get("same_row_as_before") for t in table[2:5]] == [True, True, True]
    # 区切りと同じ形の本文を送り、同じアカウントの続きは resume を省いた既定の起動
    sends = [b for p, b in fake.posts if p.endswith("/send")]
    assert sends[0] == {"clear": True, "wait_idle_sec": 60}
    assert sends[1]["clear"] is True and "README.md" in sends[1]["text"]
    start = [b for p, b in fake.posts if p == "/api/sessions"][0]
    assert "resume" not in start and "new" not in start
    assert start["model"] == "haiku" and start["permission_mode"] == "dontAsk"
    assert table[1]["new_row"] is True and table[5]["new_row"] is True
    check = [f for s, f in logged if s == "names:check_list"][0]
    assert [r["url"] for r in check["rows"]] == ["U_A", "U_B", "U_C"]


def test_names_scenario_fails_when_row_changes(monkeypatch):
    """/clear で行が替わったら落とす（同じ行のはず）。表と log には残る。"""
    with pytest.raises(drill.DrillError) as e:
        run_names(monkeypatch, FakeNames(same_row=False))
    assert "clear" in str(e.value) and "替わりました" in str(e.value)


def test_names_scenario_fails_when_other_conversation_resumed(monkeypatch):
    with pytest.raises(drill.DrillError) as e:
        run_names(monkeypatch, FakeNames(resumed="other"))
    assert "続けた会話" in str(e.value)


def test_names_scenario_tolerates_stale_session_id_after(monkeypatch):
    """送信の応答の番号が送る前と同じ（受け箱経由で古い）でも、番号では待たずに進む。"""
    table, _ = run_names(monkeypatch, FakeNames(stale_after=True))
    assert table[-1]["stage"] == "carried->account_a"

"""名前つきの新しい行で戻す台本（fresh_named_row.py）の試験（5002 には繋がない）。"""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fresh_named_row as fnr  # noqa: E402
import migration_drill as drill  # noqa: E402

SID = "0c0c0c0c-0000-4000-8000-000000000003"
PROJECT = "developing/example-session-tool/projects/example-game-b"
CWD = "F:/x/example-game-b"
OWNERS = {"account_b": "45ee-owner", "account_a": "0eae-owner"}


def bridge(body, account):
    return {
        "type": "bridge-session",
        "bridgeSessionId": f"cse_{body}",
        "ownerAccountUuid": OWNERS[account],
    }


REPLY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}


class Fake5002:
    """一覧・spec・割り当て・持ち越しの代役。会話記録はアカウントごとに持ち、持ち越すたびに写して
    返答と新しい行の登録を足す（answer=False なら返答を足さない＝一言を読む前のまま）。"""

    def __init__(self, status="idle", spec_ok=True, same_row=False, **row):
        self.row = {
            "pid": 11780,
            "name": "example-game-b",
            "account": "account_b",
            "session_id": SID,
            "status": status,
            "cwd": CWD,
            "url": "https://claude.ai/code/session_A",
            **row,
        }
        self.extra_rows = []
        self.records = {"account_b": [REPLY, bridge("A", "account_b")], "account_a": None}
        self.assigned = "account_b"
        self.spec_ok, self.same_row = spec_ok, same_row
        self.answer = True
        self.carried = []
        self.fail_carry = {}  # 何回目の持ち越しを (状態コード, 本文) で返すか

    def call(self, method, path, body=None, timeout=300):
        if path == "/api/integration/spec":
            if not self.spec_ok:
                return 200, {"spec_version": 1, "launch": {}, "accounts": {}}
            return 200, {
                "launch": {"remote_control_key": "remote_control"},
                "accounts": {"x_key": "carry_session"},
            }
        if path == "/api/sessions":
            listed = [dict(self.row)] if self.row else []
            return 200, {"sessions": listed + self.extra_rows}
        if path.startswith("/api/projects/resumable"):
            return 200, {"items": [{"session_id": SID}]}
        if path.startswith("/api/accounts?"):
            return 200, {"project": {"account": self.assigned}}
        raise AssertionError(path)

    def carry(self, project, account, prompt):
        self.carried.append((project, account, prompt))
        n = len(self.carried)
        if n in self.fail_carry:
            status, body = self.fail_carry[n]
            if status == 502:
                # 写しの後に起こせなかった: 子は止まり、割り当ては前のアカウントへ戻る
                self.row = None
            return status, body
        source = self.row["account"]
        body = "A" if self.same_row else ("B" if n == 1 else "C")
        pid = 20000 + n
        self.row = dict(
            self.row,
            pid=pid,
            account=account,
            url=f"https://claude.ai/code/session_{body}",
        )
        copied = list(self.records[source])
        self.records[account] = (
            copied + ([REPLY] if self.answer else []) + [bridge(body, account)]
        )
        self.assigned = account
        return 200, {"ok": True, "pid": pid, "session_id": SID, "account": account}

    def read(self, account, cwd, session_id):
        assert cwd == CWD and session_id == SID
        if self.records.get(account) is None:
            raise drill.DrillError(f"{account} の置き場に会話の記録がありません")
        return self.records[account]


def prepare(monkeypatch, fake, copy_check=None):
    monkeypatch.setattr(drill, "call", fake.call)
    monkeypatch.setattr(drill, "carry", fake.carry)
    monkeypatch.setattr(drill, "read_records", fake.read)
    monkeypatch.setattr(drill.time, "sleep", lambda sec: None)
    monkeypatch.setattr(drill, "IDLE_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(fnr.accounts, "require_usable", lambda name: {"name": name})
    monkeypatch.setattr(fnr.accounts, "claude_home", lambda a, main: f"H/{a['name']}")
    copies = []

    def check(*a):
        copies.append(a)
        if copy_check:
            copy_check(*a)
        return ("s", "d", [])

    monkeypatch.setattr(fnr.sm, "check_transcript_copy", check)
    return copies


def run(monkeypatch, fake, *extra, copy_check=None):
    copies = prepare(monkeypatch, fake, copy_check)
    said = []
    monkeypatch.setattr(fnr, "say", lambda step, **f: said.append((step, f)))
    argv = [
        "fresh_named_row.py",
        "--project",
        PROJECT,
        "--pid",
        "11780",
        "--session-id",
        SID,
        "--account",
        "account_b",
        "--via",
        "account_a",
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return fnr.main(), said, copies


def test_round_trip_makes_new_named_rows(monkeypatch):
    fake = Fake5002()
    code, said, copies = run(monkeypatch, fake)
    assert code == 0
    assert [c[1] for c in fake.carried] == ["account_a", "account_b"]
    assert all(c[2] == fnr.DEFAULT_PROMPT for c in fake.carried)
    # 行きの写しを確かめる向き（元＝いまのアカウント、先＝経由先）
    assert copies == [(CWD, SID, "H/account_b", "H/account_a")]
    done = said[-1][1]
    assert done["new_url"].endswith("session_C") and done["name"] == "example-game-b"
    assert [u[-1] for u in done["husks"]] == ["A", "B"]
    legs = [f for s, f in said if s.startswith("carried->")]
    assert [f["owner"] for f in legs] == [OWNERS["account_a"], OWNERS["account_b"]]


def test_dry_run_changes_nothing(monkeypatch):
    fake = Fake5002()
    code, said, _ = run(monkeypatch, fake, "--dry-run")
    assert code == 0 and fake.carried == []
    assert said[-1][0] == "dry_run"


def other_row():
    return {"pid": 999, "cwd": CWD, "session_id": "x", "account": "account_b"}


@pytest.mark.parametrize(
    "fake, message",
    [
        (Fake5002(status="busy"), "入力待ち"),
        (Fake5002(spec_ok=False), "受け付ける版"),
        (Fake5002(win_session="2"), "起動先"),
        (Fake5002(session_id="other"), "会話が other"),
        (Fake5002(account="account_a"), "アカウントが account_a"),
        (Fake5002(url="https://claude.ai/code/session_Z"), "合いません"),
    ],
)
def test_refuses_before_changing(monkeypatch, fake, message):
    code, said, _ = run(monkeypatch, fake)
    assert code == 2 and fake.carried == []
    assert message in said[-1][1]["reason"]


def test_refuses_two_rows_in_same_place(monkeypatch):
    fake = Fake5002()
    fake.extra_rows = [other_row()]
    code, said, _ = run(monkeypatch, fake)
    assert code == 2 and "2 本" in said[-1][1]["reason"]


def test_refuses_when_copy_would_be_refused(monkeypatch):
    def refuse(*a):
        raise fnr.sm.SessionError("移す先に同じ名前で中身の違う会話の記録があります")

    fake = Fake5002()
    code, said, _ = run(monkeypatch, fake, copy_check=refuse)
    assert code == 2 and fake.carried == []
    assert "中身の違う" in said[-1][1]["reason"]


def test_unreachable_before_changing_is_refused(monkeypatch):
    fake = Fake5002()
    prepare(monkeypatch, fake)

    def down(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(drill, "call", down)
    said = []
    monkeypatch.setattr(fnr, "say", lambda step, **f: said.append((step, f)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["x", "--project", PROJECT, "--pid", "1", "--session-id", SID]
        + ["--account", "account_b", "--via", "account_a"],
    )
    assert fnr.main() == 2 and "繋がりません" in said[-1][1]["reason"]


def test_same_row_after_carry_is_failure_with_restore(monkeypatch):
    # 持ち越しても行が前と同じなら、題名つきの新しい行はできていない。子は account_a で動いている
    fake = Fake5002(same_row=True)
    code, said, _ = run(monkeypatch, fake)
    assert code == 1
    failed = said[-1][1]
    assert failed["stage"] == "via" and "前の段と同じ" in failed["error"]
    assert failed["now"]["account"] == "account_a"
    assert "migration_drill.carry(" in failed["restore"]


def test_stopped_after_502_restores_by_resume_on_assigned(monkeypatch):
    # 写しの後に起こせず子は止まり、割り当ては前（account_b）へ戻った。持ち越しは 400 になるので resume を出す
    fake = Fake5002()
    fake.fail_carry = {
        1: (502, {"ok": False, "error": "起こせません", "stopped": True})
    }
    code, said, _ = run(monkeypatch, fake)
    assert code == 1
    failed = said[-1][1]
    assert "502" in failed["error"] and failed["now"]["pid"] is None
    assert failed["assigned"] == "account_b"
    assert '"resume": "' + SID in failed["restore"]
    assert "migration_drill.carry(" not in failed["restore"]


def test_failure_on_way_back_is_reported_as_back(monkeypatch):
    fake = Fake5002()
    fake.fail_carry = {2: (409, {"ok": False, "error": "作業中"})}
    code, said, _ = run(monkeypatch, fake)
    failed = said[-1][1]
    assert code == 1 and failed["stage"] == "back"
    assert failed["now"]["account"] == "account_a"
    assert "'account_b'" in failed["restore"]


def test_waits_until_prompt_is_answered(monkeypatch):
    # 一言を読む前の入力待ち（返答が増えていない）では次の段へ進まない
    fake = Fake5002()
    fake.answer = False
    code, said, _ = run(monkeypatch, fake)
    assert code == 1 and len(fake.carried) == 1
    assert "返答" in said[-1][1]["error"]


def test_connection_lost_during_carry_still_gives_restore(monkeypatch):
    fake = Fake5002()
    prepare(monkeypatch, fake)

    def lost(*a):
        fake.row = None
        raise TimeoutError("timed out")

    monkeypatch.setattr(drill, "carry", lost)
    said = []
    monkeypatch.setattr(fnr, "say", lambda step, **f: said.append((step, f)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["x", "--project", PROJECT, "--pid", "11780", "--session-id", SID]
        + ["--account", "account_b", "--via", "account_a"],
    )
    assert fnr.main() == 1
    failed = said[-1][1]
    assert "受け取れません" in failed["error"] and "resume" in failed["restore"]
    # 5002 はまだ処理中かもしれない。すぐ起こすと二重になるので、読み直してからにさせる
    assert failed["restore"].startswith("5002 がまだ処理中かもしれない")


def test_reply_without_text_counts(monkeypatch):
    # 「返事は不要」の一言には文を出さずに終わることがある（思考だけ）。それでも次の段へ進む
    fake = Fake5002()
    global REPLY
    saved = REPLY
    REPLY = {"type": "assistant", "message": {"content": [{"type": "thinking"}]}}
    try:
        fake.records["account_b"] = [REPLY, bridge("A", "account_b")]
        code, _, _ = run(monkeypatch, fake)
    finally:
        REPLY = saved
    assert code == 0


def test_unknown_assignment_does_not_give_null_account(monkeypatch):
    fake = Fake5002()
    fake.fail_carry = {1: (502, {"ok": False, "error": "起こせません"})}
    code, said, _ = run(monkeypatch, fake)
    fake.assigned = None
    plan = fnr.restore_plan(
        type(
            "A",
            (),
            {
                "project": PROJECT,
                "session_id": SID,
                "account": "account_b",
                "prompt": "p",
            },
        )
    )
    assert "割り当てを読めません" in plan["restore"] and "null" not in plan["restore"]

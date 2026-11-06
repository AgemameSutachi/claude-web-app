"""名前を付けて起こし直す台本（resume_with_name.py）の試験（5002 には繋がない）。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import resume_with_name as tool  # noqa: E402

SID = "69696969-0000-4000-8000-000000000004"
NEW_SPEC = {"spec_version": 2, "launch": {"remote_control_key": "remote_control"}}
ROW = {
    "pid": 71452,
    "session_id": SID,
    "account": "account_b",
    "status": "idle",
    "win_session": 1,
    "name": "example-game-b",
    "url": "https://claude.ai/code/session_OLD",
}


class Fake5002:
    """5002 の代役。呼ばれた順に (method, path, body) を残す。"""

    def __init__(
        self,
        spec=NEW_SPEC,
        row=ROW,
        start_status=200,
        new_url="https://claude.ai/code/session_NEW",
        resumable=(SID,),
        assigned="account_b",
    ):
        self.spec, self.row, self.start_status = spec, dict(row), start_status
        self.new_url, self.resumable, self.assigned = new_url, resumable, assigned
        self.calls = []
        self.running = [self.row]

    def __call__(self, method, path, body=None, timeout=300):
        self.calls.append((method, path, body))
        if path == "/api/integration/spec":
            return 200, self.spec
        if path.startswith("/api/projects/resumable"):
            return 200, {"items": [{"session_id": s} for s in self.resumable]}
        if path.startswith("/api/accounts?project="):
            return 200, {"project": {"account": self.assigned}}
        if path == "/api/sessions" and method == "GET":
            return 200, {"sessions": list(self.running)}
        if path.endswith("/stop"):
            self.running = []
            return 200, {"ok": True}
        if path == "/api/sessions" and method == "POST":
            if self.start_status != 200:
                return self.start_status, {"ok": False, "error": "boom"}
            new = dict(self.row, pid=80000, url=self.new_url)
            self.running = [new]
            return 200, {"ok": True, "session": {"pid": 80000}}
        raise AssertionError(path)


def run(monkeypatch, capsys, fake, *extra):
    monkeypatch.setattr(tool, "call", fake)
    monkeypatch.setattr(
        tool.sm,
        "launch_options_of",
        lambda pid, dropped=None: {"permission_mode": "dontAsk"},
    )
    monkeypatch.setattr(tool.time, "sleep", lambda sec: None)
    argv = [
        "x",
        "--project",
        "developing/example-session-tool/projects/example-game-b",
        "--pid",
        "71452",
        "--session-id",
        SID,
        "--account",
        "account_b",
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    code = tool.main()
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l]
    return code, lines


def test_restarts_same_conversation_with_title(monkeypatch, capsys):
    fake = Fake5002()
    code, lines = run(monkeypatch, capsys, fake)
    assert code == 0
    posts = [c for c in fake.calls if c[0] == "POST"]
    assert posts[0][1] == "/api/sessions/71452/stop"
    start = posts[1][2]
    assert start["resume"] == SID and start["remote_control"] is True
    assert start["account"] == "account_b" and start["win_session"] == "1"
    assert start["permission_mode"] == "dontAsk"  # 元のモードを引き継ぐ
    assert start["prompt"] == tool.DEFAULT_PROMPT
    assert "elevation" not in start  # 一覧に elevated が無ければ渡さない
    assert lines[-1]["step"] == "started"


def test_carries_elevation_from_row(monkeypatch, capsys):
    fake = Fake5002(row=dict(ROW, elevated=False))
    code, _ = run(monkeypatch, capsys, fake)
    start = [c for c in fake.calls if c[0] == "POST"][1][2]
    assert code == 0 and start["elevation"] == "standard"


def test_same_row_is_not_reported_as_renamed(monkeypatch, capsys):
    """同じアカウントの resume で前の行につなぎ直したら、成功（0）にせず 3 で知らせる。"""
    fake = Fake5002(new_url=ROW["url"])
    code, lines = run(monkeypatch, capsys, fake)
    assert code == 3 and lines[-1]["step"] == "started_same_row"


def test_url_never_appears_keeps_new_pid(monkeypatch, capsys):
    fake = Fake5002(new_url=None)
    monkeypatch.setattr(tool, "REGISTER_TIMEOUT_SEC", 0)  # 実時間で待たない
    code, lines = run(monkeypatch, capsys, fake)
    assert code == 4 and lines[-1]["step"] == "started_unconfirmed"
    assert lines[-1]["pid"] == 80000


def test_connection_error_after_stop_prints_restore(monkeypatch, capsys):
    fake = Fake5002()
    real = fake.__call__

    def broken(method, path, body=None, timeout=300):
        if method == "POST" and path == "/api/sessions":
            return 0, {"error": "URLError: timed out"}
        return real(method, path, body, timeout)

    code, lines = run(monkeypatch, capsys, broken)
    assert code == 1 and "restore" in lines[-1]


@pytest.mark.parametrize(
    "fake",
    [
        Fake5002(
            spec={"spec_version": 2, "launch": {}}
        ),  # 古い版（題名を黙って無視する）
        Fake5002(row=dict(ROW, status="busy")),  # 作業中
        Fake5002(row=dict(ROW, session_id="other")),  # 会話が違う
        Fake5002(row=dict(ROW, account="account_a")),  # アカウントが違う
        Fake5002(resumable=("other",)),  # project の取り違え（その作業場の会話でない）
        Fake5002(assigned="account_a"),  # 割り当てが違う（止めた後に断られる）
        Fake5002(row=dict(ROW, win_session=2)),  # 本アプリが起こせない起動先
    ],
)
def test_refuses_without_changing_anything(monkeypatch, capsys, fake):
    code, lines = run(monkeypatch, capsys, fake)
    assert code == 2 and lines[-1]["step"] == "refused"
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_dry_run_does_not_stop(monkeypatch, capsys):
    fake = Fake5002()
    code, lines = run(monkeypatch, capsys, fake, "--dry-run")
    assert code == 0 and lines[-1]["step"] == "dry_run"
    assert not [c for c in fake.calls if c[0] == "POST"]


def test_failed_start_prints_restore_without_title(monkeypatch, capsys):
    fake = Fake5002(start_status=400)
    code, lines = run(monkeypatch, capsys, fake)
    assert code == 1 and lines[-1]["step"] == "failed"
    assert '"resume": "' + SID in lines[-1]["restore"]
    assert '"remote_control": false' in lines[-1]["restore"]

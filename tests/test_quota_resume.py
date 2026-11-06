# -*- coding: utf-8 -*-
"""枠が戻った子への続きの依頼（quota_resume.py）・管理役への知らせ（liaison.py）・
会話の末尾を読む口（transcript_tail.py・GET /api/sessions/<pid>/tail）の試験。

本物の子へは何も送らない（送る関数は差し替える）。送り箱へも置かない（claude_send は偽物）。

実行方法:
    <python> -m pytest tests/test_quota_resume.py --rootdir <このプロジェクト>
"""

import importlib.util
import json
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import accounts
import liaison
import quota_resume as qr
import send_history
import session_manager as sm
import transcript_tail

T0 = 1_800_000_000.0
RESETS = T0 - 600  # 10分前に戻った


def session(**extra):
    base = {
        "pid": 201,
        "session_id": "s-201",
        "name": "blog",
        "cwd": r"C:\dev\developing\blog",
        "kind": "interactive",
        "status": "idle",
        "status_updated_at": (T0 - 3600) * 1000,
        "account": "account_b",
        "launched_by_app": True,
    }
    base.update(extra)
    return base


def stop(resets=RESETS, at="2027-01-15T07:00:00.000Z"):
    return {
        "window": "five_hour",
        "window_label": "5時間枠",
        "resets_at_epoch": resets,
        "resets_at": "1/15 17:00",
        "at": at,
        "recovered": resets is not None and resets <= T0,
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """送った履歴・会話記録の照合を一時フォルダへ向ける。"""
    monkeypatch.setattr(send_history, "HISTORY_FILE", str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(send_history, "listeners", [])
    monkeypatch.setattr(sm, "_transcript_sizes", lambda sid: {})
    monkeypatch.setattr(
        sm, "_transcript_has_prompt", lambda sid, text, offsets=None: False
    )
    monkeypatch.setattr(sm, "is_relogin_session", lambda s: False)


class Recorder:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, pid, **kwargs):
        self.calls.append((pid, kwargs))
        if self.error:
            raise self.error
        return {"via": "inbox"}


def make(tmp_path, found=None, sender=None, notes=None):
    found = found if found is not None else {"s-201": stop()}
    notes = notes if notes is not None else []
    resume = qr.QuotaResume(
        lambda cwd, sid: sid,
        sender=sender or Recorder(),
        notifier=lambda kind, key, text: notes.append((kind, key, text)),
        state_file=str(tmp_path / "quota_resume.json"),
        quota_stop=lambda path, now=None: found.get(path),
    )
    return resume, notes


ON = {"quota_resume_mode": "on"}


# --- quota_resume ---------------------------------------------------------


def test_sends_once_with_careful_text(tmp_path):
    sender = Recorder()
    resume, notes = make(tmp_path, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 1
    assert len(sender.calls) == 1
    pid, kwargs = sender.calls[0]
    assert pid == 201
    text = kwargs["text"]
    assert "枠切れで中断した作業だけ" in text
    assert "再開しないでください" in text
    assert "5時間枠" in text
    assert kwargs["protect_draft"] is True and kwargs["allow_unmanaged"] is True
    assert len(text) < sm.PASTE_THRESHOLD_CHARS
    # 同じ枠切れへは二度送らない（再起動の後も）
    assert resume.tick([session()], ON, now=T0 + 120) == 0
    again, _ = make(tmp_path, sender=sender)
    assert again.tick([session()], ON, now=T0 + 240) == 0
    assert len(sender.calls) == 1
    # 本アプリが頼む形・頼めたときは管理役を起こさない
    assert notes == []
    assert "【" not in sender.calls[0][1]["text"][:1]
    history = [json.loads(l) for l in open(send_history.HISTORY_FILE, encoding="utf-8")]
    assert history[-1]["kind"] == "quota_resume" and history[-1]["ok"] is True
    assert resume.state_for("s-201", stop()) == "sent"


def test_shell_status_is_sent(tmp_path):
    """裏の仕事を持った入力待ち（status=shell）へも続きを頼む（2026-11-03・評価役の中3）。"""
    sender = Recorder()
    resume, notes = make(tmp_path, sender=sender)
    assert resume.tick([session(status="shell")], ON, now=T0) == 1
    assert len(sender.calls) == 1 and notes == []


def test_not_idle_after_reset_is_told_once(tmp_path):
    """戻っても入力待ちにならない子は、30分続いたら管理役へ1回知らせる（6時間まで黙らない・2026-11-02）。"""
    sender = Recorder()
    resume, notes = make(tmp_path, sender=sender)
    busy = session(status="busy")
    assert resume.tick([busy], ON, now=T0) == 0
    assert resume.tick([busy], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC - 60) == 0
    assert notes == []
    resume.tick([busy], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC)
    assert [n[0] for n in notes] == ["quota_not_idle"]
    assert "入力待ちにならず" in notes[0][2] and "状態 busy" in notes[0][2]
    assert "quota_not_idle" in liaison.KIND_LABELS
    # 二度は知らせない（再起動の後も）
    resume.tick([busy], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 600)
    again, again_notes = make(tmp_path, sender=sender)
    again.tick([busy], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 900)
    assert len(notes) == 1 and again_notes == []
    # 入力待ちになれば今までどおり頼む
    assert resume.tick([session()], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 1200) == 1
    assert len(sender.calls) == 1


def test_waiting_child_is_left_to_attention_notice(tmp_path):
    """確かな許可待ち・答え待ちの子は liaison の別の知らせに任せ、確かでなければこちらが知らせる（評価役の中）。"""
    resume, notes = make(tmp_path)
    certain = session(
        status="waiting", attention={"kind": "permission", "certain": True}
    )
    resume.tick([certain], ON, now=T0)
    resume.tick([certain], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 60)
    assert notes == []
    unsure = session(
        status="waiting", attention={"kind": "permission", "certain": False}
    )
    resume.tick([unsure], ON, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 120)
    assert [n[0] for n in notes] == ["quota_not_idle"]


def test_switch_to_dry_run_after_detection_is_told(tmp_path):
    """on で見つけて（知らせず）、戻る前に試しの設定へ切り替わったら、頼まなかったことを知らせる（2026-11-02）。"""
    found = {"s-201": stop(resets=T0 + 600)}
    sender = Recorder()
    resume, notes = make(tmp_path, found=found, sender=sender)
    resume.tick([session()], ON, now=T0)
    assert notes == []
    resume.tick(
        [session()], {"quota_resume_mode": "dry_run"}, now=T0 + 600 + qr.GRACE_SEC
    )
    assert sender.calls == []
    assert [n[0] for n in notes] == ["quota_gave_up"]
    assert "続きを頼まない" in notes[0][2]


def test_dry_run_tells_with_same_key_for_dedupe(tmp_path):
    """見つけたときから試しの設定なら、頼まなかった知らせは見つけたときと同じ鍵（liaison が二度は送らない）。"""
    dry = {"quota_resume_mode": "dry_run"}
    resume, notes = make(tmp_path)
    resume.tick([session()], dry, now=T0)
    assert len(notes) == 2
    assert notes[0][:2] == notes[1][:2]


def test_handed_in_dry_run_is_not_sent_after_switch_to_on(tmp_path):
    """試しの設定で見つけて管理役へ「頼まない」と知らせた枠切れは、on に戻っても頼まない（二重の依頼を防ぐ・評価役の中）。"""
    found = {"s-201": stop(resets=T0 + 600)}
    sender = Recorder()
    resume, notes = make(tmp_path, found=found, sender=sender)
    resume.tick([session()], {"quota_resume_mode": "dry_run"}, now=T0)
    assert [n[0] for n in notes] == ["quota_gave_up"]
    assert resume.tick([session()], ON, now=T0 + 600 + qr.GRACE_SEC + 120) == 0
    assert sender.calls == []
    assert resume.state_for("s-201", found["s-201"]) == "handed"
    # 管理役に任せた記録は、再起動の後も頼まない
    again, _ = make(tmp_path, found=found, sender=sender)
    assert again.tick([session()], ON, now=T0 + 600 + qr.GRACE_SEC + 300) == 0
    assert sender.calls == []


def test_handed_carries_to_new_stop_line_with_same_reset(tmp_path):
    """管理役が戻る前に手で頼んでまた枠切れになっても（鍵が変わる）、任せた印を引き継いで頼まない（評価役2周目の中）。"""
    resets = T0 + 600
    found = {"s-201": stop(resets=resets)}
    sender = Recorder()
    resume, _ = make(tmp_path, found=found, sender=sender)
    resume.tick([session()], {"quota_resume_mode": "dry_run"}, now=T0)
    found["s-201"] = stop(resets=resets, at="2027-01-15T07:05:00.000Z")
    assert resume.tick([session()], ON, now=resets + qr.GRACE_SEC + 120) == 0
    assert sender.calls == []
    assert resume.state_for("s-201", found["s-201"]) == "handed"


def test_not_idle_is_not_told_in_dry_run(tmp_path):
    """試しの設定では頼まないので、「入力待ちになれば頼む」とは知らせない（評価役の中）。"""
    resume, notes = make(tmp_path)
    busy = session(status="busy")
    dry = {"quota_resume_mode": "dry_run"}
    resume.tick([busy], dry, now=T0)
    resume.tick([busy], dry, now=T0 + qr.NOT_IDLE_NOTIFY_SEC + 60)
    assert "quota_not_idle" not in [n[0] for n in notes]


def test_new_stop_in_same_conversation_is_sent_again(tmp_path):
    found = {"s-201": stop()}
    sender = Recorder()
    resume, _ = make(tmp_path, found=found, sender=sender)
    resume.tick([session()], ON, now=T0)
    found["s-201"] = stop(resets=T0 + 3000, at="2027-01-15T09:00:00.000Z")
    assert resume.tick([session()], ON, now=T0 + 3000 + qr.GRACE_SEC) == 1
    assert len(sender.calls) == 2


def test_waits_before_reset_and_grace(tmp_path):
    sender = Recorder()
    found = {"s-201": stop(resets=T0 + 100)}
    resume, notes = make(tmp_path, found=found, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 0
    # 戻りの時刻を過ぎても GRACE_SEC までは送らない
    assert resume.tick([session()], ON, now=T0 + 100 + qr.GRACE_SEC - 1) == 0
    assert not sender.calls
    assert notes == []
    assert resume.tick([session()], ON, now=T0 + 100 + qr.GRACE_SEC) == 1


def test_stopped_again_with_same_reset_is_not_resent(tmp_path):
    """頼んだ文がまた枠切れになり、戻りの時刻が古いまま新しい行が載ったら、30分あけて1回だけ頼み直し、
    それでも駄目なら諦めて管理役へ知らせる（積もるのは最大2通）。"""
    found = {"s-201": stop()}
    sender = Recorder()
    resume, notes = make(tmp_path, found=found, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 1
    found["s-201"] = stop(at="2027-01-15T08:00:00.000Z")
    assert resume.tick([session()], ON, now=T0 + 120) == 0
    assert resume.tick([session()], ON, now=T0 + 240) == 0
    assert len(sender.calls) == 1 and not notes
    # 画面には「もう1回頼むまで待っている」と出す（waiting のままだと「数分のうちに」と事実と違う）
    assert resume.state_for("s-201", found["s-201"]) == "again_wait"
    assert resume.again_wait_text("s-201", found["s-201"])
    # 待ちの間にまた枠切れの行が載っても、30分を一から数え直さない
    found["s-201"] = stop(at="2027-01-15T08:10:00.000Z")
    resume.tick([session()], ON, now=T0 + 600)
    waits = {
        v.get("again_wait_until")
        for v in resume.status()["stops"].values()
        if v.get("again_wait_until")
    }
    assert waits == {T0 + 120 + qr.AGAIN_RETRY_SEC}
    t2 = T0 + 120 + qr.AGAIN_RETRY_SEC
    assert resume.tick([session()], ON, now=t2) == 1
    assert len(sender.calls) == 2
    found["s-201"] = stop(at="2027-01-15T09:00:00.000Z")
    assert resume.tick([session()], ON, now=t2 + 120) == 0
    assert resume.tick([session()], ON, now=t2 + qr.AGAIN_RETRY_SEC + 300) == 0
    assert len(sender.calls) == 2
    assert resume.state_for("s-201", found["s-201"]) == "again"
    assert [n[0] for n in notes] == ["quota_gave_up"]
    assert "まだ戻っていなかった" in notes[0][2]


def test_task_notification_after_limit_still_counts_as_stopped(tmp_path):
    """枠切れの直後にサブエージェントの完了通知が載っても、止まったままと見る（2026-10-31 の実例の形）。"""
    path = tmp_path / "s.jsonl"
    rows = [
        {
            "type": "user",
            "message": {"content": "作業して"},
            "origin": {"kind": "human"},
        },
        {
            "type": "assistant",
            "error": "rate_limit",
            "message": {
                "content": [{"type": "text", "text": "You've hit your session limit"}]
            },
            "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": RESETS},
            "timestamp": "t2",
        },
        {"type": "system"},
        {"type": "queue-operation"},
        {
            "type": "user",
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "promptSource": "system",
            "queueTranscriptOnly": True,
            "message": {
                "content": "<task-notification><task-id>a1</task-id></task-notification>"
            },
        },
    ]
    write_rows(path, rows)
    accounts._quota_cache.clear()
    found = accounts.quota_stop(str(path), T0)
    assert found and found["window"] == "five_hour"
    items = transcript_tail.read_tail(str(path))
    assert items[-2]["text"] == "（枠切れで応答できませんでした）"
    assert items[-1]["text"] == "（サブエージェント・裏の仕事の完了通知）"


def test_too_old_after_waiting_is_told(tmp_path):
    """見つけた後、書きかけ等で送れないまま6時間を過ぎたら管理役へ知らせる。"""
    sender = Recorder(error=sm.SessionDraftError("書きかけ"))
    resume, notes = make(tmp_path, sender=sender)
    resume.tick([session()], ON, now=T0)
    assert resume.tick([session()], ON, now=RESETS + qr.MAX_AGE_SEC + 1) == 0
    assert resume.state_for("s-201", stop()) == "too_old"
    assert [n[0] for n in notes] == ["quota_gave_up"] and "6時間を過ぎた" in notes[0][2]


def test_not_sent_when_busy_or_settling_or_not_stopped(tmp_path):
    sender = Recorder()
    resume, _ = make(tmp_path, sender=sender)
    assert resume.tick([session(status="busy")], ON, now=T0) == 0
    assert resume.tick([session(status_updated_at=(T0 - 10) * 1000)], ON, now=T0) == 0
    other, _ = make(tmp_path / "x", found={}, sender=sender)
    assert other.tick([session()], ON, now=T0) == 0
    assert not sender.calls


def test_too_old_at_first_sight_is_told_once(tmp_path):
    sender = Recorder()
    found = {"s-201": stop(resets=T0 - qr.MAX_AGE_SEC - 1)}
    resume, notes = make(tmp_path, found=found, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 0
    assert resume.tick([session()], ON, now=T0 + 60) == 0
    assert not sender.calls
    assert len(notes) == 1 and "6時間を過ぎていた" in notes[0][2]
    assert resume.state_for("s-201", found["s-201"]) == "too_old"


def test_reset_unknown_is_not_sent(tmp_path):
    sender = Recorder()
    found = {"s-201": stop(resets=None)}
    resume, _ = make(tmp_path, found=found, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 0
    assert not sender.calls


def test_modes(tmp_path):
    sender = Recorder()
    resume, _ = make(tmp_path, sender=sender)
    assert resume.tick([session()], {"quota_resume_mode": "off"}, now=T0) == 0
    assert resume.tick([session()], {"quota_resume_mode": "dry_run"}, now=T0) == 1
    assert not sender.calls
    assert resume.state_for("s-201", stop()) == "would_send"
    # 知らない値は既定（on）
    assert qr.mode_of({"quota_resume_mode": "yes"}) == "on"
    assert qr.mode_of({}) == "on"


def test_stop_notice_matches_mode_and_reset(tmp_path):
    """管理役への「止まった」の文は、本アプリが頼むかどうかと事実が合う。"""
    future = {"s-201": stop(resets=T0 + 3600)}
    on, notes_on = make(tmp_path / "on", found=future)
    on.tick([session()], ON, now=T0)
    assert notes_on == []
    dry, notes_dry = make(tmp_path / "dry", found=future)
    dry.tick([session()], {"quota_resume_mode": "dry_run"}, now=T0)
    assert "頼まない" in notes_dry[0][2] and "ただの『続けて』は不可" in notes_dry[0][2]
    unknown, notes_unknown = make(
        tmp_path / "unknown", found={"s-201": stop(resets=None)}
    )
    unknown.tick([session()], ON, now=T0)
    assert "戻る時刻が分からない" in notes_unknown[0][2]


def test_skips_print_mode_and_relogin(tmp_path, monkeypatch):
    sender = Recorder()
    resume, _ = make(tmp_path, sender=sender)
    assert resume.tick([session(kind="print")], ON, now=T0) == 0
    monkeypatch.setattr(sm, "is_relogin_session", lambda s: True)
    assert resume.tick([session()], ON, now=T0) == 0
    assert not sender.calls


def test_draft_waits_without_counting(tmp_path):
    sender = Recorder(error=sm.SessionDraftError("書きかけ"))
    resume, notes = make(tmp_path, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 0
    assert resume.tick([session()], ON, now=T0 + 60) == 0
    assert len(sender.calls) == 1  # 間をおく
    sender.error = None
    assert resume.tick([session()], ON, now=T0 + qr.DRAFT_RETRY_SEC) == 1


def test_busy_retries_next_tick(tmp_path):
    sender = Recorder(error=sm.SessionBusyError("作業中"))
    resume, _ = make(tmp_path, sender=sender)
    resume.tick([session()], ON, now=T0)
    sender.error = None
    assert resume.tick([session()], ON, now=T0 + 60) == 1


def test_failures_give_up_and_tell(tmp_path):
    sender = Recorder(error=sm.SessionError("入力欄が見つかりません 画面の末尾: 秘密"))
    resume, notes = make(tmp_path, sender=sender)
    now = T0
    for _ in range(qr.MAX_ATTEMPTS):
        resume.tick([session()], ON, now=now)
        now += qr.RETRY_SEC * qr.MAX_ATTEMPTS
    assert len(sender.calls) == qr.MAX_ATTEMPTS
    assert resume.state_for("s-201", stop()) == "failed"
    resume.tick([session()], ON, now=now)
    assert len(sender.calls) == qr.MAX_ATTEMPTS
    failed = [n for n in notes if n[0] == "quota_resume_failed"]
    assert len(failed) == 1 and "秘密" not in failed[0][2]


def test_landed_despite_error_counts_as_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sm, "_transcript_has_prompt", lambda sid, text, offsets=None: True
    )
    sender = Recorder(error=sm.SessionError("照合が時間切れ"))
    resume, _ = make(tmp_path, sender=sender)
    assert resume.tick([session()], ON, now=T0) == 1
    assert resume.state_for("s-201", stop()) == "sent"


def test_with_real_transcript(tmp_path):
    """本物の quota_stop で、枠切れの行・その後の人の発言を見分ける。"""
    path = tmp_path / "s.jsonl"
    rows = [
        {"type": "user", "message": {"content": "作業して"}, "timestamp": "t1"},
        {
            "type": "assistant",
            "error": "rate_limit",
            "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": RESETS},
            "timestamp": "t2",
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sender = Recorder()
    resume = qr.QuotaResume(
        lambda cwd, sid: str(path),
        sender=sender,
        state_file=str(tmp_path / "state.json"),
    )
    assert resume.tick([session()], ON, now=T0) == 1
    # 人が止めた（最後が人の発言）会話には送らない
    accounts._quota_cache.clear()
    rows.append({"type": "user", "message": {"content": "止めて"}, "timestamp": "t3"})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    other = qr.QuotaResume(
        lambda cwd, sid: str(path), sender=sender, state_file=str(tmp_path / "s2.json")
    )
    assert other.tick([session()], ON, now=T0) == 0
    assert len(sender.calls) == 1


# --- liaison --------------------------------------------------------------


class FakeTool:
    def __init__(self, tmp_path, fail=False):
        self.puts = []
        self.fail = fail
        self.spool = str(tmp_path / "spool")

    def check(self, sender, to, text):
        return "長すぎ" if len(text) > 2000 else ""

    def load_follower(self):
        return types.SimpleNamespace(SPOOL=self.spool)

    def put(self, spool, sender, to, text):
        if self.fail:
            raise OSError("書けない")
        self.puts.append((spool, sender, to, text))
        return os.path.join(spool, f"{sender}-{len(self.puts)}.json")


LIAISON_ON = {"liaison": {"tool": "C:/x/claude_send.py"}}


def make_liaison(tmp_path, tool=None, settings=LIAISON_ON):
    tool = tool or FakeTool(tmp_path)
    notes = liaison.Liaison(
        state_file=str(tmp_path / "liaison.json"),
        settings_loader=lambda: settings,
        tool_loader=lambda path: tool,
    )
    return notes, tool


def test_dry_run_notices_reach_liaison_once(tmp_path):
    """試しの設定で見つけたときの知らせと、頼まなかったときの知らせは、本物の Liaison を通すと1件だけ溜まる（2026-11-02）。"""
    notes, _ = make_liaison(tmp_path)
    resume = qr.QuotaResume(
        lambda cwd, sid: sid,
        sender=Recorder(),
        notifier=lambda kind, key, text: notes.notify(kind, key, text, now=T0),
        state_file=str(tmp_path / "quota_resume.json"),
        quota_stop=lambda path, now=None: {"s-201": stop()}.get(path),
    )
    resume.tick([session()], {"quota_resume_mode": "dry_run"}, now=T0)
    pending = notes.status()["pending"]
    assert len(pending) == 1


def test_liaison_disabled_without_tool(tmp_path):
    notes, tool = make_liaison(tmp_path, settings={})
    assert notes.notify("quota_stop", "k", "x", now=T0) is False
    assert notes.flush(now=T0 + 999) == 0
    assert not tool.puts


def test_liaison_batches_and_dedupes(tmp_path):
    notes, tool = make_liaison(tmp_path)
    assert notes.notify("quota_gave_up", "k1", "blog が止まった", now=T0)
    assert not notes.notify("quota_gave_up", "k1", "blog が止まった", now=T0 + 1)
    assert notes.notify("send_failed", "k2", "送れなかった", now=T0 + 5)
    # まとめる間を待つ
    assert notes.flush(now=T0 + 10) == 0
    assert notes.flush(now=T0 + liaison.BATCH_SEC) == 2
    spool, sender, to, text = tool.puts[0]
    assert (sender, to) == ("claude-web-app", "管理役")
    assert "ユーザーへは知らせていません" in text
    assert "[枠切れ・本アプリは頼まない・要対応] blog が止まった" in text
    assert "[送れなかった・要対応]" in text
    # 次の1通は間をおく
    notes.notify("quota_stop", "k3", "次", now=T0 + 60)
    assert notes.flush(now=T0 + 120) == 0
    assert notes.flush(now=T0 + liaison.BATCH_SEC + liaison.MIN_INTERVAL_SEC) == 1
    # 覚えた鍵は作り直しても残る
    again, _ = make_liaison(tmp_path, tool=tool)
    assert not again.notify("quota_gave_up", "k1", "blog が止まった", now=T0 + 999)


def test_liaison_keeps_pending_when_settings_unreadable(tmp_path):
    """設定を一瞬読めない（既定＝tool が空）だけで、溜めた知らせを捨てない。"""
    settings = dict(LIAISON_ON)
    notes = liaison.Liaison(
        state_file=str(tmp_path / "liaison.json"),
        settings_loader=lambda: settings,
        tool_loader=lambda path: FakeTool(tmp_path),
    )
    notes.notify("send_failed", "k1", "x", now=T0)
    assert notes.flush(settings={}, now=T0 + 100) == 0
    assert len(notes.status()["pending"]) == 1


def test_liaison_self_clear_draft_is_told(tmp_path):
    """区切りの予約は書きかけで止まる（送り直さない）ので、書きかけでも知らせる。"""
    notes, _ = make_liaison(tmp_path)
    notes.on_history(
        {
            "ok": False,
            "kind": "self_clear",
            "source": "self_clear",
            "code": "SessionDraftError",
            "target": {"pid": 5, "name": "blog", "session_id": "s-5"},
            "error": "書きかけ",
        }
    )
    assert len(notes.status()["pending"]) == 1


def test_liaison_drops_screen_tail_from_errors(tmp_path):
    notes, _ = make_liaison(tmp_path)
    notes.on_history(
        {
            "ok": False,
            "kind": "self_clear",
            "source": "self_clear",
            "target": {"pid": 5, "name": "blog", "session_id": "s-5"},
            "error": "入力欄が見つかりません 画面の末尾: 別の子の画面の中身",
        }
    )
    text = notes.status()["pending"][0]["text"]
    assert "入力欄が見つかりません" in text and "別の子の画面" not in text


def test_liaison_keeps_pending_on_failure(tmp_path):
    tool = FakeTool(tmp_path, fail=True)
    notes, _ = make_liaison(tmp_path, tool=tool)
    notes.notify("quota_stop", "k1", "x", now=T0)
    assert notes.flush(now=T0 + 100) == 0
    assert len(notes.status()["pending"]) == 1
    tool.fail = False
    assert notes.flush(now=T0 + 101) == 0  # 置き直しの間
    assert notes.flush(now=T0 + 100 + liaison.RETRY_SEC) == 1
    assert not notes.status()["pending"]


def test_liaison_compose_splits_long(tmp_path):
    pending = [{"kind": "send_failed", "text": "あ" * 300, "at_epoch": T0}] * 10
    text, used = liaison.compose(pending)
    assert len(text) <= liaison.MAX_TEXT
    assert 0 < used < 10 and f"ほか {10 - used} 件" in text


def test_liaison_history_filter(tmp_path):
    notes, _ = make_liaison(tmp_path)
    target = {"pid": 5, "name": "blog", "session_id": "s-5"}
    base = {
        "ok": False,
        "kind": "auto_clear",
        "source": "auto_clear",
        "target": target,
        "error": "no_input_area",
    }
    notes.on_history(dict(base))
    notes.on_history(dict(base))  # 送り直しの失敗は1回だけ
    notes.on_history(
        dict(base, code="SessionDraftError", target=dict(target, session_id="s-6"))
    )
    notes.on_history(dict(base, source="screen", target=dict(target, session_id="s-7")))
    notes.on_history(dict(base, ok=True, target=dict(target, session_id="s-8")))
    notes.on_history(
        dict(
            base,
            kind="machine",
            source="machine:管理役",
            target=dict(target, session_id="s-9"),
        )
    )
    pending = notes.status()["pending"]
    assert len(pending) == 2
    assert (
        "自動の区切りの依頼" in pending[0]["text"]
        and "blog（pid 5）" in pending[0]["text"]
    )


def test_liaison_permission_waits(tmp_path):
    notes, _ = make_liaison(tmp_path)
    since = (T0 - 11 * 60) * 1000
    sessions = [
        session(
            pid=1,
            attention={
                "kind": "permission",
                "label": "許可待ち",
                "since": since,
                "certain": True,
            },
        ),
        session(
            pid=2,
            attention={
                "kind": "permission",
                "label": "許可待ち",
                "since": (T0 - 60) * 1000,
                "certain": True,
            },
        ),
        session(
            pid=3,
            attention={
                "kind": "permission",
                "label": "かも",
                "since": since,
                "certain": False,
            },
        ),
        session(
            pid=4,
            attention={
                "kind": "input",
                "label": "答え待ち",
                "since": since,
                "certain": True,
            },
        ),
        session(pid=5),
    ]
    assert notes.observe_sessions(sessions, now=T0) == 2
    assert notes.observe_sessions(sessions, now=T0 + 60) == 0
    kinds = [p["kind"] for p in notes.status()["pending"]]
    assert kinds == ["permission", "input"]
    assert "11分" in notes.status()["pending"][0]["text"]


def test_history_listener_errors_do_not_break_record(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(send_history, "listeners", [lambda e: 1 / 0, seen.append])
    entry = send_history.record("auto_clear", ok=False, source="auto_clear", error="x")
    assert seen == [entry]


# --- transcript_tail ------------------------------------------------------


def write_rows(path, rows):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_tail_picks_text_only(tmp_path):
    path = tmp_path / "t.jsonl"
    write_rows(
        path,
        [
            {
                "type": "user",
                "message": {"content": "<command-name>/clear</command-name>"},
            },
            {
                "type": "user",
                "message": {"content": "README を読んで token=abcd1234efgh"},
                "timestamp": "2027-01-15T01:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "秘めた考え"},
                        {"type": "tool_use", "name": "Read"},
                    ]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "中身"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "読みました"}]},
                "timestamp": "2027-01-15T01:01:00Z",
            },
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "子の話"}]},
            },
            {"type": "user", "isMeta": True, "message": {"content": "スキルの中身"}},
            {"type": "assistant", "error": "rate_limit", "message": {"content": []}},
        ],
    )
    items = transcript_tail.read_tail(str(path))
    assert [i["role"] for i in items] == ["user", "assistant", "assistant"]
    assert "[伏せ字]" in items[0]["text"] and "abcd1234" not in items[0]["text"]
    assert items[1]["tools"] == ["Read", "Bash"] and items[1]["text"] == "読みました"
    assert "枠切れで応答できませんでした" in items[2]["text"]
    joined = json.dumps(items, ensure_ascii=False)
    assert (
        "秘めた考え" not in joined and "子の話" not in joined and "中身" not in joined
    )


def test_tail_limit_and_long_file(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    write_rows(
        path,
        [{"type": "user", "message": {"content": f"発言 {i}"}} for i in range(300)],
    )
    monkeypatch.setattr(transcript_tail, "TAIL_BYTES", 2000)
    items = transcript_tail.read_tail(str(path), 5)
    assert [i["text"] for i in items] == [f"発言 {i}" for i in range(295, 300)]
    assert len(transcript_tail.read_tail(str(path), 9999)) <= transcript_tail.MAX_LIMIT
    with pytest.raises(OSError):
        transcript_tail.read_tail(str(tmp_path / "none.jsonl"))


@pytest.fixture(scope="module")
def app_module():
    spec = importlib.util.spec_from_file_location(
        "claude_web_app_quota", os.path.join(PROJECT_ROOT, "claude-web-app.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tail_endpoint(app_module, tmp_path, monkeypatch):
    import access_guard

    path = tmp_path / "s-201.jsonl"
    write_rows(path, [{"type": "user", "message": {"content": "こんにちは"}}])
    monkeypatch.setattr(app_module, "guard", access_guard.AccessGuard())
    monkeypatch.setattr(
        sm, "find_session_by_pid", lambda pid: session() if pid == 201 else None
    )
    monkeypatch.setattr(app_module, "transcript_path_for", lambda cwd, sid: str(path))
    client = app_module.app.test_client()
    res = client.get("/api/sessions/201/tail?limit=5")
    assert res.status_code == 200 and res.get_json()["items"][0]["text"] == "こんにちは"
    assert client.get("/api/sessions/999/tail").status_code == 404
    assert client.get("/api/sessions/201/tail?limit=x").status_code == 400
    monkeypatch.setattr(
        app_module, "transcript_path_for", lambda cwd, sid: str(tmp_path / "none")
    )
    # 会話記録がまだ無い（起こしたばかり）は失敗ではなく空
    res = client.get("/api/sessions/201/tail")
    assert res.status_code == 200 and res.get_json()["items"] == []
    # 状態の口
    monkeypatch.setattr(app_module, "load_settings", lambda: {})
    body = client.get("/api/quota-resume").get_json()
    assert body["ok"] and "stops" in body and body["liaison"]["enabled"] is False

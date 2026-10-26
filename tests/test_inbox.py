# -*- coding: utf-8 -*-
"""受け箱（方式 A・mod web-app-inbox）で送る経路の試験。

本物の子や mod は使わない。受け箱のフォルダは一時フォルダに替え、
mod の振る舞いは糸（FakeMod）で真似る。
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_manager as sm  # noqa: E402

OLD_SID = "11111111-2222-3333-4444-555555555555"
NEW_SID = "66666666-7777-8888-9999-aaaaaaaaaaaa"


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class FakeMod(threading.Thread):
    """受け箱を見回り、req を見つけたら behave(req) を1回だけ呼ぶ偽の mod。"""

    def __init__(self, sid, behave):
        super().__init__(daemon=True)
        self.sid = sid
        self.behave = behave
        self.reqs = []
        self.stop = threading.Event()

    def run(self):
        path = sm._inbox_path(self.sid, "req")
        while not self.stop.is_set():
            data = sm._read_json_file(path)
            if data and not data.get("cancelled"):
                self.reqs.append(data)
                self.behave(data)
                return
            time.sleep(0.02)


class InboxTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._originals = {
            name: getattr(sm, name)
            for name in (
                "APP_STATE_DIR",
                "_read_state",
                "is_session_alive",
                "is_launched_by_app",
                "_run_input_sender",
                "_transcript_has_prompt",
                "_transcript_sizes",
                "INBOX_QUEUE_TIMEOUT_SEC",
                "INBOX_WITHDRAW_GRACE_SEC",
                "CLEAR_CONFIRM_TIMEOUT_SEC",
                "TEXT_CONFIRM_TIMEOUT_SEC",
                "STATE_POLL_SEC",
                "INBOX_ENABLED",
                "INBOX_SETTINGS_FILE",
            )
        }
        # この機械の設定ファイル（追跡しない・inbox_enabled を書いていることがある）を読まない
        sm.INBOX_SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")
        sm.APP_STATE_DIR = self.tmp.name
        sm.INBOX_QUEUE_TIMEOUT_SEC = 0.5
        sm.INBOX_WITHDRAW_GRACE_SEC = 0.1
        # 負荷の高い試験の全体の中でも揺れないよう、確かめの待ちには余裕を持たせる
        sm.CLEAR_CONFIRM_TIMEOUT_SEC = 2.0
        sm.TEXT_CONFIRM_TIMEOUT_SEC = 2.0
        sm.STATE_POLL_SEC = 0.02
        sm.INBOX_ENABLED = True

        self.state = {"pid": 4242, "status": "idle", "sessionId": OLD_SID}
        sm._read_state = lambda pid: dict(self.state)
        sm.is_session_alive = lambda session: True
        sm.is_launched_by_app = lambda pid: True
        self.console = []

        def sender(pid, text, prefix=None, keep_draft=False):
            self.console.append(text)
            if text == "/clear":
                self.state["sessionId"] = NEW_SID

        sm._run_input_sender = sender
        # 会話記録に載ったかは、偽の mod が submit した印で見る
        self.submitted = set()
        sm._transcript_has_prompt = (
            lambda session_id, text, offsets=None: (session_id, text) in self.submitted
            or ("console", text) in self.submitted
        )
        sm._transcript_sizes = lambda session_id: {}
        self.mod = None

    def tearDown(self):
        if self.mod:
            self.mod.stop.set()
            self.mod.join(2)
        for name, value in self._originals.items():
            setattr(sm, name, value)
        self.tmp.cleanup()

    def write_alive(self, sid=OLD_SID, age_sec=0.0):
        write_json(
            sm._inbox_path(sid, "alive"),
            {"sid": sid, "version": "1.0.0", "at": (time.time() - age_sec) * 1000},
        )

    def ack(self, req, stage, **extra):
        write_json(
            sm._inbox_path(OLD_SID, "ack"),
            dict({"id": req["id"], "stage": stage}, **extra),
        )

    def start_mod(self, behave):
        self.mod = FakeMod(OLD_SID, behave)
        self.mod.start()

    def good_mod(self, req):
        """本物の mod と同じ順で ack を書き、/clear と本文を「渡す」。"""
        self.ack(req, "queued")
        sid = OLD_SID
        if req["clear"]:
            self.state["sessionId"] = NEW_SID
            sid = NEW_SID
            self.ack(req, "cleared", newSessionId=NEW_SID)
        if req["text"]:
            self.submitted.add((sid, req["text"]))
            self.ack(req, "submitted", newSessionId=sid)


class InboxAliveTest(InboxTestBase):
    def test_fresh_alive_is_seen(self):
        self.write_alive()
        self.assertEqual(sm.inbox_alive(OLD_SID)["version"], "1.0.0")

    def test_stale_alive_is_ignored(self):
        self.write_alive(age_sec=sm.INBOX_ALIVE_FRESH_SEC + 5)
        self.assertIsNone(sm.inbox_alive(OLD_SID))

    def test_alive_of_other_session_is_ignored(self):
        write_json(
            sm._inbox_path(OLD_SID, "alive"), {"sid": NEW_SID, "at": time.time() * 1000}
        )
        self.assertIsNone(sm.inbox_alive(OLD_SID))

    def test_disabled_or_bad_id(self):
        self.write_alive()
        sm.INBOX_ENABLED = False
        self.assertIsNone(sm.inbox_alive(OLD_SID))
        sm.INBOX_ENABLED = True
        self.assertIsNone(sm.inbox_alive("../evil"))
        self.assertIsNone(sm.inbox_alive(None))

    def test_broken_alive_is_ignored(self):
        path = sm._inbox_path(OLD_SID, "alive")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"sid": "')
        self.assertIsNone(sm.inbox_alive(OLD_SID))


class InboxSettingTest(InboxTestBase):
    """設定 inbox_enabled（session_manager が自分で読む・外の道具から送るときも効く）。"""

    def setUp(self):
        super().setUp()
        self._settings = sm.INBOX_SETTINGS_FILE
        sm.INBOX_SETTINGS_FILE = os.path.join(self.tmp.name, "settings.json")
        sm._inbox_settings_cache.update(mtime=None, enabled=True)
        self.write_alive()

    def tearDown(self):
        sm.INBOX_SETTINGS_FILE = self._settings
        sm._inbox_settings_cache.update(mtime=None, enabled=True)
        super().tearDown()

    def write_settings(self, data, mtime):
        with open(sm.INBOX_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.utime(sm.INBOX_SETTINGS_FILE, (mtime, mtime))

    def test_false_stops_inbox(self):
        self.write_settings({"inbox_enabled": False}, 1000)
        self.assertIsNone(sm.inbox_alive(OLD_SID))
        result = sm.send_to_session(4242, clear=True)
        self.assertEqual(result["via"], "console")

    def test_change_is_seen_without_restart(self):
        self.write_settings({"inbox_enabled": False}, 1000)
        self.assertIsNone(sm.inbox_alive(OLD_SID))
        self.write_settings({"inbox_enabled": True}, 2000)
        self.assertIsNotNone(sm.inbox_alive(OLD_SID))

    def test_missing_key_string_or_broken_file_keeps_default(self):
        self.write_settings({"port": 5002}, 1000)
        self.assertIsNotNone(sm.inbox_alive(OLD_SID))
        self.write_settings({"inbox_enabled": "false"}, 2000)
        self.assertIsNotNone(sm.inbox_alive(OLD_SID))
        with open(sm.INBOX_SETTINGS_FILE, "w", encoding="utf-8") as f:
            f.write("{")
        os.utime(sm.INBOX_SETTINGS_FILE, (3000, 3000))
        self.assertIsNotNone(sm.inbox_alive(OLD_SID))


class DraftModeTest(unittest.TestCase):
    def test_table(self):
        # 利用者の決定 (b): 台帳の子＝残す・台帳に無い子＝断る・discard_draft＝消す・自分を区切る予約＝断る
        self.assertEqual(sm.draft_mode_for(True), "keep")
        self.assertEqual(sm.draft_mode_for(False), "refuse")
        self.assertEqual(sm.draft_mode_for(False, discard_draft=True), "discard")
        self.assertEqual(sm.draft_mode_for(True, discard_draft=True), "discard")
        self.assertEqual(
            sm.draft_mode_for(True, discard_draft=True, protect_draft=True), "refuse"
        )


class SendViaInboxTest(InboxTestBase):
    def test_clear_and_text_go_through_inbox(self):
        self.write_alive()
        self.start_mod(self.good_mod)
        result = sm.send_to_session(4242, clear=True, text="次の指示です")
        self.assertEqual(result["via"], "inbox")
        self.assertEqual(result["session_id_before"], OLD_SID)
        self.assertEqual(result["session_id_after"], NEW_SID)
        self.assertEqual(self.console, [])
        req = self.mod.reqs[0]
        self.assertTrue(req["clear"])
        self.assertEqual(req["text"], "次の指示です")
        self.assertEqual(req["draft"], "keep")
        # req は消され、/clear の前の会話の alive・ack も片付く
        self.assertFalse(os.path.exists(sm._inbox_path(OLD_SID, "req")))
        self.assertFalse(os.path.exists(sm._inbox_path(OLD_SID, "alive")))
        self.assertFalse(os.path.exists(sm._inbox_path(OLD_SID, "ack")))

    def test_text_only_has_no_preamble(self):
        self.write_alive()
        self.start_mod(self.good_mod)
        body = "あ" * (sm.PASTE_THRESHOLD_CHARS + 10)
        result = sm.send_to_session(4242, text=body)
        self.assertEqual(result["via"], "inbox")
        self.assertFalse(result["preamble_used"])
        self.assertEqual(result["session_id_after"], OLD_SID)
        self.assertEqual(self.mod.reqs[0]["text"], body)
        self.assertFalse(self.mod.reqs[0]["clear"])

    def test_draft_mode_follows_target(self):
        sm.is_launched_by_app = lambda pid: False
        self.write_alive()
        self.start_mod(self.good_mod)
        sm.send_to_session(4242, text="本文", allow_unmanaged=True)
        self.assertEqual(self.mod.reqs[0]["draft"], "refuse")

    def test_discard_draft_is_passed(self):
        sm.is_launched_by_app = lambda pid: False
        self.write_alive()
        self.start_mod(self.good_mod)
        sm.send_to_session(4242, text="本文", allow_unmanaged=True, discard_draft=True)
        self.assertEqual(self.mod.reqs[0]["draft"], "discard")

    def test_no_ack_falls_back_to_console(self):
        """mod が受け取らなければ req を下げて、今の打鍵で送る。"""
        self.write_alive()
        self.submitted.add(("console", "本文"))
        result = sm.send_to_session(4242, clear=True, text="本文")
        self.assertEqual(result["via"], "console")
        self.assertEqual(self.console, ["/clear", "本文"])
        self.assertFalse(os.path.exists(sm._inbox_path(OLD_SID, "req")))

    def test_stale_alive_uses_console(self):
        self.write_alive(age_sec=sm.INBOX_ALIVE_FRESH_SEC + 5)
        result = sm.send_to_session(4242, clear=True)
        self.assertEqual(result["via"], "console")
        self.assertEqual(self.console, ["/clear"])

    def test_pickup_during_withdraw_is_not_resent(self):
        """下げた後のすき間で mod の queued が見えたら、打鍵で送り直さない（二重に送らない）。"""
        self.write_alive()
        req_path = sm._inbox_path(OLD_SID, "req")
        withdrawn = []

        def late_mod(req):
            # 本物の mod が「読み直し」を済ませた直後に本アプリが req を下げた形:
            # req が消えるのを見てから queued を書く（時間切れの後のすき間の分岐を通す）
            while os.path.exists(req_path):
                time.sleep(0.01)
            withdrawn.append(True)
            self.good_mod(req)

        self.start_mod(late_mod)
        result = sm.send_to_session(4242, clear=True)
        self.assertEqual(withdrawn, [True])
        self.assertEqual(result["via"], "inbox")
        self.assertEqual(self.console, [])

    def test_clear_with_text_when_child_starts_working_at_once(self):
        """本文つきの /clear では、子が新しい id ですぐ作業中になっても /clear を確かめられる（評価の高1）。"""
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            # /clear の直後に本文の作業へ入る（入力待ちの瞬間が無い）
            self.state.update(sessionId=NEW_SID, status="busy")
            self.ack(req, "cleared", newSessionId=NEW_SID)
            self.submitted.add((NEW_SID, req["text"]))
            self.ack(req, "submitted", newSessionId=NEW_SID)

        self.start_mod(mod)
        result = sm.send_to_session(4242, clear=True, text="引き継ぎ")
        self.assertEqual(result["via"], "inbox")
        self.assertEqual(result["session_id_after"], NEW_SID)

    def test_clear_only_still_waits_for_idle(self):
        """本文の無い /clear は、今の打鍵と同じく入力待ちに戻るまで確かめる（陰性対照）。"""
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            self.state.update(sessionId=NEW_SID, status="busy")
            self.ack(req, "cleared", newSessionId=NEW_SID)

        self.start_mod(mod)
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True)
        # 入力待ちには戻っていないが、/clear 自体は済んでいる（id が変わった）と伝える
        self.assertIn("入力待ちに戻った", str(ctx.exception))
        self.assertTrue(ctx.exception.cleared)
        self.assertEqual(ctx.exception.session_id_after, NEW_SID)
        self.assertEqual(ctx.exception.via, "inbox")

    def test_fallback_rechecks_busy(self):
        """受け取りを待つ間に作業へ戻ったら、打鍵へ戻らず busy で断る（評価の中2）。"""
        self.write_alive()

        def turn_busy():
            time.sleep(0.1)
            self.state["status"] = "busy"

        threading.Thread(target=turn_busy, daemon=True).start()
        with self.assertRaises(sm.SessionBusyError):
            sm.send_to_session(4242, text="本文")
        self.assertEqual(self.console, [])

    def test_fallback_waits_remaining_idle_time(self):
        """待つ指定があれば、打鍵へ戻る前の見直しで入力待ちに戻るのを待ってから1回だけ送る。"""
        self.write_alive()
        self.submitted.add(("console", "本文"))

        def busy_then_idle():
            time.sleep(0.1)
            self.state["status"] = "busy"
            # 受け取りの待ち（0.5秒）＋すき間を過ぎた後で入力待ちへ戻る
            time.sleep(1.2)
            self.state["status"] = "idle"

        threading.Thread(target=busy_then_idle, daemon=True).start()
        result = sm.send_to_session(4242, text="本文", wait_idle_sec=5)
        self.assertEqual(result["via"], "console")
        self.assertEqual(self.console, ["本文"])

    def test_fallback_reason_is_reported(self):
        self.write_alive()
        self.submitted.add(("console", "本文"))
        result = sm.send_to_session(4242, text="本文")
        self.assertEqual(result["via"], "console")
        self.assertIn("受け取りませんでした", result["inbox_fallback"])

    def test_inbox_not_tried_reports_no_fallback(self):
        result = sm.send_to_session(4242, clear=True)
        self.assertEqual(result["via"], "console")
        self.assertIsNone(result["inbox_fallback"])

    def test_withdraw_failure_does_not_fall_back(self):
        """req を下げられなければ、mod が後で拾いうるので打鍵で送らない。"""
        self.write_alive()
        original = sm._withdraw_inbox_req
        sm._withdraw_inbox_req = lambda path, data: False
        try:
            with self.assertRaises(sm.SessionError) as ctx:
                sm.send_to_session(4242, text="本文")
        finally:
            sm._withdraw_inbox_req = original
        self.assertIn("下げられませんでした", str(ctx.exception))
        self.assertEqual(self.console, [])

    def test_clear_confirm_timeout_reports_cleared_by_ack(self):
        """入力待ちに戻らず時間切れでも、ack が /clear 済みを示していれば cleared で返す（2周目の中1）。"""
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            # 状態ファイルはまだ古い id のまま（書かれるのが遅い）
            self.ack(req, "cleared", newSessionId=NEW_SID)

        self.start_mod(mod)
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True)
        self.assertTrue(ctx.exception.cleared)
        self.assertEqual(ctx.exception.session_id_after, NEW_SID)
        self.assertTrue(str(ctx.exception).startswith(sm.CLEARED_PREFIX))

    def test_console_failure_has_via(self):
        """打鍵の経路の失敗にも via が付く（2周目の低2）。"""

        def sender(pid, text, prefix=None, keep_draft=False):
            raise sm.SessionError("送れない")

        sm._run_input_sender = sender
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True)
        self.assertEqual(ctx.exception.via, "console")

    def test_mod_error_after_clear_reports_cleared(self):
        """/clear の後に mod の中で失敗しても、状態ファイルで /clear が済んだと伝える（評価の中3）。"""
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            self.state["sessionId"] = NEW_SID
            # 古い版の mod のように newSessionId を書かずに断る
            self.ack(req, "refused", reason="error: boom")

        self.start_mod(mod)
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True, text="本文")
        self.assertTrue(ctx.exception.cleared)
        self.assertEqual(ctx.exception.session_id_after, NEW_SID)
        self.assertTrue(str(ctx.exception).startswith(sm.CLEARED_PREFIX))
        self.assertIn("boom", str(ctx.exception))

    def test_refused_draft_before_clear(self):
        sm.is_launched_by_app = lambda pid: False
        self.write_alive()
        self.start_mod(lambda req: self.ack(req, "refused", reason="draft_present"))
        with self.assertRaises(sm.SessionDraftError) as ctx:
            sm.send_to_session(4242, clear=True, text="本文", allow_unmanaged=True)
        self.assertFalse(ctx.exception.cleared)
        self.assertNotIn("/clear は済みました", str(ctx.exception))
        self.assertEqual(self.console, [])
        self.assertEqual(self.state["sessionId"], OLD_SID)

    def test_refused_draft_after_clear_says_clear_was_done(self):
        sm.is_launched_by_app = lambda pid: False
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            self.state["sessionId"] = NEW_SID
            self.ack(req, "cleared", newSessionId=NEW_SID)
            self.ack(req, "refused", reason="draft_present", newSessionId=NEW_SID)

        self.start_mod(mod)
        with self.assertRaises(sm.SessionDraftError) as ctx:
            sm.send_to_session(4242, clear=True, text="本文", allow_unmanaged=True)
        self.assertTrue(ctx.exception.cleared)
        self.assertEqual(ctx.exception.session_id_after, NEW_SID)
        self.assertIn("/clear は済みました", str(ctx.exception))
        self.assertEqual(self.console, [])

    def test_refused_no_input_area(self):
        self.write_alive()
        self.start_mod(lambda req: self.ack(req, "refused", reason="no_input_area"))
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, text="本文", discard_draft=True)
        self.assertNotIsInstance(ctx.exception, sm.SessionDraftError)
        self.assertIn("入力欄が見えない", str(ctx.exception))

    def test_queued_but_clear_not_confirmed(self):
        """受け取られた後は打鍵へ戻らず、確かめの時間切れを伝える。"""
        self.write_alive()
        self.start_mod(lambda req: self.ack(req, "queued"))
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True)
        self.assertIn("受け箱", str(ctx.exception))
        self.assertEqual(self.console, [])
        self.assertFalse(os.path.exists(sm._inbox_path(OLD_SID, "req")))

    def test_text_not_confirmed_reports_cleared(self):
        self.write_alive()

        def mod(req):
            self.ack(req, "queued")
            self.state["sessionId"] = NEW_SID
            self.ack(req, "submitted", newSessionId=NEW_SID)

        self.start_mod(mod)
        with self.assertRaises(sm.SessionError) as ctx:
            sm.send_to_session(4242, clear=True, text="届かない本文")
        self.assertTrue(ctx.exception.cleared)
        self.assertEqual(ctx.exception.session_id_after, NEW_SID)

    def test_req_is_written_atomically_with_fields(self):
        self.write_alive()
        self.start_mod(self.good_mod)
        before = int(time.time() * 1000)
        sm.send_to_session(4242, text="本文")
        req = self.mod.reqs[0]
        self.assertEqual(
            set(req), {"id", "clear", "text", "created", "cancelled", "draft"}
        )
        self.assertFalse(req["cancelled"])
        self.assertGreaterEqual(req["created"], before)
        leftovers = [n for n in os.listdir(sm._inbox_dir()) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class CleanupInboxTest(InboxTestBase):
    def test_removes_only_old_files(self):
        self.write_alive(OLD_SID)
        self.write_alive(NEW_SID)
        old_path = sm._inbox_path(OLD_SID, "alive")
        past = time.time() - sm.INBOX_STALE_SEC - 10
        os.utime(old_path, (past, past))
        other = os.path.join(sm._inbox_dir(), "readme.txt")
        with open(other, "w", encoding="utf-8") as f:
            f.write("x")
        os.utime(other, (past, past))
        self.assertEqual(sm.cleanup_inbox(), 1)
        self.assertFalse(os.path.exists(old_path))
        self.assertTrue(os.path.exists(sm._inbox_path(NEW_SID, "alive")))
        # 受け箱の形でないファイルには触らない
        self.assertTrue(os.path.exists(other))

    def test_missing_dir_is_fine(self):
        self.assertEqual(sm.cleanup_inbox(), 0)


if __name__ == "__main__":
    unittest.main()

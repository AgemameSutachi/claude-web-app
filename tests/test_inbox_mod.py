# -*- coding: utf-8 -*-
"""mod web-app-inbox（docs/web-app-inbox-mod/hooks/register.js）の試験。

tests/js/mod_harness.mjs が偽の $（記憶の中のファイル・偽の時計・偽の口）で mod を動かし、
結果を JSON で返す。本物の Claude Code は使わない。node が無ければ飛ばす。
"""

import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "js", "mod_harness.mjs")
NODE = shutil.which("node")
OLD = "11111111-2222-3333-4444-555555555555"
NEW = "66666666-7777-8888-9999-aaaaaaaaaaaa"


@unittest.skipUnless(NODE, "node が無いので mod の試験を飛ばす")
class ModHarnessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run(
            [NODE, HARNESS],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode != 0:
            raise AssertionError(done.stderr.decode("utf-8", "replace"))
        cls.out = json.loads(done.stdout.decode("utf-8"))

    def result(self, name):
        data = self.out[name]
        self.assertNotIn("error", data, data.get("error"))
        return data

    def test_no_req_writes_only_alive(self):
        r = self.result("no_req_only_alive")
        self.assertEqual(r["calls"], [])
        self.assertEqual(r["alive"]["sid"], OLD)
        self.assertEqual(len(r["files"]), 1)
        self.assertTrue(
            r["files"][0].endswith(f"/.claude-web-app/inbox/{OLD}.alive.json")
        )

    def test_clear_and_text_keep_draft(self):
        r = self.result("clear_and_text_keep")
        self.assertEqual(r["calls"], ["command:clear", "submit:hello:true"])
        self.assertEqual(r["ack"]["stage"], "submitted")
        self.assertEqual(r["ack"]["newSessionId"], NEW)
        self.assertEqual(r["aliveNew"]["sid"], NEW)
        # keep では書きかけに触れない
        self.assertEqual(r["draft"], "kakikake")

    def test_refuse_with_draft_does_nothing(self):
        r = self.result("refuse_with_draft")
        self.assertEqual(r["calls"], [])
        self.assertEqual(r["ack"]["stage"], "refused")
        self.assertEqual(r["ack"]["reason"], "draft_present")

    def test_refuse_without_draft_sends(self):
        r = self.result("refuse_without_draft")
        self.assertEqual(r["calls"], ["submit:hello:true"])

    def test_discard_clears_then_sends(self):
        r = self.result("discard_draft")
        self.assertEqual(r["calls"], ["fill:", "submit:hello:true"])
        self.assertEqual(r["draft"], "")

    def test_discard_under_dialog_is_refused(self):
        r = self.result("discard_under_dialog")
        self.assertEqual(r["calls"], ["fill:"])
        self.assertEqual(r["ack"]["reason"], "no_input_area")

    def test_busy_writes_alive_but_waits(self):
        r = self.result("busy_waits_and_subagent_does_not_clear_busy")
        self.assertTrue(r["aliveWhileBusy"])
        self.assertEqual(r["callsWhileBusy"], [])
        self.assertEqual(r["callsAfterSubagent"], [])
        self.assertEqual(r["calls"], ["submit:hello:true"])

    def test_stale_and_cancelled_are_ignored(self):
        r = self.result("stale_and_cancelled_are_ignored")
        self.assertEqual(r["afterStale"], [])
        self.assertEqual(r["calls"], [])
        self.assertIsNone(r["ack"])

    def test_error_after_clear_reports_new_session(self):
        r = self.result("submit_error_after_clear_reports_new_sid")
        self.assertEqual(r["calls"], ["command:clear"])
        self.assertEqual(r["ack"]["stage"], "refused")
        self.assertEqual(r["ack"]["newSessionId"], NEW)
        self.assertIn("submit failed", r["ack"]["reason"])

    def test_same_req_is_not_reprocessed(self):
        self.assertEqual(self.result("not_reprocessed")["calls"], ["submit:once:true"])

    def test_non_interactive_does_nothing(self):
        r = self.result("non_interactive_does_nothing")
        self.assertFalse(r["timer"])
        self.assertEqual(r["files"], [])


if __name__ == "__main__":
    unittest.main()

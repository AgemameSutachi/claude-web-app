# -*- coding: utf-8 -*-
"""relogin_manager（ログイン更新）の単体試験。

本物の claude は起こさず、本物の資格情報も読まない（一時フォルダの偽物を使う）。
とくに、認証コードと資格情報の値が応答・画面・エラー文に残らないこと、
貼り付け欄が見えないときにコードを打たないことを確かめる。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import relogin_manager as rl
import session_manager as sm

CODE = "AbCdEfGh1234567890#xyzSTATEvalue-should-never-leak"
SECRET = "sk-ant-oat01-SECRET-TOKEN-VALUE"
PASTE_SCREEN = [
    "Browser didn't open? Use the url below to sign in",
    "",
    "Paste code here if prompted >",
]


class TestReadExpiry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = rl.CREDENTIALS_PATH
        rl.CREDENTIALS_PATH = os.path.join(self.tmp.name, ".credentials.json")

    def tearDown(self):
        rl.CREDENTIALS_PATH = self.original
        self.tmp.cleanup()

    def test_returns_only_times(self):
        with open(rl.CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "claudeAiOauth": {
                        "accessToken": SECRET,
                        "refreshToken": SECRET,
                        "expiresAt": 1790000000000,
                        "refreshTokenExpiresAt": 1790500000000,
                        "subscriptionType": "max",
                    }
                },
                f,
            )
        result = rl.read_expiry()
        self.assertEqual(result["access_expires_at"], 1790000000000)
        self.assertEqual(result["refresh_expires_at"], 1790500000000)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertNotIn("max", json.dumps(result))

    def test_missing_and_broken_files(self):
        self.assertEqual(rl.read_expiry(), {"exists": False})
        with open(rl.CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            f.write("{ broken " + SECRET)
        result = rl.read_expiry()
        self.assertIn("error", result)
        self.assertNotIn("SECRET", json.dumps(result, ensure_ascii=False))


class TestExtractUrl(unittest.TestCase):
    def test_wrapped_url_is_joined(self):
        lines = [
            " Browser didn't open? Use the url below to sign in (c to copy)",
            "",
            "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-59",
            "44d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fconsole.anthropic.com",
            "%2Foauth%2Fcode%2Fcallback&state=abc",
            "",
            " Paste code here if prompted >",
        ]
        url = rl.extract_url(lines)
        self.assertTrue(url.startswith("https://claude.ai/oauth/authorize?"))
        self.assertTrue(url.endswith("&state=abc"))

    def test_border_line_and_box_chars_are_not_joined(self):
        lines = [
            "│ https://claude.ai/oauth/authorize?client_id=abc&state=x   │",
            "────────────────────────────────",
        ]
        self.assertEqual(
            rl.extract_url(lines),
            "https://claude.ai/oauth/authorize?client_id=abc&state=x",
        )

    def test_non_oauth_url_is_ignored(self):
        self.assertIsNone(rl.extract_url(["see https://example.com/docs for help"]))


class TestSubmitCode(unittest.TestCase):
    """コードは貼り付け欄にだけ打たれ、応答・画面・例外の文面に残らない。"""

    def setUp(self):
        self.originals = (
            rl._login_pid,
            rl._type,
            rl._screen,
            rl.read_expiry,
            rl.cancel,
            rl.POLL_SEC,
            rl.CODE_TIMEOUT_SEC,
            rl.FAILURE_GRACE_SEC,
        )
        self.typed = []
        self.cancelled = []
        self.refresh = [1000]
        rl._login_pid = lambda: 4242
        rl._type = lambda pid, text, enter=True: self.typed.append(text)
        rl.read_expiry = lambda: {"exists": True, "refresh_expires_at": self.refresh[0]}
        rl.cancel = (
            lambda expected_pid=None: self.cancelled.append(expected_pid) or True
        )
        rl.POLL_SEC = 0.01
        rl.CODE_TIMEOUT_SEC = 0.3
        rl.FAILURE_GRACE_SEC = 0.05
        rl._state["code_sent"] = False

    def tearDown(self):
        (
            rl._login_pid,
            rl._type,
            rl._screen,
            rl.read_expiry,
            rl.cancel,
            rl.POLL_SEC,
            rl.CODE_TIMEOUT_SEC,
            rl.FAILURE_GRACE_SEC,
        ) = self.originals
        rl._state["code_sent"] = False

    def test_success_is_judged_by_extended_expiry_and_closes(self):
        def screen(pid):
            if self.typed:
                self.refresh[0] = 2000  # 打った後に期限が延びた
                return PASTE_SCREEN + [CODE[:30], CODE[30:], "Login successful"]
            return PASTE_SCREEN

        rl._screen = screen
        result = rl.submit_code("  " + CODE + "  ")
        self.assertTrue(result["ok"])
        self.assertEqual(self.typed, [CODE])
        self.assertEqual(self.cancelled, [4242])
        self.assertNotIn(CODE[:8], json.dumps(result, ensure_ascii=False))

    def test_warning_above_paste_field_is_not_failure(self):
        """起動時の警告（貼り付け欄より上の「invalid」）を、ログインの失敗と取り違えない。"""
        rl._screen = lambda pid: ["⚠ invalid settings in .mcp.json"] + PASTE_SCREEN
        result = rl.submit_code(CODE)
        self.assertFalse(result["ok"])
        self.assertIn("期限が延びませんでした", result["message"])

    def test_retry_screen_is_failure_even_without_paste_field(self):
        """コードが通らないと貼り付け欄が消えて「Press Enter to retry」が出る（本番の実測）。"""

        def screen(pid):
            if self.typed:
                # 貼り付け欄の行は消えるが、入力したコードの名残が映る場合を含める
                return [
                    f"OAuth error: {CODE[:20]}",
                    "  Press Enter to retry.",
                    "  Esc to cancel",
                ]
            return PASTE_SCREEN

        rl._screen = screen
        result = rl.submit_code(CODE)
        self.assertFalse(result["ok"])
        self.assertIn("失敗しました", result["message"])
        # 貼り付け欄から下を伏せる処理が効かない画面でも、コードの断片は伏せる
        self.assertNotIn(CODE[:8], json.dumps(result, ensure_ascii=False))

    def test_failure_text_then_late_success_is_success(self):
        """失敗の文言が出ても、少し後に期限が延びれば成功とする。"""
        calls = []

        def screen(pid):
            calls.append(1)
            if self.typed:
                if len(calls) > 3:
                    self.refresh[0] = 2000
                return PASTE_SCREEN + ["OAuth error: invalid (retrying)"]
            return PASTE_SCREEN

        rl._screen = screen
        rl.FAILURE_GRACE_SEC = 0.3
        original_read = rl.read_expiry
        rl.read_expiry = lambda: (
            calls.append(1),
            {"exists": True, "refresh_expires_at": self.refresh[0]},
        )[1]
        try:
            result = rl.submit_code(CODE)
        finally:
            rl.read_expiry = original_read
        self.assertTrue(result["ok"])

    def test_no_paste_field_refusal_carries_screen_without_code(self):
        rl._screen = lambda pid: ["⚠ something unexpected", "> "]
        with self.assertRaises(rl.ReloginError) as ctx:
            rl.submit_code(CODE)
        self.assertEqual(ctx.exception.screen, ["⚠ something unexpected", "> "])
        self.assertEqual(self.typed, [])

    def test_success_text_without_extended_expiry_is_not_success(self):
        """画面に「成功」と出ても、期限が延びなければ成功とは言わない（誤判定の防止）。"""
        rl._screen = lambda pid: PASTE_SCREEN + [
            "Login successful",
            "Not logged in · Run /login",
        ]
        result = rl.submit_code(CODE)
        self.assertFalse(result["ok"])
        self.assertEqual(self.cancelled, [4242])

    def test_failure_redacts_everything_below_paste_field(self):
        rl._screen = lambda pid: PASTE_SCREEN + [
            f"> {CODE[:5]}",
            CODE[5:9],
            "OAuth error: Invalid code",
        ]
        result = rl.submit_code(CODE)
        self.assertFalse(result["ok"])
        dumped = json.dumps(result, ensure_ascii=False)
        for piece in (CODE[:5], CODE[5:9]):
            self.assertNotIn(piece, dumped)
        self.assertEqual(self.cancelled, [4242])

    def test_no_paste_field_means_no_typing(self):
        """貼り付け欄が見えなければ打たない（普通の入力欄に打つと会話として送られる）。"""
        rl._screen = lambda pid: ['> Try "how do I log an error?"', "────"]
        with self.assertRaises(rl.ReloginError) as ctx:
            rl.submit_code(CODE)
        self.assertEqual(self.typed, [])
        self.assertNotIn(CODE[:8], str(ctx.exception))

    def test_second_code_is_refused(self):
        rl._screen = lambda pid: PASTE_SCREEN
        rl._state["code_sent"] = True
        with self.assertRaises(rl.ReloginError):
            rl.submit_code(CODE)
        self.assertEqual(self.typed, [])

    def test_invalid_input_is_refused_without_typing(self):
        for bad in (None, 123, "", "   ", "a\nb", "x" * (rl.CODE_MAX_LEN + 1)):
            with self.subTest(bad=repr(bad)[:20]):
                with self.assertRaises(rl.ReloginError) as ctx:
                    rl.submit_code(bad)
                if isinstance(bad, str) and bad.strip():
                    self.assertNotIn(bad.strip()[:8], str(ctx.exception))
        self.assertEqual(self.typed, [])

    def test_no_active_login_is_refused(self):
        rl._login_pid = lambda: None
        with self.assertRaises(rl.ReloginError) as ctx:
            rl.submit_code(CODE)
        self.assertNotIn(CODE[:8], str(ctx.exception))
        self.assertEqual(self.typed, [])


class TestStart(unittest.TestCase):
    def setUp(self):
        self.originals = (
            rl._login_pid,
            rl._close_orphans,
            rl._screen,
            rl._type,
            rl.cancel,
            sm.ensure_trusted,
            sm.resolve_claude_exe,
            sm._spawn_detached,
            sm._wait_state,
        )
        self.cancelled = []
        rl._login_pid = lambda: None
        rl._close_orphans = lambda: None
        rl._screen = lambda pid: ["starting..."]
        rl._type = lambda pid, text, enter=True: None
        # 代役にしないと、試験のプロセス自身を閉じてしまう
        rl.cancel = (
            lambda expected_pid=None: self.cancelled.append(expected_pid) or True
        )
        sm.ensure_trusted = lambda path: False
        sm.resolve_claude_exe = lambda configured=None: sys.executable
        sm._spawn_detached = lambda argv, cwd, env: os.getpid()

    def tearDown(self):
        (
            rl._login_pid,
            rl._close_orphans,
            rl._screen,
            rl._type,
            rl.cancel,
            sm.ensure_trusted,
            sm.resolve_claude_exe,
            sm._spawn_detached,
            sm._wait_state,
        ) = self.originals
        rl._clear_state()

    def test_failure_to_become_idle_closes_the_login(self):
        sm._wait_state = lambda pid, predicate, timeout: None
        with self.assertRaises(rl.ReloginError):
            rl.start()
        # 起こした手続き（代役の pid＝この試験のプロセス）だけを閉じる指定になっていること
        self.assertEqual(self.cancelled, [os.getpid()])

    def test_url_is_returned_without_screen(self):
        sm._wait_state = lambda pid, predicate, timeout: {"status": "idle"}
        rl._screen = lambda pid: [
            "https://claude.ai/oauth/authorize?client_id=a&state=b"
        ]
        original_poll = rl.POLL_SEC
        rl.POLL_SEC = 0.01
        try:
            result = rl.start()
        finally:
            rl.POLL_SEC = original_poll
        self.assertEqual(
            result["url"], "https://claude.ai/oauth/authorize?client_id=a&state=b"
        )
        self.assertEqual(result["screen"], [])


class TestSenderStdin(unittest.TestCase):
    """送り手の標準入力の口は、失敗しても打とうとした文字を出力に出さない。"""

    def test_failure_output_has_no_text(self):
        completed = subprocess.run(
            [sys.executable, sm.INPUT_SENDER_SCRIPT, "--stdin"],
            input=json.dumps({"pid": 999999, "raw": CODE, "enter": True}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        response = json.loads(completed.stdout)
        self.assertFalse(response["ok"])
        self.assertNotIn(CODE[:8], completed.stdout + completed.stderr)


class TestOutputWithoutUtf8Env(unittest.TestCase):
    """本番（常駐の管理アプリ配下）は PYTHONIOENCODING が無く、子の標準出力が cp932 になる。
    そこでも画面の読み取りと送り手の出力が落ちないこと（2026-10-11 本番で、画面が空になった不具合）。"""

    def _env(self):
        return {
            k: v
            for k, v in os.environ.items()
            if k.upper() not in ("PYTHONIOENCODING", "PYTHONUTF8")
        }

    def test_screen_reader_output_is_ascii_json(self):
        # read_console_screen_isolated が子に書かせるのと同じ形（❯ と罫線を含む行）
        code = "import json; print(json.dumps(['\\u276f Try', '\\u2500\\u2500'], ensure_ascii=True))"
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, env=self._env()
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout), ["❯ Try", "──"])
        with open(sm.__file__, encoding="utf-8") as f:
            self.assertIn("ensure_ascii=True))", f.read())

    def test_app_stdout_reconfigure_keeps_print_alive(self):
        """本アプリの起動時の設定（errors=backslashreplace）なら、❯ を含む行の print で落ちない。"""
        code = (
            "import sys\n"
            "for s in (sys.stdout, sys.stderr):\n"
            "    s.reconfigure(errors='backslashreplace')\n"
            "print('[Relogin] screen: \\u276f Try / \\u2500\\u2500 / 日本語')\n"
            "print(sys.stdout.encoding)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, env=self._env()
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        out = completed.stdout.decode(
            completed.stdout.splitlines()[-1].decode().strip() or "utf-8", "replace"
        )
        self.assertIn("\\u276f", out)
        with open(
            os.path.join(PROJECT_ROOT, "claude-web-app.py"), encoding="utf-8"
        ) as f:
            self.assertIn('reconfigure(errors="backslashreplace")', f.read())

    def test_sender_stdin_output_is_parseable(self):
        completed = subprocess.run(
            [sys.executable, sm.INPUT_SENDER_SCRIPT, "--stdin"],
            input=json.dumps({"pid": 999999, "raw": "x", "enter": False}).encode(),
            capture_output=True,
            timeout=30,
            env=self._env(),
        )
        self.assertFalse(json.loads(completed.stdout)["ok"])


class TestReloginSessionIsExcluded(unittest.TestCase):
    def test_relogin_dir_session_is_recognized(self):
        self.assertTrue(sm.is_relogin_session({"cwd": sm.RELOGIN_DIR}))
        self.assertFalse(sm.is_relogin_session({"cwd": PROJECT_ROOT}))


if __name__ == "__main__":
    unittest.main()

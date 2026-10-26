"""safe_console（標準出力・標準エラーの書く失敗を飲み込む包み）の試験。"""

import io
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import safe_console  # noqa: E402


class Broken:
    """読み手の居ないパイプの代わり（書くたびに OSError [Errno 22]）。"""

    def __init__(self):
        self.calls = 0

    def write(self, text):
        self.calls += 1
        raise OSError(22, "Invalid argument")

    def flush(self):
        self.calls += 1
        raise OSError(22, "Invalid argument")

    def isatty(self):
        raise OSError(22, "Invalid argument")


class _TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "server.log")

    def read_log(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()


class TestSafeStream(_TempDir):
    def test_broken_stream_does_not_raise_and_log_keeps_text(self):
        log = safe_console.LogFile(self.path)
        broken = Broken()
        stream = safe_console.SafeStream(broken, log)
        print("一行目", file=stream, flush=True)
        print("二行目", file=stream, flush=True)
        self.assertTrue(stream.stream_broken)
        # 一度失敗したら元の出力へは書かない（毎回の失敗で遅くしない）
        self.assertEqual(broken.calls, 1)
        self.assertFalse(stream.isatty())
        lines = self.read_log().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] 一行目$")
        self.assertTrue(lines[1].endswith("] 二行目"))
        log.close()

    def test_healthy_stream_gets_text_as_is(self):
        out = io.StringIO()
        stream = safe_console.SafeStream(out, None)
        print("a", "b", file=stream)
        self.assertEqual(out.getvalue(), "a b\n")
        self.assertFalse(stream.stream_broken)
        # 元の出力の属性は素通し
        self.assertEqual(stream.getvalue(), "a b\n")

    def test_partial_lines_get_one_stamp(self):
        log = safe_console.LogFile(self.path)
        log.write("前半")
        log.write("後半\n次")
        log.write("の行\n")
        log.close()
        lines = self.read_log().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("] 前半後半"))
        self.assertTrue(lines[1].endswith("] 次の行"))
        self.assertEqual(self.read_log().count("["), 2)

    def test_rotates_once_over_limit(self):
        log = safe_console.LogFile(self.path, max_bytes=50)
        log.write("x" * 60 + "\n")
        log.write("新しい行\n")
        log.close()
        self.assertTrue(os.path.exists(self.path + ".1"))
        self.assertIn("新しい行", self.read_log())
        self.assertNotIn("x" * 60, self.read_log())

    def test_unwritable_log_is_silent(self):
        # ログの置き場がファイルで塞がれている（フォルダを作れない）
        blocker = os.path.join(self.tmp.name, "sub")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("")
        log = safe_console.LogFile(self.path)
        stream = safe_console.SafeStream(Broken(), log)
        print("書けない", file=stream, flush=True)
        self.assertTrue(log.broken)

    def test_reopens_after_retry_interval(self):
        now = [0.0]
        blocker = os.path.join(self.tmp.name, "sub")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("")
        log = safe_console.LogFile(self.path, clock=lambda: now[0])
        log.write("一回目\n")
        self.assertTrue(log.broken)
        # 置き場が空いても、休みの間は試さない
        os.remove(blocker)
        now[0] = safe_console.LOG_RETRY_SEC - 1
        log.write("休み中\n")
        now[0] = safe_console.LOG_RETRY_SEC + 1
        log.write("戻った\n")
        log.close()
        self.assertFalse(log.broken)
        self.assertNotIn("休み中", self.read_log())
        self.assertIn("戻った", self.read_log())

    def test_rotation_failure_keeps_writing(self):
        log = safe_console.LogFile(self.path, max_bytes=1000)
        log.write("x" * 1100 + "\n")
        with mock.patch.object(
            safe_console.os, "replace", side_effect=PermissionError(13, "使用中")
        ) as replace:
            log.write("退かせられない\n")
            log.write("続き\n")
        log.close()
        self.assertFalse(log.broken)
        self.assertIn("退かせられない", self.read_log())
        self.assertIn("続き", self.read_log())
        # 失敗の直後に毎回試し直さない（1割ほど書いてから）
        self.assertEqual(replace.call_count, 1)

    def test_size_counts_bytes_on_disk(self):
        log = safe_console.LogFile(self.path)
        log.write("あ\n" * 3)
        log.close()
        self.assertEqual(log._size, os.path.getsize(self.path))

    def test_unencodable_text_is_rewritten(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp932", newline="\n")
        stream = safe_console.SafeStream(out, None)
        print("❯ 日本語", file=stream, flush=True)
        self.assertFalse(stream.stream_broken)
        self.assertEqual(raw.getvalue().decode("cp932"), "\\u276f 日本語\n")


class TestInstall(_TempDir):
    def test_wraps_both_once_and_say_never_raises(self):
        broken_out, broken_err = Broken(), Broken()
        with mock.patch.object(sys, "stdout", broken_out), mock.patch.object(
            sys, "stderr", broken_err
        ):
            log = safe_console.install(self.path)
            # 二度呼んでも包みは1重（同じログ・中身は元の出力）
            self.assertIs(
                safe_console.install(os.path.join(self.tmp.name, "other.log")), log
            )
            self.assertIsInstance(sys.stdout, safe_console.SafeStream)
            self.assertIs(sys.stdout._stream, broken_out)
            self.assertIs(sys.stderr._stream, broken_err)
            print("出力", flush=True)
            print("エラー", file=sys.stderr, flush=True)
            log.close()
        self.assertIn("出力", self.read_log())
        self.assertIn("エラー", self.read_log())

    def test_repoints_handlers_made_before_install(self):
        # socketio / engineio のように、install より前に元の stderr を掴んだハンドラ
        broken_err = Broken()
        with mock.patch.object(sys, "stdout", Broken()), mock.patch.object(
            sys, "stderr", broken_err
        ):
            logger = logging.getLogger("safe_console_test_early")
            handler = logging.StreamHandler()
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
            log = safe_console.install(self.path)
            self.assertIs(handler.stream, sys.stderr)
            logger.error("前から居たハンドラ")
            log.close()
        self.assertEqual(self.read_log().count("前から居たハンドラ"), 1)
        self.assertNotIn("Logging error", self.read_log())

    def test_main_installs_before_background_tasks(self):
        with open(
            os.path.join(PROJECT_ROOT, "claude-web-app.py"), encoding="utf-8"
        ) as f:
            source = f.read()
        main = source[source.index('if __name__ == "__main__":') :]
        self.assertLess(
            main.index("safe_console.install("), main.index("start_background_task(")
        )

    def test_say_without_wrapper(self):
        with mock.patch.object(sys, "stdout", Broken()):
            safe_console.say("包みの外", flush=True)


if __name__ == "__main__":
    unittest.main()

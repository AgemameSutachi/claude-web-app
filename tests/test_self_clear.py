# -*- coding: utf-8 -*-
"""self_clear（自分を区切る予約）と request_self_clear（道具）の単体試験。

本物のセッションへは送らない（状態の読み取りと send_to_session は偽物に差し替える）。
接続の持ち主の引き方だけは、試験の中で起こした子プロセスとの本物の接続で確かめる。
"""

import datetime
import http.server
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import psutil

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import request_self_clear as tool
import self_clear as sc
import send_history
import session_manager as sm


def _tool_use(tool_id, command, name="Bash"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {"command": command},
                }
            ]
        },
    }


def _tool_result(tool_id):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
    }


def _prompt(text, origin=None, is_meta=False):
    record = {"type": "user", "message": {"content": text}}
    if origin:
        record["origin"] = origin
    if is_meta:
        record["isMeta"] = True
    return record


def _queued(prompt, origin=None, timestamp=None):
    """作業中に届いてターンの途中に吸い込まれた発言（2026-10-17 実記録の形）。"""
    attachment = {"type": "queued_command", "prompt": prompt, "commandMode": "prompt"}
    if origin:
        attachment["origin"] = origin
        attachment["humanTurn"] = origin.get("kind") == "human"
    if timestamp:
        attachment["timestamp"] = timestamp
    return {"type": "attachment", "isSidechain": False, "attachment": attachment}


class _TempDir(unittest.TestCase):
    """結果の記録と会話記録を一時フォルダへ向け、予約と直近の結果を空にしてから始める。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(
                sc, "RESULT_LOG", os.path.join(self.tmp.name, "log.jsonl")
            ),
            mock.patch.object(sm, "APP_STATE_DIR", self.tmp.name),
            # 送った履歴も一時フォルダへ（本物の履歴に試験の送信を混ぜない）
            mock.patch.object(
                send_history,
                "HISTORY_FILE",
                os.path.join(self.tmp.name, "send_history.jsonl"),
            ),
            mock.patch.object(
                sm,
                "_transcript_paths",
                side_effect=lambda sid: (
                    [self.transcript(sid)]
                    if os.path.exists(self.transcript(sid))
                    else []
                ),
            ),
        ]
        for p in self.patches:
            p.start()
        sc._pending.clear()
        sc._recent.clear()
        sc._auto_expected.clear()
        self.handoff = os.path.join(self.tmp.name, "handoff.md")
        with open(self.handoff, "w", encoding="utf-8") as f:
            f.write("# 引き継ぎ\n")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        sc._pending.clear()
        sc._recent.clear()
        sc._auto_expected.clear()
        self.tmp.cleanup()

    def transcript(self, session_id):
        return os.path.join(self.tmp.name, f"{session_id}.jsonl")

    def append(self, session_id, *records):
        with open(self.transcript(session_id), "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestCallerPid(unittest.TestCase):
    def test_non_loopback_is_refused(self):
        self.assertIsNone(sc.caller_pid("100.64.0.5", 50000, 5002))

    def test_bad_ports(self):
        self.assertIsNone(sc.caller_pid("127.0.0.1", None, 5002))
        self.assertIsNone(sc.caller_pid("127.0.0.1", "x", 5002))

    def test_finds_owner_of_real_connection(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import socket,sys;s=socket.create_connection(('127.0.0.1',%d));"
                "sys.stdin.read()" % port,
            ],
            stdin=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            conn, peer = server.accept()
            found = sc.caller_pid(peer[0], peer[1], port)
            # venv の python は起動役の子として本体を起こすので、子孫まで含めて照合する
            family = {child.pid} | {
                p.pid for p in psutil.Process(child.pid).children(recursive=True)
            }
            self.assertIn(found, family)
            conn.close()
        finally:
            child.stdin.close()
            child.wait(timeout=10)
            server.close()


class TestFindSessionAncestor(unittest.TestCase):
    def test_finds_parent_session(self):
        parent = os.getppid()
        state = {"pid": parent, "sessionId": "s-1"}
        with mock.patch.object(
            sm, "_read_state", side_effect=lambda pid: state if pid == parent else None
        ), mock.patch.object(sm, "is_session_alive", return_value=True):
            self.assertEqual(sc.find_session_ancestor(os.getpid()), state)

    def test_none_when_no_session(self):
        with mock.patch.object(sm, "_read_state", return_value=None):
            self.assertIsNone(sc.find_session_ancestor(os.getpid()))

    def test_dead_state_is_skipped(self):
        with mock.patch.object(
            sm, "_read_state", return_value={"pid": 1}
        ), mock.patch.object(sm, "is_session_alive", return_value=False):
            self.assertIsNone(sc.find_session_ancestor(os.getpid()))

    def test_unknown_pid(self):
        self.assertIsNone(sc.find_session_ancestor(None))
        self.assertIsNone(sc.find_session_ancestor(2**31 - 5))


class TestTranscriptChecks(_TempDir):
    def test_running_tool_in_main_transcript(self):
        self.append("s", _tool_use("t1", "python request_self_clear.py --handoff x"))
        self.assertTrue(sc.main_transcript_runs_tool("s"))

    def test_powershell_tool_counts(self):
        self.append(
            "s", _tool_use("t1", "python request_self_clear.py --cancel", "PowerShell")
        )
        self.assertTrue(sc.main_transcript_runs_tool("s"))

    def test_finished_tool_does_not_count(self):
        # 前に打って結果まで返った呼び出しは、いまの呼び出しの証拠にならない
        self.append(
            "s",
            _tool_use("t1", "python request_self_clear.py --handoff x"),
            _tool_result("t1"),
        )
        self.assertFalse(sc.main_transcript_runs_tool("s"))

    def test_other_command_does_not_count(self):
        self.append("s", _tool_use("t1", "python -m pytest"))
        self.assertFalse(sc.main_transcript_runs_tool("s"))

    def test_command_only_naming_the_tool_does_not_count(self):
        # 道具の名前を含むだけのコマンド（--handoff / --cancel を取らない）は数えない
        self.append("s", _tool_use("t1", "grep -rn request_self_clear F:/x"))
        self.append("s", _tool_use("t2", "cat request_self_clear.py"))
        self.assertFalse(sc.main_transcript_runs_tool("s"))
        self.append(
            "s",
            _tool_use(
                "t3",
                "C:/venv/python.exe F:/app/request_self_clear.py --handoff C:/h.md",
            ),
        )
        self.assertTrue(sc.main_transcript_runs_tool("s"))

    def test_call_without_handoff_counts(self):
        # --handoff を省いた呼び出し（既定の NOW.md / README.md を使う）も数える
        commands = [
            "C:/venv/Scripts/python.exe F:/app/request_self_clear.py",
            "C:/venv/Scripts/python.exe F:/app/request_self_clear.py  ",
            'C:/venv/Scripts/python.exe "F:/app/request_self_clear.py"',
            "python F:/app/request_self_clear.py --wait-sec 120",
            "python F:/app/request_self_clear.py --url http://127.0.0.1:5002",
            "C:/venv/Scripts/Python.exe F:\\app\\request_self_clear.py",
            # 子がよく打つ、終了コードを見る・出力を絞る形（評価役が会話記録で数えた）
            'python F:/app/request_self_clear.py; echo "exit=$?"',
            "python F:/app/request_self_clear.py; echo $LASTEXITCODE",
            "python F:/app/request_self_clear.py 2>&1",
            "python F:/app/request_self_clear.py 2>&1 | tail -5",
            "python F:/app/request_self_clear.py && echo ok",
            "python F:/app/request_self_clear.py\r\n",
            "& C:/venv/Scripts/python.exe F:/app/request_self_clear.py",
            "cd F:/x && python F:/app/request_self_clear.py",
            "python request_self_clear.py",
            # PowerShell で結果を受ける・すべての出力を捨てる形、行末のコメント、python3
            "$out = C:/venv/Scripts/python.exe F:/app/request_self_clear.py",
            "python F:/app/request_self_clear.py *> $null",
            "python F:/app/request_self_clear.py  # 区切りを予約",
            "python3 F:/app/request_self_clear.py",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(sc.TOOL_CALL_RE.search(command))

    def test_naming_the_script_without_running_it_does_not_count(self):
        # python で道具を走らせていない・後ろに別の引数が続くものは、引数なしの形として数えない
        commands = [
            "grep -rn request_self_clear.py",
            "cat F:/app/request_self_clear.py",
            "python -m black F:/app/request_self_clear.py tests",
            "python -c \"open('request_self_clear.py')\"",
            "python -m pytest tests/test_self_clear.py",
            "git -C F:/app diff request_self_clear.py",
            # python が先頭のコマンドでない（grep の検索語・echo の引数など）
            "grep -n python request_self_clear.py",
            "rg python request_self_clear.py",
            "git log -- python request_self_clear.py",
            "echo python F:/app/request_self_clear.py",
            # 別のファイル
            "python F:/x/old_request_self_clear.py",
            "python F:/app/request_self_clear.py.bak",
            "python F:/app/request_self_clear.py -h",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(sc.TOOL_CALL_RE.search(command))

    def test_no_transcript(self):
        self.assertFalse(sc.main_transcript_runs_tool("none"))
        self.assertFalse(sc.main_transcript_runs_tool(None))

    def test_partial_last_line_is_left(self):
        self.append("s", _tool_use("t1", "python request_self_clear.py --handoff x"))
        with open(self.transcript("s"), "a", encoding="utf-8") as f:
            f.write('{"type": "user", "mess')
        self.assertTrue(sc.main_transcript_runs_tool("s"))

    def test_is_new_work(self):
        cases = [
            (_prompt("次の作業", {"kind": "human"}), True),
            (_prompt("連絡", {"kind": "peer"}, is_meta=True), True),
            (_prompt("<task-notification>", {"kind": "task-notification"}), True),
            (_prompt("ふつうの発言"), True),
            (_tool_result("t1"), False),
            (_prompt([{"type": "text", "text": "skill"}], is_meta=True), False),
            (_prompt("<command-name>/clear</command-name>"), False),
            (_prompt("<local-command-caveat>…"), False),
            (
                dict(
                    _prompt("This session is being continued…"),
                    isCompactSummary=True,
                    isVisibleInTranscriptOnly=True,
                ),
                False,
            ),
            ({"type": "assistant", "message": {"content": "x"}}, False),
            ({"type": "attachment"}, False),
            # 作業中に吸い込まれた発言・連絡・通知も新しい仕事
            (_queued("あと○○も", {"kind": "human"}), True),
            (_queued("連絡", {"kind": "peer"}), True),
            (_queued("<task-notification>\n<task-id>a1</task-id>"), True),
            (_queued("origin も通知も無い"), False),
            ({"type": "attachment", "attachment": {"type": "skill_listing"}}, False),
            # 本アプリが送り直した本文（自動の文）は数えない
            (_prompt(sc.RESUME_TEXT.format(path="C:/x"), {"kind": "human"}), False),
            (_queued(sc.RESUME_TEXT.format(path="C:/x"), {"kind": "human"}), False),
        ]
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(sc.is_new_work(record), expected)

    def test_new_work_since_reads_only_after_offset(self):
        self.append("s", _prompt("前の発言", {"kind": "human"}))
        offsets = sm._transcript_sizes("s")
        self.append("s", _tool_result("t1"))
        self.assertFalse(sc._new_work_since("s", offsets))
        self.append("s", _prompt("新しい仕事", {"kind": "peer"}))
        self.assertTrue(sc._new_work_since("s", offsets))
        # 読んだ分だけ位置が進む（同じ記録で2度は数えない）
        self.assertFalse(sc._new_work_since("s", offsets))

    def test_is_human_utterance(self):
        human = {"kind": "human"}
        cases = [
            (_prompt("速度比較はどうなった？", human), True),
            (
                _prompt(
                    [{"type": "text", "text": "画像つき"}, {"type": "image"}], human
                ),
                True,
            ),
            (_prompt(sm.DEFAULT_PASTE_PREAMBLE + "\n指示", human), True),
            # 本アプリが打鍵で送る自動の文（origin.kind=human で載る）は外す
            (_prompt(sc.RESUME_TEXT.format(path="C:/w/NOW.md"), human), False),
            (
                _prompt("  " + sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md"), human),
                False,
            ),
            (
                _prompt(
                    [{"type": "text", "text": "自動の区切りです。引き継ぎを"}], human
                ),
                False,
            ),
            (_prompt("連絡", {"kind": "peer"}, is_meta=True), False),
            (_prompt("<task-notification>", {"kind": "task-notification"}), False),
            (_prompt("Stop hook feedback: …", is_meta=True), False),
            (_prompt("origin の無い発言"), False),
            (dict(_prompt("サブエージェント", human), isSidechain=True), False),
            (
                {"type": "assistant", "origin": human, "message": {"content": "x"}},
                False,
            ),
            # 作業中に吸い込まれた人の発言（attachment の queued_command・2026-10-17 実記録の形）
            (_queued("編成ボタンで好きなように変えれる", human), True),
            (_queued([{"type": "text", "text": "部品の形"}], human), True),
            (_queued(sc.AUTO_RESUME_TEXT.format(path="C:/x"), human), False),
            (_queued("<task-notification>"), False),
            (_queued("連絡", {"kind": "peer"}), False),
            (dict(_queued("サブ", human), isSidechain=True), False),
        ]
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(sc.is_human_utterance(record), expected)

    def test_queued_human_uses_attachment_timestamp(self):
        record = _queued("追い足し", {"kind": "human"}, "2026-10-20T08:12:00.000Z")
        record["timestamp"] = "2026-10-20T08:13:30.000Z"
        self.assertEqual(
            sc.human_utterance_epoch(record),
            sc.record_epoch({"timestamp": "2026-10-20T08:12:00.000Z"}),
        )
        # attachment に時刻が無ければ記録の時刻
        del record["attachment"]["timestamp"]
        self.assertEqual(
            sc.human_utterance_epoch(record),
            sc.record_epoch({"timestamp": "2026-10-20T08:13:30.000Z"}),
        )
        self.assertIsNone(sc.human_utterance_epoch(_queued("x", {"kind": "peer"})))

    def test_last_human_epoch_reads_whole_transcript(self):
        def at(record, iso):
            return dict(record, timestamp=iso)

        self.assertIsNone(sc.last_human_epoch("s"))
        self.append(
            "s",
            at(_prompt("前の依頼", {"kind": "human"}), "2026-10-20T08:00:00.000Z"),
            at(_prompt("今の依頼", {"kind": "human"}), "2026-10-20T08:05:00.000Z"),
            at(
                _prompt(sc.RESUME_TEXT.format(path="C:/x"), {"kind": "human"}),
                "2026-10-20T08:09:00.000Z",
            ),
            at(_prompt("連絡", {"kind": "peer"}), "2026-10-20T08:10:00.000Z"),
            at(_prompt("human が無い時刻", {"kind": "human"}), "壊れた時刻"),
        )
        # 末尾の読み取りの範囲より大きな道具の結果が後ろに続いても見落とさない
        big = _tool_result("t1")
        big["message"]["content"][0]["content"] = "x" * (sc.TRANSCRIPT_TAIL_BYTES + 10)
        self.append("s", big)
        with open(self.transcript("s"), "a", encoding="utf-8") as f:
            f.write('{"type": "user", "origin": {"kind": "human"}, 壊れた行\n')
        self.assertEqual(
            sc.last_human_epoch("s"),
            sc.record_epoch({"timestamp": "2026-10-20T08:05:00.000Z"}),
        )
        # 作業中に吸い込まれた人の発言（user の記録にならない）も最後の発言として拾う
        self.append(
            "s", _queued("あと○○も", {"kind": "human"}, "2026-10-20T08:12:00.000Z")
        )
        self.assertEqual(
            sc.last_human_epoch("s"),
            sc.record_epoch({"timestamp": "2026-10-20T08:12:00.000Z"}),
        )


class TestValidateHandoff(_TempDir):
    def test_ok(self):
        self.assertEqual(
            sc.validate_handoff(self.handoff), os.path.normpath(self.handoff)
        )

    def test_refusals(self):
        for bad in (
            None,
            "",
            "  ",
            5,
            "relative.md",
            "/c/Users/x.md",
            r"\\host\share\x.md",
            self.handoff + "\nx",
            os.path.join(self.tmp.name, "missing.md"),
            self.tmp.name,
            "C:/" + "a" * 800,
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(sc.SelfClearError):
                    sc.validate_handoff(bad)

    def test_resume_text_is_not_pasted(self):
        path = "C:/" + "a" * (sc.HANDOFF_PATH_MAX_LEN - 3)
        self.assertLessEqual(
            len(sc.RESUME_TEXT.format(path=path)), sm.PASTE_THRESHOLD_CHARS
        )

    def test_safe_detail_cuts_screen_and_draft(self):
        detail = sc._safe_detail(
            "送信できませんでした（draft_present）: 消さずに止めました。入力欄: 秘密の書きかけ"
        )
        self.assertNotIn("秘密", detail)
        self.assertNotIn("x", sc._safe_detail("失敗。画面の末尾: x x x"))
        self.assertLessEqual(len(sc._safe_detail("あ" * 1000)), sc.DETAIL_MAX_LEN)


class TestRequest(_TempDir):
    def setUp(self):
        super().setUp()
        # 断る試験で本体の記録を待つ時間を短くする
        wait = mock.patch.object(sc, "MAIN_CHECK_WAIT_SEC", 0.05)
        wait.start()
        self.addCleanup(wait.stop)
        self.append(
            "sess-a", _tool_use("t1", "python request_self_clear.py --handoff x")
        )

    def _state(self, **extra):
        state = {
            "pid": 4242,
            "sessionId": "sess-a",
            "name": "child",
            "cwd": "C:/x",
            "status": "busy",
            "kind": "interactive",
        }
        state.update(extra)
        return state

    def _patched(self, state):
        return [
            mock.patch.object(sc, "find_session_ancestor", return_value=state),
            mock.patch.object(sm, "_process_create_time", return_value=123.0),
            mock.patch.object(sm, "is_launched_by_app", return_value=False),
        ]

    def _request(self, state, **kwargs):
        patches = self._patched(state)
        for p in patches:
            p.start()
        try:
            return sc.request_self_clear(
                1234, kwargs.pop("handoff", self.handoff), start_worker=False, **kwargs
            )
        finally:
            for p in patches:
                p.stop()

    def _cancel(self, state):
        patches = self._patched(state)
        for p in patches:
            p.start()
        try:
            return sc.cancel_self_clear(1234)
        finally:
            for p in patches:
                p.stop()

    def _code(self, state, **kwargs):
        with self.assertRaises(sc.SelfClearError) as cm:
            self._request(state, **kwargs)
        return cm.exception.code, cm.exception.status

    def test_no_caller(self):
        with self.assertRaises(sc.SelfClearError) as cm:
            sc.request_self_clear(None, self.handoff, start_worker=False)
        self.assertEqual((cm.exception.code, cm.exception.status), ("not_self", 403))

    def test_not_in_session(self):
        self.assertEqual(self._code(None), ("not_self", 403))

    def test_relogin_and_print_mode_refused(self):
        self.assertEqual(self._code(self._state(cwd=sm.RELOGIN_DIR))[0], "not_self")
        self.assertEqual(self._code(self._state(kind="print"))[0], "not_self")

    def test_subagent_is_refused(self):
        # 本体の記録に、この道具を呼ぶ実行中の tool_use が無い（サブエージェントから打った）
        self.append("sess-a", _tool_result("t1"))
        self.assertEqual(self._code(self._state()), ("not_main", 403))
        self.assertEqual(sc.status()["pending"], [])

    def test_late_tool_use_record_is_waited_for(self):
        # 並行で打つと、自分の tool_use が少し遅れて記録に載る → 待って受ける
        with mock.patch.object(
            sc, "main_transcript_runs_tool", side_effect=[False, False, True]
        ), mock.patch.object(sc, "MAIN_CHECK_WAIT_SEC", 5), mock.patch.object(
            sc, "MAIN_CHECK_POLL_SEC", 0
        ):
            self.assertEqual(self._request(self._state())["pid"], 4242)

    def test_auto_request_is_claimed_once(self):
        # 自動の区切りの依頼を送った会話からの予約は auto になり、印は使い切る
        sc.expect_auto(4242, "sess-a", time.time() + 600)
        self.assertEqual(len(sc.auto_expectations()), 1)
        self.assertTrue(self._request(self._state())["auto"])
        self.assertEqual(sc.auto_expectations(), [])

    def test_auto_mark_needs_same_conversation_and_time(self):
        # 会話が違う印・ずっと古い印では auto にしない
        sc.expect_auto(4242, "sess-other", time.time() + 600)
        self.assertFalse(self._request(self._state())["auto"])
        sc._pending.clear()
        sc._auto_expected.clear()
        sc.expect_auto(4242, "sess-a", time.time() - sc.AUTO_LATE_KEEP_SEC - 1)
        self.assertFalse(self._request(self._state())["auto"])

    def test_late_reservation_is_still_auto(self):
        # 待ちの上限を過ぎてから来た予約も、同じ会話なら自動の区切りとして扱う
        sc.expect_auto(4242, "sess-a", time.time() - 60)
        self.assertTrue(self._request(self._state())["auto"])

    def test_expired_auto_marks_are_reported_once_and_kept(self):
        sc.expect_auto(1, "s1", 100)
        sc.expect_auto(2, "s2", 300)
        expired = sc.pop_expired_auto(200)
        self.assertEqual([e["pid"] for e in expired], [1])
        self.assertEqual(sc.pop_expired_auto(250), [])
        self.assertEqual(len(sc.auto_expectations()), 2)
        # ずっと古くなったら捨てる
        sc.pop_expired_auto(100 + sc.AUTO_LATE_KEEP_SEC + 1)
        self.assertEqual([e["pid"] for e in sc.auto_expectations()], [2])
        sc.drop_auto(2, "other")
        self.assertEqual(len(sc.auto_expectations()), 1)
        sc.drop_auto(2, "s2")
        self.assertEqual(sc.auto_expectations(), [])

    def test_reserves_and_refuses_duplicate(self):
        result = self._request(self._state())
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(result["session_id"], "sess-a")
        self.assertFalse(result["managed"])
        self.assertEqual(set(result), set(sc.PUBLIC_KEYS))
        self.assertEqual(len(sc.status()["pending"]), 1)
        with self.assertRaises(sc.SelfClearError) as cm:
            self._request(self._state())
        self.assertEqual(
            (cm.exception.code, cm.exception.status), ("already_reserved", 409)
        )
        # 前の予約の引き継ぎと「このまま終えれば区切られる」を伝える
        self.assertIn(self.handoff, str(cm.exception))
        self.assertIn("ターンを終えれば", str(cm.exception))

    def test_quiet_until_from_last_human(self):
        # 人の最後の発言から10分たつ時刻を予約に置き、待ちの上限はそれより後へ延ばす
        spoke = time.time() - 120
        with mock.patch.object(sc, "last_human_epoch", return_value=spoke):
            reservation = self._request(self._state(), wait_sec=60)
        stored = sc._pending["4242"]
        self.assertAlmostEqual(
            stored["quiet_until_epoch"], spoke + sc.HUMAN_QUIET_SEC, places=3
        )
        self.assertIsNotNone(reservation["quiet_until"])
        self.assertGreaterEqual(
            stored["deadline_epoch"],
            spoke + sc.HUMAN_QUIET_SEC + sc.HUMAN_QUIET_GRACE_SEC - 0.01,
        )

    def test_quiet_until_read_from_transcript(self):
        # 差し替えずに、会話記録の人の発言（UTC の Z つき）から quiet_until を決める
        spoke = time.time() - 90
        iso = datetime.datetime.fromtimestamp(spoke, datetime.timezone.utc).isoformat()
        self.append(
            "sess-a",
            dict(_prompt("やって", {"kind": "human"}), timestamp=iso),
            dict(
                _prompt(sc.RESUME_TEXT.format(path="C:/x"), {"kind": "human"}),
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            ),
        )
        self._request(self._state(), wait_sec=60)
        self.assertAlmostEqual(
            sc._pending["4242"]["quiet_until_epoch"],
            spoke + sc.HUMAN_QUIET_SEC,
            delta=0.01,
        )

    def test_no_quiet_wait_when_human_spoke_long_ago(self):
        for spoke in (None, time.time() - sc.HUMAN_QUIET_SEC - 5):
            with self.subTest(spoke=spoke):
                sc._pending.clear()
                before = time.time()
                with mock.patch.object(sc, "last_human_epoch", return_value=spoke):
                    reservation = self._request(self._state(), wait_sec=600)
                self.assertIsNone(reservation["quiet_until"])
                # 待ちの上限は頼まれた長さのまま
                self.assertLess(sc._pending["4242"]["deadline_epoch"], before + 601)

    def test_missing_handoff(self):
        code, status = self._code(
            self._state(), handoff=os.path.join(self.tmp.name, "none.md")
        )
        self.assertEqual(status, 400)
        self.assertEqual(sc.status()["pending"], [])

    def _project(self, *names):
        """会話の cwd にする一時フォルダを作り、names のファイルを置いて返す。"""
        folder = os.path.join(self.tmp.name, "project")
        os.makedirs(folder, exist_ok=True)
        for name in names:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write("# 引き継ぎ\n")
        return folder

    def test_default_handoff_prefers_now_md(self):
        folder = self._project("NOW.md", "README.md")
        result = self._request(self._state(cwd=folder), handoff=None)
        self.assertEqual(result["handoff"], os.path.join(folder, "NOW.md"))
        self.assertEqual(result["handoff_source"], "now")

    def test_default_handoff_falls_back_to_readme(self):
        # 移行中: NOW.md が無く README.md だけの子は、今までどおり README.md で区切れる
        folder = self._project("README.md")
        result = self._request(self._state(cwd=folder), handoff=None)
        self.assertEqual(result["handoff"], os.path.join(folder, "README.md"))
        self.assertEqual(result["handoff_source"], "readme")
        # どちらを使ったかが結果の記録に残る
        self.assertEqual(sc.status()["pending"][0]["handoff_source"], "readme")

    def test_default_handoff_missing_is_refused(self):
        folder = self._project()
        with self.assertRaises(sc.SelfClearError) as cm:
            self._request(self._state(cwd=folder), handoff=None)
        self.assertEqual((cm.exception.code, cm.exception.status), ("failed", 400))
        self.assertIn("NOW.md", str(cm.exception))
        self.assertIn("--handoff", str(cm.exception))
        self.assertEqual(sc.status()["pending"], [])

    def test_default_handoff_without_cwd_is_refused(self):
        self.assertEqual(self._code(self._state(cwd=""), handoff=None), ("failed", 400))

    def test_default_handoff_ignores_folder_named_now_md(self):
        # NOW.md という名前のフォルダはファイルでないので README.md へ回る
        folder = self._project("README.md")
        os.makedirs(os.path.join(folder, "NOW.md"))
        result = self._request(self._state(cwd=folder), handoff=None)
        self.assertEqual(result["handoff_source"], "readme")

    def test_explicit_handoff_is_marked(self):
        result = self._request(self._state())
        self.assertEqual(result["handoff_source"], "explicit")

    def test_call_without_handoff_passes_identity_check(self):
        # 本体の記録に --handoff を省いた呼び出しだけが実行中でも、本人と認めて予約できる
        folder = self._project("NOW.md")
        self.append(
            "sess-b",
            _tool_use(
                "t9",
                'C:/venv/Scripts/python.exe F:/app/request_self_clear.py; echo "exit=$?"',
            ),
        )
        result = self._request(
            self._state(sessionId="sess-b", cwd=folder), handoff=None
        )
        self.assertEqual(result["handoff_source"], "now")

    def test_wait_sec_is_clamped(self):
        with mock.patch.object(sc.time, "time", return_value=1000.0):
            self._request(self._state(), wait_sec=5)
            self.assertEqual(
                sc._pending["4242"]["deadline_epoch"], 1000.0 + sc.MIN_WAIT_SEC
            )
            sc._pending.clear()
            self._request(self._state(), wait_sec=float("nan"))
            self.assertEqual(
                sc._pending["4242"]["deadline_epoch"], 1000.0 + sc.MIN_WAIT_SEC
            )
        sc._pending.clear()
        self.assertEqual(self._code(self._state(), wait_sec="abc")[1], 400)

    def test_thread_start_failure_releases_reservation(self):
        patches = self._patched(self._state()) + [
            mock.patch.object(
                sc.threading.Thread, "start", side_effect=RuntimeError("no thread")
            )
        ]
        for p in patches:
            p.start()
        try:
            with self.assertRaises(sc.SelfClearError) as cm:
                sc.request_self_clear(1234, self.handoff)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(sc.status()["pending"], [])

    def test_cancel(self):
        self._request(self._state())
        self.assertTrue(self._cancel(self._state())["cancelled"])
        self.assertEqual(sc.status()["pending"], [])
        self.assertEqual(sc.status()["recent"][-1]["outcome"], "cancelled")
        self.assertFalse(self._cancel(self._state())["cancelled"])

    def test_cancel_refused_while_sending(self):
        self._request(self._state())
        sc._pending["4242"]["sending"] = True
        result = self._cancel(self._state())
        self.assertEqual((result["cancelled"], result["sending"]), (False, True))
        self.assertEqual(len(sc.status()["pending"]), 1)

    def test_duplicate_is_reported_before_handoff_check(self):
        # 予約が生きているのに、存在しないパスで打ち直すと「予約されていない」と取り違えさせない
        self._request(self._state())
        code, status = self._code(
            self._state(), handoff=os.path.join(self.tmp.name, "none.md")
        )
        self.assertEqual((code, status), ("already_reserved", 409))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


class TestWatch(_TempDir):
    def _reservation(self, wait=600):
        self.append("sess-a", _prompt("最初の発言", {"kind": "human"}))
        reservation = {
            "pid": 4242,
            "create_time": 123.0,
            "session_id": "sess-a",
            "name": "child",
            "cwd": "C:/x",
            "managed": True,
            "handoff": self.handoff,
            "requested_at": "t",
            "deadline": "d",
            "deadline_epoch": wait,
            "offsets": sm._transcript_sizes("sess-a"),
        }
        sc._pending["4242"] = reservation
        return reservation

    def _run(self, states, send=None, wait=600, create_time=123.0, on_tick=None):
        """states: 時刻 → 状態の関数、または状態の列。send: send_to_session の偽物。"""
        clock = FakeClock()
        reservation = self._reservation(wait)
        sequence = list(states) if not callable(states) else None

        def read_state(pid):
            if on_tick:
                on_tick(clock.now)
            if sequence is None:
                return states(clock.now)
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]

        send = send or mock.Mock(
            return_value={"session_id_before": "sess-a", "session_id_after": "sess-b"}
        )
        with mock.patch.object(
            sm, "_read_state", side_effect=read_state
        ), mock.patch.object(
            sm, "is_session_alive", side_effect=lambda s: bool(s)
        ), mock.patch.object(
            sm, "_process_create_time", return_value=create_time
        ), mock.patch.object(
            sm, "send_to_session", send
        ):
            entry = sc._watch(reservation, sleep=clock.sleep, clock=clock.time)
        return entry, send, clock

    def _st(self, status, session_id="sess-a"):
        return {"pid": 4242, "sessionId": session_id, "status": status}

    def test_auto_reservation_sends_wait_text(self):
        # 自動の区切りの予約では、/clear の後に「読んで待つ」文を送る
        clock = FakeClock()
        reservation = self._reservation()
        reservation["auto"] = True
        send = mock.Mock(
            return_value={"session_id_before": "sess-a", "session_id_after": "sess-b"}
        )
        with mock.patch.object(
            sm, "_read_state", return_value=self._st("idle")
        ), mock.patch.object(
            sm, "is_session_alive", return_value=True
        ), mock.patch.object(
            sm, "_process_create_time", return_value=123.0
        ), mock.patch.object(
            sm, "send_to_session", send
        ):
            entry = sc._watch(reservation, sleep=clock.sleep, clock=clock.time)
        self.assertEqual(entry["outcome"], "done")
        self.assertTrue(entry["auto"])
        self.assertEqual(
            send.call_args[1]["text"], sc.AUTO_RESUME_TEXT.format(path=self.handoff)
        )
        self.assertLessEqual(
            len(sc.AUTO_RESUME_TEXT.format(path="C:/" + "a" * sc.HANDOFF_PATH_MAX_LEN)),
            sm.PASTE_THRESHOLD_CHARS,
        )

    def test_sends_after_stable_idle(self):
        entry, send, clock = self._run(
            lambda now: self._st("busy" if now < 5 else "idle")
        )
        self.assertEqual(entry["outcome"], "done")
        self.assertGreaterEqual(clock.now, 5 + sc.IDLE_STABLE_SEC)
        args, kwargs = send.call_args
        self.assertEqual(args, (4242,))
        self.assertTrue(kwargs["clear"])
        self.assertTrue(kwargs["allow_unmanaged"])
        self.assertTrue(kwargs["protect_draft"])
        self.assertEqual(kwargs["preamble"], "")
        self.assertEqual(kwargs["text"], sc.RESUME_TEXT.format(path=self.handoff))
        self.assertEqual(sc.status()["pending"], [])
        with open(sc.RESULT_LOG, encoding="utf-8") as f:
            logged = [json.loads(line) for line in f]
        self.assertEqual(logged[-1]["outcome"], "done")
        self.assertEqual(
            set(logged[-1]) - set(sc.PUBLIC_KEYS), {"outcome", "detail", "finished_at"}
        )

    def test_brief_idle_is_not_enough(self):
        # 3秒だけ idle になって作業へ戻る（Stop フックの差し戻し）→ その後の安定した idle で送る
        def states(now):
            if 2 <= now < 5:
                return self._st("idle")
            return self._st("busy" if now < 20 else "idle")

        entry, send, clock = self._run(states)
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(send.call_count, 1)
        self.assertGreaterEqual(clock.now, 20 + sc.IDLE_STABLE_SEC)

    def _run_quiet(self, quiet_until, on_tick=None, send=None, state=None):
        """入力待ちのまま、人の発言から10分たつ時刻（偽の時計で quiet_until）を置いて見張る。"""
        clock = FakeClock()
        reservation = self._reservation()
        reservation["quiet_until_epoch"] = quiet_until
        reservation["deadline_epoch"] = quiet_until + sc.HUMAN_QUIET_GRACE_SEC
        send = send or mock.Mock(
            return_value={"session_id_before": "sess-a", "session_id_after": "sess-b"}
        )

        def read_state(pid):
            if on_tick:
                on_tick(clock.now)
            return state() if state else self._st("idle")

        with mock.patch.object(
            sm, "_read_state", side_effect=read_state
        ), mock.patch.object(
            sm, "is_session_alive", return_value=True
        ), mock.patch.object(
            sm, "_process_create_time", return_value=123.0
        ), mock.patch.object(
            sm, "send_to_session", send
        ):
            entry = sc._watch(reservation, sleep=clock.sleep, clock=clock.time)
        return entry, send, clock

    def test_waits_until_human_quiet(self):
        # 入力待ちが続いていても、人の発言から10分たつまでは送らない
        entry, send, clock = self._run_quiet(400)
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(send.call_count, 1)
        self.assertGreaterEqual(clock.now, 400)
        self.assertLess(clock.now, 400 + sc.IDLE_STABLE_SEC)

    def test_human_speaks_while_waiting_for_quiet(self):
        # 待っている間に人が話したら送らずに取りやめる（人と話している最中に区切らない）
        def on_tick(now):
            if now == 200:
                self.append("sess-a", _prompt("まだ話している", {"kind": "human"}))

        entry, send, _ = self._run_quiet(400, on_tick=on_tick)
        self.assertEqual(entry["outcome"], "superseded")
        send.assert_not_called()

    def test_queued_human_while_waiting_for_quiet(self):
        # ターンの途中に吸い込まれた人の発言（attachment）でも取りやめる
        def on_tick(now):
            if now == 200:
                self.append("sess-a", _queued("追い足し", {"kind": "human"}))

        entry, send, _ = self._run_quiet(400, on_tick=on_tick)
        self.assertEqual(entry["outcome"], "superseded")
        send.assert_not_called()

    def test_resend_after_clear_is_not_held_by_quiet(self):
        # /clear の後に本文を送り直す道では、10分の待ちは既に明けている（もう一度10分待たない）
        send = mock.Mock(
            side_effect=[
                self._cleared_error(),
                {"session_id_before": "sess-b", "session_id_after": "sess-b"},
            ]
        )
        entry, send, clock = self._run_quiet(
            400,
            send=send,
            state=lambda: self._st(
                "idle", "sess-a" if send.call_count == 0 else "sess-b"
            ),
        )
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(send.call_count, 2)
        self.assertLess(clock.now, 400 + 3 * sc.IDLE_STABLE_SEC)

    def test_new_work_supersedes(self):
        # 予約の後、ターンの途中のツール結果は数えず、ほかのセッションからの連絡で取りやめる
        def on_tick(now):
            if now == 2:
                self.append("sess-a", _tool_result("t9"))
            if now == 4:
                self.append("sess-a", _prompt("次の仕事", {"kind": "peer"}, True))

        entry, send, _ = self._run([self._st("busy")], on_tick=on_tick)
        self.assertEqual(entry["outcome"], "superseded")
        send.assert_not_called()

    def test_busy_error_retries(self):
        send = mock.Mock(
            side_effect=[
                sm.SessionBusyError("busy"),
                {"session_id_before": "a", "session_id_after": "b"},
            ]
        )
        entry, send, _ = self._run([self._st("idle")], send=send)
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(send.call_count, 2)

    def test_draft_present_stops_and_hides_draft(self):
        send = mock.Mock(
            side_effect=sm.SessionDraftError("消さずに止めました。入力欄: 人の秘密")
        )
        entry, send, _ = self._run([self._st("idle")], send=send)
        self.assertEqual(entry["outcome"], "draft_present")
        self.assertEqual(send.call_count, 1)
        self.assertNotIn("秘密", entry["detail"])
        with open(sc.RESULT_LOG, encoding="utf-8") as f:
            self.assertNotIn("秘密", f.read())

    def _cleared_error(self, cls=sm.SessionError, message="本文を確認できず"):
        error = cls(message)
        error.cleared = True
        error.session_id_after = "sess-b"
        return error

    def test_resends_body_after_clear(self):
        send = mock.Mock(
            side_effect=[
                self._cleared_error(),
                {"session_id_before": "sess-b", "session_id_after": "sess-b"},
            ]
        )
        entry, send, _ = self._run(
            lambda now: self._st(
                "idle", "sess-a" if send.call_count == 0 else "sess-b"
            ),
            send=send,
        )
        self.assertEqual(entry["outcome"], "done")
        self.assertTrue(send.call_args_list[0].kwargs["clear"])
        self.assertFalse(send.call_args_list[1].kwargs["clear"])

    def test_body_already_delivered_is_not_resent(self):
        # 照合が遅れて例外になったが、本文は新しい会話に載っていた → 送り直さない
        def first_send(*args, **kwargs):
            self.append("sess-b", _prompt(kwargs["text"], {"kind": "human"}))
            raise self._cleared_error()

        send = mock.Mock(side_effect=first_send)
        with mock.patch.object(
            sm,
            "_transcript_has_prompt",
            side_effect=lambda sid, text, offsets: os.path.exists(self.transcript(sid)),
        ):
            entry, send, _ = self._run(
                lambda now: self._st(
                    "idle", "sess-a" if send.call_count == 0 else "sess-b"
                ),
                send=send,
            )
        self.assertEqual(entry["outcome"], "done")
        self.assertEqual(send.call_count, 1)

    def test_clear_confirm_timeout_but_conversation_changed(self):
        # /clear の確かめが時間切れでも、会話が変わっていれば /clear 済みとして本文だけ送り直す
        send = mock.Mock(
            side_effect=[
                sm.SessionError("/clear を送りましたが、確認できませんでした"),
                {"session_id_before": "sess-b", "session_id_after": "sess-b"},
            ]
        )
        entry, send, _ = self._run(
            lambda now: self._st(
                "idle", "sess-a" if send.call_count == 0 else "sess-b"
            ),
            send=send,
        )
        self.assertEqual(entry["outcome"], "done")
        self.assertFalse(send.call_args_list[1].kwargs["clear"])

    def test_cleared_then_draft(self):
        send = mock.Mock(
            side_effect=self._cleared_error(sm.SessionDraftError, "入力欄: x")
        )
        entry, _, _ = self._run([self._st("idle")], send=send)
        self.assertEqual(entry["outcome"], "cleared_without_handoff")

    def test_cleared_then_gives_up(self):
        errors = [self._cleared_error()] + [
            sm.SessionError("また失敗") for _ in range(sc.RESEND_ATTEMPTS)
        ]
        send = mock.Mock(side_effect=errors)
        entry, send, _ = self._run(
            lambda now: self._st(
                "idle", "sess-a" if send.call_count == 0 else "sess-b"
            ),
            send=send,
        )
        self.assertEqual(entry["outcome"], "cleared_without_handoff")
        self.assertEqual(send.call_count, sc.RESEND_ATTEMPTS)

    def test_other_error_is_recorded(self):
        send = mock.Mock(side_effect=sm.SessionError("確認できず。画面の末尾: 画面"))
        entry, _, _ = self._run([self._st("idle")], send=send)
        self.assertEqual(entry["outcome"], "failed")
        self.assertEqual(entry["detail"], "確認できず。")

    def test_timeout(self):
        entry, send, _ = self._run([self._st("busy")], wait=30)
        self.assertEqual(entry["outcome"], "timeout")
        send.assert_not_called()

    def test_process_gone(self):
        entry, send, _ = self._run([self._st("busy"), None])
        self.assertEqual(entry["outcome"], "process_gone")
        send.assert_not_called()

    def test_pid_reused(self):
        entry, send, _ = self._run([self._st("idle")], create_time=999.0)
        self.assertEqual(entry["outcome"], "process_gone")
        send.assert_not_called()

    def test_conversation_changed(self):
        entry, send, _ = self._run([self._st("busy"), self._st("idle", "sess-z")])
        self.assertEqual(entry["outcome"], "conversation_changed")
        send.assert_not_called()

    def test_cancelled_reservation_stops_quietly(self):
        def on_tick(now):
            if now == 3:
                sc._pending.clear()

        entry, send, _ = self._run([self._st("busy")], on_tick=on_tick)
        self.assertIsNone(entry)
        send.assert_not_called()
        self.assertEqual(sc.status()["recent"], [])

    def test_old_watcher_does_not_drop_new_reservation(self):
        reservation = self._reservation()
        newer = dict(reservation)
        sc._pending["4242"] = newer
        sc._finish(reservation, "timeout")
        self.assertIs(sc._pending["4242"], newer)

    def test_can_reserve_again_after_finish(self):
        self._run([self._st("busy")], wait=30)
        self.assertEqual(sc.status()["pending"], [])
        self.assertEqual(sc.status()["recent"][-1]["outcome"], "timeout")


class TestProtectDraft(unittest.TestCase):
    """send_to_session の protect_draft が、台帳の子でも書きかけを守るか。"""

    def _keep_draft(self, managed, **kwargs):
        sender = mock.Mock()
        state = {"pid": 77, "sessionId": "s", "status": "idle", "cwd": "C:/x"}
        with mock.patch.object(
            sm, "_read_state", return_value=state
        ), mock.patch.object(
            sm, "is_session_alive", return_value=True
        ), mock.patch.object(
            sm, "is_launched_by_app", return_value=managed
        ), mock.patch.object(
            sm, "_run_input_sender", sender
        ), mock.patch.object(
            sm, "_transcript_has_prompt", return_value=True
        ), mock.patch.object(
            sm, "_transcript_sizes", return_value={}
        ):
            sm.send_to_session(77, text="本文", allow_unmanaged=True, **kwargs)
        return sender.call_args.kwargs["keep_draft"]

    def test_managed_default_discards(self):
        self.assertFalse(self._keep_draft(True))

    def test_managed_protected(self):
        self.assertTrue(self._keep_draft(True, protect_draft=True))

    def test_protect_wins_over_discard(self):
        self.assertTrue(self._keep_draft(False, protect_draft=True, discard_draft=True))

    def test_body_error_after_clear_is_marked(self):
        states = [
            {"pid": 77, "sessionId": "s1", "status": "idle", "cwd": "C:/x"},
            {"pid": 77, "sessionId": "s2", "status": "idle", "cwd": "C:/x"},
        ]
        sender = mock.Mock(side_effect=[None, sm.SessionError("打てず")])
        with mock.patch.object(
            sm, "_read_state", side_effect=lambda pid: states[0]
        ), mock.patch.object(
            sm, "_wait_state", return_value=states[1]
        ), mock.patch.object(
            sm, "is_session_alive", return_value=True
        ), mock.patch.object(
            sm, "is_launched_by_app", return_value=True
        ), mock.patch.object(
            sm, "_run_input_sender", sender
        ), mock.patch.object(
            sm, "_transcript_sizes", return_value={}
        ):
            with self.assertRaises(sm.SessionError) as cm:
                sm.send_to_session(77, clear=True, text="本文")
        self.assertTrue(cm.exception.cleared)
        self.assertEqual(cm.exception.session_id_after, "s2")


class TestEndpoint(unittest.TestCase):
    """Flask の受け口（接続の情報の受け渡し・状態コード）。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "claude_web_app_for_test", os.path.join(PROJECT_ROOT, "claude-web-app.py")
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    def test_non_json_body(self):
        response = self.client.post("/api/sessions/request-clear", data="x")
        self.assertEqual(response.status_code, 400)

    def test_passes_connection_and_returns_202(self):
        with mock.patch.object(
            sc, "caller_pid", return_value=555
        ) as caller, mock.patch.object(
            sc, "request_self_clear", return_value={"pid": 1}
        ) as reserve:
            response = self.client.post(
                "/api/sessions/request-clear",
                json={"handoff": "C:/h.md", "wait_sec": 60},
                environ_base={"REMOTE_ADDR": "127.0.0.1", "REMOTE_PORT": "40000"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(caller.call_args.args[:2], ("127.0.0.1", "40000"))
        reserve.assert_called_once_with(555, "C:/h.md", 60)

    def test_refusal_status(self):
        with mock.patch.object(sc, "caller_pid", return_value=None):
            response = self.client.post(
                "/api/sessions/request-clear", json={"handoff": "C:/h.md"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "not_self")

    def test_cancel_and_status(self):
        with mock.patch.object(sc, "caller_pid", return_value=555), mock.patch.object(
            sc, "cancel_self_clear", return_value={"pid": 1, "cancelled": False}
        ):
            response = self.client.delete("/api/sessions/request-clear")
        self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/sessions/request-clear")
        self.assertEqual(set(response.get_json()), {"ok", "pending", "recent"})


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 202
    reply = {}
    received = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(
            (self.command, self.path, json.loads(self.rfile.read(length) or b"{}"))
        )
        body = json.dumps(type(self).reply).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _answer
    do_DELETE = _answer

    def log_message(self, *args):
        pass


class TestTool(_TempDir):
    def setUp(self):
        super().setUp()
        _Handler.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _main(self, *argv, status=202, reply=None):
        _Handler.status = status
        _Handler.reply = reply or {}
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_reserved(self):
        code, out, _ = self._main(
            "--handoff",
            self.handoff,
            "--url",
            self.url,
            "--wait-sec",
            "120",
            reply={
                "ok": True,
                "result": {
                    "pid": 1,
                    "name": "child",
                    "handoff": self.handoff,
                    "deadline": "d",
                },
            },
        )
        self.assertEqual(code, tool.EXIT_OK)
        self.assertIn("予約しました", out)
        self.assertIn("ターンを終えて", out)
        self.assertIn("入力待ちが10秒続くと /clear", out)
        self.assertNotIn("10分", out)
        method, path, body = _Handler.received[-1]
        self.assertEqual((method, path), ("POST", "/api/sessions/request-clear"))
        self.assertEqual(
            body, {"handoff": os.path.abspath(self.handoff), "wait_sec": 120}
        )

    def test_reserved_with_quiet_wait(self):
        # 人の発言から10分たっていなければ、その時刻まで待つことを伝える
        code, out, _ = self._main(
            "--url",
            self.url,
            reply={
                "ok": True,
                "result": {
                    "pid": 1,
                    "name": "child",
                    "handoff": self.handoff,
                    "deadline": "d",
                    "quiet_until": "2026-10-20T19:05:00",
                },
            },
        )
        self.assertEqual(code, tool.EXIT_OK)
        self.assertIn("人の最後の発言から10分たつ 2026-10-20T19:05:00 まで", out)
        self.assertIn("それまでに人・ほかのセッション・通知から", out)
        self.assertNotIn("10秒続くと", out)

    def test_already_reserved(self):
        code, _, err = self._main(
            "--handoff",
            self.handoff,
            "--url",
            self.url,
            status=409,
            reply={
                "ok": False,
                "error": "既に予約があります。このままターンを終えれば…",
                "code": "already_reserved",
            },
        )
        self.assertEqual(code, tool.EXIT_ALREADY_RESERVED)
        self.assertIn("ターンを終えれば", err)

    def test_refused_says_not_reserved(self):
        code, _, err = self._main(
            "--handoff",
            self.handoff,
            "--url",
            self.url,
            status=403,
            reply={
                "ok": False,
                "error": "サブエージェントからは打てません",
                "code": "not_main",
            },
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("not_main", err)
        self.assertIn("予約していません", err)

    def test_old_server_404(self):
        code, _, err = self._main(
            "--handoff", self.handoff, "--url", self.url, status=404, reply={}
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("古い版", err)

    def test_cancel(self):
        code, out, _ = self._main(
            "--cancel",
            "--url",
            self.url,
            status=200,
            reply={"ok": True, "result": {"pid": 1, "cancelled": True}},
        )
        self.assertEqual(code, tool.EXIT_OK)
        self.assertIn("取り消しました", out)
        self.assertEqual(_Handler.received[-1][0], "DELETE")

    def test_missing_file_does_not_call(self):
        code, _, err = self._main(
            "--handoff", os.path.join(self.tmp.name, "no.md"), "--url", self.url
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("予約していません", err)
        self.assertEqual(_Handler.received, [])

    def test_backslashes_eaten_by_bash(self):
        code, _, err = self._main(
            "--handoff", "C:Usersnamework" + "handoff.md", "--url", self.url
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("/ 区切り", err)
        self.assertEqual(_Handler.received, [])

    def test_git_bash_path(self):
        code, _, err = self._main("--handoff", "/c/Users/x.md", "--url", self.url)
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("Git Bash", err)
        self.assertEqual(_Handler.received, [])

    def test_cancel_while_sending(self):
        code, _, err = self._main(
            "--cancel",
            "--url",
            self.url,
            status=200,
            reply={
                "ok": True,
                "result": {"pid": 1, "cancelled": False, "sending": True},
            },
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("区切られます", err)

    def test_cancel_refused_does_not_say_cancelled(self):
        code, _, err = self._main(
            "--cancel",
            "--url",
            self.url,
            status=500,
            reply={"ok": False, "error": "x", "code": "error"},
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("生きています", err)

    def test_timeout_says_unknown(self):
        with mock.patch.object(tool, "_call", side_effect=TimeoutError("timed out")):
            code, _, err = self._main("--handoff", self.handoff, "--url", self.url)
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("分かりません", err)

    def test_handoff_and_cancel_together_is_an_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                tool.main(["--handoff", self.handoff, "--cancel"])
        self.assertEqual(cm.exception.code, 2)

    def test_without_handoff_lets_server_choose(self):
        # --handoff を省くと handoff を送らず、サーバーが会話の cwd から選ぶ
        for source, note in (
            ("now", "NOW.md を使います"),
            ("readme", "README.md を使います"),
        ):
            with self.subTest(source=source):
                code, out, _ = self._main(
                    "--url",
                    self.url,
                    "--wait-sec",
                    "90",
                    reply={
                        "ok": True,
                        "result": {
                            "pid": 1,
                            "name": "child",
                            "handoff": "C:/p/x.md",
                            "handoff_source": source,
                            "deadline": "d",
                        },
                    },
                )
                self.assertEqual(code, tool.EXIT_OK)
                self.assertIn(note, out)
                method, _, body = _Handler.received[-1]
                self.assertEqual(method, "POST")
                self.assertEqual(body, {"wait_sec": 90})

    def test_without_handoff_refused_when_no_default_file(self):
        code, _, err = self._main(
            "--url",
            self.url,
            status=400,
            reply={
                "ok": False,
                "code": "failed",
                "error": "既定の引き継ぎファイル（NOW.md・README.md）が C:/p の直下にありません",
            },
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("NOW.md", err)
        self.assertIn("予約していません", err)

    def test_not_main_without_handoff_suggests_old_server(self):
        # 再起動前の本体は --handoff を省いた呼び出しを not_main で断る。打ち直し方を添える
        reply = {"ok": False, "code": "not_main", "error": "サブエージェント…"}
        code, _, err = self._main("--url", self.url, status=403, reply=reply)
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("古い版", err)
        self.assertIn("--handoff", err)
        # --handoff を付けた呼び出しには添えない（原因が違うため）
        code, _, err = self._main(
            "--handoff", self.handoff, "--url", self.url, status=403, reply=reply
        )
        self.assertNotIn("古い版", err)

    def test_unreachable(self):
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        code, _, err = self._main(
            "--handoff", self.handoff, "--url", f"http://127.0.0.1:{port}"
        )
        self.assertEqual(code, tool.EXIT_REFUSED)
        self.assertIn("つなげません", err)
        self.assertIn("続けてかまいません", err)

    def test_port_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"port": 6001}, f)
            with mock.patch.object(tool, "SETTINGS_FILE", path):
                self.assertEqual(tool.app_port(), 6001)
            with mock.patch.object(tool, "SETTINGS_FILE", os.path.join(tmp, "none")):
                self.assertEqual(tool.app_port(), tool.DEFAULT_PORT)


class BrokenStream:
    """読み手の居ないパイプの代わり（書くたびに OSError [Errno 22]）。"""

    def __init__(self):
        self.calls = 0

    def write(self, text):
        self.calls += 1
        raise OSError(22, "Invalid argument")

    def flush(self):
        self.calls += 1
        raise OSError(22, "Invalid argument")


class TestBrokenStdout(_TempDir):
    """標準出力が書けないときも、予約・取り消し・見張り・受け口が落ちない（2026-10-22 の 500）。

    包み（safe_console.install）は起動時にしか入らないので、ここでは包み無しで、
    出力が壊れた sys.stdout のまま呼んで確かめる。
    """

    def setUp(self):
        super().setUp()
        wait = mock.patch.object(sc, "MAIN_CHECK_WAIT_SEC", 0.05)
        wait.start()
        self.addCleanup(wait.stop)
        self.append(
            "sess-a", _tool_use("t1", "python request_self_clear.py --handoff x")
        )
        self.stream = BrokenStream()
        broken = mock.patch.object(sys, "stdout", self.stream)
        broken.start()
        self.addCleanup(broken.stop)

    _state = TestRequest._state
    _patched = TestRequest._patched
    _request = TestRequest._request
    _cancel = TestRequest._cancel

    def test_reserve_and_cancel(self):
        result = self._request(self._state())
        self.assertEqual(result["pid"], 4242)
        self.assertEqual(len(sc.status()["pending"]), 1)
        cancelled = self._cancel(self._state())
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(sc.status()["pending"], [])
        # 壊れた出力へ書こうとはした（試験が出力を素通りしていない）
        self.assertGreater(self.stream.calls, 0)
        with open(sc.RESULT_LOG, encoding="utf-8") as f:
            logged = [json.loads(line) for line in f]
        self.assertEqual([e["outcome"] for e in logged], ["cancelled"])

    def test_watch_done_is_recorded_once(self):
        # 以前は done の直後に「failed: OSError」を重ねて、見張りの糸が黙って死んだ
        watch = TestWatch("_run")
        watch.tmp, watch.handoff = self.tmp, self.handoff
        entry, _, _ = TestWatch._run(watch, [watch._st("idle")])
        self.assertEqual(entry["outcome"], "done")
        with open(sc.RESULT_LOG, encoding="utf-8") as f:
            logged = [json.loads(line) for line in f]
        self.assertEqual([e["outcome"] for e in logged], ["done"])

    @classmethod
    def setUpClass(cls):
        # 出力を壊す前に1回だけ読み込む（壊れた出力を掴んだハンドラを作らない・
        # TestEndpoint のクラス属性は書き換えない）
        spec = importlib.util.spec_from_file_location(
            "claude_web_app_for_broken_stdout",
            os.path.join(PROJECT_ROOT, "claude-web-app.py"),
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    def test_reserve_endpoint_returns_202(self):
        # 本物の予約の処理（登録の後に print する）を、見張りを起こさずに通す
        real = sc.request_self_clear
        patches = self._patched(self._state())
        for p in patches:
            p.start()
        try:
            with mock.patch.object(
                sc, "caller_pid", return_value=1234
            ), mock.patch.object(
                sc,
                "request_self_clear",
                side_effect=lambda caller, handoff, wait: real(
                    caller, handoff, wait, start_worker=False
                ),
            ):
                response = self.client.post(
                    "/api/sessions/request-clear", json={"handoff": self.handoff}
                )
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["result"]["pid"], 4242)
        self.assertGreater(self.stream.calls, 0)

    def test_send_endpoint_returns_200(self):
        # 送れた後の print で 500 になり「送れなかった」と伝わっていた（2026-10-22 18:25）
        result = {
            "cleared": True,
            "text_sent": True,
            "session_id_before": "sess-a",
            "session_id_after": "sess-b",
            "elapsed_sec": 1.2,
        }
        with mock.patch.object(
            sm, "send_to_session", return_value=result
        ), mock.patch.object(sm, "find_session_by_pid", return_value=None):
            response = self.client.post(
                "/api/sessions/4242/send", json={"clear": True, "text": "本文"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertGreater(self.stream.calls, 0)

    def test_endpoint_answers_json(self):
        client = self.client
        with mock.patch.object(sc, "caller_pid", return_value=None):
            response = client.delete("/api/sessions/request-clear")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "not_self")
        with mock.patch.object(sc, "caller_pid", return_value=555), mock.patch.object(
            sc, "request_self_clear", side_effect=RuntimeError("壊れた")
        ):
            response = client.post("/api/sessions/request-clear", json={})
        # Flask の HTML の 500 ではなく、こちらの JSON の 500
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "error")

    def test_auto_clear_loop_survives_one_round(self):
        module = self.module

        class Stop(Exception):
            pass

        sleeps = []

        def sleep(sec):
            sleeps.append(sec)
            if len(sleeps) > 2:
                raise Stop()

        with mock.patch.object(
            module.socketio, "sleep", side_effect=sleep
        ), mock.patch.object(
            module.auto, "tick", side_effect=RuntimeError("見回りの失敗")
        ) as tick, mock.patch.object(
            module, "annotated_sessions", return_value=[]
        ):
            with self.assertRaises(Stop):
                module.auto_clear_loop()
        # 1周目の失敗（と出力の失敗）の後も、2周目を回した
        self.assertEqual(tick.call_count, 2)


if __name__ == "__main__":
    unittest.main()

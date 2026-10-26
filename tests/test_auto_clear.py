# -*- coding: utf-8 -*-
"""自動の区切り（auto_clear.py）の試験。本物のセッションへは送らない。"""

import datetime
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import auto_clear as ac  # noqa: E402
import self_clear as sc  # noqa: E402
import send_history  # noqa: E402
import session_manager as sm  # noqa: E402

NOW = 1_800_000_000.0
CONFIG = ac.config_from({})


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _session(**extra):
    """一覧の1行（list_sessions に attention を付けた形）。"""
    session = {
        "pid": 4242,
        "session_id": "sess-a",
        "name": "child",
        "cwd": "C:/work/child",
        "status": "idle",
        "status_updated_at": (NOW - 600) * 1000,
        "kind": "interactive",
        "started_at": 111,
        "launched_by_app": True,
        "attention": None,
        "win_session": 0,
        "elevated": True,
        "url": None,
    }
    session.update(extra)
    return session


def _assistant(epoch):
    return {"type": "assistant", "timestamp": _iso(epoch), "message": {"content": []}}


def _with_usage(record, read, created=0, fresh=3):
    """応答の記録に使用量を付ける（文脈 = read + created + fresh）。"""
    record["message"] = {
        "id": f"m{read}-{record.get('timestamp')}",
        "model": "claude-opus-5-5",
        "content": [],
        "usage": {
            "input_tokens": fresh,
            "cache_creation_input_tokens": created,
            "cache_read_input_tokens": read,
        },
    }
    return record


def _compact():
    """要約の境目（2026-10-14 実記録の形）。"""
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "isSidechain": False,
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": "auto", "preTokens": 180_000},
    }


def _human(epoch):
    return {
        "type": "user",
        "timestamp": _iso(epoch),
        "origin": {"kind": "human"},
        "message": {"content": "やって"},
    }


def _result(result, epoch=NOW - 900):
    return {"type": "user", "timestamp": _iso(epoch), "toolUseResult": result}


def _notification(text):
    return {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {"role": "user", "content": text},
    }


class TestConfig(unittest.TestCase):
    def test_defaults_and_bad_values(self):
        self.assertEqual(CONFIG["mode"], "dry_run")
        self.assertEqual(CONFIG["size_tokens"], 100_000)
        self.assertEqual(CONFIG["idle_tokens"], 50_000)
        self.assertEqual(CONFIG["idle_minutes"], 50)
        self.assertEqual(CONFIG["human_quiet_minutes"], 15)
        self.assertEqual(CONFIG["min_growth_tokens"], 20_000)
        self.assertEqual(CONFIG["port"], 5002)
        bad = ac.config_from(
            {
                "auto_clear_mode": "yes",
                "auto_clear_size_tokens": "abc",
                "auto_clear_idle_minutes": -3,
            }
        )
        self.assertEqual(bad["mode"], "dry_run")
        self.assertEqual(bad["size_tokens"], 100_000)
        self.assertEqual(bad["idle_minutes"], 50)
        self.assertEqual(ac.config_from({"auto_clear_mode": "on"})["mode"], "on")

    def test_idle_minutes_after_deadline_is_clamped(self):
        config = ac.config_from({"auto_clear_idle_minutes": 70})
        self.assertEqual(config["idle_minutes"], ac.CACHE_DEADLINE_MINUTES - 1)
        self.assertEqual(len(config["warnings"]), 1)

    def test_trigger_text_has_tool_and_port(self):
        text = ac.trigger_text(5098, python="C:\\py\\python.exe")
        self.assertTrue(text.startswith("自動の区切りです。"))
        self.assertIn("C:/py/python.exe", text)
        self.assertIn(ac.REQUEST_TOOL, text)
        self.assertIn("--url http://127.0.0.1:5098", text)
        self.assertNotIn("残す", text)
        self.assertLessEqual(len(text), sm.PASTE_THRESHOLD_CHARS)
        self.assertTrue(os.path.isfile(ac.REQUEST_TOOL))


class TestDecide(unittest.TestCase):
    def decide(self, settled_min, context, response_min, human_min=None, base=None):
        session = _session(status_updated_at=(NOW - settled_min * 60) * 1000)
        return ac.decide(
            session,
            context,
            NOW,
            CONFIG,
            last_response=None if response_min is None else NOW - response_min * 60,
            last_human=None if human_min is None else NOW - human_min * 60,
            base_context=base,
        )

    def test_growth_from_first_response_is_required(self):
        # 区切って引き継ぎを読んだだけの会話（2026-10-14 実測: 最初 62,222 → 67,754）は当てない
        self.assertIsNone(self.decide(50.5, 67_754, 50.5, base=62_222))
        self.assertIsNone(self.decide(3, 300_000, 3, base=290_000))
        # 最初の応答から2万以上伸びていれば当てる
        self.assertEqual(self.decide(50.5, 82_222, 50.5, base=62_222)[0], "idle")
        self.assertEqual(self.decide(3, 150_000, 3, base=62_222)[0], "size")
        self.assertIsNone(self.decide(50.5, 82_221, 50.5, base=62_222))
        # 最初の文脈が分からなければ伸びは見ない（従来どおり）
        self.assertEqual(self.decide(50.5, 67_754, 50.5, base=None)[0], "idle")
        self.assertEqual(self.decide(50.5, 67_754, 50.5, base=0)[0], "idle")
        # 設定で替えられる
        config = ac.config_from({"auto_clear_min_growth_tokens": 1000})
        self.assertEqual(
            ac.decide(
                _session(status_updated_at=(NOW - 3030) * 1000),
                67_754,
                NOW,
                config,
                last_response=NOW - 3030,
                base_context=62_222,
            )[0],
            "idle",
        )

    def test_size_rule_after_settle(self):
        self.assertEqual(self.decide(3, 150_000, 3)[0], "size")
        # 止まった直後（差し戻しで作業へ戻るかもしれない）は当てない
        self.assertIsNone(self.decide(1, 150_000, 1))
        self.assertIsNone(self.decide(3, 99_000, 3))

    def test_cache_age_comes_from_last_response(self):
        # 起こし直し直後（状態の時刻は新しい）でも、最後の応答が2日前なら送らない
        self.assertIsNone(self.decide(3, 800_000, 2 * 24 * 60))
        self.assertEqual(self.decide(3, 800_000, 57)[0], "size")
        self.assertIsNone(self.decide(3, 800_000, 58))
        # 最後の応答が分からない会話には当てない
        self.assertIsNone(self.decide(3, 800_000, None))

    def test_human_quiet_blocks_size_rule(self):
        # 人の発言から15分は (1) を当てない（返事を読んで考えている間に割り込まない）
        self.assertIsNone(self.decide(3, 150_000, 3, human_min=5))
        self.assertIsNone(self.decide(3, 150_000, 3, human_min=12))
        self.assertEqual(self.decide(3, 150_000, 3, human_min=16)[0], "size")
        self.assertEqual(self.decide(50, 60_000, 50, human_min=50)[0], "idle")

    def test_human_guard_blocks_both_rules(self):
        # どちらの規則も、人の最後の発言から10分は当てない（利用者の決定 2026-10-20）
        self.assertEqual(sc.HUMAN_QUIET_SEC, 600)
        self.assertIsNone(self.decide(50, 150_000, 50, human_min=5))
        self.assertIsNone(self.decide(50, 60_000, 50, human_min=9.9))
        self.assertEqual(self.decide(50, 60_000, 50, human_min=10)[0], "idle")
        # (1) の15分を短く設定しても、10分は守る
        config = ac.config_from({"auto_clear_human_quiet_minutes": 1})
        session = _session(status_updated_at=(NOW - 180) * 1000)
        self.assertIsNone(
            ac.decide(
                session,
                150_000,
                NOW,
                config,
                last_response=NOW - 180,
                last_human=NOW - 300,
            )
        )
        self.assertEqual(
            ac.decide(
                session,
                150_000,
                NOW,
                config,
                last_response=NOW - 180,
                last_human=NOW - 601,
            )[0],
            "size",
        )

    def test_automated_texts_are_listed(self):
        # 本アプリが送る自動の文は、人の発言から外す頭の一覧に入っていること
        self.assertTrue(ac.trigger_text(5002).startswith(sc.AUTOMATED_PREFIXES))
        self.assertTrue(
            sc.RESUME_TEXT.format(path="C:/x").startswith(sc.AUTOMATED_PREFIXES)
        )
        self.assertTrue(
            sc.AUTO_RESUME_TEXT.format(path="C:/x").startswith(sc.AUTOMATED_PREFIXES)
        )

    def test_idle_rule_window(self):
        self.assertIsNone(self.decide(49, 60_000, 49))
        self.assertEqual(self.decide(50, 60_000, 50)[0], "idle")
        self.assertEqual(self.decide(57.5, 60_000, 57.5)[0], "idle")
        self.assertIsNone(self.decide(58, 60_000, 58))
        self.assertIsNone(self.decide(120, 60_000, 120))
        self.assertIsNone(self.decide(55, 40_000, 55))

    def test_not_idle_or_unknown_context(self):
        self.assertIsNone(
            ac.decide(_session(status="busy"), 500_000, NOW, CONFIG, NOW - 300)
        )
        self.assertIsNone(
            ac.decide(_session(status_updated_at=None), 500_000, NOW, CONFIG, NOW - 300)
        )
        self.assertIsNone(self.decide(10, None, 10))

    def test_exclusions(self):
        self.assertEqual(ac.exclusions(_session(), [], False, False), [])
        reasons = ac.exclusions(
            _session(attention={"kind": "permission"}), ["a1"], True, True
        )
        self.assertEqual(len(reasons), 4)
        self.assertEqual(
            ac.exclusions(_session(kind="print"), [], False, False),
            ["入力欄の無い実行"],
        )


class TestBackgroundWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        open(self.path, "w").close()
        self.work = ac.BackgroundWork()

    def append(self, *records):
        with open(self.path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_agent_and_command_lifecycle(self):
        self.append(
            _result({"status": "async_launched", "agentId": "a1"}),
            _result({"backgroundTaskId": "b1", "stdout": ""}),
        )
        self.assertEqual(self.work.running(self.path, NOW), ["a1", "b1"])
        self.append(
            _notification(
                "<task-notification>\n<task-id>a1</task-id>\n<tool-use-id>x</tool-use-id>\n"
                "<status>completed</status>\n</task-notification>"
            )
        )
        self.assertEqual(self.work.running(self.path, NOW), ["b1"])

    def test_monitor_shape_and_expiry(self):
        # Monitor の結果は taskId・timeoutMs・persistent（2026-10-12 実記録）
        self.append(
            _result({"taskId": "m1", "timeoutMs": 1_800_000, "persistent": False})
        )
        self.assertEqual(self.work.running(self.path, NOW), ["m1"])
        # 途中の知らせ（status なし）では終わらない
        self.append(
            _notification(
                "<task-notification>\n<task-id>m1</task-id>\n<event>x</event>\n</task-notification>"
            )
        )
        self.assertEqual(self.work.running(self.path, NOW), ["m1"])
        # timeoutMs を過ぎたら走っていない
        self.assertEqual(self.work.running(self.path, NOW + 1800), [])
        self.append(
            _notification(
                "<task-notification>\n<task-id>m1</task-id>\n"
                "<event>[Monitor expired after 30m with 2 events delivered.]</event>\n</task-notification>"
            )
        )
        self.assertEqual(self.work.running(self.path, NOW), [])

    def test_task_stop_ends_without_notification(self):
        self.append(
            _result({"backgroundTaskId": "b7"}),
            _result(
                {
                    "message": "Successfully stopped task: b7 (timeout 1200 python x.py)",
                    "task_id": "b7",
                }
            ),
        )
        self.assertEqual(self.work.running(self.path, NOW), [])

    def test_schedule_wakeup_and_cron(self):
        self.append(
            _result(
                {
                    "scheduledFor": (NOW + 1500) * 1000,
                    "clampedDelaySeconds": 1500,
                    "wasClamped": False,
                }
            ),
            _result(
                {
                    "id": "ce0cf7bd",
                    "humanSchedule": "Every day at 5:13 AM",
                    "recurring": True,
                    "durable": False,
                }
            ),
            _result(
                {
                    "id": "d1",
                    "humanSchedule": "Every day",
                    "recurring": True,
                    "durable": True,
                }
            ),
        )
        scanned = self.work.scan(self.path, NOW)
        self.assertEqual(scanned["running"], ["cron:ce0cf7bd", "wakeup"])
        self.assertEqual(set(scanned["carried"]), {"cron:ce0cf7bd", "wakeup"})
        # 起床の時刻を過ぎたら走っていない
        self.assertEqual(
            self.work.running(self.path, NOW + 1500 + ac.WAKEUP_GRACE_SEC + 1),
            ["cron:ce0cf7bd"],
        )
        self.append(
            _result({"id": "ce0cf7bd"}),
            _result(
                {
                    "scheduledFor": 0,
                    "clampedDelaySeconds": 0,
                    "wasClamped": False,
                    "stopped": True,
                    "cancelledWakeups": 1,
                }
            ),
        )
        self.assertEqual(self.work.running(self.path, NOW), [])

    def test_last_response_and_human(self):
        self.append(_human(NOW - 600), _assistant(NOW - 500))
        scanned = self.work.scan(self.path, NOW)
        self.assertAlmostEqual(scanned["last_response"], NOW - 500, places=0)
        self.assertAlmostEqual(scanned["last_human"], NOW - 600, places=0)
        # サイドチェーン（サブエージェント）の応答は数えない
        self.append(dict(_assistant(NOW - 10), isSidechain=True))
        self.assertAlmostEqual(
            self.work.scan(self.path, NOW)["last_response"], NOW - 500, places=0
        )

    def test_automated_texts_are_not_human(self):
        # 本アプリが打鍵で送る文も origin.kind=human で載る（2026-10-20 実測）が、人の発言に数えない
        self.append(_human(NOW - 900))
        for epoch, text in (
            (NOW - 300, ac.trigger_text(5002)),
            (NOW - 200, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")),
            (NOW - 100, sc.RESUME_TEXT.format(path="C:/w/NOW.md")),
        ):
            self.append(dict(_human(epoch), message={"content": text}))
        # 部品の形（text の部品）でも見分ける
        self.append(
            dict(
                _human(NOW - 50),
                message={"content": [{"type": "text", "text": ac.trigger_text(5002)}]},
            )
        )
        self.assertAlmostEqual(
            self.work.scan(self.path, NOW)["last_human"], NOW - 900, places=0
        )
        # 他のセッションからの指示の貼り付け（前置きつき）は人の依頼の中継なので数える
        self.append(
            dict(
                _human(NOW - 20), message={"content": sm.DEFAULT_PASTE_PREAMBLE + "\nx"}
            )
        )
        self.assertAlmostEqual(
            self.work.scan(self.path, NOW)["last_human"], NOW - 20, places=0
        )

    def test_queued_human_counts(self):
        # 作業中に吸い込まれた人の発言（attachment の queued_command）は打った時刻で数える
        self.append(
            _human(NOW - 900),
            {
                "type": "attachment",
                "timestamp": _iso(NOW - 60),
                "attachment": {
                    "type": "queued_command",
                    "prompt": "あと○○も",
                    "origin": {"kind": "human"},
                    "timestamp": _iso(NOW - 120),
                },
            },
        )
        self.assertAlmostEqual(
            self.work.scan(self.path, NOW)["last_human"], NOW - 120, places=0
        )

    def test_base_context_is_first_counted_response(self):
        with_usage = _with_usage
        # 使用量の無い応答・合成の応答・サイドチェーンは基準にしない
        synthetic = with_usage(_assistant(NOW - 800), 999)
        synthetic["message"]["model"] = "<synthetic>"
        self.append(
            _assistant(NOW - 900),
            synthetic,
            dict(with_usage(_assistant(NOW - 850), 5_000), isSidechain=True),
        )
        self.assertIsNone(self.work.scan(self.path, NOW)["base_context"])
        self.append(
            with_usage(_assistant(NOW - 700), 50_000, created=12_000),
            with_usage(_assistant(NOW - 600), 90_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 62_003)
        # 読み進めても最初の値のまま
        self.append(with_usage(_assistant(NOW - 500), 120_000))
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 62_003)
        self.assertIsNone(self.work.scan(None, NOW)["base_context"])

    def test_base_context_resets_after_compact(self):
        # 要約の後は同じ会話記録に続き、文脈が縮む。基準を取り直さないと、
        # 要約の後に伸びた分を見逃す（評価役の指摘）
        self.append(
            _with_usage(_assistant(NOW - 900), 60_000),
            _with_usage(_assistant(NOW - 800), 180_000),
            dict(_compact(), isSidechain=True),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 60_003)
        self.append(_compact())
        self.assertIsNone(self.work.scan(self.path, NOW)["base_context"])
        self.append(
            _with_usage(_assistant(NOW - 700), 40_000),
            _with_usage(_assistant(NOW - 600), 79_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 40_003)

    def test_stale_and_sidechain_and_missing(self):
        later = NOW - 900 + ac.BACKGROUND_STALE_SEC + 1
        self.append(
            _result({"status": "async_launched", "agentId": "old"}),
            dict(_result({"backgroundTaskId": "side"}, NOW), isSidechain=True),
        )
        self.assertEqual(self.work.running(self.path, later), [])
        self.assertEqual(self.work.running(None, NOW), [])
        self.assertEqual(self.work.running(self.path + ".none", NOW), [])

    def test_mid_turn_notification_forms(self):
        # ターンの途中に届いた知らせは queue-operation と attachment の形で載る
        body = "<task-notification>\n<task-id>{}</task-id>\n<status>killed</status>\n</task-notification>"
        self.append(
            _result({"status": "async_launched", "agentId": "a1"}),
            _result({"status": "async_launched", "agentId": "a2"}),
            _result({"status": "async_launched", "agentId": "a3"}),
            # AI の発言や queue の remove に引用されたものは数えない
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": body.format("a3")}]},
            },
            {
                "type": "queue-operation",
                "operation": "remove",
                "content": body.format("a3"),
            },
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": body.format("a1"),
            },
            {
                "type": "attachment",
                "attachment": {
                    "type": "queued_command",
                    "origin": {"kind": "task-notification"},
                    "prompt": body.format("a2"),
                },
            },
        )
        self.assertEqual(self.work.running(self.path, NOW), ["a3"])

    def test_multiple_ids_in_one_notification(self):
        self.append(
            _result({"backgroundTaskId": "b1"}),
            _result({"backgroundTaskId": "b2"}),
            _notification(
                "<task-notification><task-id>b1</task-id><task-id>b2</task-id>"
                "<status>stopped</status></task-notification>"
            ),
        )
        self.assertEqual(self.work.running(self.path, NOW), [])


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(
                send_history,
                "HISTORY_FILE",
                os.path.join(self.tmp.name, "send_history.jsonl"),
            ),
            mock.patch.object(sm, "list_sessions", return_value=[]),
            mock.patch.object(sm, "_transcript_sizes", return_value={}),
            mock.patch.object(sm, "_transcript_has_prompt", return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sc._pending.clear()
        sc._auto_expected.clear()
        self.addCleanup(sc._pending.clear)
        self.addCleanup(sc._auto_expected.clear)
        self.context = {}
        self.tracker = mock.Mock()
        self.tracker.context_tokens.side_effect = lambda path: self.context.get(path)
        self.sender = mock.Mock(return_value={"elapsed_sec": 5.1})
        self.auto = self.make()
        self.transcript("sess-a", 150_000, _assistant(NOW - 600))

    def path(self, session_id):
        return os.path.join(self.tmp.name, f"{session_id}.jsonl")

    def transcript(self, session_id, context, *records):
        """会話記録を書き、文脈の大きさを決める。"""
        self.context[self.path(session_id)] = context
        with open(self.path(session_id), "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def make(self):
        return ac.AutoClear(
            self.tracker,
            lambda cwd, sid: self.path(sid) if sid else None,
            keep_marks=ac.KeepMarks(os.path.join(self.tmp.name, "keep.json")),
            state_file=os.path.join(self.tmp.name, "state.json"),
            result_log=os.path.join(self.tmp.name, "log.jsonl"),
            sender=self.sender,
        )

    def logged(self):
        with open(os.path.join(self.tmp.name, "log.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]


ON = {"auto_clear_mode": "on"}


class TestTick(_Base):
    def test_dry_run_records_once_without_sending(self):
        hits = self.auto.tick([_session()], {}, NOW)
        self.assertEqual([h["rule"] for h in hits], ["size"])
        self.sender.assert_not_called()
        self.assertEqual(self.logged()[-1]["outcome"], "would_send")
        self.assertEqual(self.auto.tick([_session()], {}, NOW + 60), [])
        # 再起動しても同じ会話を数え直さない
        self.assertEqual(self.make().tick([_session()], {}, NOW + 120), [])
        self.assertEqual(self.make().status()["recent"][-1]["outcome"], "would_send")

    def test_no_growth_since_first_response_is_not_hit(self):
        # 区切って引き継ぎを読んだだけの会話（伸び 5,532）は当てない（2026-10-14 の空回り）
        self.transcript(
            "sess-b",
            67_754,
            _with_usage(_assistant(NOW - 3100), 62_219),
            _with_usage(_assistant(NOW - 3030), 67_751),
        )
        session = _session(session_id="sess-b", status_updated_at=(NOW - 3030) * 1000)
        self.assertEqual(self.auto.tick([session], ON, NOW), [])
        self.sender.assert_not_called()
        self.assertEqual(self.auto.assess(session, NOW)["base_context_tokens"], 62_222)
        # 伸びれば当たる
        self.transcript("sess-b", 90_000, _with_usage(_assistant(NOW - 3000), 89_997))
        hits = self.auto.tick([session], ON, NOW + 30)
        self.assertEqual([h["rule"] for h in hits], ["idle"])

    def test_off_does_nothing(self):
        self.assertEqual(
            self.auto.tick([_session()], {"auto_clear_mode": "off"}, NOW), []
        )
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "log.jsonl")))

    def test_on_sends_trigger_and_marks_expectation(self):
        hits = self.auto.tick([_session()], dict(ON, port=5098), NOW)
        self.assertEqual(len(hits), 1)
        args, kwargs = self.sender.call_args
        self.assertEqual(args, (4242,))
        self.assertEqual(kwargs["text"], ac.trigger_text(5098))
        self.assertTrue(kwargs["protect_draft"])
        expected = sc.auto_expectations()
        self.assertEqual(
            [(e["pid"], e["session_id"]) for e in expected], [(4242, "sess-a")]
        )
        self.assertEqual(self.logged()[-1]["outcome"], "sent")
        # 同じ会話へは二度送らない
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 1)
        with open(send_history.HISTORY_FILE, encoding="utf-8") as f:
            history = [json.loads(line) for line in f]
        self.assertEqual(history[-1]["kind"], "auto_clear")
        self.assertTrue(history[-1]["ok"])

    def test_no_reservation_is_recorded_once_and_mark_kept_for_late(self):
        self.auto.tick([_session()], ON, NOW)
        wait = CONFIG["handoff_wait_minutes"] * 60
        self.auto.tick([], ON, NOW + wait + 1)
        self.assertEqual(self.logged()[-1]["outcome"], "no_reservation")
        self.auto.tick([], ON, NOW + wait + 61)
        self.assertEqual(
            [e["outcome"] for e in self.logged()].count("no_reservation"), 1
        )
        # 印は残り、遅れて来た予約も自動の区切りとして扱う
        self.assertTrue(sc.auto_expectations()[0]["late"])

    def test_busy_retries_next_tick(self):
        self.sender.side_effect = [sm.SessionBusyError("busy"), {"elapsed_sec": 1}]
        self.auto.tick([_session()], ON, NOW)
        self.assertEqual(sc.auto_expectations(), [])
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.logged()[-1]["outcome"], "sent")

    def test_draft_waits_before_retry(self):
        self.sender.side_effect = sm.SessionDraftError("書きかけ。入力欄: 秘密の下書き")
        self.auto.tick([_session()], ON, NOW)
        entry = self.logged()[-1]
        self.assertEqual(entry["outcome"], "draft_present")
        self.assertNotIn("秘密", entry["detail"])
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 1)
        self.auto.tick([_session()], ON, NOW + ac.DRAFT_RETRY_SEC + 1)
        self.assertEqual(self.sender.call_count, 2)

    def test_failure_is_not_retried(self):
        self.sender.side_effect = sm.SessionError("入力欄が見えない")
        self.auto.tick([_session()], ON, NOW)
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.logged()[-1]["outcome"], "send_failed")
        self.assertEqual(sc.auto_expectations(), [])

    def test_confirm_timeout_but_delivered_counts_as_sent(self):
        # 打鍵は済み、会話記録との照合だけが時間切れ → 子には届いているので印を残す
        self.sender.side_effect = sm.SessionError(
            "会話記録に載ったことを確認できません"
        )
        with mock.patch.object(sm, "_transcript_has_prompt", return_value=True):
            self.auto.tick([_session()], ON, NOW)
        self.assertEqual(self.logged()[-1]["outcome"], "sent")
        self.assertEqual(len(sc.auto_expectations()), 1)

    def test_unexpected_error_does_not_stop_other_hits(self):
        self.transcript("sess-b", 150_000, _assistant(NOW - 600))
        self.sender.side_effect = [RuntimeError("予想外"), {"elapsed_sec": 1}]
        self.auto.tick(
            [_session(), _session(pid=5, session_id="sess-b", started_at=5)], ON, NOW
        )
        outcomes = [e["outcome"] for e in self.logged()]
        self.assertEqual(outcomes, ["send_failed", "sent"])
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 2)

    def test_recheck_before_send(self):
        # 当たりを集めた後に押された「残す」・入った予約では送らない
        self.assertTrue(self.auto._still_eligible(_session()))
        self.auto.keep_marks.set(_session(), True)
        self.assertFalse(self.auto._still_eligible(_session()))
        self.auto.keep_marks.set(_session(), False)
        sc._pending["4242"] = {"pid": 4242}
        self.assertFalse(self.auto._still_eligible(_session()))

    def test_cold_cache_after_restart_is_not_hit(self):
        # 起こし直した直後（状態の時刻は新しい）でも、最後の応答が古ければ送らない
        self.transcript("sess-c", 800_000, _assistant(NOW - 2 * 86400))
        session = _session(
            pid=7,
            session_id="sess-c",
            started_at=7,
            status_updated_at=(NOW - 180) * 1000,
        )
        self.assertEqual(self.auto.tick([session], ON, NOW), [])

    def test_exclusions_block(self):
        cases = [
            _session(launched_by_app=False),
            _session(attention={"kind": "permission", "certain": True}),
            _session(status="waiting"),
            _session(kind="print"),
            _session(cwd=sm.RELOGIN_DIR),
        ]
        for case in cases:
            self.assertEqual(self.auto.tick([case], {}, NOW), [], case)
        # 残す印
        self.auto.keep_marks.set(_session(), True)
        self.assertEqual(self.auto.tick([_session()], {}, NOW), [])
        self.auto.keep_marks.set(_session(), False)
        # 区切りの予約が既にある
        sc._pending["4242"] = {"pid": 4242}
        self.assertEqual(self.auto.tick([_session()], {}, NOW), [])
        sc._pending.clear()
        # 裏の仕事
        self.transcript("sess-a", 150_000, _result({"backgroundTaskId": "b1"}))
        self.assertEqual(self.auto.tick([_session()], {}, NOW), [])
        self.transcript(
            "sess-a",
            150_000,
            _notification(
                "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>"
            ),
        )
        self.assertEqual(len(self.auto.tick([_session()], {}, NOW)), 1)

    def test_cron_carries_over_clear(self):
        # /clear の前の会話で始めた定期の仕事は、新しい会話でも走っているとみなす
        self.transcript(
            "sess-a",
            150_000,
            _result({"id": "c1", "humanSchedule": "毎日", "recurring": True}),
        )
        self.assertEqual(self.auto.tick([_session()], {}, NOW), [])
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        self.assertEqual(
            self.auto.tick([_session(session_id="sess-new")], {}, NOW + 60), []
        )
        view = self.auto.assess(_session(session_id="sess-new"), NOW + 60)
        self.assertIn("裏の仕事が走っている（1件）", view["excluded"])


class TestKeepMarks(_Base):
    def test_set_and_cleanup(self):
        marks = self.auto.keep_marks
        alive = _session()
        self.assertTrue(marks.set(alive, True))
        self.assertFalse(marks.set(alive, True))
        self.assertTrue(marks.is_kept(alive))
        # 別のプロセス（同じ pid でも開始時刻が違う）には効かない
        self.assertFalse(marks.is_kept(_session(started_at=999)))
        # 終わったプロセスの印は、次に付け外ししたときに掃除される
        with mock.patch.object(
            sm, "list_sessions", return_value=[_session(pid=1, started_at=1)]
        ):
            marks.set(_session(pid=1, started_at=1), True)
        self.assertEqual(list(marks.all()), ["1:1"])
        self.assertTrue(marks.set(_session(pid=1, started_at=1), False))
        self.assertEqual(marks.all(), {})


class TestRelaunch(_Base):
    def unmanaged(self, **extra):
        return _session(launched_by_app=False, win_session=1, elevated=False, **extra)

    def relaunch(self, session, **kwargs):
        kwargs.setdefault("stop", mock.Mock())
        kwargs.setdefault(
            "launch",
            mock.Mock(return_value={"pid": 99, "elevated": False, "win_session": 1}),
        )
        kwargs.setdefault("options_of", lambda pid, dropped: {})
        kwargs.setdefault("precheck", mock.Mock())
        kwargs.setdefault("check_input", mock.Mock())
        return self.auto.relaunch(session, now=NOW, **kwargs)

    def test_unmanaged_list(self):
        items = self.auto.unmanaged(
            [_session(), self.unmanaged(), self.unmanaged(pid=7, status="busy")], NOW
        )
        self.assertEqual([i["pid"] for i in items], [4242, 7])
        self.assertTrue(items[0]["can_relaunch"])
        self.assertEqual(items[0]["context_tokens"], 150_000)
        self.assertEqual(items[0]["response_age_minutes"], 10.0)
        self.assertFalse(items[1]["can_relaunch"])
        self.assertIn("入力待ちでない", items[1]["blockers"])

    def test_relaunch_once_with_options(self):
        stop = mock.Mock()
        launch = mock.Mock(
            return_value={"pid": 99, "elevated": False, "win_session": 1}
        )

        def options_of(pid, dropped):
            dropped.append("--add-dir")
            return {"model": "opus[1m]"}

        result = self.relaunch(
            self.unmanaged(),
            claude_exe="claude.exe",
            stop=stop,
            launch=launch,
            options_of=options_of,
        )
        stop.assert_called_once_with(4242)
        kwargs = launch.call_args[1]
        self.assertEqual(kwargs["resume_session_id"], "sess-a")
        self.assertEqual(kwargs["model"], "opus[1m]")
        self.assertEqual(kwargs["win_session"], "1")
        self.assertEqual(kwargs["elevation"], "standard")
        self.assertEqual(result["pid"], 99)
        self.assertEqual(result["dropped_args"], ["--add-dir"])
        self.assertTrue(self.auto.was_relaunched("sess-a"))
        with self.assertRaises(ac.RelaunchError) as cm:
            self.relaunch(self.unmanaged(pid=99), stop=stop)
        self.assertIn("一度起こし直した", str(cm.exception))
        self.assertFalse(cm.exception.stopped)
        # 再起動しても覚えている
        self.assertTrue(self.make().was_relaunched("sess-a"))

    def test_relaunch_refusals_do_not_stop(self):
        stop = mock.Mock()
        refused = [
            _session(),  # 既に管理下
            self.unmanaged(status_updated_at=(NOW - 60) * 1000),  # 止まって間もない
            self.unmanaged(attention={"kind": "permission"}),
        ]
        for case in refused:
            with self.assertRaises(ac.RelaunchError):
                self.relaunch(case, stop=stop)
        self.auto.keep_marks.set(self.unmanaged(), True)
        with self.assertRaises(ac.RelaunchError):
            self.relaunch(self.unmanaged(), stop=stop)
        stop.assert_not_called()

    def test_precheck_and_draft_are_checked_before_stop(self):
        stop = mock.Mock()
        with self.assertRaises(ac.RelaunchError) as cm:
            self.relaunch(
                self.unmanaged(),
                stop=stop,
                precheck=mock.Mock(side_effect=sm.SessionError("記録が見つかりません")),
            )
        self.assertIn("止める前の確かめ", str(cm.exception))
        with self.assertRaises(ac.RelaunchError) as cm:
            self.relaunch(
                self.unmanaged(),
                stop=stop,
                check_input=mock.Mock(
                    side_effect=sm.SessionDraftError("書きかけ。入力欄: 長い下書き")
                ),
            )
        self.assertIn("書きかけ", str(cm.exception))
        self.assertNotIn("長い下書き", str(cm.exception))
        with self.assertRaises(ac.RelaunchError):
            self.relaunch(
                self.unmanaged(),
                stop=stop,
                check_input=mock.Mock(
                    side_effect=sm.SessionError("入力欄が見えません")
                ),
            )
        stop.assert_not_called()
        self.assertFalse(self.auto.was_relaunched("sess-a"))

    def test_real_precheck(self):
        # 本物の確かめ: フォルダが無い・会話記録が無い・選択肢に無い引数は断る
        with self.assertRaises(sm.SessionError):
            ac._relaunch_precheck(self.unmanaged(cwd="Z:/none"), {}, None)
        with mock.patch.object(
            sm, "transcript_dir_for", return_value=self.tmp.name
        ), self.assertRaises(sm.SessionError):
            ac._relaunch_precheck(
                self.unmanaged(
                    cwd=self.tmp.name,
                    session_id="0" * 8 + "-0000-0000-0000-" + "0" * 12,
                ),
                {},
                None,
            )
        with mock.patch.object(sm, "validate_resume_target"), self.assertRaises(
            sm.SessionError
        ):
            ac._relaunch_precheck(
                self.unmanaged(cwd=self.tmp.name), {"model": "nope"}, None
            )

    def test_launch_failure_after_stop(self):
        launch = mock.Mock(side_effect=sm.SessionError("起動に失敗"))
        with self.assertRaises(ac.RelaunchError) as cm:
            self.relaunch(self.unmanaged(), launch=launch)
        self.assertTrue(cm.exception.stopped)
        self.assertIn("sess-a", str(cm.exception))
        self.assertTrue(self.auto.was_relaunched("sess-a"))

    def test_stop_failure_is_not_marked(self):
        stop = mock.Mock(side_effect=sm.SessionError("アクセス拒否"))
        with self.assertRaises(ac.RelaunchError) as cm:
            self.relaunch(self.unmanaged(), stop=stop)
        self.assertFalse(cm.exception.stopped)
        self.assertFalse(self.auto.was_relaunched("sess-a"))


class TestLaunchOptionsOf(unittest.TestCase):
    def _of(self, argv):
        proc = mock.Mock()
        proc.cmdline.return_value = argv
        dropped = []
        with mock.patch.object(sm.psutil, "Process", return_value=proc):
            return sm.launch_options_of(1, dropped), dropped

    def test_reads_known_values_only(self):
        found, dropped = self._of(
            [
                "claude.exe",
                "--model",
                "sonnet",
                "--effort=high",
                "-n",
                "x",
                "--add-dir",
                "D:/a",
            ]
        )
        self.assertEqual(found, {"model": "sonnet", "effort": "high"})
        self.assertEqual(dropped, ["--add-dir"])
        self.assertEqual(self._of(["claude.exe", "--model", "claude-x-9"])[0], {})
        self.assertEqual(
            self._of(["claude.exe", "--dangerously-skip-permissions"])[0],
            {"permission_mode": "bypassPermissions"},
        )
        # 最初の指示（-- の後ろ）の中の語は読まない
        self.assertEqual(self._of(["claude.exe", "--", "--model", "sonnet"]), ({}, []))

    def test_unreadable(self):
        with mock.patch.object(
            sm.psutil, "Process", side_effect=sm.psutil.AccessDenied()
        ):
            self.assertEqual(sm.launch_options_of(1), {})


class TestEndpoints(unittest.TestCase):
    """Flask の受け口（残す印・管理外の一覧・起こし直し・状態）。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "claude_web_app_for_auto_test",
            os.path.join(PROJECT_ROOT, "claude-web-app.py"),
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auto = mock.Mock()
        self.auto.keep_marks = ac.KeepMarks(os.path.join(self.tmp.name, "keep.json"))
        patches = [
            mock.patch.object(self.module, "auto", self.auto),
            mock.patch.object(self.module, "broadcast_sessions"),
            mock.patch.object(
                self.module,
                "annotated_sessions",
                return_value=[_session(launched_by_app=False)],
            ),
            mock.patch.object(
                send_history,
                "HISTORY_FILE",
                os.path.join(self.tmp.name, "send_history.jsonl"),
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_keep_put_delete_and_missing(self):
        with mock.patch.object(
            sm, "find_session_by_pid", return_value=_session()
        ), mock.patch.object(sm, "list_sessions", return_value=[_session()]):
            response = self.client.put("/api/sessions/4242/keep")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["kept"])
            self.assertTrue(self.auto.keep_marks.is_kept(_session()))
            response = self.client.delete("/api/sessions/4242/keep")
            self.assertFalse(response.get_json()["kept"])
        with mock.patch.object(sm, "find_session_by_pid", return_value=None):
            self.assertEqual(self.client.put("/api/sessions/1/keep").status_code, 404)

    def test_status_and_unmanaged(self):
        self.auto.status.return_value = {"config": {}, "recent": []}
        self.assertEqual(self.client.get("/api/auto-clear").status_code, 200)
        self.auto.unmanaged.return_value = [{"pid": 4242}]
        body = self.client.get("/api/sessions/unmanaged").get_json()
        self.assertEqual(body["sessions"], [{"pid": 4242}])

    def test_relaunch_status_codes(self):
        self.auto.relaunch.side_effect = ac.RelaunchError(
            "起こし直せません: 既に管理下"
        )
        response = self.client.post("/api/sessions/4242/relaunch")
        self.assertEqual(response.status_code, 400)
        # 止める前に断っただけなら履歴に残さない
        self.assertFalse(os.path.exists(send_history.HISTORY_FILE))
        self.auto.relaunch.side_effect = ac.RelaunchError(
            "止めたが起こせない", stopped=True
        )
        self.assertEqual(
            self.client.post("/api/sessions/4242/relaunch").status_code, 502
        )
        self.assertTrue(os.path.exists(send_history.HISTORY_FILE))
        self.assertEqual(self.client.post("/api/sessions/1/relaunch").status_code, 404)
        self.auto.relaunch.side_effect = None
        self.auto.relaunch.return_value = {"pid": 99, "options": {}}
        with mock.patch.object(
            self.module, "wait_for_session", return_value={"pid": 99}
        ):
            response = self.client.post("/api/sessions/4242/relaunch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["session"], {"pid": 99})


class TestBrokenStdout(_Base):
    """標準出力が書けないときも、送れた当たりを send_failed に化けさせない（2026-10-22）。"""

    def test_sent_is_not_turned_into_send_failed(self):
        class Broken:
            def write(self, text):
                raise OSError(22, "Invalid argument")

            def flush(self):
                raise OSError(22, "Invalid argument")

        with mock.patch.object(sys, "stdout", Broken()):
            hits = self.auto.tick([_session()], ON, NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual([e["outcome"] for e in self.logged()], ["sent"])
        # 待ちの印（自動の区切りの予約を待つ）も消えていない
        self.assertEqual(len(sc.auto_expectations()), 1)


if __name__ == "__main__":
    unittest.main()

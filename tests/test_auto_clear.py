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
        self.assertEqual(ac.exclusions(_session(), False, False), [])
        reasons = ac.exclusions(_session(attention={"kind": "permission"}), True, True)
        self.assertEqual(len(reasons), 3)
        self.assertEqual(
            ac.exclusions(_session(kind="print"), False, False),
            ["入力欄の無い実行"],
        )

    def test_shell_status_counts_as_input_ready(self):
        """裏の仕事を持った入力待ち（status=shell）も入力待ちとして規則を当てる（2026-11-03）。"""
        shell = _session(status="shell")
        self.assertIsNotNone(ac.idle_seconds(shell, NOW))
        self.assertEqual(
            ac.decide(shell, 150_000, NOW, CONFIG, NOW - 180, background=1)[0], "size"
        )

    def test_background_gets_size_rule_only(self):
        """裏の仕事が走っている会話には size だけを当てる（idle は仕事の途中で止まっているだけ）。"""
        self.assertEqual(self.decide(50.5, 82_222, 50.5)[0], "idle")
        self.assertIsNone(
            ac.decide(
                _session(status_updated_at=(NOW - 50.5 * 60) * 1000),
                82_222,
                NOW,
                CONFIG,
                NOW - 50.5 * 60,
                background=2,
            )
        )
        self.assertEqual(
            ac.decide(
                _session(status_updated_at=(NOW - 180) * 1000),
                150_000,
                NOW,
                CONFIG,
                NOW - 180,
                background=2,
            )[0],
            "size",
        )

    def test_trigger_text_with_background(self):
        plain = ac.trigger_text(5002)
        text = ac.trigger_text(5002, background=2)
        # 先頭（照合に使う）は変えず、末尾に裏の仕事の一文を足す
        self.assertTrue(text.startswith(plain))
        self.assertTrue(text.startswith(sc.AUTOMATED_PREFIXES))
        self.assertIn("2件走っているとみています", text)
        self.assertIn("実際に走っているものを確かめ", text)
        self.assertIn("id（起床の予約は予定時刻）・目的・知らせが来たらすること", text)
        self.assertEqual(ac.trigger_text(5002, background=0), plain)
        self.assertEqual(ac.trigger_text(5002, background=None), plain)


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

    @staticmethod
    def _clear_records(epoch, text):
        """/clear の後の会話記録の頭（2026-10-27 実記録の形・自動の文は origin.kind=plugin）。"""
        return [
            {
                "type": "user",
                "isMeta": True,
                "timestamp": _iso(epoch),
                "origin": {"kind": "plugin"},
                "message": {
                    "content": "<local-command-caveat>x</local-command-caveat>"
                },
            },
            {
                "type": "user",
                "timestamp": _iso(epoch),
                "origin": {"kind": "plugin"},
                "message": {"content": "<command-name>/clear</command-name>"},
            },
            {
                "type": "user",
                "timestamp": _iso(epoch),
                "origin": {"kind": "plugin"},
                "message": {"content": text},
            },
        ]

    def test_base_context_after_auto_resume_is_end_of_first_turn(self):
        # 引き継ぎを読む前（5万）ではなく、読み終えた所（7.17万）を基準にする
        self.append(
            *self._clear_records(
                NOW - 3100, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
            ),
            _with_usage(_assistant(NOW - 3097), 50_185),
            _result({"type": "text"}, NOW - 3096),
            _with_usage(_assistant(NOW - 3094), 71_740),
            _with_usage(_assistant(NOW - 3093), 71_740),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 71_743)
        # 次の入力（自動の区切りの依頼）の後は動かさない
        self.append(
            dict(_human(NOW - 60), message={"content": ac.trigger_text(5002)}),
            _with_usage(_assistant(NOW - 55), 72_700),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 71_743)

    def test_base_context_stops_at_next_prompt_after_auto_resume(self):
        # 待っている間に人が頼んだ仕事の伸びは基準に入れない（吸い込まれた発言でも同じ）
        self.append(
            *self._clear_records(
                NOW - 3100, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
            ),
            _with_usage(_assistant(NOW - 3097), 72_000),
            _human(NOW - 2000),
            _with_usage(_assistant(NOW - 1990), 90_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 72_003)
        self.append(
            *self._clear_records(
                NOW - 900, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
            )
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 72_003)
        other = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, "q.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in (
                *self._clear_records(
                    NOW - 3100, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
                ),
                _with_usage(_assistant(NOW - 3097), 72_000),
                {
                    "type": "attachment",
                    "attachment": {
                        "type": "queued_command",
                        "prompt": "これもやって",
                        "origin": {"kind": "human"},
                    },
                },
                _with_usage(_assistant(NOW - 3000), 95_000),
            ):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.assertEqual(other.scan(path, NOW)["base_context"], 72_003)

    def test_base_context_other_first_prompts_keep_first_response(self):
        # 人の依頼で始まった会話は、最初のターンが本物の仕事なので、従来どおり最初の応答を基準にする
        work = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, "p.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in (
                *self._clear_records(NOW - 3100, "やって"),
                _with_usage(_assistant(NOW - 3097), 50_000),
                _with_usage(_assistant(NOW - 3000), 60_000),
            ):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.assertEqual(work.scan(path, NOW)["base_context"], 50_003)

    def test_base_context_after_own_resume_is_end_of_first_turn(self):
        # 子が自分で区切った後（RESUME_TEXT）も、引き継ぎを読み終えた所を基準にする。
        # 2026-11-02 19:32 example-ops の再現: 読んで待っていただけで 48,289 → 72,591 になり、
        # 51分後に idle で区切りの依頼を送っていた（伝言 2f6b95093ae4）
        self.append(
            *self._clear_records(NOW - 3100, sc.RESUME_TEXT.format(path="C:/w/NOW.md")),
            _with_usage(_assistant(NOW - 3097), 48_289),
            _result({"type": "text"}, NOW - 3096),
            _with_usage(_assistant(NOW - 3090), 72_588),
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3089),
            },
        )
        scan = self.work.scan(self.path, NOW)
        self.assertEqual(scan["base_context"], 72_591)
        session = {"status": "idle", "status_updated_at": (NOW - 3080) * 1000}
        self.assertIsNone(
            ac.decide(
                session,
                72_591,
                NOW,
                ac.config_from({}),
                last_response=scan["last_response"],
                last_human=scan["last_human"],
                base_context=scan["base_context"],
            )
        )
        # 直す前の基準（最初の応答）なら当たっていた（陽性対照）
        self.assertEqual(
            ac.decide(
                session,
                72_591,
                NOW,
                ac.config_from({}),
                last_response=scan["last_response"],
                last_human=scan["last_human"],
                base_context=48_292,
            )[0],
            "idle",
        )
        # 次の入力の後の仕事の伸びは基準に入れない（その後は区切る）
        self.append(
            _human(NOW - 2000),
            _with_usage(_assistant(NOW - 1990), 100_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 72_591)

    def test_base_context_after_own_resume_stops_on_big_first_turn(self):
        # 引き継ぎを読んだと分からないまま最初のターンで4万を超えて伸びたら、そこで基準を止める
        # （大きな仕事の後に区切れなくならない）
        self.append(
            *self._clear_records(NOW - 3100, sc.RESUME_TEXT.format(path="C:/w/NOW.md")),
            _with_usage(_assistant(NOW - 3097), 50_000),
            _with_usage(_assistant(NOW - 3090), 70_000),
            _with_usage(_assistant(NOW - 3000), 150_000),
        )
        scan = self.work.scan(self.path, NOW)
        self.assertEqual(scan["base_context"], 70_003)
        # 止まった後の仕事の伸び（8万）で区切りの依頼が当たる
        session = {"status": "idle", "status_updated_at": (NOW - 2990) * 1000}
        hit = ac.decide(
            session,
            150_003,
            NOW,
            ac.config_from({}),
            last_response=scan["last_response"],
            last_human=scan["last_human"],
            base_context=scan["base_context"],
        )
        self.assertEqual(hit[0], "size")

    @staticmethod
    def _reading(epoch, read, path):
        """引き継ぎを Read する応答の記録（文脈 read + 3）。"""
        record = _with_usage(_assistant(epoch), read)
        record["message"]["content"] = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": path}}
        ]
        return record

    def _idle_hit(self, scan, context, idle_since):
        session = {"status": "idle", "status_updated_at": idle_since * 1000}
        return ac.decide(
            session,
            context,
            NOW,
            ac.config_from({}),
            last_response=scan["last_response"],
            last_human=scan["last_human"],
            base_context=scan["base_context"],
        )

    def test_base_context_after_own_resume_with_large_handoff(self):
        # 引き継ぎが大きい作業場（11-02 22:43 claude-web-app の再現）: 最初の Read が途中で切れ、
        # 止めた応答（83,146）の分割記録が offset 付きで続きを読み、90,509 まで読むだけで伸びた。
        # その後 96,752 までは仕事。続きを読み終えた所を基準にする（評価2周目の中2。上限4万だけなら 83,146）
        self.append(
            *self._clear_records(
                NOW - 3200, "  " + sc.RESUME_TEXT.format(path="C:/w/NOW.md")
            ),
            # 本文は / 区切り、Read は \ 区切りでも同じファイルとみなす
            self._reading(NOW - 3197, 47_452, "C:\\w\\now.md"),
            # 同じ応答が道具ごとに分かれた記録（文脈が同じ）では止めない
            _with_usage(_assistant(NOW - 3197), 47_452),
            _with_usage(_assistant(NOW - 3190), 83_143),
            self._reading(NOW - 3190, 83_143, "C:/w/NOW.md"),
            _with_usage(_assistant(NOW - 3185), 90_506),
            _with_usage(_assistant(NOW - 3120), 96_749),
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3119),
            },
        )
        scan = self.work.scan(self.path, NOW)
        self.assertEqual(scan["base_context"], 90_509)
        self.assertIsNone(self._idle_hit(scan, 96_752, NOW - 3110))

    def test_base_context_reading_alone_over_cap(self):
        # 引き継ぎを読んだ直後の応答だけで最初の応答から4万を超えても、読み終えた所を基準にする
        # （陽性対照: 上限だけなら最初の応答 47,003 に戻り、伸び 4.8万で当たっていた・評価2周目の中1）
        self.append(
            *self._clear_records(NOW - 3200, sc.RESUME_TEXT.format(path="C:/w/NOW.md")),
            self._reading(NOW - 3197, 47_000, "C:/w/NOW.md"),
            _with_usage(_assistant(NOW - 3190), 95_000),
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3189),
            },
        )
        scan = self.work.scan(self.path, NOW)
        self.assertEqual(scan["base_context"], 95_003)
        self.assertIsNone(self._idle_hit(scan, 95_003, NOW - 3180))

    def test_base_context_small_work_after_reading_is_growth(self):
        # 読んだ後に4万未満の仕事を続けた。その伸びは基準に入れない（陽性対照: 上限だけなら 80,003）
        self.append(
            *self._clear_records(NOW - 3200, sc.RESUME_TEXT.format(path="C:/w/NOW.md")),
            self._reading(NOW - 3197, 47_935, "C:/w/NOW.md"),
            _with_usage(_assistant(NOW - 3195), 71_401),
            _with_usage(_assistant(NOW - 3100), 80_000),
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3099),
            },
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 71_404)

    def test_base_context_after_own_resume_stops_after_reading(self):
        # 読んだ後に仕事を続けた（11-02 22:37 example-checker の再現: 読んだ直後 71,404・
        # ターンの終わり 103,506・22:55 に 120,491）。続けた仕事の伸びは基準に入れず、区切りの依頼が当たる
        self.append(
            *self._clear_records(
                NOW - 3200, sc.RESUME_TEXT.format(path="F:\\w\\NOW.md")
            ),
            self._reading(NOW - 3197, 47_938, "F:\\w\\NOW.md"),
            _with_usage(_assistant(NOW - 3195), 71_401),
            _with_usage(_assistant(NOW - 3100), 103_503),
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3099),
            },
            _with_usage(_assistant(NOW - 300), 120_488),
        )
        scan = self.work.scan(self.path, NOW)
        self.assertEqual(scan["base_context"], 71_404)
        self.assertEqual(self._idle_hit(scan, 120_491, NOW - 290)[0], "size")
        # 別のファイルを Read しても止めない（上限まで伸ばす従来の形）
        work = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, "other.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in (
                *self._clear_records(
                    NOW - 3200, sc.RESUME_TEXT.format(path="F:\\w\\NOW.md")
                ),
                self._reading(NOW - 3197, 47_938, "F:\\w\\README.md"),
                _with_usage(_assistant(NOW - 3195), 71_401),
                _with_usage(_assistant(NOW - 3190), 80_000),
            ):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.assertEqual(work.scan(path, NOW)["base_context"], 80_003)

    def test_turn_prompt_text(self):
        self.assertEqual(ac.turn_prompt_text(_human(NOW)), "やって")
        self.assertIsNone(ac.turn_prompt_text(_result({"x": 1})))
        self.assertIsNone(
            ac.turn_prompt_text(
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": "abc"}]},
                }
            )
        )
        self.assertIsNone(
            ac.turn_prompt_text(dict(_human(NOW), isMeta=True, origin=None))
        )
        self.assertIsNone(ac.turn_prompt_text(dict(_human(NOW), isCompactSummary=True)))
        self.assertIsNone(ac.turn_prompt_text(_assistant(NOW)))
        self.assertTrue(
            ac.AUTO_RESUME_HEAD.startswith("自動の区切りの /clear が済みました。")
        )
        self.assertTrue(ac.RESUME_HEAD.startswith("区切りの /clear が済みました。"))
        self.assertEqual(ac.RESUME_HEADS, (ac.AUTO_RESUME_HEAD, ac.RESUME_HEAD))
        # ほかのセッションからの連絡は isMeta=True・origin.kind=peer で載る（2026-10-27 実記録）
        self.assertTrue(ac.turn_prompt_text(self._peer(NOW)).startswith("Another"))
        # Stop フックの差し戻し・起床の予約の文（origin の無い isMeta）は入力ではない
        for text in (
            "Stop hook feedback:\nx",
            "example-game-c の待ち（wake_at 04:10）が過ぎた",
        ):
            self.assertIsNone(
                ac.turn_prompt_text(
                    {"type": "user", "isMeta": True, "message": {"content": text}}
                )
            )

    @staticmethod
    def _peer(epoch):
        return {
            "type": "user",
            "isMeta": True,
            "timestamp": _iso(epoch),
            "origin": {"kind": "peer", "name": "管理役"},
            "message": {"content": "Another Claude session sent a message:\nやって"},
        }

    def _auto_resume_file(self, name, *records):
        work = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            for record in (
                *self._clear_records(
                    NOW - 3100, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
                ),
                _with_usage(_assistant(NOW - 3097), 50_000),
                _with_usage(_assistant(NOW - 3094), 72_000),
                *records,
            ):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return work.scan(path, NOW)["base_context"]

    def test_base_context_stops_at_peer_message(self):
        # 待っている会話へ管理役が仕事を頼んだ。その伸びを基準に入れると区切れなくなる（評価役の指摘）
        base = self._auto_resume_file(
            "peer.jsonl",
            self._peer(NOW - 2000),
            _with_usage(_assistant(NOW - 1990), 138_000),
        )
        self.assertEqual(base, 72_003)

    def test_base_context_stops_at_turn_end(self):
        # 起床の予約の文など入力の形を取りこぼしても、ターンの終わりの記録で止める
        base = self._auto_resume_file(
            "turn.jsonl",
            {
                "type": "system",
                "subtype": "turn_duration",
                "timestamp": _iso(NOW - 3090),
            },
            {
                "type": "user",
                "isMeta": True,
                "timestamp": _iso(NOW - 2000),
                "message": {"content": "example-game-c の待ち（wake_at 04:10）が過ぎた"},
            },
            _with_usage(_assistant(NOW - 1990), 120_000),
        )
        self.assertEqual(base, 72_003)

    def test_base_context_open_growth_is_capped(self):
        # 「待って」に従わず最初のターンで仕事を始めた。4万を超えた伸びは基準に入れない
        base = self._auto_resume_file(
            "cap.jsonl",
            _with_usage(_assistant(NOW - 3000), 89_000),
            _with_usage(_assistant(NOW - 2900), 150_000),
            _with_usage(_assistant(NOW - 2800), 160_000),
        )
        self.assertEqual(base, 89_003)

    def test_restart_adds_only_the_jump_of_first_response(self):
        """起こし直しで足すのは最初の応答の跳ねた分だけ。前のプロセスでの仕事の伸びは残す。"""
        first, second = NOW - 4000, NOW - 1000
        self.append(
            *self._clear_records(
                NOW - 3900, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
            ),
            _with_usage(_assistant(NOW - 3897), 72_000),
            _human(NOW - 3800),
            _with_usage(_assistant(NOW - 3700), 150_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW, first)["base_context"], 72_003)
        # 同じ値の開始時刻のまま次の入力が来ても起こし直しではない
        self.append(_human(NOW - 3600), _with_usage(_assistant(NOW - 3590), 151_000))
        self.assertEqual(self.work.scan(self.path, NOW, first)["base_context"], 72_003)
        # 開始時刻を渡さない読み（running）を挟んでも替わり目とみなさない
        self.work.running(self.path, NOW)
        self.append(
            _human(second + 5),
            _with_usage(_assistant(second + 10), 171_000),
            _with_usage(_assistant(second + 20), 190_000),
        )
        # 151,003 → 171,003 の 2万だけ足す（その後の仕事の伸びは数える）
        self.assertEqual(self.work.scan(self.path, NOW, second)["base_context"], 92_003)
        # 次の入力では動かさない
        self.append(_human(second + 30), _with_usage(_assistant(second + 40), 175_000))
        self.assertEqual(self.work.scan(self.path, NOW, second)["base_context"], 92_003)

    def test_restart_big_jump_is_not_added(self):
        # 4万を超えて跳ねたなら起こし直しでは説明がつかない（仕事とみなす）
        self.append(
            _human(NOW - 3800),
            _with_usage(_assistant(NOW - 3700), 72_000),
            _human(NOW - 900),
            _with_usage(_assistant(NOW - 890), 112_001),
        )
        self.assertEqual(
            self.work.scan(self.path, NOW, NOW - 1000)["base_context"], 72_003
        )

    def test_restart_known_only_after_reading_is_ignored(self):
        # 開始時刻を知らずに読み進めた後で初めて分かったときは替わり目を決めない（従来どおり）
        self.append(
            _human(NOW - 3800),
            _with_usage(_assistant(NOW - 3700), 72_000),
        )
        self.assertEqual(self.work.scan(self.path, NOW)["base_context"], 72_003)
        self.append(_human(NOW - 900), _with_usage(_assistant(NOW - 890), 93_000))
        self.assertEqual(
            self.work.scan(self.path, NOW, NOW - 1000)["base_context"], 72_003
        )

    def test_two_processes_alternating_do_not_swallow_work(self):
        """同じ会話を2つのプロセスが動かし交互に読まれても、仕事の伸びを基準に吸い込まない（評価1周目の中1）。"""
        a, b = NOW - 4000, NOW - 3000
        self.append(_human(NOW - 3900), _with_usage(_assistant(NOW - 3890), 50_000))
        self.assertEqual(self.work.scan(self.path, NOW, a)["base_context"], 50_003)
        self.append(_human(NOW - 2900), _with_usage(_assistant(NOW - 2890), 51_000))
        self.assertEqual(self.work.scan(self.path, NOW, b)["base_context"], 51_003)
        for i, context in enumerate((65_000, 80_000, 95_000, 110_000)):
            self.append(
                _human(NOW - 2000 + i * 100),
                _with_usage(_assistant(NOW - 1990 + i * 100), context),
            )
            self.assertEqual(
                self.work.scan(self.path, NOW, a if i % 2 else b)["base_context"],
                51_003,
            )

    def test_restart_read_under_old_start_is_not_moved_later(self):
        """新しいプロセスの入力を古い行のまま読んだ後で開始時刻を知っても、後の本物の仕事を替わり目にしない（中2）。"""
        a, b = NOW - 4000, NOW - 3000
        self.append(_human(NOW - 3900), _with_usage(_assistant(NOW - 3890), 72_000))
        self.work.scan(self.path, NOW, a)
        self.append(_human(b + 3), _with_usage(_assistant(b + 10), 93_000))
        self.assertEqual(self.work.scan(self.path, NOW, a)["base_context"], 72_003)
        self.assertEqual(self.work.scan(self.path, NOW, b)["base_context"], 72_003)
        self.append(_human(NOW - 900), _with_usage(_assistant(NOW - 890), 120_000))
        self.assertEqual(self.work.scan(self.path, NOW, b)["base_context"], 72_003)

    def test_restart_without_jump_or_with_synthetic_first_response(self):
        # 跳ねが0以下なら足さない。合成の応答（使用量なし）は飛ばし、次の本物の応答で測る
        work = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, "nojump.jsonl")
        records = [
            _human(NOW - 3900),
            _with_usage(_assistant(NOW - 3890), 72_000),
            _human(NOW - 900),
            _with_usage(_assistant(NOW - 890), 71_000),
        ]
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.assertEqual(work.scan(path, NOW, NOW - 1000)["base_context"], 72_003)
        synthetic = _with_usage(_assistant(NOW - 895), 999)
        synthetic["message"]["model"] = "<synthetic>"
        records[3:3] = [synthetic]
        records[-1] = _with_usage(_assistant(NOW - 890), 90_000)
        work = ac.BackgroundWork()
        with open(path, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.assertEqual(work.scan(path, NOW, NOW - 1000)["base_context"], 90_003)

    def test_prompt_without_timestamp_is_not_restart(self):
        # 時刻の無い入力は替わり目にしない（読んだ時の時計で「今のプロセスの後」に見えるため）
        undated = _human(NOW)
        del undated["timestamp"]
        self.append(
            _human(NOW - 3900),
            _with_usage(_assistant(NOW - 3890), 72_000),
            undated,
            _with_usage(_assistant(NOW - 3000), 93_000),
        )
        self.assertEqual(
            self.work.scan(self.path, NOW, NOW - 3500)["base_context"], 72_003
        )
        # その後の時刻つきの入力も替わり目にしない（評価2周目の低1）
        self.append(_human(NOW - 2000), _with_usage(_assistant(NOW - 1990), 123_000))
        self.assertEqual(
            self.work.scan(self.path, NOW, NOW - 3500)["base_context"], 72_003
        )

    def test_process_start_does_not_change_first_turn_rules(self):
        # 会話がこのプロセスで始まった（ふつうの /clear の後）なら今までどおり最初のターンの終わり
        work = ac.BackgroundWork()
        path = os.path.join(self.tmp.name, "same.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for record in (
                *self._clear_records(
                    NOW - 3100, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
                ),
                _with_usage(_assistant(NOW - 3097), 50_000),
                _with_usage(_assistant(NOW - 3094), 72_000),
                _human(NOW - 2000),
                _with_usage(_assistant(NOW - 1990), 95_000),
            ):
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        base = work.scan(path, NOW, NOW - 5000)["base_context"]
        self.assertEqual(base, 72_003)

    def test_base_context_compact_after_auto_resume_does_not_reopen(self):
        base = self._auto_resume_file(
            "compact.jsonl",
            _compact(),
            _with_usage(_assistant(NOW - 2000), 40_000),
            _with_usage(_assistant(NOW - 1990), 90_000),
        )
        self.assertEqual(base, 40_003)

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
            # 予約の直近の結果（見回りが毎回読む）。ほかの試験の残りを持ち込まない
            mock.patch.object(sc, "_recent", []),
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

    def fresh(self, at):
        """at の5分前に応答して止まった sess-a（控えが生きていて、規則に当たる形）。"""
        self.transcript("sess-a", 150_000, _assistant(at - 300))
        return _session(status_updated_at=(at - 300) * 1000)

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

    def test_quota_stopped_session_is_excluded(self):
        """枠切れで止まった会話には区切りを頼まない（5.31節の評価で残した低・2026-11-02）。"""
        stopped = _session(quota_stop={"window": "five_hour", "recovered": False})
        self.assertEqual(self.auto.tick([stopped], ON, NOW), [])
        self.sender.assert_not_called()
        view = self.auto.assess(stopped, NOW)
        self.assertIn(
            "枠切れで止まっている（人か続きの依頼の再開待ち）", view["excluded"]
        )
        # 起こし直しも同じ理由で止める
        self.assertIn(
            "枠切れで止まっている（人か続きの依頼の再開待ち）",
            ac.relaunch_blockers(stopped, view, lambda sid: False),
        )
        # 印が消えれば（続きの依頼が会話記録に載った）今までどおり規則で決める
        hits = self.auto.tick([_session(quota_stop=None)], ON, NOW + 30)
        self.assertEqual([h["rule"] for h in hits], ["size"])

    def test_quota_stop_is_read_from_transcript_when_not_annotated(self):
        """見回りの一覧は quota_stop を付けないので、会話記録から読む。"""
        with mock.patch.object(
            ac.accounts, "quota_stop", return_value={"window": "weekly"}
        ) as read:
            self.assertEqual(self.auto.tick([_session()], ON, NOW), [])
        self.assertTrue(read.called)
        self.sender.assert_not_called()
        # 読めなければ止まっていない扱い（今までどおり規則で決める）
        with mock.patch.object(ac.accounts, "quota_stop", side_effect=OSError("x")):
            self.assertFalse(ac._quota_stopped({}, "C:\\nope.jsonl"))

    def test_resume_text_counts_as_human_for_quiet_rule(self):
        """続きの依頼の文は自動の文の頭で始まらない＝人の発言として10分の守りが効く（評価1周目の中）。"""
        import quota_resume

        text = quota_resume.resume_text(
            {"window_label": "5時間枠", "resets_at": "20:40"}
        )
        self.assertFalse(text.lstrip().startswith(ac.self_clear.AUTOMATED_PREFIXES))

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

    def _moved_transcript(self, session_id, started):
        """11-01 20:49:42 の example-daily（会話 5e6f7a8b）の形を写した会話記録。

        19:55 に自動の区切り → 引き継ぎを読んで 72,039（基準）→ ほかのセッションの連絡で 72,928 →
        19:58:56 に別のプロセス（started）が移しの文で起こし直し、その1往復だけで 93,734 に跳ねた。
        """
        turn_end = lambda epoch: {  # noqa: E731
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": _iso(epoch),
        }
        self.transcript(
            session_id,
            93_734,
            *TestBackgroundWork._clear_records(
                NOW - 3500, sc.AUTO_RESUME_TEXT.format(path="C:/w/NOW.md")
            ),
            _with_usage(_assistant(NOW - 3497), 49_114),
            _with_usage(_assistant(NOW - 3485), 72_036),
            turn_end(NOW - 3484),
            TestBackgroundWork._peer(NOW - 3360),
            _with_usage(_assistant(NOW - 3355), 72_925),
            turn_end(NOW - 3350),
            dict(
                _human(started + 3),
                message={
                    "content": "アカウントを account_a へ戻して、同じ会話を続きから起こし直しました（機械による移しで、"
                    "ユーザーの新しい依頼ではありません）。直前の作業を1行で…"
                },
            ),
            _with_usage(_assistant(NOW - 3024), 93_731),
            turn_end(NOW - 3023),
        )
        return _session(
            session_id=session_id,
            status_updated_at=(NOW - 3023) * 1000,
            started_at=int(started * 1000),
        )

    def test_restart_jump_is_not_growth(self):
        """別のプロセスでの起こし直しの1往復で跳ねた文脈は、伸びに数えない（2026-11-01 20:49:42 の再現）。"""
        session = self._moved_transcript("sess-moved", NOW - 3043)
        view = self.auto.assess(session, NOW)
        self.assertEqual(view["response_age_minutes"], 50.4)
        self.assertIsNone(view["rule"], view)
        # 基準は 72,039 に跳ねた分（93,734 − 72,928）を足した所
        self.assertEqual(view["base_context_tokens"], 72_039 + 20_806)
        self.assertEqual(self.auto.tick([session], ON, NOW), [])
        self.sender.assert_not_called()

    def test_restart_then_ordinary_work_is_hit(self):
        """起こし直しの後の仕事の伸びは今までどおり数え、区切る。"""
        session = self._moved_transcript("sess-moved", NOW - 3043)
        self.transcript(
            "sess-moved",
            118_000,
            _human(NOW - 3020),
            _with_usage(_assistant(NOW - 3010), 105_000),
            _with_usage(_assistant(NOW - 3005), 117_997),
        )
        session["status_updated_at"] = (NOW - 3005) * 1000
        view = self.auto.assess(session, NOW + 10)
        self.assertEqual(view["base_context_tokens"], 72_039 + 20_806)
        # 跳ねた後の基準から 2万以上伸びた（基準を上げても 10万を超える仕事は区切る）
        hits = self.auto.tick([session], ON, NOW + 10)
        self.assertEqual([h["rule"] for h in hits], ["size"])
        self.sender.assert_called_once()

    def test_restart_without_process_start_keeps_old_rule(self):
        """プロセスの開始時刻が分からなければ従来どおり（跳ねた分も伸びに数える）。"""
        session = self._moved_transcript("sess-moved", NOW - 3043)
        session["started_at"] = None
        view = self.auto.assess(session, NOW)
        self.assertEqual(view["base_context_tokens"], 72_039)
        self.assertEqual(view["rule"], "idle")

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

    def test_failure_is_retried_then_slowly(self):
        # 送れない相手へ毎分は送らないが、8日止めもしない（2026-10-28: no_input_area の会話が
        # 51.8万まで膨らんだ）。MAX_ATTEMPTS 回までは15分おき、その後は2時間おき
        self.sender.side_effect = sm.SessionError("入力欄が見えない")
        self.auto.tick([_session()], ON, NOW)
        self.auto.tick([_session()], ON, NOW + 60)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.logged()[-1]["outcome"], "send_failed")
        self.assertIn("15 分後から", self.logged()[-1]["detail"])
        self.assertEqual(sc.auto_expectations(), [])
        at = NOW
        counts = []
        for _ in range(ac.MAX_ATTEMPTS + 1):
            at += ac.FAIL_RETRY_SEC + 1
            # 再起動しても待ちと回数を引き継ぐ
            auto = self.make()
            session = self.fresh(at)
            self.assertIsNotNone(auto.assess(session, at, {}, set())["rule"])
            auto.tick([session], ON, at)
            counts.append(self.sender.call_count)
        self.assertEqual(counts, [2, 3, 3, 3])
        self.assertIn(
            "120 分後から、文脈が今より 5 万伸びて", self.logged()[-1]["detail"]
        )
        third = NOW + 2 * (ac.FAIL_RETRY_SEC + 1)
        at = third + ac.SLOW_RETRY_SEC + 1
        # 伸びていなければ送らない（区切らない理由があって止まっている子へ終わりなく届けない）
        auto = self.make()
        auto.tick([self.fresh(at)], ON, at)
        self.assertEqual(self.sender.call_count, 3)
        self.transcript("sess-a", 150_000 + ac.SLOW_RETRY_GROWTH_TOKENS - 1)
        auto.tick([_session(status_updated_at=(at - 300) * 1000)], ON, at + 1)
        self.assertEqual(self.sender.call_count, 3)
        self.transcript("sess-a", 150_000 + ac.SLOW_RETRY_GROWTH_TOKENS)
        auto.tick([_session(status_updated_at=(at - 300) * 1000)], ON, at + 2)
        self.assertEqual(self.sender.call_count, 4)

    def test_sent_without_result_is_retried_after_wait(self):
        # 予約の結果はメモリにしか無い。アプリを再起動して結果が消えても、送った後に
        # 予約の待ち＋送り直しの間を過ぎて同じ番号のまま当たる会話は、届かなかったとみなす
        self.auto.tick([_session()], ON, NOW)
        due = NOW + CONFIG["handoff_wait_minutes"] * 60 + ac.FAIL_RETRY_SEC
        # 再起動で予約待ちの印（メモリ）も消える
        sc._auto_expected.clear()
        auto = self.make()
        auto.tick([self.fresh(due - 60)], ON, due - 60)
        self.assertEqual(self.sender.call_count, 1)
        auto.tick([self.fresh(due + 1)], ON, due + 1)
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("送り直し（2 回目）", self.logged()[-1]["detail"])

    def test_record_failure_after_send_is_counted_once(self):
        # 送れた後の記録で落ちても、1回の送信を2回と数えない（届いた依頼をすぐ送り直さない）
        with mock.patch.object(send_history, "record", side_effect=OSError("disk")):
            self.auto.tick([_session()], ON, NOW)
        with open(os.path.join(self.tmp.name, "state.json"), encoding="utf-8") as f:
            entry = json.load(f)["sent"]["sess-a"]
        self.assertEqual(entry["attempts"], 1)
        self.assertNotIn("retry_at", entry)
        # 送れているので、予約待ちの印は残し、記録も送れた扱い
        self.assertEqual(len(sc.auto_expectations()), 1)
        self.assertEqual(self.logged()[-1]["outcome"], "sent")

    def test_failure_then_success_is_not_resent(self):
        self.sender.side_effect = [
            sm.SessionError("入力欄が見えない"),
            {"elapsed_sec": 1},
        ]
        self.auto.tick([_session()], ON, NOW)
        later = NOW + ac.FAIL_RETRY_SEC + 1
        self.auto.tick([self.fresh(later)], ON, later)
        self.assertEqual(self.logged()[-1]["outcome"], "sent")
        later += ac.FAIL_RETRY_SEC + 1
        self.auto.tick([self.fresh(later)], ON, later)
        self.assertEqual(self.sender.call_count, 2)


def _finished(outcome, at, session_id="sess-a", auto=True):
    """self_clear の予約が終わった記録（status()["recent"] の1件）。"""
    return {
        "pid": 4242,
        "name": "x",
        "session_id": session_id,
        "auto": auto,
        "outcome": outcome,
        "finished_at": datetime.datetime.fromtimestamp(at).isoformat(
            timespec="seconds"
        ),
    }


class TestRetryAfterReservation(_Base):
    """送れた依頼が区切りまで届かなかった会話の送り直し（2026-10-28 の 1a2b3c4d）。"""

    def later(self, sec):
        at = NOW + sec
        return at, self.fresh(at)

    def test_clear_refused_after_reservation_is_retried(self):
        # 予約の後の /clear が画面の読み違い（draft_present）で止まった。同じ番号のまま続くので送り直す
        self.auto.tick([_session()], ON, NOW)
        sc._recent.append(_finished("draft_present", NOW + 20))
        at, session = self.later(60)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.logged()[-1]["outcome"], "retry_scheduled")
        at, session = self.later(60 + ac.FAIL_RETRY_SEC + 1)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.logged()[-1]["outcome"], "sent")
        # 同じ記録を読み直しても、送り直した後の印を失敗に戻さない
        at, session = self.later(120 + 2 * ac.FAIL_RETRY_SEC)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 2)

    def test_cleared_reservation_is_not_retried(self):
        self.auto.tick([_session()], ON, NOW)
        sc._recent.append(_finished("done", NOW + 20))
        sc._recent.append(_finished("cleared_without_handoff", NOW + 30))
        sc._recent.append(_finished("failed", NOW + 40, auto=False))
        sc._recent.append(_finished("failed", NOW + 40, session_id="other"))
        at, session = self.later(ac.FAIL_RETRY_SEC + 100)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 1)
        self.assertNotIn("retry_scheduled", [e["outcome"] for e in self.logged()])

    def test_result_older_than_last_send_is_ignored(self):
        sc._recent.append(_finished("superseded", NOW - 600))
        self.auto.tick([_session()], ON, NOW)
        at, session = self.later(ac.FAIL_RETRY_SEC + 100)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 1)

    def test_no_reservation_is_retried(self):
        self.auto.tick([_session()], ON, NOW)
        wait = CONFIG["handoff_wait_minutes"] * 60
        self.auto.tick([], ON, NOW + wait + 1)
        self.assertIn("送り直す", self.logged()[-1]["detail"])
        at, session = self.later(wait + ac.FAIL_RETRY_SEC + 2)
        self.auto.tick([session], ON, at)
        self.assertEqual(self.sender.call_count, 2)

    def test_after_max_attempts_retries_slowly(self):
        self.auto.tick([_session()], ON, NOW)
        at = NOW
        for n in range(ac.MAX_ATTEMPTS):
            sc._recent.append(_finished("draft_present", at + 20))
            at += 60
            self.auto.tick([self.later(at - NOW)[1]], ON, at)
            at += ac.FAIL_RETRY_SEC + 1
            self.auto.tick([self.later(at - NOW)[1]], ON, at)
        self.assertEqual(self.sender.call_count, ac.MAX_ATTEMPTS)
        retried = [e for e in self.logged() if e["outcome"] == "retry_scheduled"]
        self.assertEqual(len(retried), ac.MAX_ATTEMPTS)
        self.assertIn("書きかけ", retried[-1]["detail"])
        self.assertIn("120 分後から", retried[-1]["detail"])

    def test_result_in_same_second_as_send_is_applied(self):
        # finished_at は秒で切り捨て。送った時刻（小数つき）より前の秒に丸められた結果も、その回の結果として当てる
        for shift in (0.0, 0.4):
            with self.subTest(shift=shift):
                sid = f"s{shift}"
                self.transcript(sid, 150_000, _assistant(NOW - 600))
                session = _session(pid=7, session_id=sid)
                self.auto.tick([session], ON, NOW + 0.5)
                sc._recent.append(
                    _finished("failed", NOW + 0.5 + shift, session_id=sid)
                )
                self.auto.tick([], ON, NOW + 30)
                with open(
                    os.path.join(self.tmp.name, "state.json"), encoding="utf-8"
                ) as f:
                    self.assertIn("retry_at", json.load(f)["sent"][sid])

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

    def test_background_is_cleared_by_size_with_note(self):
        """裏の仕事が走っていても size で区切りを頼み、文に裏の仕事の一文を足す（2026-11-03 管理役の承認）。

        裏の仕事を持った入力待ちは Claude Code の状態が shell になる（捨ての子で実測）。
        """
        self.transcript("sess-a", 150_000, _result({"backgroundTaskId": "b1"}))
        shell = _session(status="shell")
        view = self.auto.assess(shell, NOW)
        self.assertEqual(view["background"], 1)
        self.assertEqual(view["excluded"], [])
        hits = self.auto.tick([shell], ON, NOW)
        self.assertEqual([h["rule"] for h in hits], ["size"])
        text = self.sender.call_args.kwargs["text"]
        self.assertEqual(text, ac.trigger_text(self.auto.config["port"], background=1))
        self.assertIn("1件走っているとみています", text)
        # 起こし直し（プロセスを止める）は裏の仕事を切るので止める
        self.assertIn(
            "裏の仕事が走っている（1件）",
            ac.relaunch_blockers(
                _session(launched_by_app=False), view, lambda sid: False
            ),
        )

    def test_subagent_and_wakeup_are_cleared_by_size_with_note(self):
        """サブエージェント・起床の予約が走っていても外さず、size で区切りを頼む（2026-11-03 伝言 f4b3ddba8058）。

        どちらも /clear を越えて知らせ・起床が新しい会話へ届くと捨ての子で確かめた。
        """
        for pid, name, result in (
            (5001, "subagent", {"status": "async_launched", "agentId": "a1"}),
            (5002, "wakeup", {"scheduledFor": (NOW + 1200) * 1000}),
        ):
            with self.subTest(name):
                sid = f"sess-{name}"
                # 別の子（pid）にする。同じ子なら会話番号の替わり＝/clear として前の仕事を運ぶ
                child = dict(pid=pid, session_id=sid)
                self.transcript(sid, 150_000, _assistant(NOW - 600), _result(result))
                # 子の札は shell（サブエージェント）か idle（起床だけ）。どちらも入力待ち
                for status in ("shell", "idle"):
                    view = self.auto.assess(_session(status=status, **child), NOW)
                    self.assertEqual(view["background"], 1)
                    self.assertEqual(view["excluded"], [])
                    self.assertEqual(view["rule"], "size")
                # 前面で考え続けている（busy）間は送らない
                busy = self.auto.assess(_session(status="busy", **child), NOW)
                self.assertIsNone(busy["rule"])
                self.assertEqual(
                    self.auto.tick([_session(status="busy", **child)], ON, NOW),
                    [],
                )
                hits = self.auto.tick([_session(**child)], ON, NOW)
                self.assertEqual([h["rule"] for h in hits], ["size"])
                text = self.sender.call_args.kwargs["text"]
                self.assertIn("サブエージェント・起床の予約", text)
                self.assertIn("予定時刻", text)
                sc._pending.clear()

    def test_subagent_and_wakeup_do_not_trigger_idle_rule(self):
        """サブエージェント・起床の予約を待って止まっている子に idle の規則は当てない（仕事の途中）。"""
        at = NOW + 42 * 60
        for pid, name, result in (
            (5001, "subagent", {"status": "async_launched", "agentId": "a1"}),
            (5002, "wakeup", {"scheduledFor": (at + 1200) * 1000}),
        ):
            with self.subTest(name):
                sid = f"sess-{name}"
                session = _session(pid=pid, session_id=sid)
                self.transcript(sid, 80_000, _assistant(NOW - 600))
                # 陽性対照: 裏の仕事が無ければ idle に当たる
                self.assertEqual(self.auto.assess(session, at)["rule"], "idle")
                self.transcript(sid, 80_000, _result(result))
                view = self.auto.assess(session, at)
                self.assertEqual(view["background"], 1)
                self.assertIsNone(view["rule"])
                # 起こし直し（プロセスを止める）は裏の仕事を切るので止める
                self.assertIn(
                    "裏の仕事が走っている（1件）",
                    ac.relaunch_blockers(
                        _session(pid=pid, session_id=sid, launched_by_app=False),
                        view,
                        lambda s: False,
                    ),
                )

    def test_subagent_carries_over_clear_until_notified(self):
        """/clear の前に起こしたサブエージェントは新しい会話でも数え、新しい会話に届いた終わりの知らせで消す（評価1周目の中1）。"""
        self.transcript(
            "sess-a",
            150_000,
            _result({"status": "async_launched", "agentId": "a1"}),
        )
        self.auto.assess(_session(status="shell"), NOW)
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        new = _session(session_id="sess-new", status="shell")
        view = self.auto.assess(new, NOW + 60)
        self.assertEqual(view["background"], 1)
        self.assertEqual(view["rule"], "size")
        # 2回目の区切りの依頼にも裏の仕事の一文が付く
        hits = self.auto.tick([new], ON, NOW + 60)
        self.assertEqual([h["background"] for h in hits], [1])
        self.assertIn("1件走っているとみています", self.sender.call_args.kwargs["text"])
        # 終わりの知らせは新しい会話の記録に載る。それで運んだ番号も消える
        self.transcript(
            "sess-new",
            150_000,
            _notification(
                "<task-notification><task-id>a1</task-id><status>completed</status></task-notification>"
            ),
        )
        self.assertEqual(self.auto.assess(new, NOW + 120)["background"], 0)

    def test_end_in_old_conversation_just_before_clear_is_read(self):
        """最後の見回りから /clear までの間に前の会話へ載った終わりの知らせも読み、運ばない（評価2周目の中1）。"""
        self.transcript(
            "sess-a", 150_000, _result({"status": "async_launched", "agentId": "a1"})
        )
        self.assertEqual(self.auto.assess(_session(), NOW)["background"], 1)
        # 見回りの合間に前の会話で終わり、すぐ /clear
        self.transcript(
            "sess-a",
            150_000,
            _notification(
                "<task-notification><task-id>a1</task-id><status>completed</status></task-notification>"
            ),
        )
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        new = _session(session_id="sess-new")
        self.assertEqual(self.auto.assess(new, NOW + 60)["background"], 0)

    def test_grandparent_end_between_last_scan_and_clear(self):
        """二世代前から運んだ番号の終わりが、最後の見回りの後に前の会話へ載り、すぐ /clear しても運ばない（評価3周目の中1）。"""
        self.transcript(
            "sess-z", 150_000, _result({"status": "async_launched", "agentId": "a1"})
        )
        self.auto.assess(_session(session_id="sess-z"), NOW)
        self.transcript("sess-a2", 150_000, _assistant(NOW - 600))
        a = _session(session_id="sess-a2")
        self.assertEqual(self.auto.assess(a, NOW + 10)["background"], 1)
        self.transcript(
            "sess-a2",
            150_000,
            _notification(
                "<task-notification><task-id>a1</task-id><status>completed</status></task-notification>"
            ),
        )
        self.transcript("sess-b", 150_000, _assistant(NOW - 600))
        b = _session(session_id="sess-b")
        self.assertEqual(self.auto.assess(b, NOW + 20)["background"], 0)

    def test_carry_survives_one_missing_listing(self):
        """一覧から1回欠けただけでは運んだ番号を捨てず、長く欠けたら捨てる（評価3周目の中2・低1）。"""
        self.transcript(
            "sess-a", 150_000, _result({"status": "async_launched", "agentId": "a1"})
        )
        self.auto.assess(_session(status="shell"), NOW)
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        new = _session(session_id="sess-new", status="shell")
        self.assertEqual(self.auto.assess(new, NOW + 10)["background"], 1)
        # 1回の見回りで一覧から欠ける（状態ファイルの書きかけ等）
        self.auto.tick([], {"auto_clear_mode": "dry_run"}, NOW + 20)
        self.assertEqual(self.auto.assess(new, NOW + 30)["background"], 1)
        # 終わったプロセスは、欠けて CARRY_FORGET_SEC たったら捨てる
        self.auto.tick(
            [], {"auto_clear_mode": "dry_run"}, NOW + 30 + ac.CARRY_FORGET_SEC
        )
        self.assertEqual(self.auto._carry, {})

    def test_carried_ends_by_taskstop_but_not_by_other_ids(self):
        """運んだ番号は新しい会話の TaskStop で消え、別の番号の知らせでは消えない（評価2周目の低2）。"""
        self.transcript(
            "sess-a",
            150_000,
            _result({"backgroundTaskId": "b1"}),
            _result({"backgroundTaskId": "b2"}),
        )
        self.auto.assess(_session(), NOW)
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        new = _session(session_id="sess-new")
        self.assertEqual(self.auto.assess(new, NOW + 60)["background"], 2)
        # 陰性対照: 別の番号の終わりでは消えない
        self.transcript(
            "sess-new",
            150_000,
            _notification(
                "<task-notification><task-id>zz</task-id><status>completed</status></task-notification>"
            ),
        )
        self.assertEqual(self.auto.assess(new, NOW + 90)["background"], 2)
        self.transcript(
            "sess-new",
            150_000,
            _result({"message": "Successfully stopped task: b1 (clock)"}),
        )
        self.assertEqual(self.auto.assess(new, NOW + 120)["background"], 1)

    def test_work_of_a_dead_process_is_not_counted_or_carried(self):
        """落ちたプロセスが起こした裏の仕事は、同じ会話を起こし直したプロセスでは数えず、/clear の後へも運ばない（評価2周目の中2）。"""
        self.transcript(
            "sess-a", 150_000, _result({"backgroundTaskId": "b1"}, epoch=NOW - 900)
        )
        old = _session(started_at=(NOW - 3600) * 1000)
        self.assertEqual(self.auto.assess(old, NOW)["background"], 1)
        # 同じ会話を新しいプロセスが起こし直した（b1 は前のプロセスと一緒に消えた）
        revived = _session(pid=4343, started_at=(NOW - 300) * 1000)
        self.assertEqual(self.auto.assess(revived, NOW)["background"], 0)
        self.transcript("sess-new", 150_000, _assistant(NOW - 60))
        after = _session(pid=4343, session_id="sess-new", started_at=(NOW - 300) * 1000)
        self.assertEqual(self.auto.assess(after, NOW + 60)["background"], 0)
        # 陽性対照: 新しいプロセスが起こした仕事は数え、/clear の後へ運ぶ
        self.transcript(
            "sess-new", 150_000, _result({"backgroundTaskId": "b9"}, epoch=NOW - 30)
        )
        self.assertEqual(self.auto.assess(after, NOW + 90)["background"], 1)
        self.transcript("sess-3", 150_000, _assistant(NOW))
        third = _session(pid=4343, session_id="sess-3", started_at=(NOW - 300) * 1000)
        self.assertEqual(self.auto.assess(third, NOW + 120)["background"], 1)

    def test_wakeup_carries_over_clear(self):
        """/clear の前の会話で入れた起床の予約は、新しい会話でも走っているとみなし、文に足す。"""
        self.transcript(
            "sess-a", 150_000, _result({"scheduledFor": (NOW + 1200) * 1000})
        )
        self.auto.assess(_session(), NOW)
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        view = self.auto.assess(_session(session_id="sess-new"), NOW + 60)
        self.assertEqual(view["background"], 1)
        self.assertEqual(view["excluded"], [])
        # 起床の時刻（＋余裕）を過ぎれば数えない
        later = NOW + 1200 + ac.WAKEUP_GRACE_SEC + 1
        self.transcript("sess-new", 150_000, _assistant(later - 600))
        self.assertEqual(
            self.auto.assess(_session(session_id="sess-new"), later)["background"], 0
        )

    def test_shell_status_alone_blocks_idle_rule(self):
        """会話記録が裏の仕事を取りこぼしても、状態が shell なら idle の規則は当てない（評価役の低6）。"""
        at = NOW + 42 * 60
        self.transcript("sess-a", 80_000)
        self.assertEqual(self.auto.assess(_session(), at)["rule"], "idle")
        self.assertIsNone(self.auto.assess(_session(status="shell"), at)["rule"])

    def test_background_does_not_trigger_idle_rule(self):
        """裏の仕事を待って止まっている子に idle の規則は当てない（size に届かない文脈）。"""
        # 最後の応答（NOW - 600）から52分・文脈 8万（idle の閾値は超え、size には届かない）
        at = NOW + 42 * 60
        self.transcript("sess-a", 80_000)
        # 陽性対照: 裏の仕事が無ければ idle に当たる
        self.assertEqual(self.auto.assess(_session(), at)["rule"], "idle")
        self.transcript("sess-a", 80_000, _result({"backgroundTaskId": "b1"}))
        view = self.auto.assess(_session(status="shell"), at)
        self.assertEqual(view["background"], 1)
        self.assertIsNone(view["rule"])

    def test_cron_carries_over_clear(self):
        # /clear の前の会話で始めた定期の仕事は、新しい会話でも走っているとみなす
        self.transcript(
            "sess-a",
            150_000,
            _result({"id": "c1", "humanSchedule": "毎日", "recurring": True}),
        )
        self.auto.assess(_session(), NOW)
        self.transcript("sess-new", 150_000, _assistant(NOW - 600))
        view = self.auto.assess(_session(session_id="sess-new"), NOW + 60)
        self.assertEqual(view["background"], 1)
        # 走っていても外さず、size で当てる（裏の仕事は /clear の後も続く）
        self.assertEqual(view["excluded"], [])
        self.assertEqual(view["rule"], "size")


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
        # 題名は launch_session の既定（resume なら表示名を --remote-control で渡す）に任せる。外す "" を渡さない
        self.assertIsNone(kwargs.get("remote_control_name"))
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

    def test_relaunch_passes_chrome_on(self):
        """起こし直しでも、元の --chrome（chrome: True）を launch_session へ渡す。"""
        launch = mock.Mock(
            return_value={"pid": 99, "elevated": False, "win_session": 1}
        )
        self.relaunch(
            self.unmanaged(),
            launch=launch,
            options_of=lambda pid, dropped: {"chrome": True},
        )
        self.assertIs(launch.call_args[1].get("chrome"), True)
        # --no-chrome で起きた子は False のまま渡す（--no-chrome が付く）
        launch = mock.Mock(
            return_value={"pid": 100, "elevated": False, "win_session": 1}
        )
        self.relaunch(
            self.unmanaged(pid=4343, session_id="sess-b"),
            launch=launch,
            options_of=lambda pid, dropped: {"chrome": False},
        )
        self.assertIs(launch.call_args[1].get("chrome"), False)

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

    def test_chrome_is_carried(self):
        """--chrome は chrome: True・--no-chrome は chrome: False で持ち越し、捨てる側にも入れない。"""
        self.assertEqual(
            self._of(["claude.exe", "-n", "x", "--chrome", "--model", "haiku"]),
            ({"chrome": True, "model": "haiku"}, []),
        )
        # 後に書いたほうが勝つ。--no-chrome も持ち越す（設定の既定が真でも、起こし直しで黙って付けない）
        self.assertEqual(
            self._of(["claude.exe", "--chrome", "--no-chrome"]), ({"chrome": False}, [])
        )
        # 旗が無ければ chrome を返さない（設定に従わせる）
        self.assertEqual(self._of(["claude.exe", "-n", "x"]), ({}, []))
        self.assertEqual(
            self._of(["claude.exe", "--no-chrome", "--chrome"]), ({"chrome": True}, [])
        )
        # 最初の指示の中の --chrome は読まない
        self.assertEqual(self._of(["claude.exe", "--", "--chrome"]), ({}, []))

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

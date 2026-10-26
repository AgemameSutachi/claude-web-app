# -*- coding: utf-8 -*-
"""許可待ちの表示（attention）・今週の使用量（usage_tracker）・送った履歴（send_history）の試験。

本物の claude・会話記録・通知の記録には触らない。すべて一時フォルダに作った写しで試す。

実行方法:
    <python> -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import attention
import send_history
import session_manager as sm
import usage_tracker as ut

JST = timezone(timedelta(hours=9))


def _session(**kwargs):
    base = {
        "pid": 100,
        "session_id": "sid-1",
        "name": "demo",
        "cwd": "C:/work/demo",
        "status": "busy",
        "waiting_for": "",
        "status_updated_at": None,
        "started_at": 1000,
        "updated_at": 2000,
        "url": "https://claude.ai/code/session_x",
        "launched_by_app": True,
    }
    base.update(kwargs)
    return base


def _assistant(msg_id, ts, model="claude-opus-5-5", sidechain=False, **usage):
    record = {
        "type": "assistant",
        "timestamp": ts,
        "requestId": "req-" + msg_id,
        "message": {
            "id": msg_id,
            "model": model,
            "usage": {
                "input_tokens": usage.get("inp", 0),
                "output_tokens": usage.get("out", 0),
                "cache_creation_input_tokens": usage.get("cw", 0),
                "cache_read_input_tokens": usage.get("cr", 0),
            },
        },
    }
    if sidechain:
        record["isSidechain"] = True
    return json.dumps(record) + "\n"


class TestAttentionAssess(unittest.TestCase):
    """1つのセッションの判定（状態ファイルが主・通知の記録が控え）。"""

    def test_waiting_permission_is_certain(self):
        result = attention.assess(
            _session(
                status="waiting",
                waiting_for="permission prompt",
                status_updated_at=5000,
            )
        )
        self.assertEqual(result["kind"], "permission")
        self.assertEqual(result["label"], "許可待ち")
        self.assertTrue(result["certain"])
        self.assertEqual(result["since"], 5000)

    def test_waiting_input_and_unknown(self):
        self.assertEqual(
            attention.assess(_session(status="waiting", waiting_for="input needed"))[
                "kind"
            ],
            "input",
        )
        # 知らない待ちの種類も出す（出し漏れを避ける）
        unknown = attention.assess(
            _session(status="waiting", waiting_for="dialog:Something")
        )
        self.assertEqual(unknown["kind"], "other")
        self.assertTrue(unknown["certain"])

    def test_not_waiting_without_notification(self):
        self.assertIsNone(attention.assess(_session(status="busy")))
        self.assertIsNone(attention.assess(_session(status="idle")))

    def test_notification_without_later_activity_is_maybe(self):
        now = 1_000_000.0
        result = attention.assess(
            _session(status="busy", status_updated_at=(now - 100) * 1000),
            permission_notified_at=now - 60,
            answered_at=now - 61,
            now=now,
        )
        self.assertTrue(result["label"].startswith("許可待ちかも"))
        self.assertFalse(result["certain"])

    def test_notification_resolved_by_status_or_transcript(self):
        now = 1_000_000.0
        # 通知のあとに状態が変わった
        self.assertIsNone(
            attention.assess(
                _session(status="busy", status_updated_at=(now - 10) * 1000),
                permission_notified_at=now - 60,
                now=now,
            )
        )
        # 通知のあとに道具の結果が書かれた
        self.assertIsNone(
            attention.assess(
                _session(status="busy", status_updated_at=(now - 100) * 1000),
                permission_notified_at=now - 60,
                answered_at=now - 5,
                now=now,
            )
        )

    def test_last_tool_result_time_ignores_other_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "t.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "type": "user",
                            "timestamp": "2026-10-12T01:00:00Z",
                            "message": {"content": [{"type": "tool_result"}]},
                        }
                    )
                    + "\n"
                )
                # 道具の結果より後の、結果でない行（添付・応答）は答えとして数えない
                f.write(
                    json.dumps(
                        {"type": "attachment", "timestamp": "2026-10-12T01:05:00Z"}
                    )
                    + "\n"
                )
                f.write(_assistant("m9", "2026-10-12T01:06:00Z", out=1))
            self.assertEqual(
                attention.last_tool_result_time(path),
                datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc).timestamp(),
            )
            self.assertIsNone(
                attention.last_tool_result_time(os.path.join(tmp, "no.jsonl"))
            )
            self.assertIsNone(attention.last_tool_result_time(None))

    def test_old_notification_is_ignored(self):
        now = 1_000_000.0
        self.assertIsNone(
            attention.assess(
                _session(status="busy"),
                permission_notified_at=now - attention.NOTIFICATION_MAX_AGE_SEC - 1,
                now=now,
            )
        )


class TestNotificationLog(unittest.TestCase):
    """通知の記録の差分読み。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events-202610.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def _append(self, *records, partial=None):
        with open(self.path, "a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if partial:
                f.write(partial)

    def test_reads_only_permission_and_incrementally(self):
        log = attention.NotificationLog(self.tmp.name)
        self._append(
            {
                "ts": "2026-10-12T10:00:00+09:00",
                "event": "Notification",
                "session_id": "a",
                "message": "Claude is waiting for your input",
            },
            {
                "ts": "2026-10-12T10:01:00+09:00",
                "event": "Notification",
                "session_id": "a",
                "message": "Claude needs your permission to use Bash",
            },
            partial='{"ts": "2026-10-12T10:09:00+09:00", "event": "Notif',
        )
        log.refresh()
        expected = datetime(2026, 10, 12, 10, 1, tzinfo=JST).timestamp()
        self.assertEqual(log.last_permission("a"), expected)
        self.assertIsNone(log.last_permission("b"))

        # 書きかけの行の続きが書かれたら、次の refresh で読む
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(
                'ication", "session_id": "a", "message": "Claude needs your permission"}\n'
            )
        log.refresh()
        self.assertEqual(
            log.last_permission("a"),
            datetime(2026, 10, 12, 10, 9, tzinfo=JST).timestamp(),
        )

    def test_missing_dir_is_quiet(self):
        log = attention.NotificationLog(os.path.join(self.tmp.name, "nothing"))
        log.refresh()
        self.assertIsNone(log.last_permission("a"))


class TestAttentionAnnotate(unittest.TestCase):
    """一覧への印付けと並べ替え。"""

    def test_waiting_first_then_maybe_then_others(self):
        now = time.time()
        log = attention.NotificationLog("unused")
        log._last_permission = {"sid-maybe": now - 30}
        sessions = [
            _session(pid=1, session_id="sid-plain", status="idle"),
            _session(
                pid=2,
                session_id="sid-maybe",
                status="busy",
                status_updated_at=(now - 120) * 1000,
            ),
            _session(
                pid=3,
                session_id="sid-wait",
                status="waiting",
                waiting_for="permission prompt",
            ),
        ]
        with mock.patch.object(attention.NotificationLog, "refresh"):
            result = attention.annotate(sessions, log, lambda cwd, sid: None)
        self.assertEqual([s["pid"] for s in result], [3, 2, 1])
        self.assertTrue(result[0]["attention"]["certain"])
        self.assertFalse(result[1]["attention"]["certain"])
        self.assertIsNone(result[2]["attention"])


class TestWeekStart(unittest.TestCase):
    def test_boundaries(self):
        # 2026-10-11 は日曜
        sunday_before = datetime(2026, 10, 11, 19, 59, tzinfo=JST)
        sunday_after = datetime(2026, 10, 11, 20, 0, tzinfo=JST)
        saturday = datetime(2026, 10, 17, 23, 0, tzinfo=JST)
        self.assertEqual(
            ut.week_start(sunday_before), datetime(2026, 10, 4, 20, 0, tzinfo=JST)
        )
        self.assertEqual(ut.week_start(sunday_after), sunday_after)
        self.assertEqual(ut.week_start(saturday), sunday_after)
        # UTC で渡しても日本時間で区切る（2026-10-11 11:00 UTC = 20:00 JST）
        self.assertEqual(
            ut.week_start(datetime(2026, 10, 11, 11, 0, tzinfo=timezone.utc)),
            sunday_after,
        )


class TestCost(unittest.TestCase):
    def test_opus_5_5_price_and_dated_model(self):
        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "cache_creation_input_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
        }
        # 入力4 + 出力20 + 書込5分5 + 読出0.2
        self.assertAlmostEqual(ut.cost_of("claude-opus-5-5", usage), 29.2)
        self.assertAlmostEqual(
            ut.cost_of("claude-haiku-4-5-20251001", {"output_tokens": 1_000_000}), 5.0
        )
        self.assertIsNone(ut.cost_of("claude-unknown-9", usage))

    def test_one_hour_cache_write(self):
        usage = {
            "cache_creation_input_tokens": 1_000_000,
            "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
        }
        self.assertAlmostEqual(ut.cost_of("claude-opus-5-5", usage), 8.0)


class TestUsageTracker(unittest.TestCase):
    """会話記録の差分読み・重複の畳み・週の区切り・サブエージェントの付け先。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = os.path.join(self.tmp.name, "C--work-demo")
        os.makedirs(os.path.join(self.folder, "sid-1", "subagents"))
        self.main = os.path.join(self.folder, "sid-1.jsonl")
        self.sub = os.path.join(self.folder, "sid-1", "subagents", "agent-a.jsonl")
        self.other = os.path.join(self.folder, "sid-2.jsonl")
        # 週の始まりは 2026-10-11 20:00 JST = 11:00 UTC
        self.now = datetime(2026, 10, 12, 18, 0, tzinfo=JST).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, path, *lines):
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(lines)

    def test_counts_this_week_only_and_dedups(self):
        self._write(
            self.main,
            # 先週の分（数えない）
            _assistant("m0", "2026-10-11T10:59:00Z", out=1_000_000),
            # 今週の分。同じ応答が2行に写されている（1回だけ数える）
            _assistant("m1", "2026-10-11T11:00:00Z", out=1_000_000),
            _assistant("m1", "2026-10-11T11:00:00Z", out=1_000_000),
            json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n",
        )
        self._write(self.sub, _assistant("s1", "2026-10-12T01:00:00Z", out=500_000))
        self._write(self.other, _assistant("o1", "2026-10-12T02:00:00Z", inp=1_000_000))
        tracker = ut.UsageTracker(self.tmp.name)
        snap = tracker.refresh(now=self.now)
        # sid-1 = 本体 $20 + サブエージェント $10、sid-2 = 入力 $4
        self.assertAlmostEqual(snap["per_session"]["sid-1"]["cost"], 30.0)
        self.assertAlmostEqual(snap["per_session"]["sid-2"]["cost"], 4.0)
        self.assertAlmostEqual(snap["total_cost"], 34.0)

        # 追記した分だけを読む（前の分を二重に数えない）
        self._write(self.main, _assistant("m2", "2026-10-12T03:00:00Z", out=100_000))
        snap = tracker.refresh(now=self.now)
        self.assertAlmostEqual(snap["per_session"]["sid-1"]["cost"], 32.0)

    def test_partial_line_waits_for_completion(self):
        line = _assistant("m1", "2026-10-12T00:00:00Z", out=1_000_000)
        self._write(self.main, line[:40])
        tracker = ut.UsageTracker(self.tmp.name)
        self.assertEqual(
            tracker.refresh(now=self.now)["per_session"]["sid-1"]["cost"], 0.0
        )
        self._write(self.main, line[40:])
        self.assertAlmostEqual(
            tracker.refresh(now=self.now)["per_session"]["sid-1"]["cost"], 20.0
        )

    def test_unknown_model_is_reported(self):
        self._write(
            self.main, _assistant("m1", "2026-10-12T00:00:00Z", model="x-model", out=10)
        )
        snap = ut.UsageTracker(self.tmp.name).refresh(now=self.now)
        self.assertEqual(snap["unknown_models"], ["x-model"])
        self.assertEqual(snap["per_session"]["sid-1"]["tokens"], 10)

    def test_context_tokens_uses_last_main_chain_reply(self):
        self._write(
            self.main,
            _assistant("m1", "2026-10-12T00:00:00Z", inp=10, cw=100, cr=1000),
            _assistant("m2", "2026-10-12T00:01:00Z", inp=1, cw=2, cr=200_000),
            _assistant("m3", "2026-10-12T00:02:00Z", sidechain=True, cr=5),
        )
        tracker = ut.UsageTracker(self.tmp.name)
        self.assertEqual(tracker.context_tokens(self.main), 200_003)
        self.assertIsNone(tracker.context_tokens(self.other))


class TestLineageAndDescribe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "lineage.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_ids_survive_clear_and_restart(self):
        lineage = ut.SessionLineage(self.path)
        lineage.observe([_session(session_id="a")], now=100)
        lineage.observe([_session(session_id="b")], now=200)
        # 作り直しても（アプリの再起動）覚えている
        again = ut.SessionLineage(self.path)
        self.assertEqual(again.session_ids(_session(session_id="b")), ["a", "b"])
        # 別のプロセス（開始時刻が違う）には混ぜない
        self.assertEqual(
            again.session_ids(_session(session_id="c", started_at=9999)), ["c"]
        )

    def test_old_entries_are_pruned(self):
        lineage = ut.SessionLineage(self.path)
        lineage.observe([_session(pid=1, session_id="a")], now=0)
        lineage.observe(
            [_session(pid=2, session_id="b")], now=ut.SessionLineage.KEEP_SEC + 10
        )
        self.assertEqual(lineage.session_ids(_session(pid=1, session_id="z")), ["z"])

    def test_describe_marks_heavy_rows(self):
        tracker = ut.UsageTracker("unused")
        tracker._snapshot = {
            "week_start": "x",
            "per_session": {
                "a": {"cost": 6.0, "tokens": 6},
                "b": {"cost": 2.0, "tokens": 2},
            },
            "total_cost": 100.0,
            "total_tokens": 100,
            "unknown_models": [],
            "refreshed_at": 0,
        }
        lineage = ut.SessionLineage(self.path)
        lineage.observe([_session(session_id="a")], now=1)
        session = _session(session_id="b")
        with mock.patch.object(tracker, "context_tokens", return_value=160_000):
            result = ut.describe(session, tracker, lineage, "path")
        # /clear 前の a と今の b を足して 8%（5% 超）・文脈 16万（15万超）
        self.assertEqual(result["week_cost"], 8.0)
        self.assertEqual(result["week_share"], 8.0)
        self.assertTrue(result["warn_usage"])
        self.assertTrue(result["warn_context"])

        with mock.patch.object(tracker, "context_tokens", return_value=1000):
            light = ut.describe(
                _session(pid=5, session_id="b"), tracker, lineage, "path"
            )
        self.assertFalse(light["warn_usage"])
        self.assertFalse(light["warn_context"])

    def test_describe_before_first_refresh(self):
        tracker = ut.UsageTracker("unused")
        result = ut.describe(_session(), tracker, ut.SessionLineage(self.path), None)
        self.assertIsNone(result["week_cost"])
        self.assertIsNone(ut.summary(tracker))


class TestSendHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(
            send_history, "HISTORY_FILE", os.path.join(self.tmp.name, "h.jsonl")
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_masks_secrets_and_keeps_head(self):
        text = (
            "鍵は sk-ant-api03-abcdefghijklmnop と ghp_abcdefghijklmnopqrstuvwxyz012345、"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz password=hunter2pass です"
        )
        masked = send_history.mask_secrets(text)
        for secret in (
            "sk-ant-api03",
            "ghp_abc",
            "abcdefghijklmnopqrstuvwxyz ",
            "hunter2pass",
        ):
            self.assertNotIn(secret, masked)
        self.assertIn("Bearer [伏せ字]", masked)
        self.assertIn("password=[伏せ字]", masked)

        # 引用符つき・JSON・Basic・URL 埋め込みの形（評価役の指摘・ダミー値）
        for text, secret in (
            ('password="hunter2pass"', "hunter2pass"),
            ('{"api_key": "abcd1234efgh5678"}', "abcd1234efgh5678"),
            ("token: 'abcdef123456'", "abcdef123456"),
            ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA"),
            ("https://user:s3cretpass@example.com/x", "s3cretpass"),
            ("key sk-proj-AbCd_ef-1234567890abcdefXYZ", "AbCd_ef-1234567890"),
            ('password: "my secret pass phrase"', "secret pass phrase"),
            ("pass=hunter2xyz", "hunter2xyz"),
            ("mysql -u root -phunter2xyz db", "hunter2xyz"),
            ("パスワード：hunter2xyz", "hunter2xyz"),
            ("client_secret=abc,def123456", "def123456"),
        ):
            masked = send_history.mask_secrets(text)
            self.assertNotIn(secret, masked, text)
            self.assertIn("[伏せ字]", masked, text)
        self.assertEqual(
            send_history.mask_secrets("https://user:s3cretpass@example.com/x"),
            "https://user:[伏せ字]@example.com/x",
        )
        # 数字の無い Basic の値・secret_key も伏せる
        for text, secret in (
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("secret_key: abcdEFGH", "abcdEFGH"),
            # キャメルケースのキー・環境変数の前置き（評価役の指摘・高）
            ("apiToken=hunter2xyz", "hunter2xyz"),
            ('{"accessToken":"hunter2xyz"}', "hunter2xyz"),
            ("PGPASSWORD=hunter2xyz psql -h db", "hunter2xyz"),
            ("dbPassword: hunter2xyz", "hunter2xyz"),
            ("clientSecret: hunter2xyz", "hunter2xyz"),
            ("adminPass=hunter2xyz", "hunter2xyz"),
            # 「パスワードは …」の書き方
            ("パスワードは hunter2xyz です", "hunter2xyz"),
            ("パスワードは「hunter2xyz」です", "hunter2xyz"),
            # JSON の形の Basic・引数で渡す形・全角の＝
            ('"Authorization": "Basic dXNlcjpwYXNz"', "dXNlcjpwYXNz"),
            ("mytool --password hunter2xyz", "hunter2xyz"),
            ("curl -u admin:hunter2xyz https://x", "hunter2xyz"),
            ("passphrase=hunter2xyz", "hunter2xyz"),
            ("private_key=hunter2xyz", "hunter2xyz"),
            ("password＝hunter2xyz", "hunter2xyz"),
            ('mytool --password "hunter 2xyz"', "hunter 2xyz"),
            ("mytool --db-password hunter2xyz", "hunter2xyz"),
            ("curl --user=admin:hunter2xyz https://x", "hunter2xyz"),
            ("curl -uadmin:hunter2xyz https://x", "hunter2xyz"),
            ("userPwd=hunter2xyz", "hunter2xyz"),
        ):
            self.assertNotIn(secret, send_history.mask_secrets(text), text)
        # 日本語の指示文・英字の続く語は壊さない（評価役が実測した伏せすぎの形）
        for text in (
            "パスワード：を入力する画面を作って、ログイン後にトップへ戻す",
            "password: の欄を作る",
            "token=の値を表示する画面",
            "設定の token: 欄は空でよい。次に README を直して",
            "bypass: true を設定",
            "pass の数を数えて",
            "compass: north",
            "surpass: 10 を超える",
            "パスワードは8文字以上にして",
            "パスワードは画面で入力させる",
            "--password オプションを追加して",
            "--password で渡すのをやめて",
            "パスワードは「再設定」ボタンから変えられるようにして",
            "token: 「アクセス トークン」と表示",
        ):
            self.assertEqual(send_history.mask_secrets(text), text)
        # 英語の文としての basic は伏せない（伏せすぎの誤検知）
        self.assertEqual(
            send_history.mask_secrets("basic authentication の設定"),
            "basic authentication の設定",
        )

        head = send_history.text_head("あ" * 300 + "\n改行")
        self.assertEqual(len(head), send_history.TEXT_HEAD_CHARS + 1)
        self.assertTrue(head.endswith("…"))
        self.assertEqual(send_history.text_head("一\n二"), "一 二")

    def test_kind(self):
        self.assertEqual(send_history.send_kind(True, "x"), "clear_text")
        self.assertEqual(send_history.send_kind(True, " "), "clear")
        self.assertEqual(send_history.send_kind(False, "x"), "text")

    def test_record_and_list_newest_first_with_filters(self):
        send_history.record(
            "launch", True, target={"pid": 1, "name": "alpha"}, text="作って"
        )
        send_history.record(
            "text",
            False,
            target={"pid": 2, "name": "beta", "managed": False},
            text="続けて",
            error="busy です",
            code="busy",
        )
        send_history.record(
            "broadcast", None, target=["alpha", "beta"], text="保存して"
        )
        items = send_history.list_entries()
        self.assertEqual([i["kind"] for i in items], ["broadcast", "text", "launch"])
        self.assertEqual(
            [i["kind"] for i in send_history.list_entries(kind="text")], ["text"]
        )
        self.assertEqual(
            [i["kind"] for i in send_history.list_entries(query="ALPHA")],
            ["broadcast", "launch"],
        )
        self.assertEqual(len(send_history.list_entries(query="busy")), 1)
        self.assertEqual(len(send_history.list_entries(limit=1)), 1)

    def test_rotation_keeps_previous_generation(self):
        with mock.patch.object(send_history, "MAX_FILE_BYTES", 200):
            for i in range(10):
                send_history.record("text", True, target={"pid": i}, text="x" * 50)
        self.assertTrue(os.path.exists(send_history.HISTORY_FILE + ".1"))
        items = send_history.list_entries(limit=100)
        # いまのファイルと1つ前の世代を合わせて新しい順
        self.assertEqual(items[0]["target"]["pid"], 9)
        self.assertGreater(len(items), 1)


class TestEndpoints(unittest.TestCase):
    """本体への組み込み（送った履歴の記録・履歴の口・一覧の並び）。"""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "claude_web_app_for_insights",
            os.path.join(PROJECT_ROOT, "claude-web-app.py"),
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.client = cls.module.app.test_client()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(
                send_history, "HISTORY_FILE", os.path.join(self.tmp.name, "h.jsonl")
            ),
            mock.patch.object(
                self.module,
                "lineage",
                ut.SessionLineage(os.path.join(self.tmp.name, "lineage.json")),
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_send_is_recorded_with_target_before_clear(self):
        before = _session(pid=42, session_id="old", launched_by_app=False)
        result = {
            "cleared": True,
            "text_sent": True,
            "session_id_before": "old",
            "session_id_after": "new",
            "elapsed_sec": 1.0,
        }
        with mock.patch.object(
            sm, "find_session_by_pid", return_value=before
        ), mock.patch.object(sm, "send_to_session", return_value=result):
            response = self.client.post(
                "/api/sessions/42/send",
                json={
                    "clear": True,
                    "text": "次の作業 token=abcdef123456",
                    "allow_unmanaged": True,
                },
                headers={"X-Claude-Web-App-Screen": "1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_history_target", response.get_json())
        [item] = send_history.list_entries()
        self.assertEqual(item["kind"], "clear_text")
        self.assertTrue(item["ok"])
        self.assertEqual(item["source"], "screen")
        self.assertEqual(item["target"]["session_id"], "old")
        self.assertFalse(item["target"]["managed"])
        self.assertTrue(item["detail"]["allow_unmanaged"])
        self.assertIn("token=[伏せ字]", item["text"])

    def test_refused_send_is_recorded_and_bad_body_is_not(self):
        with mock.patch.object(
            sm, "find_session_by_pid", return_value=None
        ), mock.patch.object(
            sm, "send_to_session", side_effect=sm.SessionNotManagedError("台帳に無い")
        ):
            response = self.client.post("/api/sessions/7/send", json={"text": "やあ"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/sessions/7/send", json={"text": 3})
        self.assertEqual(response.status_code, 400)
        [item] = send_history.list_entries()
        self.assertFalse(item["ok"])
        self.assertEqual(item["code"], "not_managed")
        self.assertEqual(item["source"], "api")
        self.assertEqual(item["target"], {"pid": 7})

    def test_launch_failure_is_recorded(self):
        response = self.client.post(
            "/api/sessions",
            json={"project": "developing/__no_such_project__", "prompt": "やって"},
        )
        self.assertEqual(response.status_code, 404)
        [item] = send_history.list_entries()
        self.assertEqual(item["kind"], "launch")
        self.assertFalse(item["ok"])
        self.assertEqual(item["code"], "project_missing")
        # 形の不備は残さず、応答の code は従来どおり failed
        response = self.client.post("/api/sessions", json={"project": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "failed")
        self.assertEqual(len(send_history.list_entries()), 1)

    def test_broadcast_partial_failure_is_not_ok(self):
        with mock.patch.object(self.module.socketio, "emit"):
            self.module._notify_broadcast_done(
                {
                    "id": "j1",
                    "status": "done",
                    "targets": ["a", "b"],
                    "message": "m",
                    "results": {"a": "SENT", "b": "FAILED 見つからない"},
                }
            )
            self.module._notify_broadcast_done(
                {
                    "id": "j2",
                    "status": "done",
                    "targets": ["a"],
                    "message": "m",
                    "results": {"a": "SENT"},
                }
            )
        items = send_history.list_entries()
        self.assertTrue(items[0]["ok"])
        self.assertFalse(items[1]["ok"])
        self.assertEqual(
            [i["detail"]["job"] for i in send_history.list_entries(failed_only=True)],
            ["j1"],
        )
        data = self.client.get("/api/history?result=failed").get_json()
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(self.client.get("/api/history?limit=x").status_code, 400)

    def test_history_api_and_page(self):
        send_history.record("clear", True, target={"pid": 1, "name": "n"})
        data = self.client.get("/api/history?kind=clear").get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(self.client.get("/api/history?kind=bogus").status_code, 400)
        page = self.client.get("/history")
        self.assertEqual(page.status_code, 200)
        self.assertIn("送った履歴", page.get_data(as_text=True))

    def test_payload_puts_waiting_first_and_adds_usage(self):
        sessions = [
            _session(pid=1, session_id="a", status="idle"),
            _session(
                pid=2, session_id="b", status="waiting", waiting_for="permission prompt"
            ),
        ]
        with mock.patch.object(
            sm, "list_sessions", return_value=sessions
        ), mock.patch.object(self.module.notification_log, "refresh"):
            payload = self.module.build_sessions_payload()
        self.assertEqual([s["pid"] for s in payload["sessions"]], [2, 1])
        self.assertEqual(payload["sessions"][0]["attention"]["label"], "許可待ち")
        self.assertIn("usage", payload["sessions"][0])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""週の枠で止まった子を ntfy で知らせる見回り（quota_alert.py）の試験。"""

import http.server
import json
import os
import threading
import unittest

import pytest

import liaison
import quota_alert as qa

NOW = 1_793_600_000.0


def _session(pid, name, account="account_b", session_id=None, **extra):
    session = {
        "pid": pid,
        "name": name,
        "cwd": f"F:/w/{name}",
        "session_id": session_id or f"sess-{pid}",
        "account": account,
        "kind": "interactive",
        "status": "idle",
    }
    session.update(extra)
    return session


def _stop(window="weekly", resets=NOW + 4 * 86400, recovered=False, at="t1"):
    return {
        "window": window,
        "window_label": "週の枠" if window == "weekly" else "5時間枠",
        "resets_at_epoch": resets,
        "resets_at": "11/07 10:00",
        "at": at,
        "recovered": recovered,
    }


CONFIG = {
    "server": "http://127.0.0.1:1",
    "topic": "t",
    "token": "",
    "fallback_server": "",
    "fallback_token": "",
    "timeout": 1,
}


class QuotaAlertTickTest(unittest.TestCase):
    def setUp(self):
        self.stops = {}
        self.posts = []
        self.fail = 0
        self.config = dict(CONFIG)

    def make(self, state_file):
        def poster(config, title, message):
            if self.fail:
                self.fail -= 1
                raise RuntimeError("届かない")
            self.posts.append((title, message))
            return config["server"]

        self.told = []
        return qa.QuotaAlert(
            lambda cwd, sid: sid,
            quota_stop=lambda path, now: self.stops.get(path),
            poster=poster,
            config_loader=lambda: self.config,
            state_file=state_file,
            notifier=lambda kind, key, text: self.told.append((kind, key, text)),
        )

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.state = str(tmp_path / "quota_alert.json")

    def test_weekly_stop_alerts_once_per_account(self):
        alert = self.make(self.state)
        a, b = _session(1, "alpha"), _session(2, "beta")
        self.stops = {"sess-1": _stop(), "sess-2": _stop()}
        self.assertEqual(alert.tick([a, b], {}, NOW), 1)
        title, message = self.posts[0]
        self.assertEqual(title, "account_b が週の枠で止まった")
        self.assertIn("alpha（pid 1）", message)
        self.assertIn("beta（pid 2）", message)
        self.assertIn("11/07 10:00", message)
        # 同じ停止では鳴らさない（会話記録に枠切れの行が増えても・後から別の子が止まっても）
        self.stops["sess-1"] = _stop(at="t2")
        c = _session(3, "gamma")
        self.stops["sess-3"] = _stop()
        self.assertEqual(alert.tick([a, b, c], {}, NOW + 60), 0)
        self.assertEqual(len(self.posts), 1)
        record = alert.status()["alerts"][f"account_b|{NOW + 4 * 86400}"]
        self.assertEqual(record["status"], "sent")
        self.assertEqual(len(record["children"]), 3)
        # 再起動しても鳴らし直さない
        again = self.make(self.state)
        self.assertEqual(again.tick([a, b, c], {}, NOW + 120), 0)
        self.assertEqual(len(self.posts), 1)

    def test_five_hour_and_recovered_do_not_alert(self):
        alert = self.make(self.state)
        a, b = _session(1, "alpha"), _session(2, "beta", account="")
        self.stops = {
            "sess-1": _stop(window="five_hour", resets=NOW + 3600),
            "sess-2": _stop(recovered=True, resets=NOW - 60),
        }
        self.assertEqual(alert.tick([a, b], {}, NOW), 0)
        self.assertEqual(self.posts, [])
        self.assertEqual(alert.status()["alerts"], {})

    def test_other_account_or_new_reset_alerts_again(self):
        alert = self.make(self.state)
        a, b = _session(1, "alpha"), _session(2, "beta", account="")
        self.stops = {"sess-1": _stop(), "sess-2": _stop()}
        self.assertEqual(alert.tick([a, b], {}, NOW), 2)
        self.assertEqual(
            sorted(t for t, _ in self.posts),
            ["account_b が週の枠で止まった", "本家 が週の枠で止まった"],
        )
        # 次の週の停止（戻りの時刻が違う）は別の停止
        self.stops = {"sess-1": _stop(resets=NOW + 11 * 86400)}
        self.assertEqual(alert.tick([a], {}, NOW + 7 * 86400), 1)

    def test_retry_then_give_up(self):
        alert = self.make(self.state)
        a = _session(1, "alpha")
        self.stops = {"sess-1": _stop()}
        self.fail = 5
        self.assertEqual(alert.tick([a], {}, NOW), 0)
        # 送り直しの間（5分×回数）の前は試さない
        self.assertEqual(alert.tick([a], {}, NOW + 60), 0)
        self.assertEqual(self.fail, 4)
        self.assertEqual(alert.tick([a], {}, NOW + qa.RETRY_SEC + 1), 0)
        self.assertEqual(alert.tick([a], {}, NOW + 4 * qa.RETRY_SEC), 0)
        record = next(iter(alert.status()["alerts"].values()))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], qa.MAX_ATTEMPTS)
        self.assertIn("届かない", record["error"])
        # 諦めたことは管理役へ1件だけ知らせる（送り直しのたびには知らせない）
        self.assertEqual([t[0] for t in self.told], ["quota_alert_failed"])
        self.assertIn("3 回送れず諦めた", self.told[0][2])
        # 諦めた後は試さない
        self.fail = 0
        self.assertEqual(alert.tick([a], {}, NOW + 10 * qa.RETRY_SEC), 0)
        self.assertEqual(self.posts, [])

    def test_retry_succeeds(self):
        alert = self.make(self.state)
        a = _session(1, "alpha")
        self.stops = {"sess-1": _stop()}
        self.fail = 1
        self.assertEqual(alert.tick([a], {}, NOW), 0)
        self.assertEqual(alert.tick([a], {}, NOW + qa.RETRY_SEC + 1), 1)

    def test_no_config_waits_until_config_appears(self):
        alert = self.make(self.state)
        a = _session(1, "alpha")
        self.stops = {"sess-1": _stop()}
        self.config = None
        self.assertEqual(alert.tick([a], {}, NOW), 0)
        record = next(iter(alert.status()["alerts"].values()))
        self.assertEqual(record["status"], "no_config")
        self.assertEqual(record["attempts"], 0)
        # 鳴らせないことを管理役へ1件だけ知らせる（読み直すたびには知らせない）
        self.assertEqual(alert.tick([a], {}, NOW + qa.RETRY_SEC + 1), 0)
        self.assertEqual(len(self.told), 1)
        self.assertIn("config/ntfy.ini", self.told[0][2])
        # 設定が置かれたら、まだ戻っていない停止を鳴らす
        self.config = dict(CONFIG)
        self.assertEqual(alert.tick([a], {}, NOW + 2 * qa.RETRY_SEC + 2), 1)

    def test_no_config_then_gave_up_both_reach_liaison(self):
        # 本物の Liaison とつなぐ。設定なしの後に設定が置かれ、送れずに諦めたことも管理役へ届く（評価2周目の中1）
        settings = {"liaison": {"tool": ""}}
        notices = liaison.Liaison(
            state_file=self.state + ".liaison", settings_loader=lambda: settings
        )
        alert = self.make(self.state)
        alert.notifier = lambda kind, key, text: notices.notify(kind, key, text)
        a = _session(1, "alpha")
        self.stops = {"sess-1": _stop()}
        self.config = None
        # 管理役への知らせが無効の間は溜まらず、印も付かない（低1）
        self.assertEqual(alert.tick([a], {}, NOW), 0)
        self.assertEqual(notices.status()["pending"], [])
        settings["liaison"]["tool"] = "C:/x/claude_send.py"
        self.assertEqual(alert.tick([a], {}, NOW + qa.RETRY_SEC + 1), 0)
        self.assertEqual(len(notices.status()["pending"]), 1)
        # 設定が置かれたが送れない → 3回で諦める
        self.config = dict(CONFIG)
        self.fail = 9
        t = NOW + 2 * qa.RETRY_SEC + 2
        for _ in range(qa.MAX_ATTEMPTS):
            alert.tick([a], {}, t)
            record = next(iter(alert.status()["alerts"].values()))
            self.assertNotEqual(record["status"], "no_config")
            t += qa.RETRY_SEC * qa.MAX_ATTEMPTS + 1
        self.assertEqual(record["status"], "failed")
        texts = [p["text"] for p in notices.status()["pending"]]
        self.assertEqual(len(texts), 2)
        self.assertIn("config/ntfy.ini", texts[0])
        self.assertIn("送れず諦めた", texts[1])
        # 以後は知らせない
        alert.tick([a], {}, t + 3600)
        self.assertEqual(len(notices.status()["pending"]), 2)

    def test_gave_up_told_later_when_liaison_was_off(self):
        settings = {"liaison": {"tool": ""}}
        notices = liaison.Liaison(
            state_file=self.state + ".liaison", settings_loader=lambda: settings
        )
        alert = self.make(self.state)
        alert.notifier = lambda kind, key, text: notices.notify(kind, key, text)
        a = _session(1, "alpha")
        self.stops = {"sess-1": _stop()}
        self.fail = 9
        t = NOW
        for _ in range(qa.MAX_ATTEMPTS):
            alert.tick([a], {}, t)
            t += qa.RETRY_SEC * qa.MAX_ATTEMPTS + 1
        self.assertEqual(notices.status()["pending"], [])
        settings["liaison"]["tool"] = "C:/x/claude_send.py"
        alert.tick([a], {}, t)
        self.assertEqual(len(notices.status()["pending"]), 1)
        self.assertEqual(self.posts, [])

    def test_mode_off_and_skipped_sessions(self):
        alert = self.make(self.state)
        self.stops = {"sess-1": _stop(), "sess-2": _stop(), "sess-3": _stop()}
        sessions = [
            _session(1, "alpha"),
            _session(2, "beta", kind="print"),
            dict(_session(3, "gamma"), session_id=None),
        ]
        self.assertEqual(alert.tick(sessions, {"quota_alert_mode": "off"}, NOW), 0)
        self.assertEqual(alert.status()["mode"], "off")
        self.assertEqual(alert.tick(sessions, {"quota_alert_mode": "x"}, NOW), 1)
        self.assertIn("alpha", self.posts[0][1])
        self.assertNotIn("beta", self.posts[0][1])

    def test_many_children_are_summarized(self):
        alert = self.make(self.state)
        sessions = [_session(i, f"c{i}") for i in range(1, 14)]
        self.stops = {f"sess-{i}": _stop() for i in range(1, 14)}
        self.assertEqual(alert.tick(sessions, {}, NOW), 1)
        self.assertIn("ほか 3 本", self.posts[0][1])

    def test_unreadable_transcript_is_skipped(self):
        def broken(path, now):
            raise OSError("読めない")

        alert = qa.QuotaAlert(
            lambda cwd, sid: sid,
            quota_stop=broken,
            poster=lambda *a: "x",
            config_loader=lambda: CONFIG,
            state_file=self.state,
        )
        self.assertEqual(alert.tick([_session(1, "alpha")], {}, NOW), 0)


class _Recorder(http.server.BaseHTTPRequestHandler):
    """受けた POST を覚える偽の ntfy。status を返す。"""

    received = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        type(self).received.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body.decode("utf-8")),
            }
        )
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def _serve(status):
    handler = type("Handler", (_Recorder,), {"received": [], "status": status})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


class PostToNtfyTest(unittest.TestCase):
    def test_json_japanese_token_and_user_agent(self):
        server, handler = _serve(200)
        try:
            config = dict(
                CONFIG,
                server=f"http://127.0.0.1:{server.server_port}",
                topic="test-topic",
                token="tk",
            )
            used = qa.post_to_ntfy(config, "account_b が週の枠で止まった", "本文・日本語")
        finally:
            server.shutdown()
        self.assertEqual(used, config["server"])
        got = handler.received[0]
        self.assertEqual(got["path"], "/")
        self.assertEqual(
            got["body"],
            {
                "topic": "test-topic",
                "title": "account_b が週の枠で止まった",
                "message": "本文・日本語",
                "priority": 3,
                "tags": ["warning"],
            },
        )
        self.assertEqual(got["headers"]["Authorization"], "Bearer tk")
        self.assertEqual(got["headers"]["User-Agent"], qa.USER_AGENT)
        self.assertIn("application/json", got["headers"]["Content-Type"])

    def test_fallback_when_primary_fails(self):
        bad, _ = _serve(500)
        good, handler = _serve(200)
        try:
            config = dict(
                CONFIG,
                server=f"http://127.0.0.1:{bad.server_port}",
                fallback_server=f"http://127.0.0.1:{good.server_port}",
                fallback_token="fb",
            )
            used = qa.post_to_ntfy(config, "題", "本文")
        finally:
            bad.shutdown()
            good.shutdown()
        self.assertEqual(used, config["fallback_server"])
        self.assertEqual(handler.received[0]["headers"]["Authorization"], "Bearer fb")

    def test_malformed_server_falls_back(self):
        # scheme の無い書き間違い（ValueError）も予備へ回す
        good, handler = _serve(200)
        try:
            config = dict(
                CONFIG,
                server="198.51.100.12:30125",
                fallback_server=f"http://127.0.0.1:{good.server_port}",
            )
            used = qa.post_to_ntfy(config, "題", "本文")
        finally:
            good.shutdown()
        self.assertEqual(used, config["fallback_server"])
        self.assertEqual(len(handler.received), 1)

    def test_no_fallback_raises(self):
        bad, _ = _serve(403)
        try:
            config = dict(CONFIG, server=f"http://127.0.0.1:{bad.server_port}")
            with self.assertRaises(RuntimeError):
                qa.post_to_ntfy(config, "題", "本文")
        finally:
            bad.shutdown()


class LoadConfigTest(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.dir = tmp_path

    def test_reads_ini_with_bom(self):
        path = self.dir / "ntfy.ini"
        path.write_text(
            "[DEFAULT]\nNTFY_SERVER = http://h:1\nNTFY_TOPIC = top\nNTFY_TOKEN = tk\n"
            "NTFY_FALLBACK_SERVER = https://ntfy.sh\nNTFY_TIMEOUT_SEC = 7\n",
            encoding="utf-8-sig",
        )
        config = qa.load_config(str(path))
        self.assertEqual(config["server"], "http://h:1")
        self.assertEqual(config["topic"], "top")
        self.assertEqual(config["token"], "tk")
        self.assertEqual(config["fallback_server"], "https://ntfy.sh")
        self.assertEqual(config["timeout"], 7)

    def test_missing_or_empty_topic(self):
        self.assertIsNone(qa.load_config(str(self.dir / "none.ini")))
        path = self.dir / "empty.ini"
        path.write_text("[DEFAULT]\nNTFY_SERVER = http://h:1\n", encoding="utf-8")
        self.assertIsNone(qa.load_config(str(path)))

    def test_tests_never_read_the_real_config(self):
        # conftest が CONFIG_FILE を一時の置き場へ向けている（本物のトピックへ鳴らさない）
        self.assertNotEqual(
            os.path.normcase(qa.CONFIG_FILE),
            os.path.normcase(
                os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config",
                    "ntfy.ini",
                )
            ),
        )
        self.assertIsNone(qa.load_config())

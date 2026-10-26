# -*- coding: utf-8 -*-
"""別の機械で動かすための設定と入口の守りの試験。

access_guard（接続元の絞り込み・合言葉）、本体の before_request と Socket.IO の接続の確かめ、
置き場・機械名・Origin の設定、一斉指示の定型文、配る束（make_bundle）を見る。

実行方法:
    <python> -m unittest discover -s tests -v
"""

import importlib.util
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import access_guard
import broadcaster
import make_bundle

TOKEN = "correct-horse-battery-staple"


def load_app_module():
    """ファイル名にハイフンを含むメインモジュールを動的に読み込む。"""
    module_path = os.path.join(PROJECT_ROOT, "claude-web-app.py")
    spec = importlib.util.spec_from_file_location("claude_web_app", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAccessGuard(unittest.TestCase):
    def test_loopback(self):
        for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "127.5.6.7"):
            self.assertTrue(access_guard.is_loopback(addr), addr)
        for addr in ("100.64.1.2", "0.0.0.0", "", None, "localhost", "garbage"):
            self.assertFalse(access_guard.is_loopback(addr), addr)

    def test_empty_guard_is_inactive_and_allows_all(self):
        guard = access_guard.from_settings({})
        self.assertFalse(guard.active)
        self.assertTrue(guard.authorized("8.8.8.8", {}, {}))

    def test_short_token_refused(self):
        with self.assertRaises(access_guard.AccessConfigError):
            access_guard.AccessGuard(token="short")

    def test_bad_network_refused(self):
        with self.assertRaises(access_guard.AccessConfigError):
            access_guard.AccessGuard(networks=["100.64.0.0/10", "not-a-net"])
        with self.assertRaises(access_guard.AccessConfigError):
            access_guard.AccessGuard(networks={"a": 1})

    def test_network_filter(self):
        guard = access_guard.AccessGuard(networks=["100.64.0.0/10"])
        self.assertTrue(guard.active)
        self.assertTrue(guard.network_allowed("100.101.102.103"))
        self.assertTrue(guard.network_allowed("127.0.0.1"))
        self.assertTrue(guard.network_allowed("::1"))
        self.assertFalse(guard.network_allowed("192.0.2.5"))
        self.assertFalse(guard.network_allowed("fd7a:115c:a1e0::1"))
        self.assertFalse(guard.network_allowed(None))
        # 合言葉が無ければ範囲の内側は素通し
        self.assertTrue(guard.authorized("100.101.102.103", {}, {}))
        self.assertFalse(guard.authorized("192.0.2.5", {}, {}))

    def test_token(self):
        guard = access_guard.AccessGuard(token=TOKEN)
        self.assertTrue(guard.token_required)
        # ループバックは合言葉なし
        self.assertTrue(guard.authorized("127.0.0.1", {"Host": "127.0.0.1:5002"}, {}))
        self.assertFalse(guard.authorized("100.1.2.3", {}, {}))
        self.assertTrue(
            guard.authorized("100.1.2.3", {"Authorization": f"Bearer {TOKEN}"}, {})
        )
        self.assertTrue(
            guard.authorized("100.1.2.3", {access_guard.TOKEN_HEADER: TOKEN}, {})
        )
        self.assertFalse(
            guard.authorized(
                "100.1.2.3", {"Authorization": "Bearer wrong-token-xx"}, {}
            )
        )
        cookie = {access_guard.COOKIE_NAME: guard.cookie_value()}
        self.assertTrue(guard.authorized("100.1.2.3", {}, cookie))
        # 印は合言葉そのものではない
        self.assertNotIn(TOKEN, guard.cookie_value())
        self.assertFalse(
            guard.authorized("100.1.2.3", {}, {access_guard.COOKIE_NAME: TOKEN})
        )

    def test_token_and_network_both_needed(self):
        guard = access_guard.AccessGuard(token=TOKEN, networks=["100.64.0.0/10"])
        headers = {"Authorization": f"Bearer {TOKEN}"}
        self.assertTrue(guard.authorized("100.70.0.1", headers, {}))
        self.assertFalse(guard.authorized("192.0.2.5", headers, {}))


class TestAppAccess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.original_guard = self.app.guard
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.app.guard = self.original_guard

    def get(self, path, addr, **kwargs):
        return self.client.get(path, environ_base={"REMOTE_ADDR": addr}, **kwargs)

    def test_default_guard_matches_settings_without_keys(self):
        # 守りを書かない設定（auth_token・allowed_networks なし）では守りは効かない
        self.assertFalse(access_guard.from_settings({"host": "0.0.0.0"}).active)

    def test_no_guard_passes(self):
        self.app.guard = access_guard.AccessGuard()
        res = self.get("/api/sessions/request-clear", "100.1.2.3")
        self.assertEqual(res.status_code, 200)

    def test_token_required_for_remote_api(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        res = self.get("/api/sessions/request-clear", "100.1.2.3")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["code"], "unauthorized")
        res = self.get(
            "/api/sessions/request-clear",
            "100.1.2.3",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(res.status_code, 200)
        # 同じ機械からは合言葉なし（区切りの予約・resumer）
        res = self.get("/api/sessions/request-clear", "127.0.0.1")
        self.assertEqual(res.status_code, 200)

    def test_html_shows_login_page_without_echoing(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        res = self.get("/history", "100.1.2.3")
        self.assertEqual(res.status_code, 401)
        self.assertIn("合言葉", res.get_data(as_text=True))
        res = self.get("/history?token=wrong-but-long-enough", "100.1.2.3")
        self.assertEqual(res.status_code, 401)
        self.assertNotIn("wrong-but-long-enough", res.get_data(as_text=True))

    def test_token_in_url_sets_cookie_and_strips_it(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        res = self.get(f"/history?a=1&token={TOKEN}", "100.1.2.3")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/history?a=1"))
        cookie = res.headers.get("Set-Cookie", "")
        self.assertIn(access_guard.COOKIE_NAME, cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn(TOKEN, cookie)
        # 覚えた印で API も通る
        res = self.get("/api/sessions/request-clear", "100.1.2.3")
        self.assertEqual(res.status_code, 200)

    def test_auth_form(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        env = {"REMOTE_ADDR": "100.1.2.3"}
        self.assertEqual(self.client.get("/auth", environ_base=env).status_code, 200)
        res = self.client.post("/auth", data={"token": "nope"}, environ_base=env)
        self.assertEqual(res.status_code, 401)
        res = self.client.post("/auth", data={"token": TOKEN}, environ_base=env)
        self.assertEqual(res.status_code, 303)
        self.assertIn(access_guard.COOKIE_NAME, res.headers.get("Set-Cookie", ""))

    def test_network_refused(self):
        self.app.guard = access_guard.AccessGuard(networks=["100.64.0.0/10"])
        self.assertEqual(
            self.get("/api/sessions/request-clear", "192.0.2.5").status_code, 403
        )
        # 範囲の外は入口の画面も出さない
        self.assertEqual(
            self.client.get(
                "/auth", environ_base={"REMOTE_ADDR": "192.0.2.5"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.get("/api/sessions/request-clear", "100.70.0.1").status_code, 200
        )

    def test_relogin_can_be_disabled(self):
        self.app.guard = access_guard.AccessGuard()
        with mock.patch.object(
            self.app, "load_settings", return_value={"relogin_enabled": False}
        ):
            self.assertEqual(self.get("/relogin", "127.0.0.1").status_code, 404)
            self.assertEqual(
                self.client.post(
                    "/api/relogin/start", environ_base={"REMOTE_ADDR": "127.0.0.1"}
                ).status_code,
                404,
            )

    def test_relogin_page_shows_machine_name(self):
        self.app.guard = access_guard.AccessGuard()
        with mock.patch.object(
            self.app,
            "load_settings",
            return_value={"relogin_enabled": True, "machine_name": "pc-other"},
        ):
            res = self.get("/relogin", "127.0.0.1")
        self.assertEqual(res.status_code, 200)
        text = res.get_data(as_text=True)
        self.assertIn("Claude ログイン更新（pc-other）", text)
        # 見出しに決め打ちの機械名が残っていない（設定の名前だけが出る）
        self.assertEqual(
            text.count("Claude ログイン更新（"),
            text.count("Claude ログイン更新（pc-other）"),
        )

    def test_socket_connect_refused_without_token(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        with self.app.app.test_request_context(
            "/socket.io/", environ_base={"REMOTE_ADDR": "100.1.2.3"}
        ):
            self.assertFalse(self.app.handle_connect())

    def test_socket_connect_via_test_client(self):
        # 試験用の相手は接続元が無い（ループバック扱いにならない）ので、合言葉の有無で分かれる
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        refused = self.app.socketio.test_client(self.app.app)
        self.assertFalse(refused.is_connected())
        client = self.app.socketio.test_client(
            self.app.app, headers={"Authorization": f"Bearer {TOKEN}"}
        )
        try:
            self.assertTrue(client.is_connected())
        finally:
            client.disconnect()


class TestNoGuardDefaults(unittest.TestCase):
    """守りの無い設定（公開の既定: 127.0.0.1・合言葉なし）でも、別サイトから操作させない。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.original_guard = self.app.guard
        self.original_host = self.app.STARTUP_SETTINGS.get("host")
        self.app.guard = access_guard.AccessGuard()
        self.app.STARTUP_SETTINGS["host"] = "127.0.0.1"
        self.client = self.app.app.test_client()
        self.env = {"REMOTE_ADDR": "127.0.0.1"}

    def tearDown(self):
        self.app.guard = self.original_guard
        self.app.STARTUP_SETTINGS["host"] = self.original_host

    def test_host_refused(self):
        refused = self.app.host_refused
        none = frozenset()
        # ループバックの名前・IP はいつも通す（127.0.0.2 等も）
        for host in (
            "127.0.0.1:5002",
            "localhost:5002",
            "[::1]:5002",
            "LOCALHOST",
            "localhost.:5002",
            "::1",
            "127.0.0.2:5002",
        ):
            with self.subTest(host=host):
                self.assertFalse(refused(none, host))
        # 既定（allowed_hosts なし）では、それ以外の名前・IP は断る
        for host in (
            "evil.example:5002",
            "127.0.0.1.evil.example",
            "",
            "[::2]:5002",
            "100.70.0.1:5002",
            "pc.example.ts.net:5002",
        ):
            with self.subTest(host=host):
                self.assertTrue(refused(none, host))
        # allowed_hosts に書いた名前・IP は通す（大文字・末尾ドット・IPv6 の書き方の違いもそろえる）
        allowed = self.app.parse_allowed_hosts(
            ["My-PC", "pc.example.ts.net.", "100.70.0.1", "FD7A:115C:A1E0:0::1"]
        )
        for host in (
            "my-pc:5002",
            "PC.EXAMPLE.TS.NET",
            "100.70.0.1:5002",
            "[fd7a:115c:a1e0::1]:5002",
        ):
            with self.subTest(host=host):
                self.assertFalse(refused(allowed, host))
        self.assertTrue(refused(allowed, "evil.example:5002"))
        self.assertTrue(refused(allowed, "my-pc.evil.example"))

    def test_parse_allowed_hosts_errors(self):
        parse = self.app.parse_allowed_hosts
        self.assertEqual(parse(None), frozenset())
        self.assertEqual(parse([]), frozenset())
        # IPv6 は括弧つきでも括弧なしでもよい
        self.assertEqual(parse(["[fd7a::1]", "fd7a::1"]), frozenset({"fd7a::1"}))
        for bad in (
            "my-pc",
            ["ok", ""],
            ["ok", 3],
            {"a": 1},
            ["http://my-pc"],
            ["http://my-pc:5002"],
            ["my-pc:5002"],
            ["user@my-pc"],
            ["my pc"],
            ["[fd7a::1]:5002"],
            # 空になる名前・ワイルドカードや区切り・改行・IPv6 でない括弧も断る
            ["."],
            ["[]"],
            ["*"],
            ["*.ts.net"],
            ["a,b"],
            ["my\npc"],
            ["[my-pc]"],
            ["[100.70.0.1]"],
            ["-pc"],
            ["[fd7a::1]junk]"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse(bad)

    def test_cross_site_post_refused_without_guard(self):
        res = self.client.post(
            "/api/sessions/999999/stop",
            environ_base=self.env,
            headers={"Host": "127.0.0.1:5002", "Origin": "https://evil.example"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "cross_origin")
        res = self.client.post(
            "/api/sessions/999999/stop",
            environ_base=self.env,
            headers={"Host": "127.0.0.1:5002", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(res.status_code, 403)

    def test_tools_without_origin_pass(self):
        # urllib・resumer 相当（Origin も Sec-Fetch-Site も無い）は今までどおり通る
        res = self.client.post(
            "/api/sessions/request-clear",
            data="x",
            content_type="text/plain",
            environ_base=self.env,
            headers={"Host": "127.0.0.1:5002"},
        )
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")
        res = self.client.post(
            "/api/sessions/999999/stop",
            environ_base=self.env,
            headers={"Host": "127.0.0.1:5002", "Origin": "http://127.0.0.1:5002"},
        )
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")

    def test_rebinding_host_refused(self):
        res = self.client.get(
            "/api/sessions/request-clear",
            environ_base=self.env,
            headers={"Host": "evil.example:5002"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "forbidden")
        self.assertEqual(
            self.client.get(
                "/api/sessions/request-clear",
                environ_base=self.env,
                headers={"Host": "127.0.0.1:5002"},
            ).status_code,
            200,
        )

    def test_socket_connect_rebinding_refused(self):
        with self.app.app.test_request_context(
            "/socket.io/",
            environ_base=self.env,
            headers={"Host": "evil.example:5002"},
        ):
            self.assertFalse(self.app.handle_connect())


class TestAccessDetails(unittest.TestCase):
    """2周目で足した所: ログの伏せ字・中継の印・戻り先・断りの文面。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.original_guard = self.app.guard
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.app.guard = self.original_guard

    def test_redact_token(self):
        line = f'GET /history?a=1&token={TOKEN}&b=2 HTTP/1.1" 303'
        redacted = access_guard.redact_token(line)
        self.assertNotIn(TOKEN, redacted)
        self.assertIn("token=***&b=2", redacted)
        self.assertEqual(access_guard.redact_token("GET /?x=1"), "GET /?x=1")

    def test_werkzeug_log_filter(self):
        import logging

        logger = access_guard.install_log_redaction("werkzeug-test")
        access_guard.install_log_redaction("werkzeug-test")
        self.assertEqual(
            sum(isinstance(f, access_guard.TokenRedactFilter) for f in logger.filters),
            1,
        )
        record = logging.LogRecord(
            "werkzeug-test",
            logging.INFO,
            "",
            0,
            "%s - %s",
            ("1.2.3.4", f"GET /?token={TOKEN}"),
            None,
        )
        logger.filters[0].filter(record)
        self.assertNotIn(TOKEN, record.getMessage())
        # 本体の読み込みで werkzeug のロガーにも付いている
        self.assertTrue(
            any(
                isinstance(f, access_guard.TokenRedactFilter)
                for f in logging.getLogger("werkzeug").filters
            )
        )

    def test_proxied_loopback_needs_token(self):
        guard = access_guard.AccessGuard(token=TOKEN)
        self.assertTrue(guard.authorized("127.0.0.1", {"Host": "127.0.0.1:5002"}, {}))
        for header in ("X-Forwarded-For", "Tailscale-User-Login", "Forwarded"):
            self.assertFalse(guard.authorized("127.0.0.1", {header: "x"}, {}), header)
        self.app.guard = guard
        res = self.client.get(
            "/api/sessions/request-clear",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Forwarded-For": "100.1.2.3"},
        )
        self.assertEqual(res.status_code, 401)

    def test_safe_next(self):
        self.assertEqual(self.app._safe_next("/history?a=1"), "/history?a=1")
        self.assertEqual(self.app._safe_next("/authx"), "/authx")
        for bad in (
            "/\t/evil.example",
            "/\n/evil.example",
            "/ /evil.example",
            "/\x7f",
            "/auth?next=/",
            "//evil.example/",
            "https://evil.example/",
            "/\\evil",
            "",
            None,
            "/auth",
        ):
            self.assertEqual(self.app._safe_next(bad), "/", bad)

    def test_login_returns_to_original_page(self):
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        env = {"REMOTE_ADDR": "100.1.2.3"}
        res = self.client.get("/history?kind=x", environ_base=env)
        self.assertIn('name="next" value="/history?kind=x"', res.get_data(as_text=True))
        res = self.client.post(
            "/auth", data={"token": TOKEN, "next": "/history?kind=x"}, environ_base=env
        )
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/history?kind=x"))
        res = self.client.post(
            "/auth", data={"token": TOKEN, "next": "//evil.example/"}, environ_base=env
        )
        self.assertTrue(res.headers["Location"].endswith("/"))
        self.assertNotIn("evil", res.headers["Location"])

    def test_network_refusal_says_what_to_do(self):
        self.app.guard = access_guard.AccessGuard(networks=["100.64.0.0/10"])
        env = {"REMOTE_ADDR": "192.0.2.5"}
        page = self.client.get("/history", environ_base=env)
        self.assertEqual(page.status_code, 403)
        self.assertIn("Tailscale", page.get_data(as_text=True))
        self.assertIn("192.0.2.5", page.get_data(as_text=True))
        api = self.client.get("/api/sessions/request-clear", environ_base=env)
        self.assertEqual(api.status_code, 403)
        self.assertEqual(api.get_json()["code"], "forbidden")


class TestAppSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def test_setting_dir(self):
        default = r"F:\x\developing"
        self.assertEqual(self.app._setting_dir({}, "developing_dir", default), default)
        self.assertEqual(
            self.app._setting_dir({"developing_dir": "  "}, "developing_dir", default),
            default,
        )
        self.assertEqual(
            self.app._setting_dir({"developing_dir": 3}, "developing_dir", default),
            default,
        )
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                self.app._setting_dir(
                    {"developing_dir": tmp}, "developing_dir", default
                ),
                os.path.abspath(tmp),
            )

    def test_unset_dirs_are_empty(self):
        # 新しい機械（置き場の指定なし）では別の機械の道を使わず、未設定（空）になる
        self.assertEqual(
            self.app._setting_dir({"host": "0.0.0.0"}, "developing_dir"), ""
        )
        self.assertEqual(self.app.DEFAULT_SETTINGS["developing_dir"], "")

    def test_startup_settings_are_applied(self):
        # 起動時に読んだ設定ファイル（この機械の実物）の置き場・守り・Origin が、そのまま効いている。
        # 設定ファイルは機械ごとで追跡しないため、中身の値ではなく「読んだものが効いているか」を見る
        startup = self.app.STARTUP_SETTINGS
        self.assertEqual(
            self.app.DEVELOPING_DIR, self.app._setting_dir(startup, "developing_dir")
        )
        self.assertEqual(
            self.app.WORKING_DIR, self.app._setting_dir(startup, "working_dir")
        )
        self.assertEqual(
            self.app.guard.active, access_guard.from_settings(startup).active
        )
        self.assertEqual(
            self.app.socketio_cors_origins(startup, self.app.guard),
            self.app.socketio_cors_origins(
                startup, access_guard.from_settings(startup)
            ),
        )

    def test_unset_dir_is_not_a_location(self):
        original = (self.app.DEVELOPING_DIR, self.app.WORKING_DIR)
        self.app.DEVELOPING_DIR = self.app.WORKING_DIR = ""
        try:
            self.assertIsNone(self.app.resolve_location("working"))
            self.assertIsNone(self.app.resolve_project_path("working/x"))
            self.assertIsNone(self.app.project_key_for_path(os.getcwd()))
        finally:
            self.app.DEVELOPING_DIR, self.app.WORKING_DIR = original

    def test_machine_name(self):
        self.assertEqual(
            self.app.machine_name({"machine_name": " pc-other "}), "pc-other"
        )
        self.assertTrue(self.app.machine_name({"machine_name": ""}))

    def test_cors(self):
        none = access_guard.AccessGuard()
        token = access_guard.AccessGuard(token=TOKEN)
        # 守りの無い設定でも、明示が無ければ同じオリジンだけ（以前は "*"）
        self.assertIsNone(self.app.socketio_cors_origins({}, none))
        self.assertEqual(
            self.app.socketio_cors_origins({"cors_allowed_origins": "*"}, none), "*"
        )
        self.assertIsNone(self.app.socketio_cors_origins({}, token))
        # 範囲だけで守るときも、他のサイトのページからは繋がせない
        nets = access_guard.AccessGuard(networks=["100.64.0.0/10"])
        self.assertIsNone(self.app.socketio_cors_origins({}, nets))
        self.assertEqual(
            self.app.socketio_cors_origins(
                {"cors_allowed_origins": ["http://a"]}, token
            ),
            ["http://a"],
        )

    def test_default_host_is_loopback(self):
        self.assertEqual(self.app.DEFAULT_SETTINGS["host"], "127.0.0.1")


class TestPresetMessage(unittest.TestCase):
    def test_machine_name_in_preset(self):
        self.assertIn(
            "pc-other をまもなく再起動",
            broadcaster.preset_message("reboot", "pc-other"),
        )
        self.assertIn("この PC をまもなく再起動", broadcaster.preset_message("reboot"))
        self.assertIn(
            "この PC をまもなく再起動", broadcaster.preset_message("reboot", "")
        )
        self.assertEqual(broadcaster.preset_message("unknown", "x"), "")


class TestMakeBundle(unittest.TestCase):
    def test_excluded(self):
        self.assertTrue(make_bundle.is_excluded("config/settings.json"))
        self.assertTrue(make_bundle.is_excluded("projects/a/b.py"))
        self.assertFalse(make_bundle.is_excluded("docs/config/x.md"))
        self.assertFalse(make_bundle.is_excluded("request_self_clear.py"))

    def test_build_from_head(self):
        # ZIP で落とした木（.git が無い）では束を作れないので飛ばす
        if not os.path.exists(os.path.join(make_bundle.BASE_DIR, ".git")):
            self.skipTest("git の作業ツリーではない")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "system_update.zip")
            # HEAD にまだ無いファイル（作業中に足したもの）で落ちないよう、必須の並びを絞る
            with mock.patch.object(
                make_bundle,
                "REQUIRED_FILES",
                ("claude-web-app.py", "request_self_clear.py"),
            ):
                names = make_bundle.build_bundle(out)
            self.assertFalse(os.path.exists(out + ".tmp"))
            with zipfile.ZipFile(out) as z:
                listed = z.namelist()
                self.assertEqual(z.read("update_target.txt").decode(), "claude-web-app")
                self.assertTrue(z.read("commit_message.txt").decode().strip())
        self.assertIn("request_self_clear.py", listed)
        self.assertIn("claude-web-app.py", listed)
        self.assertFalse([n for n in listed if n.startswith(("config/", "projects/"))])
        self.assertEqual(len(names) + 2, len(listed))

    def test_missing_required_file_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "b.zip")
            with mock.patch.object(make_bundle, "REQUIRED_FILES", ("no-such.py",)):
                with self.assertRaises(make_bundle.BundleError):
                    make_bundle.build_bundle(out)
            self.assertEqual(os.listdir(tmp), [])


class TestCrossOrigin(unittest.TestCase):
    """印（クッキー）で通した GET 以外の要求は、同じオリジンからだけ受け付ける。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.original_guard = self.app.guard
        self.app.guard = access_guard.AccessGuard(token=TOKEN)
        self.client = self.app.app.test_client()
        self.client.set_cookie(access_guard.COOKIE_NAME, self.app.guard.cookie_value())
        self.env = {"REMOTE_ADDR": "100.1.2.3"}

    def tearDown(self):
        self.app.guard = self.original_guard

    def post(self, headers):
        return self.client.post(
            "/api/sessions/request-clear",
            data="x",
            content_type="text/plain",
            environ_base=self.env,
            headers=headers,
        )

    def test_cross_origin_helper(self):
        own = "http://100.1.2.3:5002"
        refused = access_guard.cross_origin_refused
        self.assertFalse(refused("cookie", {"Origin": "http://100.1.2.3:5002"}, own))
        self.assertFalse(refused("cookie", {"Origin": "HTTP://100.1.2.3:5002/"}, own))
        self.assertTrue(refused("cookie", {"Origin": "http://100.1.2.3:5001"}, own))
        self.assertTrue(refused("cookie", {"Origin": "null"}, own))
        self.assertFalse(refused("cookie", {"Sec-Fetch-Site": "same-origin"}, own))
        self.assertTrue(refused("cookie", {"Sec-Fetch-Site": "same-site"}, own))
        self.assertTrue(refused("cookie", {}, own))
        # 範囲だけの守り: 印が無いので、何も付いていない要求（curl 等）は通す
        self.assertFalse(refused("open", {}, own))
        self.assertTrue(refused("open", {"Origin": "http://evil.example"}, own))
        self.assertTrue(refused("open", {"Sec-Fetch-Site": "cross-site"}, own))

    def test_own_origin_behind_proxy(self):
        headers = {
            "X-Forwarded-For": "100.1.2.3",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "pc-other.tail.ts.net",
        }
        self.assertEqual(
            access_guard.own_origin(headers, "http", "127.0.0.1:5002"),
            "https://pc-other.tail.ts.net",
        )
        self.assertEqual(
            access_guard.own_origin({}, "http", "100.1.2.3:5002"),
            "http://100.1.2.3:5002",
        )

    def test_proxied_same_origin_post_passes(self):
        res = self.client.post(
            "/api/sessions/request-clear",
            data="x",
            content_type="text/plain",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "X-Forwarded-For": "100.1.2.3",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "pc-other.tail.ts.net",
                "Origin": "https://pc-other.tail.ts.net",
            },
        )
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")

    def test_range_only_cross_site_post_refused(self):
        self.app.guard = access_guard.AccessGuard(networks=["100.64.0.0/10"])
        env = {"REMOTE_ADDR": "100.70.0.1"}
        res = self.client.post(
            "/api/sessions/999999/stop",
            environ_base=env,
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "cross_origin")

    def test_local_cross_site_post_refused(self):
        env = {"REMOTE_ADDR": "127.0.0.1"}
        res = self.client.post(
            "/api/sessions/999999/stop",
            environ_base=env,
            headers={"Host": "127.0.0.1:5002", "Origin": "https://evil.example"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "cross_origin")
        # urllib 相当（Origin も Sec-Fetch-Site も無い）は通る
        res = self.client.post(
            "/api/sessions/request-clear",
            data="x",
            content_type="text/plain",
            environ_base=env,
            headers={"Host": "127.0.0.1:5002"},
        )
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")

    def test_loopback_with_foreign_host_is_not_local(self):
        guard = self.app.guard
        self.assertTrue(guard.authorized("127.0.0.1", {"Host": "127.0.0.1:5002"}, {}))
        self.assertTrue(guard.authorized("127.0.0.1", {"Host": "localhost:5002"}, {}))
        self.assertTrue(guard.authorized("::1", {"Host": "[::1]:5002"}, {}))
        self.assertFalse(
            guard.authorized("127.0.0.1", {"Host": "evil.example:5002"}, {})
        )

    def test_other_port_refused(self):
        res = self.post({"Origin": "http://100.1.2.3:5001"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "cross_origin")

    def test_no_origin_refused(self):
        self.assertEqual(self.post({}).status_code, 403)

    def test_same_origin_passes_guard(self):
        # 守りは通り、本体の処理（本人確認）で断られる＝403 cross_origin ではない
        res = self.post({"Origin": "http://localhost"})
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")

    def test_header_token_needs_no_origin(self):
        res = self.post({"Authorization": f"Bearer {TOKEN}"})
        self.assertNotEqual(res.get_json().get("code"), "cross_origin")

    def test_get_with_cookie_passes(self):
        res = self.client.get("/api/sessions/request-clear", environ_base=self.env)
        self.assertEqual(res.status_code, 200)

    def test_bad_url_token_on_api_is_json(self):
        res = self.client.get(
            "/api/sessions/request-clear?token=wrong-but-long-enough",
            environ_base=self.env,
        )
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["code"], "unauthorized")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""外の道具（claude-session-resumer）向けの仕様の口（GET /api/integration/spec）と設定の例の試験。

実行方法:
    <python> -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import access_guard
import session_manager as sm


def load_app_module():
    """ファイル名にハイフンを含むメインモジュールを動的に読み込む。"""
    module_path = os.path.join(PROJECT_ROOT, "claude-web-app.py")
    spec = importlib.util.spec_from_file_location("claude_web_app", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_source(name):
    with open(os.path.join(PROJECT_ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestIntegrationSpec(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.original_guard = self.app.guard
        self.client = self.app.app.test_client()

    def tearDown(self):
        self.app.guard = self.original_guard

    def get(self, addr="127.0.0.1", **kwargs):
        return self.client.get(
            "/api/integration/spec", environ_base={"REMOTE_ADDR": addr}, **kwargs
        )

    def test_shape(self):
        res = self.get()
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["spec_version"], 1)
        self.assertEqual(body["registry"]["path"], sm.LAUNCHED_REGISTRY)
        self.assertEqual(body["registry"]["create_time_tolerance_sec"], 2.0)
        self.assertEqual(body["project_key"]["groups"], ["developing", "working"])
        self.assertEqual(
            body["launch"]["option_keys"], list(self.app.LAUNCH_OPTION_KEYS)
        )
        # 「既定」（空文字）は選択肢に出さない
        self.assertNotIn("", body["launch"]["permission_mode"])
        self.assertIn("bypassPermissions", body["launch"]["permission_mode"])
        self.assertEqual(body["launch"]["win_session"], ["0", "1"])
        self.assertEqual(body["launch"]["elevation"], ["admin", "standard"])
        self.assertEqual(
            body["launch"]["refused_before_launch"]["status_400_phrases"],
            list(self.app.PRE_LAUNCH_REFUSALS),
        )

    def test_roots_follow_startup_settings(self):
        body = self.get().get_json()
        self.assertEqual(
            body["project_roots"]["developing"], self.app.DEVELOPING_DIR or None
        )
        self.assertEqual(body["project_roots"]["working"], self.app.WORKING_DIR or None)
        original = (self.app.DEVELOPING_DIR, self.app.WORKING_DIR)
        self.app.DEVELOPING_DIR = self.app.WORKING_DIR = ""
        try:
            body = self.get().get_json()
            # 未設定の置き場は null（resumer はその置き場を本アプリ経由にしない）
            self.assertIsNone(body["project_roots"]["developing"])
            self.assertIsNone(body["project_roots"]["working"])
        finally:
            self.app.DEVELOPING_DIR, self.app.WORKING_DIR = original

    def test_guard_applies(self):
        # パスを含むので、守りのある機械では外から合言葉なしに読ませない
        self.app.guard = access_guard.AccessGuard(token="correct-horse-battery-staple")
        self.assertEqual(self.get().status_code, 200)  # ループバックは通す
        self.assertIn(self.get("100.70.0.1").status_code, (401, 403))

    def test_refusal_phrases_exist_in_source(self):
        # 断り文が出どころ（本体・session_manager の error 文）から消えていないか。
        # 定義の行そのものは数えない
        source = read_source("claude-web-app.py") + read_source("session_manager.py")
        start = source.index("PRE_LAUNCH_REFUSALS = (")
        end = source.index(")", start)
        rest = source[:start] + source[end:]
        for phrase in self.app.PRE_LAUNCH_REFUSALS:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, rest)
        # 「すでに動いている」の文は session_manager の error 文に出る
        self.assertIn(
            self.app.ALREADY_RUNNING_PHRASE, read_source("session_manager.py")
        )
        body = self.get().get_json()
        self.assertEqual(
            body["launch"]["already_running"]["phrase"], self.app.ALREADY_RUNNING_PHRASE
        )

    def test_permission_modes_match_choices(self):
        # 権限モードは session_manager の選択肢（空文字＝既定を除く）と同じ。
        # resumer の写し（WEB_APP_PERMISSION_MODES）とも同じ6つ（2026-10-25 に照合済み）
        expected = {v for v, _ in sm.PERMISSION_MODE_CHOICES if v}
        body = self.get().get_json()
        self.assertEqual(set(body["launch"]["permission_mode"]), expected)


class TestSettingsExample(unittest.TestCase):
    def test_example_covers_defaults(self):
        app = load_app_module()
        path = os.path.join(PROJECT_ROOT, "config", "settings.example.json")
        with open(path, encoding="utf-8") as f:
            example = json.load(f)
        self.assertEqual(set(example), set(app.DEFAULT_SETTINGS))
        # 例は外から開けない形（同じ機械からだけ）・合言葉なしで配る
        self.assertEqual(example["host"], "127.0.0.1")
        self.assertEqual(example["auth_token"], "")
        self.assertEqual(
            example["auto_clear_mode"], app.DEFAULT_SETTINGS["auto_clear_mode"]
        )


if __name__ == "__main__":
    unittest.main()

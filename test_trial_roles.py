# -*- coding: utf-8 -*-
"""trial_roles.py の試験（一時フォルダだけを使う・本物の設定には触らない）。"""

import json
import os
import tempfile
import unittest

import trial_roles as tr

A = r"F:\dev\example-analyzer"
B = r"F:\dev\example-daily"


class TrialRolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.cfg = os.path.join(base, "cfg")
        self.home = os.path.join(base, "home")
        os.makedirs(self.cfg)
        os.makedirs(self.home)
        self.path = os.path.join(base, "trial_roles.json")
        self.err = os.path.join(base, tr.ERROR_NAME)
        tr._last_good = None
        tr._last_error = None

    def tearDown(self):
        tr._last_good = None
        tr._last_error = None
        self.tmp.cleanup()

    def write(self, obj, bom=False):
        with open(self.path, "w", encoding="utf-8-sig" if bom else "utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def conf(self, roles=("example-analyzer",)):
        self.write({"config_dir": self.cfg, "home": self.home, "roles": list(roles)})
        return tr.load(self.path)

    # 陽性対照: 載った役だけ3つが足される
    def test_apply_adds_three_for_listed_role(self):
        conf = self.conf()
        env = tr.apply({"PATH": "x"}, A, conf)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.cfg)
        self.assertEqual(env["HOME"], self.home)
        self.assertEqual(env["USERPROFILE"], self.home)
        self.assertEqual(env["PATH"], "x")

    def test_role_match_ignores_case_and_trailing_sep(self):
        conf = self.conf(["Example-Analyzer"])
        for cwd in ("F:/dev/EXAMPLE-ANALYZER/", "F:\\dev\\example-analyzer\\.", " F:\\dev\\example-analyzer ", b"F:/dev/example-analyzer"):
            self.assertTrue(tr.is_trial(cwd, conf), cwd)

    # 陰性対照: 載っていない役・部分一致・サブフォルダは足さない
    def test_apply_leaves_other_roles(self):
        conf = self.conf()
        for cwd in (B, r"F:\dev\example-analyzer-old", A + r"\sub", "F:\\", "", None):
            env = tr.apply({"PATH": "x", "HOME": "h", "USERPROFILE": "u"}, cwd, conf)
            self.assertNotIn("CLAUDE_CONFIG_DIR", env, cwd)
            self.assertEqual(env["HOME"], "h", cwd)
            self.assertEqual(env["USERPROFILE"], "u", cwd)

    def test_other_role_does_not_inherit_trial_home(self):
        conf = self.conf()
        env = tr.apply({"HOME": self.home, "USERPROFILE": self.home + os.sep}, B, conf)
        self.assertEqual(env["HOME"], tr.MAIN_USER_DIR)
        self.assertEqual(env["USERPROFILE"], tr.MAIN_USER_DIR)

    def test_apply_does_not_mutate_input(self):
        conf = self.conf()
        src = {"PATH": "x"}
        tr.apply(src, A, conf)
        self.assertEqual(src, {"PATH": "x"})

    def test_bom_is_read(self):
        self.write({"config_dir": self.cfg, "home": self.home, "roles": ["example-analyzer"]}, bom=True)
        self.assertTrue(tr.is_trial(A, tr.load(self.path)))

    # 試用の役が無い（正常）: None・理由は残さない
    def test_no_trial_roles_is_normal(self):
        for c in (
            {"config_dir": self.cfg, "home": self.home, "roles": []},
            {"config_dir": self.cfg, "home": self.home, "roles": None},
            {"config_dir": self.cfg, "home": self.home, "roles": ["  "]},
            {"config_dir": self.cfg, "home": self.home},
        ):
            self.write(c)
            self.assertIsNone(tr.load(self.path), c)
            self.assertIsNone(tr.last_error(), c)
        self.assertIsNone(tr.load(self.path + ".missing"))

    def test_roles_dedup(self):
        conf = self.conf(["example-analyzer", "EXAMPLE-ANALYZER "])
        self.assertEqual(conf["roles"], {"example-analyzer"})

    # 作業場のフルパスを書いた＝書き損じ。黙って捨てず、使えない扱いにして理由を残す
    def test_separator_in_role_is_broken(self):
        for bad in (r"F:\dev\example-analyzer", "a/b", "c:x"):
            tr._last_good = None
            self.write({"config_dir": self.cfg, "home": self.home, "roles": [bad]})
            self.assertIsNone(tr.load(self.path), bad)
            self.assertIn("区切り", tr.last_error(), bad)
            self.assertTrue(os.path.isfile(self.err), bad)

    # 消して書き直す保存の途中（一瞬無い）は見直して読む（試用の役が既にいる＝前に読めた設定があるとき）
    def test_momentary_missing_is_rechecked(self):
        good = {"config_dir": self.cfg, "home": self.home, "roles": ["example-analyzer"]}
        self.conf()
        os.remove(self.path)
        real_sleep = tr.time.sleep
        tr.time.sleep = lambda s: self.write(good)
        try:
            conf = tr.load(self.path)
        finally:
            tr.time.sleep = real_sleep
        self.assertTrue(tr.is_trial(A, conf))

    # 壊れた（読み直しても駄目）: 前に読めた設定が無ければ None・理由を残す
    def test_broken_without_last_good(self):
        cases = [
            "{broken",
            "[]",
            {"config_dir": self.cfg, "home": self.home, "roles": "example-analyzer"},
            {"config_dir": self.cfg + "-none", "home": self.home, "roles": ["a"]},
            {"config_dir": self.cfg, "home": self.home + "-none", "roles": ["a"]},
            {"config_dir": 1, "home": self.home, "roles": ["a"]},
            {"home": self.home, "roles": ["a"]},
        ]
        for c in cases:
            tr._last_good = None
            self.write(c)
            self.assertIsNone(tr.load(self.path), c)
            self.assertIsNotNone(tr.last_error(), c)
            self.assertTrue(os.path.isfile(self.err), c)

    # 壊れた: 前に読めた設定があればそれを使う（試用の役が本家へ黙って流れない）・読めたら理由を消す
    def test_broken_keeps_last_good(self):
        good = self.conf()
        self.write("{half")
        self.assertEqual(tr.load(self.path), good)
        self.assertTrue(os.path.isfile(self.err))
        self.conf()
        self.assertIsNone(tr.last_error())
        self.assertFalse(os.path.isfile(self.err))

    # 1回目は壊れていて、読み直しで読める（書き換えの途中）
    def test_retry_reads_second_time(self):
        good = {"config_dir": self.cfg, "home": self.home, "roles": ["example-analyzer"]}
        self.write("{half")
        real_sleep = tr.time.sleep
        tr.time.sleep = lambda s: self.write(good)
        try:
            conf = tr.load(self.path)
        finally:
            tr.time.sleep = real_sleep
        self.assertTrue(tr.is_trial(A, conf))
        self.assertIsNone(tr.last_error())

    # 別のプロセス（_last_good が無い）でも、隣の lastgood で試用を保つ
    def test_lastgood_file_used_by_fresh_process(self):
        good = self.conf()
        tr._last_good = None
        self.write("{half")
        self.assertEqual(tr.load(self.path), good)

    # 設定を消した・roles を空にした＝意図した戻し。前に読めた設定も捨てる
    def test_removal_drops_lastgood(self):
        self.conf()
        os.remove(self.path)
        self.assertIsNone(tr.load(self.path))
        self.write("{half")
        self.assertIsNone(tr.load(self.path))
        self.conf()
        self.write({"config_dir": self.cfg, "home": self.home, "roles": []})
        self.assertIsNone(tr.load(self.path))
        tr._last_good = None
        self.write("{half")
        self.assertIsNone(tr.load(self.path))

    def test_env_path_missing_is_reported(self):
        old = os.environ.get(tr.ENV_PATH)
        os.environ[tr.ENV_PATH] = self.path + ".missing"
        try:
            self.assertIsNone(tr.load())
            self.assertIn(tr.ENV_PATH, tr.last_error())
        finally:
            if old is None:
                del os.environ[tr.ENV_PATH]
            else:
                os.environ[tr.ENV_PATH] = old

    def test_config_dir_same_as_main_is_broken(self):
        old = tr.MAIN_HOME
        tr.MAIN_HOME = self.cfg
        try:
            self.write({"config_dir": self.cfg, "home": self.home, "roles": ["a"]})
            self.assertIsNone(tr.load(self.path))
            self.assertIn("本家", tr.last_error())
        finally:
            tr.MAIN_HOME = old

    def test_apply_without_conf_is_passthrough(self):
        # conf が None のときは試用の home を知らないので、継いだ HOME も直せない（素通し）
        env = tr.apply({"HOME": self.home}, B, None)
        self.assertEqual(env, {"HOME": self.home})

    def test_claude_json_path(self):
        conf = self.conf()
        self.assertEqual(tr.claude_json_path(A, conf), os.path.join(self.cfg, ".claude.json"))
        self.assertIsNone(tr.claude_json_path(B, conf))
        self.assertIsNone(tr.claude_json_path(A, None))

    def test_claude_homes(self):
        conf = self.conf()
        self.assertEqual(tr.claude_homes(conf), [tr.MAIN_HOME, self.cfg])
        self.assertEqual(tr.claude_homes(None), [tr.MAIN_HOME])

    def test_record_allowed(self):
        conf = self.conf()
        self.assertTrue(tr.record_allowed(A, self.cfg, conf))
        self.assertTrue(tr.record_allowed(A, self.cfg + os.sep, conf))
        self.assertFalse(tr.record_allowed(A, tr.MAIN_HOME, conf))  # 移した役の本家の古い会話は開かない
        self.assertTrue(tr.record_allowed(B, tr.MAIN_HOME, conf))
        self.assertFalse(tr.record_allowed(B, self.cfg, conf))  # 外した役の試用の会話を本家で開かない
        # 試用が無いときは本家の会話だけ
        self.assertTrue(tr.record_allowed(A, tr.MAIN_HOME, None))
        self.assertTrue(tr.record_allowed(B, tr.MAIN_HOME, None))

    def test_is_trial_none_conf(self):
        self.assertFalse(tr.is_trial(A, None))

    def test_env_path_override(self):
        self.conf()
        old = os.environ.get(tr.ENV_PATH)
        os.environ[tr.ENV_PATH] = self.path
        try:
            self.assertTrue(tr.is_trial(A, tr.load()))
        finally:
            if old is None:
                del os.environ[tr.ENV_PATH]
            else:
                os.environ[tr.ENV_PATH] = old

    # 普段の道（設定も lastgood も無い）は待たない＝web-app の一覧・送りのたびに 0.2秒遅れない
    def test_missing_without_lastgood_does_not_wait(self):
        import time as _time

        started = _time.monotonic()
        self.assertIsNone(tr.load(self.path))
        self.assertLess(_time.monotonic() - started, 0.1)

    # session_manager が置き場を引く2つ（陽性・陰性）
    def test_home_for_trial_and_other(self):
        conf = self.conf()
        self.assertEqual(tr.home_for(A, conf), self.cfg)
        self.assertIsNone(tr.home_for(B, conf))
        self.assertIsNone(tr.home_for(A, None))

    def test_extra_homes(self):
        self.assertEqual(tr.extra_homes(None), [])
        conf = self.conf()
        self.assertEqual(tr.extra_homes(conf), [self.cfg])

    def test_norm_windows_paths(self):
        self.assertEqual(tr._norm("C:/Users/X/cfg/"), tr._norm(r"c:\users\x\CFG"))


if __name__ == "__main__":
    unittest.main()

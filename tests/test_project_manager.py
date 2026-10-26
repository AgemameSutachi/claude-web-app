# -*- coding: utf-8 -*-
"""プロジェクト作成機能（project_manager）のユニットテスト。

追加パッケージを必要としないよう、標準ライブラリの unittest で記述している。

実行方法:
    <python> -m unittest discover -s tests -v
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

# テスト対象をインポートできるようプロジェクトルートをパスへ追加する
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import project_manager as pm
import session_manager as sm


def load_app_module():
    """ファイル名にハイフンを含むメインモジュールを動的に読み込む。"""
    module_path = os.path.join(PROJECT_ROOT, "claude-web-app.py")
    spec = importlib.util.spec_from_file_location("claude_web_app", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSanitizeProjectName(unittest.TestCase):
    """生成された名前を、フォルダ名として安全な形へ正規化できるかを検証する。"""

    def test_アンダースコアと大文字がkebab_caseへ変換される(self):
        self.assertEqual(
            pm.sanitize_project_name("Power_Monitor_App"), "power-monitor-app"
        )

    def test_引用符やマークダウン装飾が除去される(self):
        self.assertEqual(pm.sanitize_project_name('`"invoice-ocr"`'), "invoice-ocr")

    def test_説明文が付いた場合は最初の行のみ採用される(self):
        raw = "invoice-ocr-batch\nThis name describes the project."
        self.assertEqual(pm.sanitize_project_name(raw), "invoice-ocr-batch")

    def test_日本語や記号は除去される(self):
        self.assertEqual(
            pm.sanitize_project_name("電力監視 power/monitor!"), "power-monitor"
        )

    def test_全角英数字が半角へ正規化される(self):
        self.assertEqual(
            pm.sanitize_project_name("Ｐｏｗｅｒ　Ｍｏｎｉｔｏｒ"), "power-monitor"
        )

    def test_最大長を超える名前は単語境界で切り詰められる(self):
        raw = "very-long-project-name-that-exceeds-the-limit"
        name = pm.sanitize_project_name(raw)
        self.assertLessEqual(len(name), pm.MAX_NAME_LENGTH)
        self.assertFalse(name.endswith("-"))
        self.assertTrue(raw.startswith(name))

    def test_連続ハイフンと前後ハイフンが整理される(self):
        self.assertEqual(pm.sanitize_project_name("--foo---bar--"), "foo-bar")

    def test_Windowsの予約デバイス名は退避される(self):
        self.assertEqual(pm.sanitize_project_name("CON"), "con-project")

    def test_有効な文字が無い場合は空文字を返す(self):
        self.assertEqual(pm.sanitize_project_name("！？＃"), "")
        self.assertEqual(pm.sanitize_project_name(None), "")


class TestSingleLineConversion(unittest.TestCase):
    """対話型CLIへ流し込む指示文の整形処理を検証する。"""

    def test_改行が空白へ畳まれて1行になる(self):
        raw = "電力使用量を監視したい。\nしきい値を超えたらLINEに通知する。"
        self.assertEqual(
            pm.to_single_line(raw),
            "電力使用量を監視したい。 しきい値を超えたらLINEに通知する。",
        )

    def test_連続する空白や前後の空白が整理される(self):
        self.assertEqual(pm.to_single_line("  a\t\t b \n\n c  "), "a b c")

    def test_空入力は空文字を返す(self):
        self.assertEqual(pm.to_single_line(""), "")
        self.assertEqual(pm.to_single_line(None), "")


class TestProjectCreation(unittest.TestCase):
    """フォルダ作成とSQLiteへの記録・検索を、一時ディレクトリ上で検証する。"""

    def setUp(self):
        # テスト用に一時ディレクトリと一時DBへ差し替える
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_test_")
        self.parent_dir = os.path.join(self.temp_dir, "sample-project")
        os.makedirs(self.parent_dir)
        self.parent_key = "developing/sample-project"

        self.original_db_file = pm.DB_FILE
        pm.DB_FILE = os.path.join(self.temp_dir, "projects.db")
        pm.init_db()

    def tearDown(self):
        pm.DB_FILE = self.original_db_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_projectsフォルダ配下にフォルダとREADMEが作成される(self):
        created = pm.create_sub_project(
            parent_key=self.parent_key,
            parent_dir=self.parent_dir,
            name="power-monitor",
            source_chat="電力使用量をLINEに通知したい",
        )

        expected_path = os.path.join(self.parent_dir, "projects", "power-monitor")
        self.assertTrue(os.path.isdir(expected_path))
        self.assertEqual(created["abs_path"], expected_path)
        self.assertEqual(
            created["project_key"], "developing/sample-project/projects/power-monitor"
        )

        # 命名の根拠を残すREADMEが生成されている
        readme_path = os.path.join(expected_path, "README.md")
        self.assertTrue(os.path.isfile(readme_path))
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("電力使用量をLINEに通知したい", content)

    def test_同名プロジェクトは連番が付与される(self):
        first = pm.create_sub_project(self.parent_key, self.parent_dir, "dup-name")
        second = pm.create_sub_project(self.parent_key, self.parent_dir, "dup-name")
        self.assertEqual(first["name"], "dup-name")
        self.assertEqual(second["name"], "dup-name-2")

    def test_名前が空の場合は例外となる(self):
        with self.assertRaises(ValueError):
            pm.create_sub_project(self.parent_key, self.parent_dir, "！？")

    def test_一覧が更新日順と名前順で並べ替えられる(self):
        pm.create_sub_project(self.parent_key, self.parent_dir, "banana")
        pm.create_sub_project(self.parent_key, self.parent_dir, "apple")

        by_name = pm.list_sub_projects(self.parent_key, sort="name")
        self.assertEqual([p["name"] for p in by_name], ["apple", "banana"])

        # 更新日順でも件数が変わらないことを確認する
        by_updated = pm.list_sub_projects(self.parent_key, sort="updated")
        self.assertEqual(len(by_updated), 2)

    def test_チャット内容による検索ができる(self):
        pm.create_sub_project(
            self.parent_key,
            self.parent_dir,
            "invoice-ocr",
            source_chat="請求書PDFから金額を抽出してCSVにする",
        )
        pm.create_sub_project(
            self.parent_key,
            self.parent_dir,
            "power-monitor",
            source_chat="電力使用量をLINEに通知したい",
        )

        # 名前に含まれない語でも、元チャットの内容で絞り込める
        hits = pm.list_sub_projects(self.parent_key, query="請求書")
        self.assertEqual([p["name"] for p in hits], ["invoice-ocr"])

        hits = pm.list_sub_projects(self.parent_key, query="LINE")
        self.assertEqual([p["name"] for p in hits], ["power-monitor"])

        hits = pm.list_sub_projects(self.parent_key, query="存在しない語")
        self.assertEqual(hits, [])

    def test_親を指定しない全プロジェクト横断検索ができる(self):
        pm.create_sub_project(
            self.parent_key,
            self.parent_dir,
            "invoice-ocr",
            source_chat="請求書PDFから金額を抽出する",
        )

        # 別の親プロジェクト配下にも作成する
        other_parent_dir = os.path.join(self.temp_dir, "other-project")
        os.makedirs(other_parent_dir)
        pm.create_sub_project(
            "developing/other-project",
            other_parent_dir,
            "receipt-scanner",
            source_chat="レシートを読み取って請求書と突き合わせる",
        )

        # parent_key を省略すると親をまたいで検索できる
        hits = pm.list_sub_projects(query="請求書")
        self.assertEqual(
            sorted(p["name"] for p in hits), ["invoice-ocr", "receipt-scanner"]
        )

        # 親を指定した場合はその配下だけに絞られる
        hits = pm.list_sub_projects(self.parent_key, query="請求書")
        self.assertEqual([p["name"] for p in hits], ["invoice-ocr"])

    def test_件数上限を指定できる(self):
        for i in range(5):
            pm.create_sub_project(self.parent_key, self.parent_dir, f"item-{i}")
        self.assertEqual(len(pm.list_sub_projects(limit=3)), 3)

    def test_手動作成フォルダの取り込みと削除済みフォルダの除去(self):
        pm.create_sub_project(self.parent_key, self.parent_dir, "created-by-app")

        # Webアプリを介さず手動で作られたフォルダ
        manual_path = os.path.join(self.parent_dir, "projects", "created-by-hand")
        os.makedirs(manual_path)

        names = [
            p["name"]
            for p in pm.list_sub_projects(self.parent_key, parent_dir=self.parent_dir)
        ]
        self.assertIn("created-by-hand", names)

        # フォルダを削除すると一覧からも消える
        shutil.rmtree(manual_path)
        names = [
            p["name"]
            for p in pm.list_sub_projects(self.parent_key, parent_dir=self.parent_dir)
        ]
        self.assertNotIn("created-by-hand", names)
        self.assertIn("created-by-app", names)

    def test_手で改名したフォルダは記録が付け替わり作成時の指示が残る(self):
        created = pm.create_sub_project(
            self.parent_key,
            self.parent_dir,
            "temp-session",
            source_chat="一時的な調査をするセッションが欲しい",
        )
        new_path = os.path.join(self.parent_dir, "projects", "temp-session-develop")
        os.rename(created["abs_path"], new_path)

        renamed = pm.sync_sub_projects(self.parent_key, self.parent_dir)
        self.assertEqual(
            renamed,
            [
                (
                    "developing/sample-project/projects/temp-session",
                    "developing/sample-project/projects/temp-session-develop",
                )
            ],
        )
        rows = pm.list_sub_projects(self.parent_key)
        self.assertEqual([p["name"] for p in rows], ["temp-session-develop"])
        self.assertEqual(rows[0]["abs_path"], new_path)
        # 作成時の指示（検索の手がかり）が引き継がれている
        hits = pm.list_sub_projects(query="一時的な調査")
        self.assertEqual([p["name"] for p in hits], ["temp-session-develop"])
        # 2回目の同期では何も起きない
        self.assertEqual(pm.sync_sub_projects(self.parent_key, self.parent_dir), [])

    def test_作成時刻が離れたフォルダは改名とみなさない(self):
        pm.create_sub_project(self.parent_key, self.parent_dir, "old-one")
        shutil.rmtree(os.path.join(self.parent_dir, "projects", "old-one"))
        os.makedirs(os.path.join(self.parent_dir, "projects", "new-one"))
        later = time.time() + 3600
        with mock.patch.object(pm, "_folder_birth_time", return_value=later):
            renamed = pm.sync_sub_projects(self.parent_key, self.parent_dir)
        self.assertEqual(renamed, [])
        names = [p["name"] for p in pm.list_sub_projects(self.parent_key)]
        self.assertEqual(names, ["new-one"])

    def test_親を改名しても子の記録と指示が引き継がれる(self):
        pm.create_sub_project(
            self.parent_key, self.parent_dir, "child", source_chat="親ごと移る子"
        )
        new_parent_dir = os.path.join(self.temp_dir, "renamed-parent")
        os.rename(self.parent_dir, new_parent_dir)

        renamed = pm.sync_sub_projects("developing/renamed-parent", new_parent_dir)
        self.assertEqual(
            renamed,
            [
                (
                    "developing/sample-project/projects/child",
                    "developing/renamed-parent/projects/child",
                )
            ],
        )
        hits = pm.list_sub_projects(query="親ごと移る子")
        self.assertEqual(
            [(p["parent_key"], p["name"]) for p in hits],
            [("developing/renamed-parent", "child")],
        )

    def test_変化が無ければDBを書き換えない(self):
        pm.create_sub_project(self.parent_key, self.parent_dir, "steady")
        pm.sync_sub_projects(self.parent_key, self.parent_dir)
        before = os.path.getmtime(pm.DB_FILE)
        time.sleep(0.05)
        pm.sync_sub_projects(self.parent_key, self.parent_dir)
        self.assertEqual(before, os.path.getmtime(pm.DB_FILE))

    def test_トップレベルの改名は付け替えだけで取り込みはしない(self):
        group_dir = os.path.join(self.temp_dir, "developing")
        os.makedirs(os.path.join(group_dir, "unrelated"))
        created = pm.create_project(
            group_dir,
            "developing",
            "top-one",
            source_chat="直下の指示",
            parent_key="developing",
        )
        os.rename(created["abs_path"], os.path.join(group_dir, "top-renamed"))

        # 無関係なフォルダは1日前に作られたことにする（同じ瞬間だと候補が2つになり対にしない）
        real_birth_time = pm._folder_birth_time

        def birth_time(path):
            if os.path.basename(path) == "unrelated":
                return real_birth_time(path) - 86400
            return real_birth_time(path)

        with mock.patch.object(pm, "_folder_birth_time", side_effect=birth_time):
            renamed = pm.sync_top_level("developing", group_dir)
        self.assertEqual(renamed, [("developing/top-one", "developing/top-renamed")])
        hits = pm.list_sub_projects(query="直下の指示")
        self.assertEqual([p["project_key"] for p in hits], ["developing/top-renamed"])
        # 記録の無いフォルダは取り込まない
        self.assertEqual(pm.list_sub_projects(query="unrelated"), [])

    def test_まとめて消すのは一時フォルダ側だけ(self):
        sub = pm.create_sub_project(self.parent_key, self.parent_dir, "gone-sub")
        group_dir = os.path.join(self.temp_dir, "developing")
        top = pm.create_project(
            group_dir, "developing", "gone-top", parent_key="developing"
        )
        shutil.rmtree(sub["abs_path"])
        shutil.rmtree(top["abs_path"])

        self.assertEqual(pm.prune_vanished_sub_projects(), 1)
        keys = [p["project_key"] for p in pm.list_sub_projects()]
        self.assertEqual(keys, ["developing/gone-top"])

    def test_改名の突き合わせは一意に決まるものだけ(self):
        # 候補が2つあれば対にしない（取り違えて別の指示を付けないため）
        self.assertEqual(pm._pair_renamed({"a": 100.0, "b": 100.5}, {"x": 100.2}), [])
        self.assertEqual(pm._pair_renamed({"a": 100.0}, {"x": 100.2, "y": 100.4}), [])
        # 1対1なら対になる・作成時刻が読めないものは対にしない
        self.assertEqual(
            pm._pair_renamed({"a": 100.0, "b": 500.0}, {"x": 100.2, "y": None}),
            [("a", "x")],
        )
        self.assertEqual(pm._pair_renamed({"a": None}, {"x": 100.0}), [])

    def test_親ごとのサブプロジェクト件数が集計される(self):
        pm.create_sub_project(self.parent_key, self.parent_dir, "one")
        pm.create_sub_project(self.parent_key, self.parent_dir, "two")
        counts = pm.count_sub_projects_by_parent()
        self.assertEqual(counts.get(self.parent_key), 2)


class TestProjectPathResolution(unittest.TestCase):
    """プロジェクトキーからのパス解決と、不正なキーの拒否を検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        # 実際のプロジェクトフォルダを参照せずに済むよう、一時ディレクトリへ差し替える
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_path_")
        self.developing = os.path.join(self.temp_dir, "developing")
        os.makedirs(os.path.join(self.developing, "sample", "projects", "child"))

        self.original_developing = self.app.DEVELOPING_DIR
        self.app.DEVELOPING_DIR = self.developing

    def tearDown(self):
        self.app.DEVELOPING_DIR = self.original_developing
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_トップレベルのプロジェクトが解決できる(self):
        path = self.app.resolve_project_path("developing/sample")
        self.assertEqual(path, os.path.join(self.developing, "sample"))

    def test_サブプロジェクトが解決できる(self):
        path = self.app.resolve_project_path("developing/sample/projects/child")
        self.assertEqual(
            path, os.path.join(self.developing, "sample", "projects", "child")
        )

    def test_親ディレクトリ参照を含むキーは拒否される(self):
        self.assertIsNone(self.app.resolve_project_path("developing/../../windows"))
        self.assertIsNone(self.app.resolve_project_path("developing/sample/../.."))

    def test_未知のグループや存在しないパスは拒否される(self):
        self.assertIsNone(self.app.resolve_project_path("unknown/sample"))
        self.assertIsNone(self.app.resolve_project_path("developing/not-exists"))
        self.assertIsNone(self.app.resolve_project_path("default"))
        self.assertIsNone(self.app.resolve_project_path(""))

    def test_作成先はセッションに依らず設定値で決まる(self):
        original_load_settings = self.app.load_settings
        try:
            self.app.load_settings = lambda: {"project_root": "developing/custom-root"}
            self.assertEqual(self.app.get_project_root_key(), "developing/custom-root")

            # 設定に project_root が無い場合は既定値へフォールバックする
            self.app.load_settings = lambda: {}
            self.assertEqual(
                self.app.get_project_root_key(),
                self.app.DEFAULT_SETTINGS["project_root"],
            )
        finally:
            self.app.load_settings = original_load_settings


class TestRenameSyncInApp(unittest.TestCase):
    """手で改名したフォルダが、検索と起動回数の記録にも反映されるかを検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_rename_")
        self.developing = os.path.join(self.temp_dir, "developing")
        self.parent_dir = os.path.join(self.developing, "sample")
        os.makedirs(self.parent_dir)
        self.saved = {
            "DEVELOPING_DIR": self.app.DEVELOPING_DIR,
            "WORKING_DIR": self.app.WORKING_DIR,
            "USAGE_FILE": self.app.USAGE_FILE,
        }
        self.app.DEVELOPING_DIR = self.developing
        self.app.WORKING_DIR = os.path.join(self.temp_dir, "working")
        self.app.USAGE_FILE = os.path.join(self.temp_dir, "usage.json")
        self.original_db_file = pm.DB_FILE
        pm.DB_FILE = os.path.join(self.temp_dir, "projects.db")
        pm.init_db()

    def tearDown(self):
        for key, value in self.saved.items():
            setattr(self.app, key, value)
        pm.DB_FILE = self.original_db_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_検索の前に改名が反映され起動回数も引き継がれる(self):
        created = pm.create_sub_project(
            "developing/sample", self.parent_dir, "temp-session", source_chat="調査用"
        )
        self.app.save_project_usage({created["project_key"]: 3})
        os.rename(
            created["abs_path"],
            os.path.join(self.parent_dir, "projects", "temp-session-develop"),
        )

        self.app.sync_all_sub_projects()

        hits = pm.list_sub_projects(query="調査用")
        self.assertEqual([p["name"] for p in hits], ["temp-session-develop"])
        self.assertEqual(
            self.app.load_project_usage(),
            {"developing/sample/projects/temp-session-develop": 3},
        )


class TestSessionDisplayName(unittest.TestCase):
    """`claude -n` へ渡すセッション表示名の生成を検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def test_サブプロジェクトは末尾のプロジェクト名が使われる(self):
        self.assertEqual(
            self.app.build_session_display_name(
                "developing/claude-web-app/projects/invoice-ocr-batch"
            ),
            "invoice-ocr-batch",
        )

    def test_トップレベルのプロジェクトも末尾のフォルダ名が使われる(self):
        self.assertEqual(
            self.app.build_session_display_name("developing/claude-web-app"),
            "claude-web-app",
        )

    def test_defaultや空のキーでは名前を付けない(self):
        self.assertEqual(self.app.build_session_display_name("default"), "")
        self.assertEqual(self.app.build_session_display_name(""), "")
        self.assertEqual(self.app.build_session_display_name(None), "")

    def test_末尾のスラッシュがあっても名前を取り出せる(self):
        self.assertEqual(
            self.app.build_session_display_name("developing/sample/projects/child/"),
            "child",
        )


class TestProjectMove(unittest.TestCase):
    """プロジェクトフォルダの置き場の移動を、一時ディレクトリ上で検証する。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_move_")
        # 一時フォルダ（親プロジェクトの projects/ 配下）を模したもの
        self.parent_dir = os.path.join(self.temp_dir, "claude-web-app")
        os.makedirs(self.parent_dir)
        self.parent_key = "developing/claude-web-app"
        # developing / working 相当の置き場
        self.developing_dir = os.path.join(self.temp_dir, "developing")
        self.working_dir = os.path.join(self.temp_dir, "working")
        os.makedirs(self.developing_dir)
        os.makedirs(self.working_dir)

        self.original_db_file = pm.DB_FILE
        pm.DB_FILE = os.path.join(self.temp_dir, "projects.db")
        pm.init_db()

    def tearDown(self):
        pm.DB_FILE = self.original_db_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create(self, name, source_chat="請求書PDFから金額を抽出する"):
        return pm.create_sub_project(
            self.parent_key, self.parent_dir, name, source_chat=source_chat
        )

    def test_一時フォルダからdevelopingへ移動できる(self):
        created = self._create("invoice-ocr")
        moved = pm.move_project(
            project_key=created["project_key"],
            src_path=created["abs_path"],
            dest_dir=self.developing_dir,
            key_prefix="developing",
            parent_key="developing",
        )

        # 実体が移動していること
        self.assertFalse(os.path.exists(created["abs_path"]))
        self.assertTrue(os.path.isdir(moved["abs_path"]))
        self.assertEqual(
            moved["abs_path"], os.path.join(self.developing_dir, "invoice-ocr")
        )
        self.assertEqual(moved["project_key"], "developing/invoice-ocr")
        self.assertEqual(moved["previous_key"], created["project_key"])
        self.assertFalse(moved["renamed"])

        # 作成時の指示（検索の手がかり）が移動後も残っていること
        hits = pm.list_sub_projects(query="請求書")
        self.assertEqual([p["project_key"] for p in hits], ["developing/invoice-ocr"])

    def test_developingからworkingへ移動できる(self):
        created = self._create("power-monitor")
        first = pm.move_project(
            created["project_key"],
            created["abs_path"],
            self.developing_dir,
            "developing",
            "developing",
        )
        second = pm.move_project(
            first["project_key"],
            first["abs_path"],
            self.working_dir,
            "working",
            "working",
        )

        self.assertEqual(second["project_key"], "working/power-monitor")
        self.assertTrue(os.path.isdir(os.path.join(self.working_dir, "power-monitor")))
        self.assertFalse(os.path.exists(first["abs_path"]))

    def test_移動先に同名があれば連番を付けて退避する(self):
        # 移動先に、無関係な同名フォルダを先に作っておく
        existing = os.path.join(self.developing_dir, "invoice-ocr")
        os.makedirs(existing)
        marker = os.path.join(existing, "keep.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("既存フォルダの中身")

        created = self._create("invoice-ocr")
        moved = pm.move_project(
            created["project_key"],
            created["abs_path"],
            self.developing_dir,
            "developing",
            "developing",
        )

        self.assertTrue(moved["renamed"])
        self.assertEqual(moved["name"], "invoice-ocr-2")
        # 既存フォルダが上書きされていないこと（これが最も大事）
        self.assertTrue(os.path.isfile(marker))

    def test_同じ置き場への移動は拒否される(self):
        created = self._create("same-place")
        with self.assertRaises(ValueError):
            pm.move_project(
                created["project_key"],
                created["abs_path"],
                os.path.join(self.parent_dir, pm.PROJECTS_SUBDIR),
                f"{self.parent_key}/{pm.PROJECTS_SUBDIR}",
                self.parent_key,
            )

    def test_DBに記録が無いフォルダも移動できる(self):
        # Webアプリを介さず手で作られたプロジェクトを想定する
        manual = os.path.join(self.developing_dir, "manual-project")
        os.makedirs(manual)

        moved = pm.move_project(
            "developing/manual-project", manual, self.working_dir, "working", "working"
        )
        self.assertEqual(moved["project_key"], "working/manual-project")
        self.assertTrue(os.path.isdir(moved["abs_path"]))


def use_temp_locations(test, app):
    """置き場（developing / working と project_root）を一時フォルダへ差し替える。

    設定ファイルは機械ごとで追跡しないので、この機械の置き場に頼らずに試す
    （clone したばかりの機械では置き場が未設定＝空のため）。
    """
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    developing = os.path.join(tmp.name, "developing")
    working = os.path.join(tmp.name, "working")
    os.makedirs(os.path.join(developing, "claude-web-app", pm.PROJECTS_SUBDIR))
    os.makedirs(working)
    original = (app.DEVELOPING_DIR, app.WORKING_DIR, app.get_project_root_key)
    app.DEVELOPING_DIR, app.WORKING_DIR = developing, working
    app.get_project_root_key = lambda: "developing/claude-web-app"

    def restore():
        app.DEVELOPING_DIR, app.WORKING_DIR, app.get_project_root_key = original

    test.addCleanup(restore)


class TestProjectLocations(unittest.TestCase):
    """置き場（temp / developing / working）の解決と保護判定を検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        use_temp_locations(self, self.app)

    def test_developingとworkingは直下が置き場になる(self):
        info = self.app.resolve_location("developing")
        self.assertEqual(info["dir"], self.app.DEVELOPING_DIR)
        self.assertEqual(info["key_prefix"], "developing")

        info = self.app.resolve_location("working")
        self.assertEqual(info["dir"], self.app.WORKING_DIR)
        self.assertEqual(info["key_prefix"], "working")

    def test_一時フォルダはproject_root配下のprojectsになる(self):
        info = self.app.resolve_location("temp")
        root_key = self.app.get_project_root_key()
        self.assertEqual(info["key_prefix"], f"{root_key}/{pm.PROJECTS_SUBDIR}")
        self.assertEqual(info["parent_key"], root_key)
        self.assertTrue(info["dir"].endswith(pm.PROJECTS_SUBDIR))

    def test_未知の置き場はNoneを返す(self):
        self.assertIsNone(self.app.resolve_location("archive"))
        self.assertIsNone(self.app.resolve_location(""))
        self.assertIsNone(self.app.resolve_location(None))

    def test_作成できる置き場は一時フォルダとdevelopingのみ(self):
        labels = {
            loc["id"]: loc["can_create"] for loc in self.app.get_location_labels()
        }
        self.assertTrue(labels["temp"])
        self.assertTrue(labels["developing"])
        self.assertFalse(labels["working"])

    def test_本アプリ自身を含むフォルダは移動できない(self):
        # 自分自身、およびその親は移動対象にしてはいけない
        self.assertTrue(self.app.is_protected_path(self.app.BASE_DIR))
        self.assertTrue(self.app.is_protected_path(os.path.dirname(self.app.BASE_DIR)))
        # 無関係なフォルダは移動してよい
        self.assertFalse(
            self.app.is_protected_path(
                os.path.join(self.app.DEVELOPING_DIR, "some-other-project")
            )
        )


class TestSessionEnvironment(unittest.TestCase):
    """切り離し起動時の環境変数の扱いを検証する。"""

    def test_Claude由来のマーカーが取り除かれる(self):
        # これらを引き継ぐと「子セッション」と判定され、記録もブリッジも止まる
        env = {
            "PATH": "C:/bin",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_BRIDGE_SESSION_ID": "session_xxx",
            "USERNAME": "alice",
        }
        cleaned = sm.sanitize_env(env)
        self.assertEqual(cleaned, {"PATH": "C:/bin", "USERNAME": "alice"})


class TestTrustRegistration(unittest.TestCase):
    """フォルダの信頼登録（~/.claude.json）を一時ファイル上で検証する。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_trust_")
        self.config_path = os.path.join(self.temp_dir, "claude.json")
        self.original_config = sm.CLAUDE_CONFIG
        self.original_backup = sm.CLAUDE_CONFIG_BACKUP
        sm.CLAUDE_CONFIG = self.config_path
        sm.CLAUDE_CONFIG_BACKUP = self.config_path + ".bak"

        self.project_dir = os.path.join(self.temp_dir, "sample-project")
        os.makedirs(self.project_dir)

    def tearDown(self):
        sm.CLAUDE_CONFIG = self.original_config
        sm.CLAUDE_CONFIG_BACKUP = self.original_backup
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_キーはスラッシュ区切りの絶対パスになる(self):
        key = sm.config_key_for(r"F:\SharedDrive0\develop\developing\sample")
        self.assertEqual(key, "F:/SharedDrive0/develop/developing/sample")

    def test_未登録のフォルダを信頼済みにできる(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"projects": {}, "numStartups": 42}, f)

        self.assertFalse(sm.is_trusted(self.project_dir))
        self.assertTrue(sm.ensure_trusted(self.project_dir))
        self.assertTrue(sm.is_trusted(self.project_dir))

        # 既存の内容を壊していないこと
        with open(self.config_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["numStartups"], 42)

    def test_既に信頼済みなら書き込まない(self):
        sm.ensure_trusted(self.project_dir)
        before = os.path.getmtime(self.config_path)
        self.assertFalse(sm.ensure_trusted(self.project_dir))
        self.assertEqual(before, os.path.getmtime(self.config_path))

    def test_書き換え前に控えが作られる(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"projects": {}}, f)
        sm.ensure_trusted(self.project_dir)
        self.assertTrue(os.path.isfile(sm.CLAUDE_CONFIG_BACKUP))


class TestSessionListing(unittest.TestCase):
    """稼働中セッションの判定と一覧を検証する。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cwa_sessions_")
        self.original_dir = sm.SESSIONS_DIR
        sm.SESSIONS_DIR = self.temp_dir

    def tearDown(self):
        sm.SESSIONS_DIR = self.original_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write(self, name, data):
        with open(os.path.join(self.temp_dir, name), "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_存在しないpidのセッションは生存扱いしない(self):
        self.assertFalse(sm.is_session_alive({"pid": 999999999}))

    def test_claude以外のプロセスは生存扱いしない(self):
        # 自分自身（python）の pid を渡しても claude ではないので False
        self.assertFalse(sm.is_session_alive({"pid": os.getpid()}))

    def test_終了済みのセッション記録は一覧に出ない(self):
        self._write(
            "999999999.json",
            {"pid": 999999999, "name": "dead-session", "cwd": "C:/tmp"},
        )
        self.assertEqual(sm.list_sessions(), [])

    def test_壊れた記録があっても一覧は落ちない(self):
        with open(
            os.path.join(self.temp_dir, "broken.json"), "w", encoding="utf-8"
        ) as f:
            f.write("{ this is not json")
        self.assertEqual(sm.list_sessions(), [])

    def test_稼働していないpidの停止要求はエラーになる(self):
        with self.assertRaises(sm.SessionError):
            sm.stop_session(999999999)


class TestSessionProjectMapping(unittest.TestCase):
    """セッションの作業ディレクトリからプロジェクトキーを逆引きできるかを検証する。"""

    @classmethod
    def setUpClass(cls):
        cls.app = load_app_module()

    def setUp(self):
        use_temp_locations(self, self.app)

    def test_developing配下はプロジェクトキーに変換される(self):
        path = os.path.join(self.app.DEVELOPING_DIR, "claude-web-app")
        self.assertEqual(
            self.app.project_key_for_path(path), "developing/claude-web-app"
        )

    def test_サブプロジェクトも階層付きで変換される(self):
        path = os.path.join(
            self.app.DEVELOPING_DIR, "claude-web-app", "projects", "invoice-ocr"
        )
        self.assertEqual(
            self.app.project_key_for_path(path),
            "developing/claude-web-app/projects/invoice-ocr",
        )

    def test_管理対象外のフォルダはNoneになる(self):
        # PCのターミナル等で起動されたセッションは一覧には出すが、キーは付かない
        self.assertIsNone(self.app.project_key_for_path(r"C:\Windows\Temp"))
        self.assertIsNone(self.app.project_key_for_path(""))


class _FakeResponse:
    """urlopen の戻り値の代わり（with 文と read() だけを持つ）。"""

    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class TestGenerateProjectNamePayload(unittest.TestCase):
    """ollama へ送る命名の要求の中身を検証する（実際には送らない）。"""

    def _capture(self, settings):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["payload"] = json.loads(request.data.decode("utf-8"))
            return _FakeResponse({"response": "sample-web-tool"})

        with mock.patch.object(pm.urllib.request, "urlopen", fake_urlopen):
            name, err = pm.generate_project_name("定価で買う手段を探す", settings)
        return sent["payload"], name, err

    def test_default_model_is_qwen35_9b(self):
        payload, name, err = self._capture({})
        self.assertEqual(payload["model"], "qwen3.5:9b")
        self.assertEqual((name, err), ("sample-web-tool", None))

    def test_thinking_is_disabled(self):
        # Qwen3.5 は思考が既定で有効。num_predict 32 を思考で使い切らせないため切る。
        # 思考に対応しないモデルでも false を送る（ollama 0.40.5 は 200 を返すことを実測。
        # 400 になるのは true のときだけ）
        payload, _, _ = self._capture({"ollama_model": "qwen2.5-coder:7b"})
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["model"], "qwen2.5-coder:7b")

    def test_model_not_found_is_not_reported_as_connection_error(self):
        def fake_urlopen(request, timeout=None):
            raise pm.urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(b'{"error":"model \\"qwen3.5:9b\\" not found"}'),
            )

        with mock.patch.object(pm.urllib.request, "urlopen", fake_urlopen):
            name, err = pm.generate_project_name("命名の材料", {})
        self.assertTrue(name.startswith("project-"))
        self.assertIn("HTTP 404", err)
        self.assertIn('model "qwen3.5:9b" not found', err)
        self.assertNotIn("接続できませんでした", err)

    def test_app_default_setting_matches(self):
        app = load_app_module()
        self.assertEqual(app.DEFAULT_SETTINGS["ollama_model"], "qwen3.5:9b")


if __name__ == "__main__":
    unittest.main(verbosity=2)

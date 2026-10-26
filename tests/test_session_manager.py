# -*- coding: utf-8 -*-
"""セッション起動の「切り離し」（session_manager._spawn_detached）のユニットテスト。

守りたい性質はただ一つ:
**本アプリを `taskkill /F /T`（プロセスツリー全体）で殺しても、
起動済みの claude セッションが巻き添えにならないこと**。
2026-08-21 に、直接 Popen していたことが原因で稼働中セッション5本を
失う実障害が起きたため、ここで実挙動（本物の taskkill）を固定する。

実行方法:
    <python> -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

# テスト対象をインポートできるようプロジェクトルートをパスへ追加する
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import psutil

import session_input_sender
import session_manager as sm

# claude の代役。テストでは本物を起こさず、ただ眠るだけの python を使う
SLEEPER = [sys.executable, "-c", "import time; time.sleep(120)"]


def _kill_quietly(pid):
    """後片付け用。すでに死んでいてもエラーにしない。"""
    subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False
    )


class TestSpawnDetached(unittest.TestCase):
    """_spawn_detached が親子の鎖を本当に切っているかを実挙動で検証する。"""

    def setUp(self):
        self._cleanup_pids = []

    def tearDown(self):
        for pid in self._cleanup_pids:
            _kill_quietly(pid)

    def test_起動したプロセスが生きていてpidが正しい(self):
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        proc = psutil.Process(pid)
        self.assertIn("python", proc.name().lower())

    def test_起動直後から親の鎖が切れている(self):
        """ランチャーは pid 報告と同時に終了するため、返ってきた時点で
        起動プロセスの親（ランチャー）は死んでいなければならない。"""
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        proc = psutil.Process(pid)
        ppid = proc.ppid()
        # 親は自分（テストプロセス）ではなく、かつ既に消滅していること
        self.assertNotEqual(ppid, os.getpid())
        self.assertIsNone(proc.parent(), "親（ランチャー）が生き残っている")

    def test_疑似アプリをツリーキルしてもセッション役は生き残る(self):
        """事故の再現実験。_spawn_detached を使う疑似アプリを起こし、
        server_manager と同じ `taskkill /F /T` で殺して、
        起動されたセッション役が生き残ることを確かめる。"""
        app_code = (
            "import sys; sys.path.insert(0, {root!r})\n"
            "import session_manager as sm\n"
            "import sys, time\n"
            "pid = sm._spawn_detached("
            "[sys.executable, '-c', 'import time; time.sleep(120)'],"
            " {cwd!r}, sm.sanitize_env())\n"
            "print(pid, flush=True)\n"
            "time.sleep(120)\n"  # アプリは生かしたままにしてツリーキルを受ける
        ).format(root=PROJECT_ROOT, cwd=os.getcwd())
        app = subprocess.Popen(
            [sys.executable, "-c", app_code],
            stdout=subprocess.PIPE,
            text=True,
        )
        self._cleanup_pids.append(app.pid)
        session_pid = int(app.stdout.readline().strip())
        self._cleanup_pids.append(session_pid)

        # server_manager の停止処理と同じ形でアプリをツリーキルする
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(app.pid)],
            capture_output=True,
            check=False,
        )
        time.sleep(1)

        self.assertFalse(psutil.pid_exists(app.pid), "疑似アプリが死んでいない")
        self.assertTrue(
            psutil.pid_exists(session_pid),
            "セッション役がツリーキルの巻き添えで死んだ（切り離し失敗）",
        )

    def test_環境変数がランチャー越しに引き継がれる(self):
        """sanitize_env の結果がそのまま起動先に届くこと（CLAUDE* は落ちる）。"""
        marker_path = os.path.join(
            os.environ.get("TEMP", os.getcwd()), "spawn_detached_env_test.txt"
        )
        if os.path.exists(marker_path):
            os.remove(marker_path)
        env = sm.sanitize_env()
        env["SPAWN_TEST_MARKER"] = "spawn-ok"
        env["CLAUDE_CODE_CHILD_SESSION"] = "must-be-removed"
        env = sm.sanitize_env(env)  # CLAUDE* を確実に落とす
        writer = (
            "import os; open({p!r}, 'w', encoding='utf-8').write("
            "os.environ.get('SPAWN_TEST_MARKER','') + '/' + "
            "os.environ.get('CLAUDE_CODE_CHILD_SESSION','(none)'))"
        ).format(p=marker_path)
        pid = sm._spawn_detached([sys.executable, "-c", writer], os.getcwd(), env)
        self._cleanup_pids.append(pid)
        for _ in range(50):  # 最大5秒待つ
            if os.path.exists(marker_path):
                break
            time.sleep(0.1)
        with open(marker_path, "r", encoding="utf-8") as f:
            content = f.read()
        os.remove(marker_path)
        self.assertEqual(content, "spawn-ok/(none)")

    def test_ランチャー異常時はSessionErrorになる(self):
        """存在しない実行ファイルを渡すと、例外が SessionError へ翻訳される。"""
        with self.assertRaises(sm.SessionError):
            sm._spawn_detached(
                ["Z:/no/such/binary.exe"], os.getcwd(), sm.sanitize_env()
            )


class TestBuildLaunchArgs(unittest.TestCase):
    """起動オプション（モデル・権限モード・思考の深さ）の検証と引数化。"""

    def test_empty_options_add_nothing(self):
        """何も選ばなければ引数を足さない（settings.json に従わせる）。"""
        self.assertEqual(sm.build_launch_args(), [])
        self.assertEqual(sm.build_launch_args("", "", ""), [])

    def test_valid_options_become_flags(self):
        self.assertEqual(
            sm.build_launch_args("opus[1m]", "bypassPermissions", "max"),
            [
                "--model",
                "opus[1m]",
                "--permission-mode",
                "bypassPermissions",
                "--effort",
                "max",
            ],
        )

    def test_unknown_values_are_rejected(self):
        """選択肢に無い値（引数の注入を含む）は SessionError で拒否する。"""
        for kwargs in (
            {"model": "--dangerously-skip-permissions"},
            {"permission_mode": "yolo"},
            {"effort": "ultra"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(sm.SessionError):
                    sm.build_launch_args(**kwargs)

    def test_every_permission_mode_is_accepted_by_claude(self):
        """画面の選択肢が、実物の `claude --help` に載っているモードと一致すること。
        （claude 側で名前が変わったときに気づくための検査）"""
        try:
            exe = sm.resolve_claude_exe()
        except sm.SessionError:
            self.skipTest("claude が見つからない環境")
        completed = subprocess.run(
            [exe, "--help"], capture_output=True, text=True, encoding="utf-8"
        )
        help_text = " ".join(completed.stdout.split())
        for value, _ in sm.PERMISSION_MODE_CHOICES:
            if value:
                with self.subTest(mode=value):
                    self.assertIn(f'"{value}"', help_text)


class TestLaunchSessionArgv(unittest.TestCase):
    """launch_session が組み立てるコマンドライン（実際の起動はしない）。"""

    def setUp(self):
        self.captured = {}
        self._originals = (
            sm._spawn_detached,
            sm._spawn_via_task,
            sm.ensure_trusted,
            sm._spawn_restricted,
        )
        sm._spawn_detached = lambda argv, cwd, env: self._capture("direct", argv)
        # 昇格したまま試験を走らせても、本物の起動をしないよう SAFER の経路も代役にする
        sm._spawn_restricted = lambda argv, cwd: self._capture("restricted", argv)
        sm._spawn_via_task = lambda argv, cwd, target, run_level="LeastPrivilege": (
            self._capture(target, argv, run_level)
        )
        sm.ensure_trusted = lambda path: False

    def tearDown(self):
        (
            sm._spawn_detached,
            sm._spawn_via_task,
            sm.ensure_trusted,
            sm._spawn_restricted,
        ) = self._originals

    def _capture(self, route, argv, run_level=None):
        self.captured = {"route": route, "argv": argv, "run_level": run_level}
        return os.getpid()

    def test_elevation_goes_through_task_with_run_level(self):
        """本アプリと違う権限を指定したら、同じセッションでもタスク経由で RunLevel を決める。
        本アプリと同じ権限なら、タスク（セッション0では S4U）を通さず二段起動にする。"""
        current = sm.process_win_session(os.getpid())
        same = "0" if current == 0 else "1"
        own_admin = sm.process_elevation(os.getpid()) is True
        for elevation, level in (
            ("admin", "HighestAvailable"),
            ("standard", "LeastPrivilege"),
        ):
            with self.subTest(elevation=elevation):
                result = sm.launch_session(
                    os.getcwd(), claude_exe=sys.executable, elevation=elevation
                )
                if (elevation == "admin") == own_admin:
                    self.assertEqual(self.captured["route"], "direct")
                elif elevation == "standard":
                    # 昇格した本アプリから通常へは SAFER で絞ったトークン
                    self.assertEqual(self.captured["route"], "restricted")
                else:
                    self.assertEqual(self.captured["route"], same)
                    self.assertEqual(self.captured["run_level"], level)
                self.assertIn(result["elevated"], (True, False))

    def test_standard_from_elevated_app_uses_restricted_token(self):
        """昇格した本アプリから同じセッションへ「通常」は、SAFER で絞ったトークンで起こす。"""
        originals = (sm.process_elevation, sm._spawn_restricted)
        sm.process_elevation = lambda pid: True
        sm._spawn_restricted = lambda argv, cwd: self._capture("restricted", argv)
        try:
            sm.launch_session(
                os.getcwd(), claude_exe=sys.executable, elevation="standard"
            )
            self.assertEqual(self.captured["route"], "restricted")
            sm.launch_session(os.getcwd(), claude_exe=sys.executable, elevation="admin")
            self.assertEqual(self.captured["route"], "direct")
        finally:
            sm.process_elevation, sm._spawn_restricted = originals

    def test_elevation_to_other_session_uses_task(self):
        other = "1" if sm.process_win_session(os.getpid()) == 0 else "0"
        sm.launch_session(
            os.getcwd(), claude_exe=sys.executable, win_session=other, elevation="admin"
        )
        self.assertEqual(self.captured["route"], other)
        self.assertEqual(self.captured["run_level"], "HighestAvailable")

    def test_default_elevation_keeps_direct_spawn(self):
        """権限を指定しなければ、従来どおり（同じセッションなら二段起動）。"""
        sm.launch_session(os.getcwd(), claude_exe=sys.executable)
        self.assertEqual(self.captured["route"], "direct")

    def test_invalid_elevation_is_refused(self):
        with self.assertRaises(sm.SessionError):
            sm.launch_session(os.getcwd(), claude_exe=sys.executable, elevation="root")
        self.assertEqual(self.captured, {})

    def test_resume_uses_resume_flag_without_name(self):
        """再開は --resume で起こし、-n で元の題名を上書きしない。"""
        original = sm.validate_resume_target
        sm.validate_resume_target = lambda cwd, session_id: None
        try:
            sid = "0b9426d2-85af-4af0-843f-7d1f0c30432a"
            result = sm.launch_session(
                os.getcwd(),
                name="demo",
                prompt="続きをお願いします",
                claude_exe=sys.executable,
                model="sonnet",
                resume_session_id=sid,
            )
        finally:
            sm.validate_resume_target = original
        self.assertEqual(
            self.captured["argv"][1:],
            ["--resume", sid, "--model", "sonnet", "--", "続きをお願いします"],
        )
        self.assertEqual(result["resumed"], sid)

    def test_invalid_resume_target_fails_before_trusting_folder(self):
        called = []
        sm.ensure_trusted = lambda path: called.append(path)
        with self.assertRaises(sm.SessionError):
            sm.launch_session(
                os.getcwd(), claude_exe=sys.executable, resume_session_id="../x"
            )
        self.assertEqual(called, [])

    def test_options_and_prompt_are_placed_in_order(self):
        """オプションは名前の後、指示は `--` の後ろ（`-` 始まりの指示を引数と誤認させない）。"""
        sm.launch_session(
            os.getcwd(),
            name="demo",
            prompt="-v を付けて実行して",
            claude_exe=sys.executable,
            model="sonnet",
            permission_mode="plan",
        )
        self.assertEqual(
            self.captured["argv"][1:],
            [
                "-n",
                "demo",
                "--model",
                "sonnet",
                "--permission-mode",
                "plan",
                "--",
                "-v を付けて実行して",
            ],
        )

    def test_same_session_uses_direct_spawn(self):
        """本アプリと同じセッションを指定したら、タスクを経由しない。"""
        current = str(sm.process_win_session(os.getpid()))
        sm.launch_session(os.getcwd(), claude_exe=sys.executable, win_session=current)
        self.assertEqual(self.captured["route"], "direct")

    def test_other_session_uses_task(self):
        other = "1" if sm.process_win_session(os.getpid()) == 0 else "0"
        sm.launch_session(os.getcwd(), claude_exe=sys.executable, win_session=other)
        self.assertEqual(self.captured["route"], other)

    def test_invalid_option_fails_before_trusting_folder(self):
        """不正な指定では、フォルダの信頼登録（設定ファイルの書き換え）をしない。"""
        called = []
        sm.ensure_trusted = lambda path: called.append(path)
        for kwargs in ({"model": "gpt-5"}, {"win_session": "2"}):
            with self.subTest(**kwargs):
                with self.assertRaises(sm.SessionError):
                    sm.launch_session(os.getcwd(), claude_exe=sys.executable, **kwargs)
        self.assertEqual(called, [])


class TestElevation(unittest.TestCase):
    """権限（昇格）の読み取りとタスク定義の RunLevel。"""

    def test_own_process_elevation_is_readable(self):
        self.assertIn(sm.process_elevation(os.getpid()), (True, False))

    def test_missing_process_is_none(self):
        self.assertIsNone(sm.process_elevation(999999))
        self.assertIsNone(sm.process_elevation("abc"))

    def test_task_xml_carries_run_level(self):
        for level in ("LeastPrivilege", "HighestAvailable"):
            with self.subTest(level=level):
                xml = sm._build_task_xml("1", "python.exe", '"a" "b"', level)
                self.assertIn(f"<RunLevel>{level}</RunLevel>", xml)
        with self.assertRaises(sm.SessionError):
            sm._build_task_xml("1", "python.exe", "", "SYSTEM")


class TestResumable(unittest.TestCase):
    """過去の会話の一覧（resume の候補）と、--resume に渡す前の検証。"""

    CWD = r"F:\SharedDrive0\develop\developing\demo_app"

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.originals = (sm.CLAUDE_HOME, sm.list_sessions)
        sm.CLAUDE_HOME = self.tmp.name
        self.live = []
        sm.list_sessions = lambda: [{"session_id": s, "pid": 1} for s in self.live]
        self.folder = sm.transcript_dir_for(self.CWD)
        os.makedirs(self.folder)

    def tearDown(self):
        sm.CLAUDE_HOME, sm.list_sessions = self.originals
        self.tmp.cleanup()

    def _write(self, sid, records, mtime=None):
        path = os.path.join(self.folder, f"{sid}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def _user(self, text, cwd=None, meta=False):
        record = {
            "type": "user",
            "cwd": cwd or self.CWD,
            "message": {"role": "user", "content": text},
        }
        if meta:
            record["isMeta"] = True
        return record

    def test_folder_name_replaces_non_alphanumerics(self):
        self.assertEqual(
            os.path.basename(sm.transcript_dir_for(self.CWD)),
            "F--SharedDrive0-develop-developing-demo-app",
        )

    def test_list_is_newest_first_with_title_and_live_flag(self):
        a = "11111111-1111-1111-1111-111111111111"
        b = "22222222-2222-2222-2222-222222222222"
        self._write(
            a,
            [
                {"type": "custom-title", "customTitle": "古い題"},
                self._user("<command-name>/clear</command-name>", meta=True),
                self._user("最初の指示A"),
                {"type": "custom-title", "customTitle": "新しい題"},
            ],
            mtime=1000,
        )
        self._write(b, [self._user("最初の指示B")], mtime=2000)
        self.live = [b]
        items = sm.list_resumable(self.CWD)
        self.assertEqual([i["session_id"] for i in items], [b, a])
        self.assertEqual(items[1]["title"], "新しい題")
        self.assertEqual(items[1]["first_prompt"], "最初の指示A")
        self.assertTrue(items[0]["live"])
        self.assertFalse(items[1]["live"])

    def test_conversation_started_by_other_session_is_listed(self):
        """/clear の直後に他のセッションからの指示で始まる会話も出し、その本文を見せる。"""
        sid = "77777777-7777-7777-7777-777777777777"
        relayed = (
            "Another Claude session sent a message:\n"
            '<cross-session-message from="uds:x" from-name="管理役" from-mode="bypass">\n'
            "README を直して\n</cross-session-message>"
        )
        self._write(
            sid,
            [
                self._user("<command-name>/clear</command-name>", meta=True),
                self._user(relayed, meta=True),
                {"type": "assistant", "message": {"content": []}},
            ],
        )
        items = sm.list_resumable(self.CWD)
        self.assertEqual([i["session_id"] for i in items], [sid])
        self.assertEqual(items[0]["first_prompt"], "（管理役から）README を直して")

    def test_empty_and_foreign_conversations_are_hidden(self):
        """指示の無い会話と、名前が衝突した別フォルダの会話は出さない。"""
        self._write("33333333-3333-3333-3333-333333333333", [{"type": "custom-title"}])
        self._write(
            "44444444-4444-4444-4444-444444444444",
            [
                self._user(
                    "別の場所", cwd=r"F:\SharedDrive0\develop\developing\demo-app"
                )
            ],
        )
        self._write("not-a-session", [self._user("名前が違う")])
        self.assertEqual(sm.list_resumable(self.CWD), [])

    def test_hidden_conversations_are_left_out(self):
        """試験で起こした会話などは、外す記録に足すと一覧に出ない（記録そのものは消さない）。"""
        original_state = sm.APP_STATE_DIR
        sm.APP_STATE_DIR = os.path.join(self.tmp.name, "state")
        try:
            sid = "88888888-8888-8888-8888-888888888888"
            path = self._write(sid, [self._user("試験の指示")])
            self.assertEqual(len(sm.list_resumable(self.CWD)), 1)
            self.assertTrue(sm.hide_resumable(self.CWD, sid))
            self.assertFalse(sm.hide_resumable(self.CWD, sid))
            self.assertEqual(sm.list_resumable(self.CWD), [])
            self.assertTrue(os.path.exists(path))
            for bad in ("../x", "99999999-9999-9999-9999-999999999999", None):
                with self.assertRaises(sm.SessionError):
                    sm.hide_resumable(self.CWD, bad)
        finally:
            sm.APP_STATE_DIR = original_state

    def test_validate_resume_target(self):
        sid = "55555555-5555-5555-5555-555555555555"
        self._write(sid, [self._user("指示")])
        sm.validate_resume_target(self.CWD, sid)  # 通る
        for bad in ("../../x", "55555555-5555-5555-5555-55555555555Z", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(sm.SessionError):
                    sm.validate_resume_target(self.CWD, bad)
        with self.assertRaises(sm.SessionError):  # 記録が無い
            sm.validate_resume_target(self.CWD, "66666666-6666-6666-6666-666666666666")
        self.live = [sid]
        with self.assertRaises(sm.SessionError):  # いま動いている
            sm.validate_resume_target(self.CWD, sid)


class TestSpawnRestricted(unittest.TestCase):
    """SAFER の「通常の利用者」トークンで起こせるかを実挙動で検証する（眠るだけの python）。"""

    def test_launches_non_elevated_detached_process(self):
        # 本番の本アプリと同じ置き場なので、試験の前後の差分で後片付けを見る
        before = (
            set(os.listdir(sm.TASK_LAUNCH_DIR))
            if os.path.isdir(sm.TASK_LAUNCH_DIR)
            else set()
        )
        pid = sm._spawn_restricted(SLEEPER, os.getcwd())
        try:
            proc = psutil.Process(pid)
            self.assertIn("python", proc.name().lower())
            self.assertEqual(
                sm.process_win_session(pid), sm.process_win_session(os.getpid())
            )
            self.assertIs(sm.process_elevation(pid), False)
            # 起動役は報告と同時に終了し、親子の鎖は切れていること
            self.assertIsNone(proc.parent(), "起動役が生き残っている")
        finally:
            _kill_quietly(pid)
        self.assertEqual(set(os.listdir(sm.TASK_LAUNCH_DIR)) - before, set())

    def test_standard_to_other_session0_is_refused(self):
        """別のセッション0へ「通常」は S4U で昇格したままになるので、起こさずに断る。"""
        if sm.process_win_session(os.getpid()) == 0:
            self.skipTest(
                "このプロセスがセッション0にいるので、この組み合わせにならない"
            )
        # 断る処理が将来壊れても、本物のタスクを登録しないよう代役にしておく
        called = []
        original = sm._spawn_via_task
        sm._spawn_via_task = lambda *args, **kwargs: called.append(args) or os.getpid()
        try:
            with self.assertRaises(sm.SessionError):
                sm.launch_session(
                    os.getcwd(),
                    claude_exe=sys.executable,
                    win_session="0",
                    elevation="standard",
                    trust=False,
                )
        finally:
            sm._spawn_via_task = original
        self.assertEqual(called, [])


class TestSpawnViaTask(unittest.TestCase):
    """使い捨てタスク経由で、指定の Windows セッションに起動できるかを実挙動で検証する。
    （セッション1はデスクトップにログオンしている必要がある）"""

    def setUp(self):
        self._cleanup_pids = []

    def tearDown(self):
        for pid in self._cleanup_pids:
            _kill_quietly(pid)

    def _assert_launched_in(self, target):
        pid = sm._spawn_via_task(SLEEPER, os.getcwd(), target)
        self._cleanup_pids.append(pid)
        proc = psutil.Process(pid)
        self.assertIn("python", proc.name().lower())
        actual = sm.process_win_session(pid)
        if target == "0":
            self.assertEqual(actual, 0)
        else:
            self.assertNotEqual(actual, 0)
        # 起動役は報告と同時に終了し、親子の鎖は切れていること
        self.assertIsNone(proc.parent(), "起動役が生き残っている")

    @unittest.skipUnless(
        sm.process_elevation(os.getpid()),
        "セッション0（S4U）のタスクの登録には管理者権限が要る"
        "（2026-10-11 実測: 通常権限からはアクセス拒否。本番の本アプリは昇格して動く）",
    )
    def test_launch_into_session0(self):
        self._assert_launched_in("0")

    def test_launch_into_session1(self):
        self._assert_launched_in("1")

    def test_temporary_tasks_and_files_are_removed(self):
        """起動に使ったタスクと要求・応答ファイルは残さない。"""
        pid = sm._spawn_via_task(SLEEPER, os.getcwd(), "1")
        self._cleanup_pids.append(pid)
        listing = subprocess.run(
            ["schtasks", "/query", "/fo", "csv", "/nh"],
            capture_output=True,
        ).stdout.decode("cp932", "replace")
        self.assertNotIn("claude-web-app-launch-", listing)
        self.assertEqual(os.listdir(sm.TASK_LAUNCH_DIR), [])

    def test_launch_failure_is_reported(self):
        """存在しない実行ファイルは、時間切れを待たずに SessionError になる。"""
        started = time.time()
        with self.assertRaises(sm.SessionError):
            sm._spawn_via_task(["Z:/no/such/binary.exe"], os.getcwd(), "1")
        self.assertLess(time.time() - started, 10)


class TestNormalizeSendText(unittest.TestCase):
    """送る本文の整形と検証（/clear 以外のコマンドを本文として送らせない）。"""

    def test_newlines_become_single_line(self):
        """Enter で送信されるため、改行は空白にして1行にする。"""
        self.assertEqual(
            sm.normalize_send_text("  一行目\n\n 二行目\r\n三行目 "),
            "一行目 二行目 三行目",
        )

    def test_empty_text_is_allowed(self):
        self.assertEqual(sm.normalize_send_text(None), "")
        self.assertEqual(sm.normalize_send_text(" \n "), "")

    def test_command_prefixes_are_rejected(self):
        for text in ("/model opus", "!rm -rf x", "# メモ", "\n  /clear"):
            with self.subTest(text=text):
                with self.assertRaises(sm.SessionError):
                    sm.normalize_send_text(text)

    def test_too_long_text_is_rejected(self):
        with self.assertRaises(sm.SessionError):
            sm.normalize_send_text("あ" * (sm.SEND_TEXT_MAX_LEN + 1))


class TestInputAreaState(unittest.TestCase):
    """画面から入力欄の状態を判定する（行は 2026-09-30 の実測画面から採った）。"""

    BORDER = "─" * 40
    STATUS = "  ⏸ plan mode on (shift+tab to cycle)"

    def setUp(self):
        import session_input_sender

        self.state = session_input_sender.input_area_state

    def test_placeholder_means_empty(self):
        lines = ["", '❯\xa0Try "fix typecheck errors"', self.BORDER, self.STATUS]
        self.assertEqual(self.state(lines), "empty")

    def test_bare_prompt_means_empty(self):
        self.assertEqual(self.state(["❯\xa0", self.BORDER]), "empty")

    def test_draft_on_prompt_line(self):
        self.assertEqual(self.state(["❯\xa0abc draft", self.BORDER]), "draft")

    def test_wrapped_or_multiline_draft(self):
        """1行目が消えても、字下げした続きの行が残っていれば書きかけ。"""
        self.assertEqual(
            self.state(['❯\xa0Try "x"', "  second line", self.BORDER]), "draft"
        )

    def test_ascii_prompt_on_desktop_session(self):
        """デスクトップ(S1)では入力欄の記号が ASCII の「>」で描かれる（実測）。"""
        lines = [
            "─" * 20 + " send-probe-1 ─",
            '> Try "fix lint errors"',
            self.BORDER,
            "  ⏸ plan mode on (shift+tab to cycle) · ←for agents",
        ]
        self.assertEqual(self.state(lines), "empty")
        self.assertEqual(self.state(["> abc draft", self.BORDER]), "draft")

    def test_quote_line_without_border_is_not_input_area(self):
        """会話中の引用（> で始まる行）を入力欄と取り違えない。"""
        lines = ["Do you want to proceed?", "> quoted text", "", "1. Yes  2. No"]
        self.assertEqual(self.state(lines), "missing")

    def test_no_prompt_means_missing(self):
        """入力欄が見えない（ダイアログ等）ときは打たない。"""
        self.assertEqual(self.state(["Do you trust this folder?", "1. Yes"]), "missing")

    def test_last_prompt_line_is_used(self):
        """過去の発言も ❯ で始まるため、最後の ❯ 行を入力欄とみなす。"""
        lines = ["❯\xa0Reply with just OK", "● OK", '❯\xa0Try "x"', self.BORDER]
        self.assertEqual(self.state(lines), "empty")

    # 帯が出て描画が1行ずれ、下の罫線が消えた画面（2026-10-14 の実測）
    SHIFTED = [
        "●Update(docs\\daily.md)",
        "メモなし" + " " * 80 + "メモを編集  ファイル [-]",
        "──",
        ">\xa0" + "─" * 60 + " demo-app-session1 ─",
        "{prompt}",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents",
    ]

    def shifted(self, prompt):
        return [line.replace("{prompt}", prompt) for line in self.SHIFTED]

    def test_shifted_screen_without_bottom_border_is_empty(self):
        self.assertEqual(self.state(self.shifted(">")), "empty")
        self.assertEqual(self.state(self.shifted("❯\xa0")), "empty")

    def test_shifted_screen_draft_is_draft(self):
        import session_input_sender

        lines = self.shifted("> abc draft")
        self.assertEqual(session_input_sender.input_area(lines), ("draft", "abc draft"))

    def test_shifted_screen_status_line_is_not_draft(self):
        """状態行（字下げした行）を書きかけの続きと取り違えない。"""
        import session_input_sender

        self.assertEqual(
            session_input_sender.input_area(self.shifted(">")), ("empty", "")
        )

    def test_quote_under_border_with_text_below_is_not_input_area(self):
        """罫線の下の引用でも、下に字下げの無い本文が続けば入力欄ではない。"""
        lines = [self.BORDER, "> quoted text", "1. Yes  2. No"]
        self.assertEqual(self.state(lines), "missing")

    def test_shifted_screen_multiline_draft_is_draft(self):
        """崩れた画面で1行目が空・案内文でも、続きの行があれば書きかけ（消さない）。"""
        for prompt in (">", '> Try "x"'):
            lines = self.shifted(prompt)
            lines.insert(-1, "  second line")
            self.assertEqual(self.state(lines), "draft", prompt)

    def test_border_without_mark_above_is_not_input_area(self):
        """記号の重ならない罫線の下の選択肢・候補メニュー・過去の発言は入力欄ではない。"""
        cases = [
            [self.BORDER, " ❯ 1. Yes", "   2. No", "  Esc to cancel"],
            [self.BORDER, "> /clear", "  /clear   Clear conversation", "  /compact"],
            ["  " + self.BORDER, "> old message", "  wrapped part", ""],
        ]
        for lines in cases:
            self.assertEqual(self.state(lines), "missing", lines[1])


SCREENS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "screens"
)


def load_screen(name):
    """実物の画面の写し（tests/fixtures/screens）を (行, カーソル, 期待する状態) で返す。"""
    with open(os.path.join(SCREENS_DIR, name + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    return data["lines"], tuple(data["cursor"]), data["expected_state"]


class TestInputAreaAtCursor(unittest.TestCase):
    """カーソルの所の入力欄だけを見る判定（mod の帯・ペインが何を描いても取り違えない）。"""

    def area(self, lines, cursor):
        return session_input_sender.input_area(lines, cursor)

    def test_real_screens(self):
        """2026-10-14 に写した実物（本物の子3本・試験用の子5本）を、期待どおりに読む。"""
        names = sorted(n[:-5] for n in os.listdir(SCREENS_DIR) if n.endswith(".json"))
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            with self.subTest(name=name):
                lines, cursor, expected = load_screen(name)
                self.assertEqual(self.area(lines, cursor)[0], expected)

    def test_real_screen_texts(self):
        lines, cursor, _ = load_screen("band_suggestion_demo")
        self.assertEqual(
            self.area(lines, cursor),
            ("draft", "pc-otherで別の端末で同じ手順を試してみた"),
        )
        lines, cursor, _ = load_screen("trial_wrapped_draft")
        state, text = self.area(lines, cursor)
        self.assertTrue(text.startswith("human draft line"))
        self.assertTrue(text.endswith("more words here"))

    def test_dialog_that_shape_scan_misread_is_missing(self):
        """/model のダイアログ: 上に残る「❯ /model」＋罫線を、形で探す従来の判定は書きかけと読んだ
        （そのまま送ると、提案とみなして本文と Enter を打ち、ダイアログの選択を確定させうる）。"""
        lines, cursor, _ = load_screen("trial_model_dialog")
        self.assertEqual(session_input_sender.input_area(lines), ("draft", "/model"))
        self.assertEqual(self.area(lines, cursor), ("missing", ""))

    def test_band_rows_above_do_not_matter(self):
        """帯（AbovePrompt）が何行増えても、カーソルの所の入力欄を読む。"""
        lines, (row, column), _ = load_screen("band_empty_demo")
        band = [i for i, line in enumerate(lines) if "メモを編集" in line][0]
        for extra in (0, 1, 3, 8):
            # 上の帯は記号と罫線の行（入力欄に似せた行）を交互に足す
            rows = ["> 帯の引用" if k % 2 == 0 else "─" * 40 for k in range(extra)]
            grown = lines[: band + 1] + rows + lines[band + 1 :]
            with self.subTest(extra=extra):
                self.assertEqual(self.area(grown, (row + extra, column)), ("empty", ""))

    def test_mod_rows_below_input_do_not_fool(self):
        """入力欄の下に mod が「罫線・> 引用・罫線」を描いても、カーソルの所の欄を読む。
        画面の下から形で探す従来の判定は、下の偽物を書きかけと読む（陽性対照）。"""
        lines, cursor, _ = load_screen("band_empty_demo")
        grown = lines + ["─" * 40, "> 下の mod の引用", "─" * 40]
        self.assertEqual(
            session_input_sender.input_area(grown), ("draft", "下の mod の引用")
        )
        self.assertEqual(self.area(grown, cursor), ("empty", ""))

    def test_indented_option_between_rules_is_missing(self):
        """罫線に挟まれたダイアログの選ばれた選択肢（字下げした ❯）にカーソルがあっても入力欄にしない。"""
        rule = "─" * 40
        for option in (" ❯ 1. Yes", "  ❯ 1. Yes"):
            lines = [rule, option, "   2. No", "  Esc to cancel", rule]
            with self.subTest(option=option):
                self.assertEqual(self.area(lines, (1, 1)), ("missing", ""))

    def test_cursor_on_rule_outside_box_is_missing(self):
        """下の罫線のある欄では、カーソルが罫線の上（欄の外）なら1行のずれを許さない。"""
        rule = "─" * 40
        lines = ["本文", rule, "> abc", rule, "  状態行"]
        self.assertEqual(self.area(lines, (2, 5)), ("draft", "abc"))
        for row in (1, 3):
            with self.subTest(row=row):
                self.assertEqual(self.area(lines, (row, 5)), ("missing", ""))

    def test_decoy_prompt_in_pane_is_not_used(self):
        """ペインに「罫線・> 引用・罫線」があっても、カーソルの無い所は入力欄にしない。"""
        lines, cursor, _ = load_screen("band_empty_demo")
        decoy = ["─" * 40, "> 引用されたメモ", "─" * 40]
        self.assertEqual(
            self.area(decoy + lines, (cursor[0] + 3, cursor[1])), ("empty", "")
        )
        # カーソルが入力欄の外（本文の中）にあれば、入力欄があっても打たない
        self.assertEqual(self.area(decoy + lines, (cursor[0] - 6, 0)), ("missing", ""))

    def test_pane_docked_beside_is_cut_off(self):
        """横に並んだペイン（幅の広い端末）の中身を、書きかけと取り違えない。"""
        # 実物の画面を左80桁に縮め、右にペイン（仕切り │）を並べた合成の画面。罫線は欄の幅（80桁）まで
        lines, cursor, _ = load_screen("band_empty_demo")
        beside = [
            ("─" * 80 if line.startswith("─") else line[:80].ljust(80))
            + " │ ペインの行 > abc ───"
            for line in lines
        ]
        self.assertEqual(self.area(beside, cursor), ("empty", ""))
        typed = list(beside)
        # 全角7字＝14桁。「> 」と合わせて16桁、残り64桁を空白で埋めて80桁
        typed[cursor[0]] = "> 本当の書きかけ" + " " * 64 + " │ ペインの行"
        self.assertEqual(self.area(typed, cursor), ("draft", "本当の書きかけ"))

    def test_shifted_screen_with_cursor_on_or_below_prompt(self):
        """崩れた画面（下の罫線が消える・2026-10-14 実測の形）で、カーソルが記号の行か1行下にあっても読む。"""
        shifted = TestInputAreaState.SHIFTED
        lines = [line.replace("{prompt}", ">") for line in shifted]
        row = lines.index(">")
        for cursor in ((row, 1), (row + 1, 2)):
            with self.subTest(cursor=cursor):
                self.assertEqual(self.area(lines, cursor), ("empty", ""))
        lines = [line.replace("{prompt}", "> abc draft") for line in shifted]
        self.assertEqual(self.area(lines, (row, 13)), ("draft", "abc draft"))
        lines = [line.replace("{prompt}", ">") for line in shifted]
        lines.insert(row + 1, "  second line")
        self.assertEqual(self.area(lines, (row, 1))[0], "draft")

    def test_cursor_far_from_prompt_is_missing(self):
        """カーソルが入力欄から2行以上離れていれば、入力欄の形があっても打たない。"""
        lines, (row, column), _ = load_screen("band_empty_demo")
        for moved in (row - 2, row + 2, -1, len(lines), len(lines) + 5):
            with self.subTest(row=moved):
                self.assertEqual(self.area(lines, (moved, column)), ("missing", ""))

    def test_mark_without_rule_above_is_missing(self):
        """カーソルの行が「> …」でも、すぐ上が罫線でなければ入力欄ではない（会話中の引用など）。"""
        lines = ["本文", "> 引用", "─" * 40, "  状態行"]
        self.assertEqual(self.area(lines, (1, 2)), ("missing", ""))


class FakeScreenConsole:
    """カーソルつきで画面を返す代役。打鍵は数えるだけで、画面は変えない。"""

    def __init__(self, lines, cursor):
        self.lines = lines
        self.cursor = cursor
        self.records = 0

    def write(self, records):
        self.records += len(records)

    def read_lines(self):
        return list(self.lines)

    def read_screen(self):
        return list(self.lines), self.cursor

    def close(self):
        pass


class TestSenderRefusesWithoutInputArea(unittest.TestCase):
    """入力欄が見つからなければ、1打鍵も送らずに no_input_area で断る（陽性対照）。"""

    def setUp(self):
        self.original = session_input_sender.Console

    def tearDown(self):
        session_input_sender.Console = self.original

    def test_dialog_screen_gets_no_keys(self):
        lines, cursor, _ = load_screen("trial_model_dialog")
        for options in ({}, {"keep_draft": True}, {"check_only": True}):
            with self.subTest(options=options):
                fake = FakeScreenConsole(lines, cursor)
                session_input_sender.Console = lambda pid, fake=fake: fake
                with self.assertRaises(session_input_sender.SendError) as ctx:
                    session_input_sender.send_line(1, "/clear", settle_sec=0, **options)
                self.assertEqual(ctx.exception.code, "no_input_area")
                self.assertEqual(fake.records, 0)

    def test_cursor_away_from_input_gets_no_keys(self):
        lines, (row, column), _ = load_screen("band_empty_demo")
        fake = FakeScreenConsole(lines, (row - 3, column))
        session_input_sender.Console = lambda pid: fake
        with self.assertRaises(session_input_sender.SendError) as ctx:
            session_input_sender.send_line(1, "次の指示", settle_sec=0)
        self.assertEqual(ctx.exception.code, "no_input_area")
        self.assertEqual(fake.records, 0)


class TestLaunchedRegistry(unittest.TestCase):
    """本アプリが起こした子の台帳（送れる相手を限るための記録）。"""

    def setUp(self):
        self._original = sm.LAUNCHED_REGISTRY
        self._path = os.path.join(
            os.environ.get("TEMP", os.getcwd()), f"launched_test_{os.getpid()}.json"
        )
        sm.LAUNCHED_REGISTRY = self._path
        self._cleanup_pids = []

    def tearDown(self):
        sm.LAUNCHED_REGISTRY = self._original
        for pid in self._cleanup_pids:
            _kill_quietly(pid)
        for path in (self._path, self._path + ".tmp"):
            if os.path.exists(path):
                os.remove(path)

    def test_recorded_process_is_recognized(self):
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        self.assertFalse(sm.is_launched_by_app(pid))
        sm.record_launched(pid, "demo", os.getcwd())
        self.assertTrue(sm.is_launched_by_app(pid))

    def test_reused_pid_is_not_recognized(self):
        """同じ pid でも開始時刻が違えば別プロセス（pid の再利用）とみなす。"""
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({str(os.getpid()): {"create_time": 1.0}}, f)
        self.assertFalse(sm.is_launched_by_app(os.getpid()))

    def test_dead_entries_are_pruned(self):
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"999999": {"create_time": 1.0}}, f)
        sm.record_launched(pid, "demo", os.getcwd())
        with open(self._path, "r", encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), [str(pid)])

    def _fake_alive_state(self):
        originals = (sm._read_state, sm.is_session_alive)
        sm._read_state = lambda pid: {"pid": pid, "name": "demo", "cwd": "C:/demo"}
        sm.is_session_alive = lambda state: True
        return originals

    def test_detached_session_can_be_adopted(self):
        """親の居ない（切り離して起こされた）子は台帳に取り込め、記録に adopted が残る。"""
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        originals = self._fake_alive_state()
        try:
            result = sm.adopt_session(pid)
            again = sm.adopt_session(pid)
        finally:
            sm._read_state, sm.is_session_alive = originals
        self.assertTrue(result["adopted"])
        self.assertFalse(again["adopted"])
        self.assertTrue(sm.is_launched_by_app(pid))
        with open(self._path, "r", encoding="utf-8") as f:
            self.assertTrue(json.load(f)[str(pid)]["adopted"])

    def test_session_with_live_parent_is_not_adopted(self):
        """親が生きている（ターミナルから起こされた）相手は取り込まない。"""
        proc = subprocess.Popen(SLEEPER)
        self._cleanup_pids.append(proc.pid)
        originals = self._fake_alive_state()
        try:
            with self.assertRaises(sm.SessionError):
                sm.adopt_session(proc.pid)
        finally:
            sm._read_state, sm.is_session_alive = originals
        self.assertFalse(sm.is_launched_by_app(proc.pid))

    def test_registry_is_read_from_disk(self):
        """台帳はファイルにあり、アプリの再起動（メモリの消失）では消えない。"""
        pid = sm._spawn_detached(SLEEPER, os.getcwd(), sm.sanitize_env())
        self._cleanup_pids.append(pid)
        sm.record_launched(pid, "demo", os.getcwd())
        with open(self._path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertIn(str(pid), on_disk)
        self.assertTrue(sm._entry_matches(str(pid), on_disk[str(pid)]))


class TestSendToSessionGuards(unittest.TestCase):
    """send_to_session が送る前に断る条件（実際の打ち込みはしない）。"""

    def setUp(self):
        self._originals = (
            sm._read_state,
            sm.is_session_alive,
            sm.is_launched_by_app,
            sm._run_input_sender,
        )
        self.state = {
            "pid": 4242,
            "status": "idle",
            "sessionId": "s1",
            "bridgeSessionId": "b1",
        }
        self.sent = []
        sm._read_state = lambda pid: dict(self.state)
        sm.is_session_alive = lambda session: True
        sm.is_launched_by_app = lambda pid: True
        self.prefixes = []

        self.keep_drafts = []
        # 真なら、/clear を打つと会話の番号が変わる（効いた）ように振る舞う
        self.clear_works = False

        def record_sender(pid, text, prefix=None, keep_draft=False):
            self.sent.append(text)
            self.prefixes.append(prefix)
            self.keep_drafts.append(keep_draft)
            if text == "/clear" and self.clear_works:
                self.state["sessionId"] = self.state["sessionId"] + "+"

        sm._run_input_sender = record_sender

    def tearDown(self):
        (
            sm._read_state,
            sm.is_session_alive,
            sm.is_launched_by_app,
            sm._run_input_sender,
        ) = self._originals

    def test_not_launched_by_app_is_refused(self):
        sm.is_launched_by_app = lambda pid: False
        with self.assertRaises(sm.SessionNotManagedError):
            sm.send_to_session(4242, clear=True)
        self.assertEqual(self.sent, [])

    def test_unmanaged_needs_explicit_allow_and_keeps_draft(self):
        """台帳に無い相手へは allow_unmanaged で送れ、最初の打鍵で書きかけを守る。"""
        sm.is_launched_by_app = lambda pid: False
        self.clear_works = True
        result = sm.send_to_session(4242, clear=True, allow_unmanaged=True)
        self.assertEqual(self.sent, ["/clear"])
        self.assertEqual(self.keep_drafts, [True])
        self.assertFalse(result["managed"])

    def test_unmanaged_checks_draft_before_every_input(self):
        original_has = sm._transcript_has_prompt
        sm._transcript_has_prompt = lambda session_id, text, offsets=None: True
        sm.is_launched_by_app = lambda pid: False
        self.clear_works = True
        try:
            sm.send_to_session(4242, clear=True, text="次", allow_unmanaged=True)
            sm.send_to_session(4242, text="次の2", allow_unmanaged=True)
        finally:
            sm._transcript_has_prompt = original_has
        # /clear の直後の本文でも、その数秒に人が打ち始めているかもしれないので確かめる
        self.assertEqual(self.keep_drafts, [True, True, True])

    def test_draft_found_after_clear_says_clear_was_done(self):
        """/clear の後、本文の前で書きかけが見つかったら「/clear は済みました」と伝える。"""
        sm.is_launched_by_app = lambda pid: False

        def sender(pid, text, prefix=None, keep_draft=False):
            if text == "/clear":
                self.state["sessionId"] = "s1+"
                return
            raise sm.SessionDraftError("入力欄に書きかけ")

        sm._run_input_sender = sender
        with self.assertRaises(sm.SessionDraftError) as ctx:
            sm.send_to_session(4242, clear=True, text="本文", allow_unmanaged=True)
        self.assertTrue(str(ctx.exception).startswith("/clear は済みました"))

    def test_discard_draft_and_managed_do_not_keep_draft(self):
        sm.is_launched_by_app = lambda pid: False
        self.clear_works = True
        sm.send_to_session(4242, clear=True, allow_unmanaged=True, discard_draft=True)
        sm.is_launched_by_app = lambda pid: True
        sm.send_to_session(4242, clear=True)
        self.assertEqual(self.keep_drafts, [False, False])

    def test_busy_session_is_refused_without_wait(self):
        self.state["status"] = "busy"
        with self.assertRaises(sm.SessionBusyError):
            sm.send_to_session(4242, clear=True)
        self.assertEqual(self.sent, [])

    def test_busy_session_waits_until_timeout(self):
        self.state["status"] = "busy"
        started = time.time()
        with self.assertRaises(sm.SessionBusyError):
            sm.send_to_session(4242, clear=True, wait_idle_sec=1)
        self.assertGreaterEqual(time.time() - started, 0.9)
        self.assertEqual(self.sent, [])

    def test_nothing_to_send_is_refused(self):
        with self.assertRaises(sm.SessionError):
            sm.send_to_session(4242)

    def test_clear_not_confirmed_is_an_error(self):
        """/clear を打っても会話の番号が変わらなければ、効いたとは報告しない。"""
        original_timeout = sm.CLEAR_CONFIRM_TIMEOUT_SEC
        sm.CLEAR_CONFIRM_TIMEOUT_SEC = 0.5
        try:
            with self.assertRaises(sm.SessionError):
                sm.send_to_session(4242, clear=True)
        finally:
            sm.CLEAR_CONFIRM_TIMEOUT_SEC = original_timeout
        self.assertEqual(self.sent, ["/clear"])

    def test_clear_then_text_are_sent_in_order(self):
        """/clear が効いた（sessionId が変わった）後に本文を送る。"""
        original_has = sm._transcript_has_prompt

        def fake_sender(pid, text, prefix=None, keep_draft=False):
            self.sent.append(text)
            if text == "/clear":
                self.state["sessionId"] = "s2"

        sm._run_input_sender = fake_sender
        sm._transcript_has_prompt = lambda session_id, text, offsets=None: (
            session_id == "s2"
        )
        try:
            result = sm.send_to_session(4242, clear=True, text="次の指示\nです")
        finally:
            sm._transcript_has_prompt = original_has
        self.assertEqual(self.sent, ["/clear", "次の指示 です"])
        self.assertEqual(result["session_id_before"], "s1")
        self.assertEqual(result["session_id_after"], "s2")
        self.assertTrue(result["bridge_unchanged"])

    def test_bridge_unknown_before_send_is_not_reported_as_changed(self):
        """送る前に claude.ai の画面が未登録なら、「変わった」とは言わず None を返す。"""
        original_has = sm._transcript_has_prompt
        self.state.pop("bridgeSessionId", None)

        def fake_sender(pid, text, prefix=None, keep_draft=False):
            self.sent.append(text)
            self.state["bridgeSessionId"] = "b-late"

        sm._run_input_sender = fake_sender
        sm._transcript_has_prompt = lambda session_id, text, offsets=None: True
        try:
            result = sm.send_to_session(4242, text="本文")
        finally:
            sm._transcript_has_prompt = original_has
        self.assertIsNone(result["bridge_unchanged"])
        self.assertEqual(result["bridge_session_id"], "b-late")

    def _send_text(self, text, **kwargs):
        original_has = sm._transcript_has_prompt
        sm._transcript_has_prompt = lambda session_id, text, offsets=None: True
        try:
            return sm.send_to_session(4242, text=text, **kwargs)
        finally:
            sm._transcript_has_prompt = original_has

    def test_short_text_has_no_preamble(self):
        """貼り付けにならない長さ（800字以下）は素の文のまま。"""
        result = self._send_text("あ" * sm.PASTE_THRESHOLD_CHARS)
        self.assertEqual(self.prefixes, [""])
        self.assertFalse(result["preamble_used"])

    def _with_settings(self, data):
        """前置きの既定を読む設定ファイルを、一時フォルダの写しに差し替える。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "settings.json")
        if data is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        original = sm.INBOX_SETTINGS_FILE
        sm.INBOX_SETTINGS_FILE = path
        self.addCleanup(setattr, sm, "INBOX_SETTINGS_FILE", original)

    def test_long_text_gets_default_preamble(self):
        # この機械の設定（paste_preamble を書いていることがある）は読まない
        self._with_settings(None)
        result = self._send_text("あ" * (sm.PASTE_THRESHOLD_CHARS + 1))
        self.assertEqual(self.prefixes, [sm.DEFAULT_PASTE_PREAMBLE])
        self.assertTrue(result["preamble_used"])
        self.assertTrue(sm.DEFAULT_PASTE_PREAMBLE)

    def test_default_preamble_from_settings(self):
        self._with_settings({"paste_preamble": "設定の前置き"})
        self._send_text("あ" * 2600)
        self._with_settings({"paste_preamble": ""})
        self._send_text("あ" * 2600)
        # 文字列でない値は既定のまま
        self._with_settings({"paste_preamble": 3})
        self._send_text("あ" * 2600)
        self.assertEqual(self.prefixes, ["設定の前置き", "", sm.DEFAULT_PASTE_PREAMBLE])
        # 本体の受け口（preamble を省いた API）も同じ既定になる
        self.assertEqual(
            sm.paste_prefix_for("あ" * 2600, None), sm.DEFAULT_PASTE_PREAMBLE
        )

    def test_preamble_can_be_replaced_or_disabled(self):
        self._send_text("あ" * 2600, preamble="別の前置き")
        self._send_text("あ" * 2600, preamble="")
        self.assertEqual(self.prefixes, ["別の前置き", ""])

    def test_bad_preamble_is_refused_before_sending(self):
        with self.assertRaises(sm.SessionError):
            self._send_text("あ" * 2600, preamble="/clear")
        with self.assertRaises(sm.SessionError):
            self._send_text("あ" * 2600, preamble="い" * 900)
        self.assertEqual(self.sent, [])


class TestTranscriptMatch(unittest.TestCase):
    """会話記録との照合: 送る前より後ろだけを、本文の先頭で見る。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.original_home = sm.CLAUDE_HOME
        sm.CLAUDE_HOME = self.tmp.name
        folder = os.path.join(self.tmp.name, "projects", "p")
        os.makedirs(folder)
        self.path = os.path.join(folder, "sid.jsonl")

    def tearDown(self):
        sm.CLAUDE_HOME = self.original_home
        self.tmp.cleanup()

    def _append(self, content):
        line = {"type": "user", "message": {"role": "user", "content": content}}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def test_long_text_matches_by_head(self):
        """約2,600字の本文でも、記録に先頭が載れば確認できる。"""
        body = ('全角の「本文」と "引用符" と \\ を含む長文。' * 120)[:2600]
        offsets = sm._transcript_sizes("sid")
        self._append(body)
        self.assertTrue(sm._transcript_has_prompt("sid", body, offsets))

    def test_text_before_send_is_not_counted(self):
        """同じ本文が送る前から記録にあっても、新しく載るまでは確認済みにしない。"""
        self._append("同じ指示")
        offsets = sm._transcript_sizes("sid")
        self.assertFalse(sm._transcript_has_prompt("sid", "同じ指示", offsets))
        self._append("同じ指示")
        self.assertTrue(sm._transcript_has_prompt("sid", "同じ指示", offsets))


class FakeConsole:
    """送り手の Console の代役。claude の入力欄のふるまいを真似る。

    accept_enter_at: 何回目の Enter で送信されるか
    suggestion: 欄が空のときに出る提案（薄字だが、画面の文字は書きかけと同じに見える）
    suggestion_after_submit: 送信の直後に出る提案（返答がすぐ終わった場合）
    long_draft: 行数の多い書きかけ（Ctrl+U 1回で見た目の1行ずつ消える。2026-09-30 実測）
    """

    def __init__(
        self,
        accept_enter_at=1,
        suggestion="",
        suggestion_after_submit="",
        long_draft=(),
    ):
        self.accept_enter_at = accept_enter_at
        self.enters = 0
        self.typed = []
        self.buffer = ""
        self.submitted = []
        self.suggestion = suggestion
        self.suggestion_after_submit = suggestion_after_submit
        self.long_draft = list(long_draft)

    def write(self, records):
        for record in records:
            event = record.Event.KeyEvent
            if not event.bKeyDown:
                continue
            if event.wVirtualKeyCode == session_input_sender.VK_RETURN:
                self.enters += 1
                if self.enters >= self.accept_enter_at:
                    self.submitted.append(self.buffer)
                    self.buffer = ""
                    self.suggestion = self.suggestion_after_submit
            elif event.uChar == "\x15":
                if self.buffer:
                    self.buffer = ""
                elif self.long_draft:
                    self.long_draft.pop()
            elif event.uChar == "\b":
                if self.buffer:
                    self.buffer = self.buffer[:-1]
                elif self.long_draft:
                    self.long_draft[-1] = self.long_draft[-1][:-1]
            else:
                self.typed.append(event.uChar)
                self.buffer += event.uChar

    def read_lines(self):
        draft = self.long_draft + ([self.buffer] if self.buffer else [])
        if draft:
            return ["> \xa0" + draft[0]] + ["  " + d for d in draft[1:]] + ["─" * 10]
        return ["> \xa0" + (self.suggestion or 'Try "fix"'), "─" * 10]

    def close(self):
        pass


class CursorFakeConsole(FakeConsole):
    """FakeConsole にカーソルと上の罫線を足した代役（本番と同じ、カーソルの所を見る経路を通す）。

    帯（mod の AbovePrompt）を1行描き、カーソルは書きかけの最後の行の末尾に置く。
    """

    def read_screen(self):
        box = super().read_lines()
        lines = ["メモなし  メモを編集  ファイル [-]", "─" * 10] + box
        last = len(lines) - 2  # 下の罫線の1つ上＝書きかけの最後の行
        return lines, (last, len(lines[last]))


class TestSenderAtCursor(unittest.TestCase):
    """カーソルの所を見る経路で、送る流れ全体（消す→確かめ→打つ→Enter の確かめ）が通る。"""

    def setUp(self):
        self.originals = (
            session_input_sender.Console,
            session_input_sender.STABLE_POLL_SEC,
            session_input_sender.ENTER_CHECK_SEC,
        )
        session_input_sender.STABLE_POLL_SEC = 0.01
        session_input_sender.ENTER_CHECK_SEC = 0.01

    def tearDown(self):
        (
            session_input_sender.Console,
            session_input_sender.STABLE_POLL_SEC,
            session_input_sender.ENTER_CHECK_SEC,
        ) = self.originals

    def send(self, fake, text="次の指示", **options):
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, text, settle_sec=0, **options)

    def test_plain_send(self):
        fake = CursorFakeConsole()
        self.send(fake, "本文" * 1300)
        self.assertEqual(fake.submitted, ["本文" * 1300])
        self.assertEqual(fake.enters, 1)

    def test_suggestion_is_replaced_and_sent(self):
        for options in ({}, {"keep_draft": True}):
            with self.subTest(options=options):
                fake = CursorFakeConsole(suggestion="hello2.txt を削除して")
                self.send(fake, **options)
                self.assertEqual(fake.submitted, ["次の指示"])

    def test_human_draft_is_kept_with_keep_draft(self):
        fake = CursorFakeConsole(long_draft=["1行目", "2行目"])
        with self.assertRaises(session_input_sender.SendError) as ctx:
            self.send(fake, keep_draft=True)
        self.assertEqual(ctx.exception.code, "draft_present")
        self.assertEqual(fake.long_draft, ["1行目", "2行目"])
        self.assertEqual(fake.enters, 0)

    def test_swallowed_enter_is_retried(self):
        fake = CursorFakeConsole(accept_enter_at=2)
        self.send(fake)
        self.assertEqual(fake.enters, 2)


class TestSenderEnterHandling(unittest.TestCase):
    """本文と Enter を分けて打ち、飲み込まれた Enter は打ち直す。"""

    def setUp(self):
        self.originals = (
            session_input_sender.Console,
            session_input_sender.STABLE_POLL_SEC,
            session_input_sender.ENTER_CHECK_SEC,
        )
        session_input_sender.STABLE_POLL_SEC = 0.01
        session_input_sender.ENTER_CHECK_SEC = 0.01

    def tearDown(self):
        (
            session_input_sender.Console,
            session_input_sender.STABLE_POLL_SEC,
            session_input_sender.ENTER_CHECK_SEC,
        ) = self.originals

    def _send(self, fake):
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "本文" * 1300, settle_sec=0)

    def test_enter_is_sent_after_text(self):
        fake = FakeConsole(accept_enter_at=1)
        self._send(fake)
        self.assertEqual(len(fake.typed), 2600)
        self.assertEqual(fake.enters, 1)

    def test_prefix_is_typed_before_text_as_one_message(self):
        """前置き → 本文 → Enter の順で、Enter は最後に1回だけ。"""
        fake = FakeConsole(accept_enter_at=1)
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "本文" * 1300, settle_sec=0, prefix="前置き")
        self.assertEqual("".join(fake.typed), "前置き" + "本文" * 1300)
        self.assertEqual(fake.enters, 1)

    def test_suggestion_is_treated_as_empty_and_replaced(self):
        """作業後の提案（Ctrl+U で消えない）は書きかけとみなさず、本文で置き換えて送る。"""
        fake = FakeConsole(suggestion="hello2.txt を削除して")
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "次の指示", settle_sec=0)
        self.assertEqual(fake.submitted, ["次の指示"])

    def test_real_draft_that_cannot_be_cleared_is_refused(self):
        """消しても戻る提案と違い、消すたびに減る長い書きかけは打たずに断る。"""
        fake = FakeConsole(long_draft=[f"人の書きかけ{i}行目" for i in range(50)])
        session_input_sender.Console = lambda pid: fake
        with self.assertRaises(session_input_sender.SendError) as ctx:
            session_input_sender.send_line(1, "次の指示", settle_sec=0)
        self.assertEqual(ctx.exception.code, "draft_remains")
        self.assertEqual(fake.submitted, [])
        self.assertEqual(fake.enters, 0)

    def test_suggestion_after_quick_reply_is_not_sent(self):
        """送信の直後に提案が出ても、それに Enter を打ち直して送らない。"""
        fake = FakeConsole(suggestion_after_submit="hello2.txt を削除して")
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "次の指示", settle_sec=0)
        self.assertEqual(fake.submitted, ["次の指示"])
        self.assertEqual(fake.enters, 1)

    def test_keep_draft_refuses_and_leaves_human_draft_intact(self):
        """台帳に無い相手で人の書きかけがあれば、確かめの1文字だけ消して断る（書きかけは残る）。"""
        for draft in (["人の書きかけ"], ["1行目", "2行目"], ["x"]):
            with self.subTest(draft=draft):
                fake = FakeConsole(long_draft=draft)
                session_input_sender.Console = lambda pid, fake=fake: fake
                with self.assertRaises(session_input_sender.SendError) as ctx:
                    session_input_sender.send_line(
                        1, "/clear", settle_sec=0, keep_draft=True
                    )
                self.assertEqual(ctx.exception.code, "draft_present")
                self.assertEqual(fake.long_draft, draft)
                self.assertEqual(fake.buffer, "")
                self.assertEqual(fake.enters, 0)

    def test_keep_draft_replaces_suggestion_and_sends(self):
        """作業後の提案は、1文字打つと置き換わるので見分けて送る（台帳に無い相手でも）。"""
        fake = FakeConsole(suggestion="hello2.txt を削除して")
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "/clear", settle_sec=0, keep_draft=True)
        self.assertEqual(fake.submitted, ["/clear"])

    def test_keep_draft_sends_when_empty(self):
        fake = FakeConsole()
        session_input_sender.Console = lambda pid: fake
        session_input_sender.send_line(1, "/clear", settle_sec=0, keep_draft=True)
        self.assertEqual(fake.submitted, ["/clear"])

    def test_swallowed_enter_is_retried(self):
        fake = FakeConsole(accept_enter_at=2)
        self._send(fake)
        self.assertEqual(fake.enters, 2)

    def test_enter_never_accepted_is_an_error(self):
        fake = FakeConsole(accept_enter_at=99)
        with self.assertRaises(session_input_sender.SendError) as ctx:
            self._send(fake)
        self.assertEqual(ctx.exception.code, "enter_not_accepted")
        self.assertEqual(fake.enters, session_input_sender.ENTER_RETRY)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""試験が本物の状態（~/.claude-web-app）に触れないことの試験（2026-11-02・伝言 8bc5f64d28af・b3f365fea148）。

本物の launch_session・record_launched を呼ぶ試験が本物の台帳 launched.json に書き、掃除で生きている子の記録まで消した。
conftest.py が状態の置き場をすべて一時フォルダへ向け、試験の前後で本物の台帳などが変わっていれば全体を失敗にする。
ここでは、どの置き場も本物を向いていないこと（読み込みのときに作られる本アプリの部品も含む）を確かめる。
"""

import importlib.util
import os

import pytest

import auto_clear
import conftest
import liaison
import machine_send
import migration_drill
import quota_resume
import relogin_manager
import self_clear
import send_history
import session_manager as sm


def _inside_real(path):
    real = os.path.normcase(os.path.abspath(conftest.REAL_APP_STATE_DIR))
    target = os.path.normcase(os.path.abspath(path))
    return target == real or target.startswith(real + os.sep)


def test_launched_registry_points_to_temp():
    assert os.path.normcase(sm.LAUNCHED_REGISTRY) != os.path.normcase(
        conftest.REAL_LAUNCHED_REGISTRY
    )


def test_record_launched_writes_only_temp(tmp_path):
    before = conftest._guarded_fingerprints()
    sm.record_launched(os.getpid(), "isolation", str(tmp_path))
    assert os.path.isfile(sm.LAUNCHED_REGISTRY)
    assert sm.is_launched_by_app(os.getpid())
    assert conftest._guarded_fingerprints() == before


def test_registry_harm_tells_test_writes_from_app_launches(tmp_path):
    """見張りは、生きている記録が消えた・試験の記録が足されたときだけ跡とみなし、5002 が子を足しただけなら黙る。"""
    me = str(os.getpid())
    live = {me: {"create_time": sm._process_create_time(os.getpid()), "name": "live"}}
    # 生きている記録が消えた（掃除で消した形）
    assert conftest._registry_harm(live, {})
    # 試験のプロセスの記録が足された
    assert conftest._registry_harm({}, {me: {"name": "trial", "cwd": "C:\\x"}})
    # 一時フォルダを作業場とする記録が足された
    assert conftest._registry_harm({}, {"1": {"name": "t", "cwd": str(tmp_path)}})
    # 5002 が子を起こして足しただけ（ほかの pid・ふつうの作業場）は跡ではない
    added = {"1": {"name": "example-price-tool", "cwd": "F:\\SharedDrive0\\develop"}}
    assert conftest._registry_harm(live, dict(live, **added)) == []
    # 死んだ記録が消えたのは跡ではない（5002 の掃除）
    dead = {"2": {"create_time": 1.0, "name": "old"}}
    assert conftest._registry_harm(dead, {}) == []
    # 試験の間にこのプロジェクトを作業場とする記録が足された（試験が os.getcwd() で起こす形・評価役の中）
    started = 1000.0
    project_entry = {
        "3": {"name": "x", "cwd": conftest.PROJECT_ROOT, "launched_at": 1001.0}
    }
    assert conftest._registry_harm({}, project_entry, started)
    # 同じ作業場でも試験の前から足されていた記録は跡ではない
    older = {"3": dict(project_entry["3"], launched_at=999.0)}
    assert conftest._registry_harm({}, older, started) == []


def test_module_state_paths_point_to_temp():
    paths = {
        "sm.APP_STATE_DIR": sm.APP_STATE_DIR,
        "sm.LAUNCHED_REGISTRY": sm.LAUNCHED_REGISTRY,
        "sm.TASK_LAUNCH_DIR": sm.TASK_LAUNCH_DIR,
        "sm.inbox": sm._inbox_dir(),
        "sm.hidden_resumable": sm._hidden_resumable_path(),
        "auto_clear.STATE_FILE": auto_clear.STATE_FILE,
        "auto_clear.KEEP_FILE": auto_clear.KEEP_FILE,
        "auto_clear.RESULT_LOG": auto_clear.RESULT_LOG,
        "liaison.STATE_FILE": liaison.STATE_FILE,
        "quota_resume.STATE_FILE": quota_resume.STATE_FILE,
        "machine_send.STATE_FILE": machine_send.STATE_FILE,
        "self_clear.RESULT_LOG": self_clear.RESULT_LOG,
        "send_history.HISTORY_FILE": send_history.HISTORY_FILE,
        "relogin_manager.CHECK_DIR": relogin_manager.CHECK_DIR,
        "sm.RELOGIN_DIR": sm.RELOGIN_DIR,
        "relogin_manager.RELOGIN_DIR": relogin_manager.RELOGIN_DIR,
        "migration_drill.STATE_DIR": migration_drill.STATE_DIR,
        "migration_drill.LOG_PATH": migration_drill.LOG_PATH,
        "migration_drill.PHASE_PATH": migration_drill.PHASE_PATH,
    }
    leaked = {k: v for k, v in paths.items() if _inside_real(v)}
    assert leaked == {}


def test_default_components_follow_temp_paths():
    """置き場を省いて作った部品は、使うたびに（差し替えた）定数を引く。"""
    assert not _inside_real(auto_clear.KeepMarks().path)
    assert not _inside_real(liaison.Liaison().state_file)
    resume = quota_resume.QuotaResume(lambda cwd, sid: None)
    assert not _inside_real(resume.state_file)


@pytest.fixture(scope="module")
def app_module():
    spec = importlib.util.spec_from_file_location(
        "claude_web_app_isolation",
        os.path.join(conftest.PROJECT_ROOT, "claude-web-app.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_app_components_follow_temp_paths(app_module):
    """本アプリの読み込みのときに作られる部品（読み込みが試験の外でも）も、一時の置き場へ書く。"""
    for label, path in {
        "notices": app_module.notices.state_file,
        "quota": app_module.quota.state_file,
        "auto.state": app_module.auto.state_file,
        "auto.result_log": app_module.auto.result_log,
        "auto.keep": app_module.auto.keep_marks.path,
        "lineage": app_module.lineage.path,
    }.items():
        assert not _inside_real(path), label

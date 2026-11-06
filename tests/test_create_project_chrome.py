# -*- coding: utf-8 -*-
"""プロジェクト作成の口（Socket.IO create_project）が chrome を起動へ渡す試験（2026-11-01・管理役の伝言 529ebb9132d1）。

起動・移しの口と同じく、true は --chrome・false は --no-chrome・省けば何も足さない。
真偽でない値はフォルダを作る前に断る。本物のフォルダは作らず、起動もしない（作成と起動は差し替える）。

実行方法:
    <python> -m pytest tests/test_create_project_chrome.py --rootdir <このプロジェクト>
"""

import importlib.util
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import access_guard


@pytest.fixture(scope="module")
def app_module():
    spec = importlib.util.spec_from_file_location(
        "claude_web_app_create_chrome", os.path.join(PROJECT_ROOT, "claude-web-app.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(app_module, monkeypatch, tmp_path):
    created_dirs = []
    started = []

    def fake_create(dest_dir, key_prefix, name, source_chat, parent_key):
        created_dirs.append(name)
        return {
            "name": name,
            "project_key": f"{key_prefix}{name}",
            "parent_key": parent_key,
        }

    monkeypatch.setattr(app_module, "guard", access_guard.AccessGuard())
    monkeypatch.setattr(
        app_module,
        "resolve_location",
        lambda location: {
            "dir": str(tmp_path),
            "key_prefix": "developing/",
            "parent_key": "developing",
        },
    )
    monkeypatch.setattr(app_module.pm, "create_project", fake_create)
    monkeypatch.setattr(
        app_module, "resolve_project_path", lambda key, **kw: str(tmp_path / "p")
    )
    monkeypatch.setattr(
        app_module, "handle_start_session", lambda message: started.append(message)
    )
    client = app_module.socketio.test_client(app_module.app)
    assert client.is_connected()
    yield client, created_dirs, started
    client.disconnect()


def create(client, **extra):
    client.emit(
        "create_project",
        {"name": "demo", "chat": "最初の指示", "location": "developing", **extra},
    )
    return [
        r["args"][0]
        for r in client.get_received()
        if r["name"] == "project_create_result"
    ]


@pytest.mark.parametrize("value", [True, False])
def test_chrome_is_passed_to_launch(harness, value):
    client, created, started = harness
    results = create(client, chrome=value)
    assert results and results[0]["ok"] is True
    assert created == ["demo"]
    assert started[0]["chrome"] is value
    assert started[0]["project"] == "developing/demo"


def test_chrome_omitted_adds_nothing(harness):
    client, created, started = harness
    create(client)
    # 省けば None（起動の口は何も足さない＝claude の設定に従う・今までどおり）
    assert started[0]["chrome"] is None


def test_scaffold_warning_reaches_screen_and_launch_continues(
    app_module, harness, monkeypatch
):
    # git が無い等で作成時の警告が出ても、画面へ返して起動は続ける（2026-11-03・依頼 7c136ff2a3f5）
    client, created, started = harness
    monkeypatch.setattr(
        app_module.pm,
        "create_project",
        lambda **kw: {
            "name": kw["name"],
            "project_key": f"{kw['key_prefix']}{kw['name']}",
            "parent_key": kw["parent_key"],
            "warning": "git が見つからないため、ローカルの git リポジトリを作れませんでした",
        },
    )
    results = create(client)
    assert results and results[0]["ok"] is True
    assert "git が見つからない" in results[0]["project"]["warning"]
    assert started and started[0]["project"] == "developing/demo"


@pytest.mark.parametrize("value", ["true", 1, [True]])
def test_non_bool_chrome_is_refused_before_creating(harness, value):
    client, created, started = harness
    results = create(client, chrome=value)
    assert results and results[0]["ok"] is False
    assert "chrome は true か false" in results[0]["error"]
    # フォルダを作らず、起動もしない
    assert created == [] and started == []

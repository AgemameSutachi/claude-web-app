# -*- coding: utf-8 -*-
"""web-app が書き出す trial_roles.json の写しを、claude-session-resumer の trial_roles.py が
web-app と同じ判断で読めるかの試験（2026-10-28 に割り当ての正本を web-app の台帳へ移した）。

守りたい性質:
- 写しは trial_roles.load で読める（注記のキー _note があっても壊れない・error.txt を出さない）。
- 写しから引いた「どの役を本家でないアカウントで起こすか」が、web-app の割り当てと同じ。
- 本家へ戻した作業場は、写しでも本家（is_trial が偽）になる。

試験の一時フォルダは conftest.py が用意する（本物の設定・台帳・trial_roles.json には触らない）。

実行方法:
    <python> -m pytest tests  （conftest.py の差し替えが効くよう pytest で）
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import accounts
import trial_roles


def _setup(tmp_path):
    cfg = tmp_path / "sub" / "cfg"
    home = tmp_path / "sub" / "home"
    cfg.mkdir(parents=True)
    home.mkdir(parents=True)
    mirror = tmp_path / "mirror" / "trial_roles.json"
    mirror.parent.mkdir()
    with open(accounts.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "accounts": [
                    {"name": "mainacct"},
                    {"name": "subacct", "config_dir": str(cfg), "home": str(home)},
                ],
                "trial_roles_mirror": str(mirror),
            },
            f,
        )
    work = tmp_path / "work"
    paths = {name: work / name for name in ("alpha", "beta", "gamma")}
    for p in paths.values():
        p.mkdir(parents=True)
    return cfg, home, mirror, paths


def test_mirror_is_read_by_trial_roles_with_same_decisions(tmp_path):
    cfg, home, mirror, paths = _setup(tmp_path)
    accounts.assign(str(paths["alpha"]), "developing/alpha", "subacct", "create")
    accounts.assign(str(paths["beta"]), "developing/beta", "subacct", "move")
    conf = trial_roles.load(str(mirror))
    assert trial_roles.last_error() is None
    assert not os.path.exists(mirror.parent / trial_roles.ERROR_NAME)
    for name, path in paths.items():
        web_app = accounts.account_for(str(path))["name"] == "subacct"
        assert trial_roles.is_trial(str(path), conf) == web_app, name
    env = trial_roles.apply({}, str(paths["alpha"]), conf)
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg) and env["HOME"] == str(home)


def test_moving_back_to_main_clears_role_in_mirror(tmp_path):
    _cfg, _home, mirror, paths = _setup(tmp_path)
    accounts.assign(str(paths["alpha"]), "developing/alpha", "subacct", "create")
    accounts.assign(str(paths["alpha"]), "developing/alpha", "mainacct", "move")
    # roles が空＝試用の役なし（本家で起こす）
    assert trial_roles.load(str(mirror)) is None
    assert not trial_roles.is_trial(str(paths["alpha"]), trial_roles.load(str(mirror)))

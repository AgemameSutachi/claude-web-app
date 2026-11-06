# -*- coding: utf-8 -*-
"""アカウント（accounts.py）と、それを使う起動・一覧・使用量・ログイン更新・移す口の試験。

守りたい性質:
- 割り当ての正本は台帳。載っていない作業場は本家。台帳が読めない・割り当て先が無いときは
  黙って本家で起こさずに断る。
- 本家でないアカウントの作業場は、どの道（二段起動・タスク・制限トークン）でもその置き場の
  CLAUDE_CONFIG_DIR・HOME・USERPROFILE を載せて起こす（2026-10-28 に S1 で断られた件）。
- 割り当てと違うアカウントを名指しした起動は、信頼登録より前に断る（会話は持ち越せない）。
- 一覧・状態・会話の記録は全アカウントの置き場を見て、各行に動いているアカウントを付ける。
- 割り当てを変えるたびに trial_roles.json の写しを書き、正本が無い最初の1回だけ写しから取り込む。
- 週次の使用%は取れないとき「分からない」（推測しない）。既定は両方取れたときだけ比べる。

本物の claude は起こさず、置き場・設定・台帳はすべて一時フォルダ（conftest.py が向ける）。

実行方法:
    <python> -m pytest tests  （conftest.py の差し替えが効くよう pytest で）
"""

import json
import os
import subprocess
import sys
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import accounts
import relogin_manager as rl
import session_manager as sm
import session_task_launcher
import usage_tracker as ut


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """本家と subacct の置き場・作業場を作り、設定に2アカウントを書く。"""
    main_home = tmp_path / "main" / ".claude"
    sub_cfg = tmp_path / "sub" / "cfg"
    sub_home = tmp_path / "sub" / "home"
    statusline = tmp_path / "sub" / "statusline"
    for d in (main_home, sub_cfg, sub_home):
        (d / "sessions").mkdir(parents=True)
        (d / "projects").mkdir(parents=True)
    statusline.mkdir(parents=True)
    work = tmp_path / "work"
    sub_cwd = work / "sub-role"
    main_cwd = work / "plain-role"
    sub_cwd.mkdir(parents=True)
    main_cwd.mkdir(parents=True)
    (sub_cfg / ".claude.json").write_text(json.dumps({"projects": {}}), "utf-8")
    mirror = tmp_path / "trial_roles.json"
    settings = {
        "accounts": [
            {"name": "mainacct", "label": "mainacct"},
            {
                "name": "subacct",
                "label": "subacct アカウント",
                "config_dir": str(sub_cfg),
                "home": str(sub_home),
                "statusline_dir": str(statusline),
            },
        ],
        "trial_roles_mirror": str(mirror),
    }
    with open(accounts.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    monkeypatch.setattr(sm, "CLAUDE_HOME", str(main_home))
    return {
        "main_home": str(main_home),
        "sub_cfg": str(sub_cfg),
        "sub_home": str(sub_home),
        "statusline": str(statusline),
        "sub_cwd": str(sub_cwd),
        "main_cwd": str(main_cwd),
        "mirror": str(mirror),
        "settings": settings,
    }


def write_settings(data):
    with open(accounts.SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


# --- 定義 -------------------------------------------------------------------


def test_no_settings_means_main_only():
    defs = accounts.definitions()
    assert [a["name"] for a in defs] == ["main"]
    assert defs[0]["main"] and defs[0]["error"] is None


def test_definitions_and_errors(homes, tmp_path):
    defs = accounts.definitions()
    assert [a["name"] for a in defs] == ["mainacct", "subacct"]
    assert defs[0]["main"] and not defs[1]["main"]
    # 置き場が無い定義は error を付けて残す（黙って本家へ流さない）
    settings = dict(homes["settings"])
    settings["accounts"] = [
        settings["accounts"][0],
        dict(settings["accounts"][1], config_dir=str(tmp_path / "nope")),
    ]
    write_settings(settings)
    broken = accounts.get("subacct")
    assert broken["error"]
    with pytest.raises(accounts.AccountError) as ctx:
        accounts.require_usable("subacct")
    assert "mainacct で起こしてください" in str(ctx.value)
    with pytest.raises(accounts.AccountError):
        accounts.require_usable("unknown")


def test_env_overrides(homes):
    sub = accounts.get("subacct")
    assert accounts.env_overrides(sub) == {
        "CLAUDE_CONFIG_DIR": homes["sub_cfg"],
        "HOME": homes["sub_home"],
        "USERPROFILE": homes["sub_home"],
    }
    main = accounts.get("mainacct")
    # 親が subacct の HOME を継いでいたら本家のフォルダへ戻す
    fixed = accounts.apply_env({"HOME": homes["sub_home"], "PATH": "x"}, main)
    assert fixed["HOME"] == accounts.USER_DIR
    assert accounts.apply_env({"HOME": "C:\\other"}, main)["HOME"] == "C:\\other"


# --- 割り当て・写し・取り込み ------------------------------------------------


def test_assign_writes_state_and_mirror(homes):
    result = accounts.assign(
        homes["sub_cwd"], "developing/sub-role", "subacct", "create"
    )
    assert result["previous"] is None
    assert result["mirror"]["written"]
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"
    assert accounts.account_for(homes["main_cwd"])["name"] == "mainacct"
    with open(homes["mirror"], encoding="utf-8") as f:
        mirror = json.load(f)
    assert mirror["roles"] == ["sub-role"]
    assert mirror["config_dir"] == homes["sub_cfg"]
    assert "正本" in mirror["_note"]
    # 中身が同じなら書き直さない
    again = accounts.write_mirror(accounts.state())
    assert again["written"] is False and again["error"] is None
    assert accounts.mirror_status()["in_sync"] is True
    # 本家へ戻すと台帳から外れ、写しの roles も空になる
    back = accounts.assign(homes["sub_cwd"], "developing/sub-role", "mainacct", "move")
    assert back["previous"] == "subacct"
    assert accounts.assigned_name(homes["sub_cwd"]) is None
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == []


def test_refresh_mirror_note(homes):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    with open(homes["mirror"], encoding="utf-8") as f:
        mirror = json.load(f)
    # 正本は絶対パスで書く（~ は本家以外のアカウントでは別の home を指す）
    assert accounts.STATE_PATH in mirror["_note"] and "~" not in mirror["_note"]
    # 注記だけが古い写しは書き直す（手直しとみなさない）
    mirror["_note"] = "古い注記 ~/.claude-web-app/account_assignments.json"
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(mirror, f)
    assert accounts.refresh_mirror_note()["written"] is True
    assert accounts.mirror_status()["in_sync"] is True
    # 役が手で直されていれば触らない
    mirror["roles"] = ["sub-role", "hand-added"]
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(mirror, f)
    result = accounts.refresh_mirror_note()
    assert result["written"] is False and "手で直されて" in result["error"]
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["sub-role", "hand-added"]


def test_mirror_drift_is_reported(homes):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": homes["sub_cfg"], "home": homes["sub_home"], "roles": ["x"]},
            f,
        )
    status = accounts.mirror_status()
    assert status["in_sync"] is False
    assert status["file_roles"] == ["x"] and status["roles"] == ["sub-role"]


def test_rename_path_moves_assignment_and_children(homes, tmp_path):
    parent = tmp_path / "work" / "parent"
    child = parent / "projects" / "kid"
    child.mkdir(parents=True)
    accounts.assign(str(parent), "developing/parent", "subacct", "create")
    accounts.assign(str(child), "developing/parent/projects/kid", "subacct", "create")
    new_parent = tmp_path / "work2" / "parent2"
    moved = accounts.rename_path(str(parent), str(new_parent), "working/parent2")
    assert moved == 2
    assert accounts.assigned_name(str(new_parent)) == "subacct"
    assert accounts.assigned_name(str(new_parent / "projects" / "kid")) == "subacct"
    assert accounts.assigned_name(str(parent)) is None
    keys = sorted(e["project_key"] for e in accounts.state()["assignments"].values())
    assert keys == ["working/parent2", "working/parent2/projects/kid"]


def test_import_trial_roles_once(homes):
    with open(homes["mirror"], "w", encoding="utf-8-sig") as f:
        json.dump(
            {
                "config_dir": homes["sub_cfg"],
                "home": homes["sub_home"],
                "roles": ["sub-role", "ghost", "twin"],
            },
            f,
        )
    found = {
        "sub-role": [(homes["sub_cwd"], "developing/sub-role")],
        "ghost": [],
        "twin": [("C:\\a\\twin", "developing/twin"), ("C:\\b\\twin", "working/twin")],
    }
    report = accounts.import_trial_roles(lambda name: found[name])
    assert report["imported"] == [
        {"role": "sub-role", "project_key": "developing/sub-role"}
    ]
    assert sorted(u["role"] for u in report["unresolved"]) == ["ghost", "twin"]
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"
    # 作業場へ解けなかった役は、今までどおりフォルダ名で照らす
    assert accounts.assigned_name("D:\\elsewhere\\ghost") == "subacct"
    # 写しは同じ役を保つ（resumer の動きが変わらない）
    assert accounts.mirror_body(accounts.state())["roles"] == [
        "ghost",
        "sub-role",
        "twin",
    ]
    # 2回目は取り込まない（正本は台帳）
    assert "skipped" in accounts.import_trial_roles(lambda name: [])


def test_import_refuses_other_config_dir(homes, tmp_path):
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": str(tmp_path), "home": homes["sub_home"], "roles": ["a"]}, f
        )
    report = accounts.import_trial_roles(lambda name: [])
    assert "違います" in report["skipped"]
    assert not os.path.exists(accounts.STATE_PATH)


def test_broken_state_is_an_error(homes):
    with open(accounts.STATE_PATH, "w", encoding="utf-8") as f:
        f.write("{broken")
    with pytest.raises(accounts.AccountError):
        accounts.account_for(homes["sub_cwd"])


# --- 評価役の指摘（2026-10-28 1周目）の再現 ------------------------------------


def test_typo_in_config_dir_does_not_make_sub_the_main(homes):
    """置き場のキーを書き損じた sub が先に並んでも、本家に化けない（高1）。"""
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    settings = dict(homes["settings"])
    sub = dict(settings["accounts"][1])
    sub["configdir"] = sub.pop("config_dir")
    settings["accounts"] = [sub, settings["accounts"][0]]
    write_settings(settings)
    with pytest.raises(accounts.AccountError):
        accounts.require_usable("subacct")
    with pytest.raises(accounts.AccountError):
        accounts.require_usable(accounts.account_for(homes["sub_cwd"])["name"])
    # 割り当ての無い作業場は、名前の無い既定の本家で起きる（今までどおり）
    assert accounts.account_for(homes["main_cwd"])["main"]


def test_explicit_main_flag(homes):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        settings["accounts"][1],
        dict(settings["accounts"][0], main=True),
    ]
    write_settings(settings)
    assert accounts.main_account()["name"] == "mainacct"
    assert accounts.get("subacct")["error"] is None
    # 本家の印を付けた定義に置き場があれば使わない
    settings["accounts"] = [dict(settings["accounts"][0], main=True)]
    write_settings(settings)
    assert accounts.get("subacct")["error"]


def test_assigned_name_resolving_to_main_is_refused(homes):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    settings = dict(homes["settings"])
    settings["accounts"] = [{"name": "subacct", "main": True}]
    write_settings(settings)
    with pytest.raises(accounts.AccountError):
        accounts.account_for(homes["sub_cwd"])


def _write_mirror(homes, roles):
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": homes["sub_cfg"], "home": homes["sub_home"], "roles": roles},
            f,
        )


def test_no_state_with_mirror_roles_imports_before_assign(homes):
    """台帳が無いまま割り当てても、写しの役を先に取り込む（高2）。"""
    _write_mirror(homes, ["old-role", "sub-role"])
    found = {"old-role": [("C:\\w\\old-role", "developing/old-role")]}
    accounts.set_role_resolver(lambda name: found.get(name, []))
    accounts.assign(homes["main_cwd"], "developing/plain-role", "subacct", "create")
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["old-role", "plain-role", "sub-role"]
    assert accounts.assigned_name("C:\\w\\old-role") == "subacct"


def test_no_state_without_resolver_keeps_roles_and_refuses_assign(homes):
    _write_mirror(homes, ["sub-role"])
    # 取り込めなくても、写しの役は本家へ流さない
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"
    with pytest.raises(accounts.AccountError) as ctx:
        accounts.assign(homes["main_cwd"], "developing/plain-role", "subacct", "x")
    assert "まだ取り込めていない" in str(ctx.value)
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["sub-role"]
    assert not os.path.exists(accounts.STATE_PATH)


def test_pending_import_reason_and_retry_cache(homes, monkeypatch):
    """取り込めない理由を出し、しばらくは置き場を探し直さない（2周目の中1・低2）。"""
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {
                "config_dir": "C:\\elsewhere",
                "home": homes["sub_home"],
                "roles": ["sub-role"],
            },
            f,
        )
    accounts.set_role_resolver(lambda name: [])
    tries = []
    original = accounts.import_trial_roles
    monkeypatch.setattr(
        accounts,
        "import_trial_roles",
        lambda resolve: tries.append(1) or original(resolve),
    )
    reason = accounts.pending_import_reason()
    assert "違います" in reason
    accounts.pending_import_reason()
    assert accounts.state()["pending_import"] == reason
    # 失敗の直後は取り込みを試し直さない（一覧の配信のたびに置き場を探さない）
    assert len(tries) == 1
    assert not os.path.exists(accounts.STATE_PATH)
    # 間が過ぎれば試し直す
    monkeypatch.setattr(accounts, "IMPORT_RETRY_SEC", 0)
    accounts.pending_import_reason()
    assert len(tries) == 2
    with pytest.raises(accounts.AccountError) as ctx:
        accounts.assign(homes["main_cwd"], "developing/plain-role", "subacct", "x")
    assert "違います" in str(ctx.value)


def test_api_accounts_reports_pending(app_module, homes):
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": "C:\\elsewhere", "home": homes["sub_home"], "roles": ["x"]},
            f,
        )
    body = (
        app_module.app.test_client()
        .get("/api/accounts", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        .get_json()
    )
    assert body["pending_import"]
    status, moved = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct", "launch": False}
    )
    assert status == 400 and "何も止めていません" in moved["error"]


def test_broken_account_label_is_marked(homes, tmp_path):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        settings["accounts"][0],
        dict(settings["accounts"][1], home=str(tmp_path / "missing")),
    ]
    write_settings(settings)
    assert accounts.get("subacct")["label"].endswith("（設定の誤り）")


def test_hand_edited_mirror_is_not_overwritten(homes):
    """写しが手で直されていたら、次の割り当てで黙って消さない（中3）。"""
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    _write_mirror(homes, ["hand-added", "sub-role"])
    result = accounts.assign(
        homes["main_cwd"], "developing/plain-role", "subacct", "move"
    )
    assert result["mirror"]["written"] is False
    assert "手で直されている" in result["mirror"]["error"]
    # resumer がどの役をどちらで起こすかを、役の名前で言う
    assert "plain-role" in result["mirror"]["error"]
    assert "hand-added" in result["mirror"]["error"]
    with open(homes["mirror"], encoding="utf-8") as f:
        assert "hand-added" in json.load(f)["roles"]
    # 除けると次の割り当てで台帳どおりに書き直す
    os.remove(homes["mirror"])
    accounts.assign(homes["main_cwd"], "developing/plain-role", "mainacct", "move")
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["sub-role"]


def test_moving_one_legacy_twin_keeps_the_other(homes, tmp_path):
    """フォルダ名の印は、同じ名前のほかの作業場から外さない（中5）。"""
    _write_mirror(homes, ["foo"])
    a, b = tmp_path / "dev" / "foo", tmp_path / "work" / "foo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    accounts.import_trial_roles(
        lambda name: [(str(a), "developing/foo"), (str(b), "working/foo")]
    )
    accounts.assign(str(a), "developing/foo", "mainacct", "move")
    assert accounts.assigned_name(str(a)) is None
    assert accounts.assigned_name(str(b)) == "subacct"


# --- 起動 -------------------------------------------------------------------


@pytest.fixture
def spawns(monkeypatch):
    """起動の道を差し替えて、どの道で何を渡したかを記録する。"""
    calls = {"trusted": []}

    def capture(route, env=None, overrides=None, run_level=None):
        calls.update(
            {"route": route, "env": env, "overrides": overrides, "run_level": run_level}
        )
        return os.getpid()

    monkeypatch.setattr(
        sm, "_spawn_detached", lambda argv, cwd, env: capture("direct", env=env)
    )
    monkeypatch.setattr(
        sm,
        "_spawn_restricted",
        lambda argv, cwd, env_overrides=None: capture(
            "restricted", overrides=env_overrides
        ),
    )
    monkeypatch.setattr(
        sm,
        "_spawn_via_task",
        lambda argv, cwd, target, run_level="LeastPrivilege", env_overrides=None: capture(
            "task:" + str(target), overrides=env_overrides, run_level=run_level
        ),
    )
    monkeypatch.setattr(
        sm,
        "ensure_trusted",
        lambda path, config_path=None: calls["trusted"].append(config_path),
    )
    monkeypatch.setattr(sm, "record_launched", lambda pid, name, cwd: None)
    return calls


def test_sub_account_direct_route_gets_env(homes, spawns):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    result = sm.launch_session(homes["sub_cwd"], claude_exe=sys.executable)
    assert result["account"] == "subacct"
    assert spawns["route"] == "direct"
    assert spawns["env"]["CLAUDE_CONFIG_DIR"] == homes["sub_cfg"]
    assert spawns["env"]["HOME"] == homes["sub_home"]
    assert spawns["trusted"] == [os.path.join(homes["sub_cfg"], ".claude.json")]


def test_main_account_keeps_main_env(homes, spawns):
    result = sm.launch_session(homes["main_cwd"], claude_exe=sys.executable)
    assert result["account"] == "mainacct"
    assert "CLAUDE_CONFIG_DIR" not in spawns["env"]
    assert spawns["trusted"] == [None]


def test_sub_account_on_other_session_goes_by_task_with_env(homes, spawns):
    """別の Windows セッションへも断らずに、起動役へ置き場を渡して起こす（00:25 に断られた件）。"""
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    other = "1" if sm.process_win_session(os.getpid()) == 0 else "0"
    sm.launch_session(homes["sub_cwd"], claude_exe=sys.executable, win_session=other)
    assert spawns["route"] == "task:" + other
    assert spawns["overrides"]["CLAUDE_CONFIG_DIR"] == homes["sub_cfg"]
    assert spawns["overrides"]["USERPROFILE"] == homes["sub_home"]


def test_sub_account_with_elevation_goes_with_env(homes, spawns, monkeypatch):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    monkeypatch.setattr(sm, "process_elevation", lambda pid: True)
    sm.launch_session(homes["sub_cwd"], claude_exe=sys.executable, elevation="standard")
    assert spawns["route"] == "restricted"
    assert spawns["overrides"]["CLAUDE_CONFIG_DIR"] == homes["sub_cfg"]


def test_named_account_must_match_assignment(homes, spawns):
    with pytest.raises(sm.SessionError) as ctx:
        sm.launch_session(
            homes["main_cwd"], claude_exe=sys.executable, account="subacct"
        )
    assert "割り当てられています" in str(ctx.value)
    assert spawns["trusted"] == [] and "route" not in spawns
    # 割り当てどおりの名前は通る
    sm.launch_session(homes["main_cwd"], claude_exe=sys.executable, account="mainacct")
    assert spawns["route"] == "direct"


def test_unusable_assigned_account_is_refused_before_trust(homes, spawns, tmp_path):
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    settings = dict(homes["settings"])
    settings["accounts"] = [
        settings["accounts"][0],
        dict(settings["accounts"][1], home=str(tmp_path / "missing")),
    ]
    write_settings(settings)
    with pytest.raises(sm.SessionError) as ctx:
        sm.launch_session(homes["sub_cwd"], claude_exe=sys.executable)
    assert "の設定が使えません" in str(ctx.value)
    assert spawns["trusted"] == [] and "route" not in spawns


def test_resume_validation_after_account_check(homes, spawns, monkeypatch):
    called = []
    monkeypatch.setattr(
        sm, "validate_resume_target", lambda cwd, sid: called.append(sid)
    )
    with pytest.raises(sm.SessionError):
        sm.launch_session(
            homes["main_cwd"],
            claude_exe=sys.executable,
            account="subacct",
            resume_session_id="0123abcd-0000-0000-0000-000000000000",
        )
    assert called == [] and spawns["trusted"] == []


def test_launcher_request_keeps_only_account_keys():
    request = sm._launcher_request(
        ["a"], "C:\\x", "C:\\r.json", {"CLAUDE_CONFIG_DIR": "C:\\c", "PATH": "evil"}
    )
    assert request["env_overrides"] == {"CLAUDE_CONFIG_DIR": "C:\\c"}
    assert "env_overrides" not in sm._launcher_request(
        ["a"], "C:\\x", "C:\\r.json", None
    )


def test_task_launcher_applies_overrides(tmp_path):
    """起動役（本物のスクリプト）が要求の差分を子の env に載せ、ほかのキーは載せない。"""
    out = tmp_path / "env.json"
    child = (
        "import json,os,sys;"
        "json.dump({k: os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR','HOME','USERPROFILE','EVIL')},"
        f"open(r'{out}','w'))"
    )
    request = tmp_path / "req.json"
    response = tmp_path / "res.json"
    request.write_text(
        json.dumps(
            {
                "argv": [sys.executable, "-c", child],
                "cwd": str(tmp_path),
                "response": str(response),
                "env_overrides": {
                    "CLAUDE_CONFIG_DIR": "C:\\cfg-x",
                    "HOME": "C:\\home-x",
                    "USERPROFILE": "C:\\home-x",
                    "EVIL": "1",
                },
            }
        ),
        "utf-8",
    )
    script = os.path.join(PROJECT_ROOT, "session_task_launcher.py")
    subprocess.run([sys.executable, script, str(request)], check=True, timeout=30)
    assert "pid" in json.loads(response.read_text("utf-8"))
    deadline = time.time() + 20
    while not out.exists() and time.time() < deadline:
        time.sleep(0.2)
    time.sleep(0.3)
    env = json.loads(out.read_text("utf-8"))
    assert env == {
        "CLAUDE_CONFIG_DIR": "C:\\cfg-x",
        "HOME": "C:\\home-x",
        "USERPROFILE": "C:\\home-x",
        "EVIL": None,
    }


def test_task_launcher_sanitizes_without_overrides(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    env = session_task_launcher.sanitized_env(None)
    assert "CLAUDE_CODE_CHILD_SESSION" not in env
    assert session_task_launcher.sanitized_env({"HOME": 3})["HOME"] == os.environ.get(
        "HOME"
    )


# --- 一覧・状態・会話の記録 --------------------------------------------------


def _write_state(home, name, data):
    with open(os.path.join(home, "sessions", name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_list_sessions_reads_both_homes_with_account(homes, monkeypatch):
    monkeypatch.setattr(
        sm, "is_session_alive", lambda data: data.get("pid") in (101, 202)
    )
    _write_state(homes["main_home"], "101.json", {"pid": 101, "name": "main"})
    _write_state(homes["sub_cfg"], "202.json", {"pid": 202, "name": "sub"})
    _write_state(homes["sub_cfg"], "101.json", {"pid": 101, "name": "dup"})
    rows = {s["name"]: s["account"] for s in sm.list_sessions()}
    assert rows == {"main": "mainacct", "sub": "subacct"}


def test_read_state_prefers_alive(homes, monkeypatch):
    monkeypatch.setattr(sm, "is_session_alive", lambda data: data.get("where") == "sub")
    _write_state(homes["main_home"], "303.json", {"pid": 303, "where": "main"})
    _write_state(homes["sub_cfg"], "303.json", {"pid": 303, "where": "sub"})
    assert sm._read_state(303)["where"] == "sub"


def test_transcript_dirs_follow_assignment(homes):
    sid = "0123abcd-0000-0000-0000-000000000000"
    for home in (homes["main_home"], homes["sub_cfg"]):
        folder = os.path.join(home, "projects", "x")
        os.makedirs(folder, exist_ok=True)
        open(os.path.join(folder, f"{sid}.jsonl"), "w").close()
    assert len(sm._transcript_paths(sid)) == 2
    assert sm.transcript_dir_for(homes["sub_cwd"]).startswith(homes["main_home"])
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    assert sm.transcript_dir_for(homes["sub_cwd"]).startswith(homes["sub_cfg"])


def test_unusable_extra_home_is_not_listed(homes, tmp_path):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        settings["accounts"][0],
        dict(settings["accounts"][1], config_dir=str(tmp_path / "gone")),
    ]
    write_settings(settings)
    assert sm._all_homes() == [homes["main_home"]]


# --- 信頼登録 ----------------------------------------------------------------


def test_trust_written_to_account_config_only(tmp_path, monkeypatch):
    main_config = tmp_path / "main.claude.json"
    main_config.write_text(json.dumps({"projects": {}, "who": "main"}), "utf-8")
    monkeypatch.setattr(sm, "CLAUDE_CONFIG", str(main_config))
    sub_config = tmp_path / "cfg" / ".claude.json"
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(sm.SessionError) as ctx:
        sm.ensure_trusted(str(project), config_path=str(sub_config))
    assert "アカウントの設定ファイル" in str(ctx.value)
    assert not sub_config.exists()
    sub_config.parent.mkdir()
    sub_config.write_text(json.dumps({"projects": {}, "who": "sub"}), "utf-8")
    assert sm.ensure_trusted(str(project), config_path=str(sub_config))
    saved = json.loads(sub_config.read_text("utf-8"))
    assert saved["who"] == "sub"
    assert sm.is_trusted(str(project), config_path=str(sub_config))
    assert not sm.is_trusted(str(project))


# --- 週次の使用% -------------------------------------------------------------


def _statusline(directory, name, used, resets, mtime=None):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "rate_limits": {
                    "seven_day": {"used_percentage": used, "resets_at": resets}
                }
            },
            f,
        )
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_weekly_from_statusline(homes):
    now = time.time()
    _statusline(homes["statusline"], "old.json", 50, now + 3600, mtime=now - 600)
    _statusline(homes["statusline"], "new.json", 7, now + 3600, mtime=now - 60)
    result = accounts.weekly_usage(accounts.get("subacct"), now=now)
    assert result["known"] and result["used_percentage"] == 7.0
    assert result["stale"] is False and result["source"] == "statusline"


def test_weekly_unknown_when_reset_passed_or_missing(homes, tmp_path):
    now = time.time()
    _statusline(homes["statusline"], "a.json", 30, now - 10)
    assert accounts.weekly_usage(accounts.get("subacct"), now=now)["known"] is False
    # 取り元が無いアカウントも「分からない」
    result = accounts.weekly_usage(accounts.get("mainacct"), now=now)
    assert result["known"] is False and result["used_percentage"] is None


def test_weekly_from_limits_url(homes, monkeypatch):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        dict(settings["accounts"][0], limits_url="http://x/api/limits"),
        settings["accounts"][1],
    ]
    write_settings(settings)

    class Res:
        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = {
        "available": True,
        "weekly_limit": {
            "used_percentage": 49.0,
            "resets_at": "2026-11-01T19:59:59+09:00",
        },
        "fetched_at": "t",
        "is_stale": False,
    }
    monkeypatch.setattr(
        accounts.urllib.request, "urlopen", lambda url, timeout: Res(body)
    )
    result = accounts.weekly_usage(accounts.get("mainacct"))
    assert result["known"] and result["used_percentage"] == 49.0
    accounts._usage_cache.clear()

    def boom(url, timeout):
        raise OSError("down")

    monkeypatch.setattr(accounts.urllib.request, "urlopen", boom)
    assert accounts.weekly_usage(accounts.get("mainacct"))["known"] is False


def _known(used, stale=False, resets_at=None):
    return {
        "known": True,
        "used_percentage": used,
        "stale": stale,
        "resets_at": resets_at,
    }


def _both(weekly, five):
    return {"weekly": weekly, "five_hour": five}


def test_default_choice(homes):
    known = _known
    # 枠の大きさが同じ（既定 1）なら、週次・5時間枠の少ないほうの残りで比べる
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(49), known(10)),
            "subacct": _both(known(1), known(5)),
        }
    )
    assert name == "subacct" and "残りが多いほう" in reason
    assert (
        accounts.default_choice(
            {
                "mainacct": _both(known(10), known(10)),
                "subacct": _both(known(60), known(5)),
            }
        )[0]
        == "mainacct"
    )
    # 片方が分からない・古いときは比べない（本家）
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(90), known(10)),
            "subacct": _both({"known": False}, known(1)),
        }
    )
    assert name == "mainacct" and "分からない" in reason
    assert (
        accounts.default_choice(
            {
                "mainacct": _both(known(90), known(10)),
                "subacct": _both(known(1, stale=True), known(1)),
            }
        )[0]
        == "mainacct"
    )


def test_default_choice_five_hour_line_and_capacity(homes):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        dict(settings["accounts"][0], capacity=20, five_hour_capacity=20),
        dict(settings["accounts"][1], capacity=1),
    ]
    write_settings(settings)
    known = _known
    # 5時間枠が線（既定 80%）以上なら外す（週次の残りが多くても）
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(50), known(3)),
            "subacct": _both(
                known(12), known(85, resets_at="2026-10-28T03:50:00+09:00")
            ),
        }
    )
    assert name == "mainacct"
    assert "5時間枠 85% 使用で、目安 80% に達しています" in reason
    assert "（10/28 03:50 に回復）" in reason
    # %では sub のほうが空いていても、枠の大きさを入れると本家
    # （本家 min((100-50)*20, (100-3)*20)=1000 > sub min((100-12)*1, (100-10)*1)=88）
    name, _ = accounts.default_choice(
        {
            "mainacct": _both(known(50), known(3)),
            "subacct": _both(known(12), known(10)),
        }
    )
    assert name == "mainacct"
    # 本家がほぼ尽きていれば sub
    assert (
        accounts.default_choice(
            {
                "mainacct": _both(known(99), known(10)),
                "subacct": _both(known(12), known(10)),
            }
        )[0]
        == "subacct"
    )
    # 線は設定で変えられる（アカウントの定義が全体より優先）
    settings["five_hour_max_pct"] = 90
    settings["accounts"][1]["five_hour_max_pct"] = 50
    write_settings(settings)
    sub = accounts.get("subacct")
    assert accounts.policy(sub) == {
        "capacity": 1.0,
        "five_hour_capacity": 1.0,
        "five_hour_unlimited": False,
        "five_hour_max_pct": 50.0,
    }
    assert accounts.policy(accounts.get("mainacct"))["five_hour_max_pct"] == 90.0
    assert accounts.over_line(sub, _both(known(1), known(55)))
    assert accounts.over_line(sub, _both(known(1), known(45))) is None
    # 両方が線を超えたら本家
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(10), known(95)),
            "subacct": _both(known(1), known(60)),
        }
    )
    assert name == "mainacct" and "どのアカウントも既定から外れるので" in reason
    # 本家だけが線を超えたら sub
    assert (
        accounts.default_choice(
            {
                "mainacct": _both(known(10), known(95)),
                "subacct": _both(known(1), known(10)),
            }
        )[0]
        == "subacct"
    )


def test_default_choice_separates_weekly_and_five_hour_capacity(homes):
    """週の枠と5時間枠は別の比で数える（2026-10-30 管理役の訂正: 旧 20x の週の枠は Pro の約10倍）。"""
    settings = dict(homes["settings"])
    settings["accounts"] = [
        dict(settings["accounts"][0], capacity=10, five_hour_capacity=20),
        dict(settings["accounts"][1], capacity=1, five_hour_capacity=1),
    ]
    write_settings(settings)
    known = _known
    # 週: (100-92)*10=80 < (100-10)*1=90 → sub（5時間は本家が (100-0)*20 で余っていても、少ないほうで比べる）
    assert (
        accounts.default_choice(
            {
                "mainacct": _both(known(92), known(0)),
                "subacct": _both(known(10), known(0)),
            }
        )[0]
        == "subacct"
    )
    usages = {
        "mainacct": _both(known(0), known(50)),
        "subacct": _both(known(10), known(10)),
    }
    # 5時間: 本家 (100-50)*20=1000・週 1000 → 本家（5時間の比が 1 なら 50 < 90 で sub になる）
    assert accounts.default_choice(usages)[0] == "mainacct"
    settings["accounts"][0]["five_hour_capacity"] = 1
    write_settings(settings)
    assert accounts.default_choice(usages)[0] == "subacct"


def test_five_hour_unlimited_account(homes):
    """5時間枠の無いアカウントは、5時間枠を比べから外す（値が無くても「分からない」と数えない）。"""
    settings = dict(homes["settings"])
    settings["accounts"] = [
        dict(settings["accounts"][0], capacity=10, five_hour_unlimited=True),
        dict(settings["accounts"][1], capacity=1),
    ]
    write_settings(settings)
    known = _known
    main = accounts.get("mainacct")
    sub = accounts.get("subacct")
    # 本家の5時間枠が無い（known: false・null）・値が返る（モニターは Ultra にも返す）のどれでも、
    # 本家は週の枠だけ・sub は5時間枠も入れて比べる
    for five in ({"known": False}, None, known(60)):
        usages = {
            "mainacct": _both(known(95), five),
            "subacct": _both(known(10), known(70)),
        }
        assert accounts.headroom(main, usages["mainacct"]) == 50.0
        assert accounts.headroom(sub, usages["subacct"]) == 30.0
        name, reason = accounts.default_choice(usages)
        assert name == "mainacct"
        assert "分からない" not in reason and "枠の大きさを入れて比べた" in reason
    # sub の5時間枠が少なくなければ sub
    name, _ = accounts.default_choice(
        {
            "mainacct": _both(known(95), {"known": False}),
            "subacct": _both(known(10), known(5)),
        }
    )
    assert name == "subacct"
    # 値が目安以上なら（本人確認の前の一時的な上限の保険）、ほかと同じに既定から外す
    assert accounts.over_line(main, _both(known(1), known(85)))
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(1), known(85)),
            "subacct": _both(known(10), known(5)),
        }
    )
    assert name == "subacct" and "外したもの: mainacct" in reason
    # 古い5時間枠も「古い値」と数えない
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(95), known(10, stale=True)),
            "subacct": _both(known(10), known(70)),
        }
    )
    assert name == "mainacct" and "古い値" not in reason
    # 5時間枠の無いアカウントでも、週の枠が分からなければ比べない
    name, reason = accounts.default_choice(
        {
            "mainacct": _both({"known": False}, {"known": False}),
            "subacct": _both(known(10), known(5)),
        }
    )
    assert name == "mainacct" and "週の枠が分からない" in reason
    # 印の無いアカウントの5時間枠が分からないときは今までどおり週の枠だけ（50 < 90 で sub）
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(95), {"known": False}),
            "subacct": _both(known(10), {"known": False}),
        }
    )
    assert name == "subacct" and "subacct アカウントの5時間枠が分からない" in reason
    assert "mainacctの5時間枠" not in reason


def test_policy_ignores_bad_values(homes, capsys):
    accounts._warned_policy.clear()
    settings = dict(homes["settings"])
    settings["five_hour_max_pct"] = "80"
    settings["accounts"] = [
        dict(
            settings["accounts"][0],
            capacity=0,
            five_hour_max_pct=150,
            five_hour_unlimited="yes",
        ),
        dict(settings["accounts"][1], capacity=True, five_hour_capacity=-3),
    ]
    write_settings(settings)
    for name in ("mainacct", "subacct"):
        assert accounts.policy(accounts.get(name)) == {
            "capacity": 1.0,
            "five_hour_capacity": 1.0,
            "five_hour_unlimited": False,
            "five_hour_max_pct": 80.0,
        }
    # 使えない値はログに一言（同じ値は1回だけ）
    first = capsys.readouterr().out
    assert "mainacct の five_hour_max_pct" in first and "150" in first
    assert "'80'" in first and "subacct の capacity" in first
    assert "subacct の five_hour_capacity" in first
    assert "mainacct の five_hour_unlimited" in first
    accounts.policy(accounts.get("mainacct"))
    assert capsys.readouterr().out == ""


def test_over_line_wording(homes):
    known = _known
    sub = accounts.get("subacct")
    # 目安ちょうどでも外す（「達しています」）・%は整数
    detail = accounts.over_detail(sub, _both(known(1), known(80)))
    assert detail["window"] == "five_hour"
    assert (
        detail["text"]
        == "subacct アカウント: 5時間枠 80% 使用で、目安 80% に達しています"
    )
    assert "85% 使用" in accounts.over_line(sub, _both(known(1), known(85.4)))
    # 週の枠を使い切ったときは目安の括弧を付けない
    detail = accounts.over_detail(
        sub, _both(known(100, resets_at="2026-11-01T19:59:59+09:00"), known(10))
    )
    assert detail["window"] == "weekly"
    assert (
        detail["text"]
        == "subacct アカウント: 週の枠を使い切っています（11/01 19:59 に回復）"
    )
    # 古い値は目安超えと数えない
    assert accounts.over_line(sub, _both(known(1), known(99, stale=True))) is None


def test_default_choice_compares_weekly_when_five_hour_unknown(homes):
    known = _known
    # 本家の5時間枠が分からなくても本家に固定せず、週の枠だけで比べる
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(90), {"known": False}),
            "subacct": _both(known(10), known(5)),
        }
    )
    assert name == "subacct"
    assert "mainacctの5時間枠が分からないので、週の枠だけで比べた" in reason
    # 括弧を入れ子にしない
    assert reason.count("（") == 1
    # 古い5時間枠も同じ（どのアカウントのどの枠かを出す）
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(10), known(5)),
            "subacct": _both(known(50), known(99, stale=True)),
        }
    )
    assert name == "mainacct"
    assert "subacct アカウントの5時間枠が古い値なので" in reason
    # 週の枠が分からなければ今までどおり本家（どの枠かを出す）
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(known(90), known(10)),
            "subacct": _both(known(1, stale=True), known(1)),
        }
    )
    assert (
        name == "mainacct"
        and "subacct アカウントの週の枠が古い値なので比べられず" in reason
    )
    # 括弧が2つ続かない（外したものは「。」で続ける）
    _, reason = accounts.default_choice(
        {
            "mainacct": _both(known(10), known(95)),
            "subacct": _both(known(1), known(10)),
        }
    )
    assert (
        "）（" not in reason
        and "subacct アカウントにしています。外したもの: mainacct:" in reason
    )


def test_five_hour_from_limits_url(homes, monkeypatch):
    settings = dict(homes["settings"])
    settings["accounts"] = [
        dict(settings["accounts"][0], limits_url="http://x/api/limits"),
        settings["accounts"][1],
    ]
    write_settings(settings)
    body = {
        "available": True,
        "weekly_limit": {"used_percentage": 20.0, "resets_at": None},
        "five_hour_limit": {
            "used_percentage": 61.0,
            "resets_at": "2026-10-28T03:50:00+09:00",
        },
        "fetched_at": "t",
        "is_stale": False,
    }

    class Res:
        def read(self):
            return json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(accounts.urllib.request, "urlopen", lambda url, timeout: Res())
    result = accounts.usage(accounts.get("mainacct"))
    assert result["five_hour"]["used_percentage"] == 61.0
    assert accounts.format_reset(result["five_hour"]["resets_at"]) == "10/28 03:50"
    # 5時間枠だけ無い応答は、週の枠を残して5時間枠だけ分からない
    accounts._usage_cache.clear()
    del body["five_hour_limit"]
    result = accounts.usage(accounts.get("mainacct"))
    assert result["weekly"]["known"] and result["five_hour"]["known"] is False
    assert "5時間枠" in result["five_hour"]["detail"]


def test_statusline_missing_names_the_folder(homes):
    result = accounts.usage(accounts.get("subacct"))
    assert homes["statusline"] in result["five_hour"]["detail"]
    assert homes["statusline"] in result["weekly"]["detail"]
    # 画面の注記に添える置き場名（place）も持つ
    assert result["five_hour"]["place"] == homes["statusline"]
    assert result["weekly"]["place"] == homes["statusline"]


def test_format_reset_one_format():
    assert accounts.format_reset("2026-10-28T03:50:00+09:00") == "10/28 03:50"
    assert accounts.format_reset("2026-10-27T18:50:00Z") == "10/28 03:50"
    assert accounts.format_reset(None) == "" and accounts.format_reset("x") == ""
    assert accounts.format_reset(True) == ""
    # epoch 秒も日本時間で同じ書式（2025-10-27T18:50:00Z）
    assert accounts.format_reset(1761591000) == "10/28 03:50"
    assert accounts.format_reset(1761591000.0) == "10/28 03:50"


def test_round_half_up_matches_math_round():
    """画面の Math.round とそろえる（round は偶数丸めで 84.5 → 84 になる）。"""
    assert accounts.round_half_up(84.5) == 85
    assert accounts.round_half_up(85.5) == 86
    assert accounts.round_half_up(84.4) == 84
    assert accounts.round_half_up(0) == 0


def test_five_hour_from_statusline(homes):
    now = time.time()
    path = os.path.join(homes["statusline"], "s.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "rate_limits": {
                    "seven_day": {"used_percentage": 12, "resets_at": now + 86400},
                    "five_hour": {"used_percentage": 88, "resets_at": now + 3600},
                }
            },
            f,
        )
    result = accounts.usage(accounts.get("subacct"), now=now)
    assert result["five_hour"]["known"] and result["five_hour"]["used_percentage"] == 88
    assert result["weekly"]["used_percentage"] == 12
    # 5時間枠は戻りの時刻を過ぎていれば 0%（週次は分からないのまま）
    accounts._usage_cache.clear()
    later = now + 7200
    result = accounts.usage(accounts.get("subacct"), now=later)
    assert result["five_hour"]["used_percentage"] == 0.0
    assert result["five_hour"]["reset_passed"] is True
    assert result["weekly"]["known"] is True


def _transcript(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def test_quota_stop(tmp_path):
    now = time.time()
    path = str(tmp_path / "t.jsonl")
    stop = {
        "type": "assistant",
        "error": "rate_limit",
        "isApiErrorMessage": True,
        "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": now + 600},
        "timestamp": "2026-10-28T00:00:00Z",
    }
    _transcript(path, [{"type": "user"}, stop, {"type": "system"}])
    found = accounts.quota_stop(path, now=now)
    assert found["window"] == "five_hour" and found["window_label"] == "5時間枠"
    assert found["resets_at"] and found["recovered"] is False
    # 戻りの時刻を過ぎても、人の次の入力があるまでは出し続け、回復した印を付ける
    later = accounts.quota_stop(path, now=now + 601)
    assert later["window"] == "five_hour" and later["recovered"] is True
    # 印はキャッシュに残らない（時刻の前に読み直せば False のまま）
    assert accounts.quota_stop(path, now=now)["recovered"] is False
    # 後で人が打ち直していれば止まっていない
    _transcript(path, [stop, {"type": "user", "message": {"content": "続けて"}}])
    assert accounts.quota_stop(path, now=now) is None
    # 普通の応答で終わっていれば None・週次は weekly
    _transcript(path, [stop, {"type": "assistant"}])
    assert accounts.quota_stop(path, now=now) is None
    weekly = dict(stop, quotaLimits={"rateLimitType": "seven_day", "resetsAt": now + 9})
    _transcript(path, [weekly])
    assert accounts.quota_stop(path, now=now)["window"] == "weekly"
    assert accounts.quota_stop(str(tmp_path / "none.jsonl")) is None


def test_quota_stop_skips_meta_and_sidechain(tmp_path):
    """枠切れの後の isMeta の user 行・isSidechain の行は「人の入力」「普通の応答」と数えない。"""
    now = time.time()
    path = str(tmp_path / "t.jsonl")
    stop = {
        "type": "assistant",
        "error": "rate_limit",
        "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": now + 600},
    }
    _transcript(
        path,
        [
            stop,
            {"type": "user", "isMeta": True, "message": {"content": "meta"}},
            {"type": "user", "isSidechain": True, "message": {"content": "sub"}},
            {"type": "assistant", "isSidechain": True},
        ],
    )
    assert accounts.quota_stop(path, now=now)["window"] == "five_hour"
    # 陽性対照: 同じ形で isSidechain の無い user 行なら止まっていない
    _transcript(path, [stop, {"type": "user", "message": {"content": "続けて"}}])
    assert accounts.quota_stop(path, now=now) is None


# --- 使用量の集計・ログイン更新 ------------------------------------------------


def test_usage_tracker_counts_each_account(tmp_path):
    now = time.time()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
    sources = []
    for name in ("mainacct", "subacct"):
        projects = tmp_path / name / "projects"
        folder = projects / "f"
        folder.mkdir(parents=True)
        record = {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": f"m-{name}",
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 1_000_000},
            },
        }
        (folder / f"{name}.jsonl").write_text(json.dumps(record) + "\n", "utf-8")
        sources.append((name, str(projects)))
    tracker = ut.UsageTracker(lambda: sources)
    snap = tracker.refresh(now=now)
    assert snap["per_account"] == {"mainacct": 2.0, "subacct": 2.0}
    assert snap["total_cost"] == 4.0
    assert set(snap["per_session"]) == {"mainacct", "subacct"}
    assert ut.summary(tracker)["per_account"] == {"mainacct": 2.0, "subacct": 2.0}


def test_relogin_reads_each_account_expiry(homes, monkeypatch, tmp_path):
    main_cred = tmp_path / "main_cred.json"
    main_cred.write_text(
        json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 111}}), "utf-8"
    )
    monkeypatch.setattr(rl, "CREDENTIALS_PATH", str(main_cred))
    sub_cred = os.path.join(homes["sub_cfg"], ".credentials.json")
    with open(sub_cred, "w", encoding="utf-8") as f:
        json.dump(
            {"claudeAiOauth": {"refreshTokenExpiresAt": 222, "accessToken": "secret"}},
            f,
        )
    assert rl.read_expiry()["refresh_expires_at"] == 111
    sub = rl.read_expiry(accounts.get("subacct"))
    assert sub["refresh_expires_at"] == 222
    assert "secret" not in json.dumps(sub)
    listed = {
        a["name"]: a["expiry"]["refresh_expires_at"] for a in rl.status()["accounts"]
    }
    assert listed == {"mainacct": 111, "subacct": 222}


# --- 画面・API（claude-web-app.py） ---------------------------------------------


@pytest.fixture
def app_module(homes, monkeypatch):
    from test_integration_spec import load_app_module

    module = load_app_module()
    projects = {
        "developing/sub-role": homes["sub_cwd"],
        "developing/plain-role": homes["main_cwd"],
    }
    monkeypatch.setattr(
        module, "resolve_project_path", lambda key, must_exist=True: projects.get(key)
    )
    monkeypatch.setattr(module, "broadcast_sessions", lambda: None)
    return module


def test_api_accounts_lists_usage_and_project(app_module, homes):
    now = time.time()
    _statusline(homes["statusline"], "s.json", 3, now + 3600)
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    client = app_module.app.test_client()
    body = client.get(
        "/api/accounts?project=developing/sub-role",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    ).get_json()
    names = {a["name"]: a for a in body["accounts"]}
    assert names["subacct"]["weekly"]["used_percentage"] == 3.0
    assert names["mainacct"]["weekly"]["known"] is False  # 取り元が無い＝分からない
    assert body["default"] == "mainacct"  # 片方が分からないので比べない
    assert body["project"] == {
        "project_key": "developing/sub-role",
        "account": "subacct",
        "assigned": True,
        "reason": "assigned",
    }


def _statusline_both(directory, weekly, five, resets):
    with open(os.path.join(directory, "both.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "rate_limits": {
                    "seven_day": {"used_percentage": weekly, "resets_at": resets},
                    "five_hour": {"used_percentage": five, "resets_at": resets},
                }
            },
            f,
        )


def test_api_accounts_has_window_keys(app_module, homes):
    """/api/accounts の各アカウントに5時間枠・目安・外す理由・回復の時刻（サーバーが書いた文字列）を載せる。"""
    resets = time.time() + 3600
    _statusline_both(homes["statusline"], 12, 85, resets)
    body = (
        app_module.app.test_client()
        .get("/api/accounts", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        .get_json()
    )
    sub = {a["name"]: a for a in body["accounts"]}["subacct"]
    for key in (
        "five_hour",
        "weekly",
        "five_hour_max_pct",
        "capacity",
        "five_hour_capacity",
        "five_hour_unlimited",
        "over_line",
        "over_window",
        "five_hour_resets",
        "weekly_resets",
    ):
        assert key in sub, key
    assert sub["five_hour_capacity"] == 1.0 and sub["five_hour_unlimited"] is False
    assert sub["five_hour"]["used_percentage"] == 85
    assert sub["five_hour_max_pct"] == 80.0
    assert sub["over_window"] == "five_hour"
    assert sub["over_line"].startswith("subacct アカウント: 5時間枠 85% 使用")
    # 時刻は format_reset の1通り（画面で epoch から組み立てない）
    assert sub["five_hour_resets"] == accounts.format_reset(resets)
    assert len(sub["five_hour_resets"]) == len("10/28 03:50")
    main = {a["name"]: a for a in body["accounts"]}["mainacct"]
    assert main["five_hour"]["known"] is False and main["over_line"] is None
    assert main["five_hour_resets"] == ""


def test_sessions_payload_account_brief(app_module, homes, monkeypatch):
    """一覧の accounts に本数・作業中の本数・枠の値。1つのアカウントで例外が出ても一覧と割り当ての照合は続く。"""
    _statusline_both(homes["statusline"], 12, 85, time.time() + 3600)
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    sessions = [
        {"pid": 1, "cwd": homes["sub_cwd"], "account": "subacct", "status": "busy"},
        {"pid": 2, "cwd": homes["sub_cwd"], "account": "mainacct", "status": "idle"},
        {"pid": 3, "cwd": homes["main_cwd"], "account": "mainacct", "status": "idle"},
    ]
    monkeypatch.setattr(sm, "list_sessions", lambda: [dict(s) for s in sessions])
    payload = app_module.build_sessions_payload()
    briefs = {a["name"]: a for a in payload["accounts"]}
    assert briefs["subacct"]["running"] == 1 and briefs["subacct"]["busy"] == 1
    assert briefs["mainacct"]["running"] == 2 and briefs["mainacct"]["busy"] == 0
    assert briefs["subacct"]["over_window"] == "five_hour"
    assert briefs["subacct"]["five_hour_resets"]
    assert briefs["subacct"]["five_hour_max_pct"] == 80.0
    assert briefs["subacct"]["over_line"].startswith(
        "subacct アカウント: 5時間枠 85% 使用"
    )
    assert "over_line" in briefs["mainacct"] and briefs["mainacct"]["over_line"] is None
    rows = {s["pid"]: s for s in payload["sessions"]}
    assert rows[2]["account_expected"] == "subacct"
    assert all("quota_stop" in s for s in payload["sessions"])

    # 使用%の読み取りで例外 → そのアカウントは名前と本数だけ・割り当ての照合はそのまま
    real_usage = accounts.usage

    def broken(account, now=None):
        if account["name"] == "subacct":
            raise RuntimeError("boom")
        return real_usage(account, now)

    monkeypatch.setattr(accounts, "usage", broken)
    payload = app_module.build_sessions_payload()
    briefs = {a["name"]: a for a in payload["accounts"]}
    assert briefs["subacct"]["running"] == 1 and "five_hour" not in briefs["subacct"]
    assert "five_hour" in briefs["mainacct"]
    assert {s["pid"]: s for s in payload["sessions"]}[2][
        "account_expected"
    ] == "subacct"


def test_usage_unknown_or_broken_settings_do_not_raise(app_module, homes, tmp_path):
    """5時間枠が分からない・設定に誤りがあるときも例外を出さず、分からないとして返す。"""
    settings = dict(homes["settings"])
    settings["five_hour_max_pct"] = "x"
    settings["accounts"] = [
        dict(settings["accounts"][0], capacity="big"),
        dict(settings["accounts"][1], home=str(tmp_path / "missing")),
    ]
    write_settings(settings)
    body = (
        app_module.app.test_client()
        .get("/api/accounts", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        .get_json()
    )
    sub = {a["name"]: a for a in body["accounts"]}["subacct"]
    assert sub["usable"] is False
    assert sub["five_hour"]["known"] is False and sub["weekly"]["known"] is False
    assert sub["five_hour_max_pct"] == 80.0 and sub["over_line"] is None
    assert body["default"] == "mainacct"


def test_move_rewrites_stops_and_launches(app_module, homes, monkeypatch):
    stopped, launched = [], []
    monkeypatch.setattr(
        app_module.sm,
        "list_sessions",
        lambda: [
            {"pid": 77, "cwd": homes["main_cwd"], "status": "idle", "win_session": 1}
        ],
    )
    monkeypatch.setattr(
        app_module.sm,
        "stop_session",
        lambda pid: stopped.append(pid) or {"forced": False},
    )

    def fake_start(message, source="api"):
        # 起こす時点では割り当てがもう移っている
        launched.append((dict(message), accounts.assigned_name(homes["main_cwd"])))
        return 200, {"ok": True, "session": {"pid": 88}, "account": message["account"]}

    monkeypatch.setattr(app_module, "start_session_request", fake_start)
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct"}
    )
    assert status == 200 and body["ok"]
    assert body["previous"] == "mainacct" and body["account"] == "subacct"
    assert stopped == [77]
    message, assigned_at_launch = launched[0]
    assert assigned_at_launch == "subacct"
    assert message["win_session"] == "1"  # 前の起動先を引き継ぐ
    assert "NOW.md" in message["prompt"]
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["plain-role"]


def test_move_refuses_busy_without_force(app_module, homes, monkeypatch):
    monkeypatch.setattr(
        app_module.sm,
        "list_sessions",
        lambda: [{"pid": 77, "cwd": homes["main_cwd"], "status": "busy"}],
    )
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct"}
    )
    assert status == 409 and body["code"] == "busy"
    assert accounts.assigned_name(homes["main_cwd"]) is None


def test_move_rolls_back_when_stop_fails(app_module, homes, monkeypatch):
    monkeypatch.setattr(
        app_module.sm,
        "list_sessions",
        lambda: [{"pid": 77, "cwd": homes["main_cwd"], "status": "idle"}],
    )

    def fail(pid):
        raise app_module.sm.SessionError("access denied")

    monkeypatch.setattr(app_module.sm, "stop_session", fail)
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct"}
    )
    assert status == 502 and "前に戻しました" in body["error"]
    assert accounts.assigned_name(homes["main_cwd"]) is None


def test_move_same_account_and_bad_input(app_module):
    assert (
        app_module.move_account_request(
            {"project": "developing/plain-role", "account": "mainacct"}
        )[0]
        == 400
    )
    assert (
        app_module.move_account_request({"project": "nope", "account": "subacct"})[0]
        == 404
    )
    assert (
        app_module.move_account_request(
            {"project": "developing/plain-role", "account": "zzz"}
        )[0]
        == 400
    )
    assert app_module.move_account_request([1])[0] == 400


def test_move_rejects_non_bool_flags(app_module):
    for key in ("force", "launch"):
        status, body = app_module.move_account_request(
            {"project": "developing/plain-role", "account": "subacct", key: "false"}
        )
        assert status == 400 and key in body["error"]
    assert (
        accounts.assigned_name(app_module.resolve_project_path("developing/plain-role"))
        is None
    )


def test_move_treats_vanished_session_as_stopped(app_module, homes, monkeypatch):
    monkeypatch.setattr(
        app_module.sm,
        "list_sessions",
        lambda: [{"pid": 77, "cwd": homes["main_cwd"], "status": "idle"}],
    )

    def gone(pid):
        raise app_module.sm.SessionError("not found")

    monkeypatch.setattr(app_module.sm, "stop_session", gone)
    monkeypatch.setattr(app_module.sm, "find_session_by_pid", lambda pid: None)
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct", "launch": False}
    )
    assert status == 200 and body["stopped"] == [77]
    assert accounts.assigned_name(homes["main_cwd"]) == "subacct"


CARRY_SID = "0123abcd-1111-2222-3333-444455556666"


def _write_transcript(home, cwd, sid, extra=""):
    """会話記録（jsonl）とサブエージェントのフォルダを home の置き場に書く。"""
    folder = sm.transcript_dir_in(home, cwd)
    os.makedirs(os.path.join(folder, sid, "subagents"), exist_ok=True)
    path = os.path.join(folder, f"{sid}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"type": "user", "cwd": cwd, "message": {"content": "最初の指示"}},
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps({"type": "assistant"})
            + "\n"
            + extra
        )
    with open(os.path.join(folder, sid, "subagents", "a.jsonl"), "w") as f:
        f.write("{}\n")
    return path


class _FakeChild:
    """移しの試験の相手: 動いている1本を、止める・起こすに合わせて差し替える。"""

    def __init__(self, app_module, monkeypatch, cwd, pid):
        self.cwd = cwd
        self.running = [
            {
                "pid": pid,
                "cwd": cwd,
                "status": "idle",
                "session_id": CARRY_SID,
                "win_session": 0,
            }
        ]
        self.stopped, self.launched = [], []
        self.next_pid = pid + 1
        monkeypatch.setattr(app_module.sm, "list_sessions", lambda: list(self.running))
        monkeypatch.setattr(app_module.sm, "stop_session", self.stop)
        monkeypatch.setattr(
            app_module.sm,
            "launch_options_of",
            lambda pid, dropped=None: {"permission_mode": "dontAsk"},
        )
        monkeypatch.setattr(app_module, "start_session_request", self.start)

    def stop(self, pid):
        self.stopped.append(pid)
        self.running = [s for s in self.running if s["pid"] != pid]
        return {"forced": False}

    def start(self, message, source="api"):
        # 本物と同じ確かめ（割り当てたアカウントの置き場に記録があり、動いていない）を通す
        if message.get("resume"):
            sm.validate_resume_target(self.cwd, message["resume"])
        self.launched.append(dict(message))
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        session = {"pid": pid, "session_id": message.get("resume") or "new"}
        self.running = [dict(session, cwd=self.cwd, status="idle", win_session=0)]
        return 200, {"ok": True, "session": session, "account": message["account"]}


def test_move_carry_round_trip(app_module, homes, monkeypatch):
    """会話を持ち越して account_a 相当→account_b 相当→account_a 相当へ往復する（1手ずつ）。"""
    cwd = homes["main_cwd"]
    original = _write_transcript(homes["main_home"], cwd, CARRY_SID)
    child = _FakeChild(app_module, monkeypatch, cwd, 77)

    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 200 and body["ok"], body
    assert (body["account"], body["pid"], body["session_id"]) == (
        "subacct",
        78,
        CARRY_SID,
    )
    assert body["carried"] == CARRY_SID and body["copied_files"] == 2
    assert body["set_aside"] == []
    assert child.stopped == [77]
    message = child.launched[0]
    assert message["resume"] == CARRY_SID and "new" not in message
    assert message["permission_mode"] == "dontAsk"  # 元のモードを引き継ぐ
    assert message["remote_control"] is True  # claude.ai の新しい行に題名を渡す
    assert message["win_session"] == "0"
    assert "同じ会話" in message["prompt"]
    copied = os.path.join(
        sm.transcript_dir_in(homes["sub_cfg"], cwd), f"{CARRY_SID}.jsonl"
    )
    with open(copied, encoding="utf-8") as a, open(original, encoding="utf-8") as b:
        assert a.read() == b.read()
    assert os.path.isfile(original)  # 元は残す
    assert accounts.assigned_name(cwd) == "subacct"

    # account_b 相当で会話が伸びた後、本家へ持ち帰る。本家の残りは前の版なので、よけてから写す
    with open(copied, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"type": "assistant", "note": "移した先で伸びた"}, ensure_ascii=False
            )
            + "\n"
        )
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "mainacct",
            "carry_session": True,
        }
    )
    assert status == 200 and body["ok"], body
    assert (body["account"], body["pid"], body["session_id"]) == (
        "mainacct",
        79,
        CARRY_SID,
    )
    assert len(body["set_aside"]) == 2  # jsonl とフォルダ
    assert all(".before-carry-" in p and os.path.exists(p) for p in body["set_aside"])
    with open(original, encoding="utf-8") as f:
        assert "移した先で伸びた" in f.read()
    assert accounts.assigned_name(cwd) is None
    assert child.stopped == [77, 78]


def test_move_carry_remote_control_can_be_turned_off(app_module, homes, monkeypatch):
    """持ち越す移しは既定で題名を渡すが、remote_control: false で外せる。文字列は 400。"""
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID)
    child = _FakeChild(app_module, monkeypatch, cwd, 77)
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "remote_control": "false",
        }
    )
    assert status == 400 and "remote_control" in body["error"]
    assert child.stopped == []
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
            "remote_control": False,
        }
    )
    assert status == 200 and child.launched[-1]["remote_control"] is False


def test_move_carry_refuses_different_record_without_changes(
    app_module, homes, monkeypatch
):
    """移す先に同じ名前で中身の違う記録があれば、割り当ても停止も変えずに断る。"""
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID)
    _write_transcript(homes["sub_cfg"], cwd, CARRY_SID, extra='{"type": "other"}\n')
    with open(
        os.path.join(sm.transcript_dir_in(homes["sub_cfg"], cwd), f"{CARRY_SID}.jsonl"),
        "r+",
        encoding="utf-8",
    ) as f:
        f.write("X")  # 先頭を変えて「前の版」でなくする
    child = _FakeChild(app_module, monkeypatch, cwd, 77)
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 409 and body["code"] == "carry_refused"
    assert "何も変えていません" in body["error"]
    assert child.stopped == [] and child.launched == []
    assert accounts.assigned_name(cwd) is None


def test_move_carry_needs_a_conversation_and_one_session(
    app_module, homes, monkeypatch
):
    cwd = homes["main_cwd"]
    child = _FakeChild(app_module, monkeypatch, cwd, 77)
    child.running = []
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 400 and "持ち越せる会話がありません" in body["error"]
    child.running = [
        {"pid": 1, "cwd": cwd, "status": "idle", "session_id": CARRY_SID},
        {"pid": 2, "cwd": cwd, "status": "idle", "session_id": CARRY_SID},
    ]
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 409 and body["code"] == "ambiguous"
    assert child.stopped == [] and accounts.assigned_name(cwd) is None


def test_move_carry_rolls_back_when_copy_fails(app_module, homes, monkeypatch):
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID)
    child = _FakeChild(app_module, monkeypatch, cwd, 77)

    def broken(*args):
        raise app_module.sm.SessionError("disk full")

    monkeypatch.setattr(app_module.sm, "copy_transcript", broken)
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 502 and "割り当ては前に戻しました" in body["error"]
    assert CARRY_SID in body["error"]
    assert accounts.assigned_name(cwd) is None and child.launched == []


def test_move_without_carry_starts_new_conversation(app_module, homes, monkeypatch):
    """持ち越さない移しは、移す先に前の会話が残っていても new: true で新しい会話にする。"""
    cwd = homes["main_cwd"]
    child = _FakeChild(app_module, monkeypatch, cwd, 77)
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct"}
    )
    assert status == 200 and child.launched[0]["new"] is True
    assert "resume" not in child.launched[0]


def test_copy_transcript_restores_set_aside_on_failure(homes, monkeypatch):
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID, extra='{"type": "x"}\n')
    stale = _write_transcript(homes["sub_cfg"], cwd, CARRY_SID)
    with open(stale, encoding="utf-8") as f:
        before = f.read()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(sm.shutil, "copy2", fail)
    with pytest.raises(sm.SessionError):
        sm.copy_transcript(cwd, CARRY_SID, homes["main_home"], homes["sub_cfg"])
    with open(stale, encoding="utf-8") as f:
        assert f.read() == before  # よけた前の版は元の名前へ戻る
    folder = sm.transcript_dir_in(homes["sub_cfg"], cwd)
    assert not [n for n in os.listdir(folder) if "carry" in n]


def _capture_launch(app_module, monkeypatch):
    calls = []

    def fake_launch(**kwargs):
        calls.append(kwargs)
        return {
            "pid": 5,
            "trusted": False,
            "win_session": 0,
            "elevated": None,
            "resumed": kwargs.get("resume_session_id"),
            "account": "mainacct",
        }

    monkeypatch.setattr(app_module.sm, "launch_session", fake_launch)
    monkeypatch.setattr(
        app_module, "wait_for_session", lambda pid: {"pid": pid, "account": "mainacct"}
    )
    monkeypatch.setattr(app_module, "increment_project_usage", lambda key: None)
    return calls


def test_start_resumes_latest_by_default(app_module, homes, monkeypatch):
    """resume を省くと、いちばん新しい会話（動いていなければ）の続きで起こす。new: true なら新しい会話。"""
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID)
    calls = _capture_launch(app_module, monkeypatch)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 200 and body["auto_resumed"] is True
    assert calls[-1]["resume_session_id"] == CARRY_SID

    status, body = app_module._start_session(
        {"project": "developing/plain-role", "new": True}
    )
    assert status == 200 and body["auto_resumed"] is False
    assert calls[-1]["resume_session_id"] is None

    # いちばん新しい会話が動いていれば、古い会話へ戻らず新しい会話
    monkeypatch.setattr(
        app_module.sm,
        "list_sessions",
        lambda: [{"pid": 9, "cwd": cwd, "session_id": CARRY_SID}],
    )
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 200 and calls[-1]["resume_session_id"] is None

    status, body = app_module._start_session(
        {"project": "developing/plain-role", "new": "true"}
    )
    assert status == 400 and body["code"] == "invalid_body"


def test_start_does_not_resume_conversation_being_launched(
    app_module, homes, monkeypatch
):
    """起こしている途中（状態ファイルの前）に2つ目の起動が来ても、同じ会話を2本起こさない。"""
    _write_transcript(homes["main_home"], homes["main_cwd"], CARRY_SID)
    calls = _capture_launch(app_module, monkeypatch)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    inner = []
    entered = []
    real_launch = app_module.sm.launch_session

    def launch_with_second_request(**kwargs):
        if not entered:
            entered.append(True)  # 入れ子の起動ではもう割り込まない
            # 1本目の起動の途中で、既定の起動・明示の resume・新しい会話が届く
            for extra in ({}, {"resume": CARRY_SID}, {"new": True}):
                inner.append(
                    app_module._start_session(
                        {"project": "developing/plain-role", **extra}
                    )
                )
        return real_launch(**kwargs)

    monkeypatch.setattr(app_module.sm, "launch_session", launch_with_second_request)
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 200 and body["resumed"] == CARRY_SID
    # 既定の選択も明示の resume も断る（新しい会話へ逃がさない）。new は通る
    for i in (0, 1):
        assert inner[i][0] == 409 and "起こしている途中" in inner[i][1]["error"]
    assert inner[2][0] == 200 and inner[2][1]["resumed"] is None
    assert [c["resume_session_id"] for c in calls] == [None, CARRY_SID]
    assert app_module._launching == {}


def test_start_keeps_reservation_while_unregistered_pid_lives(
    app_module, homes, monkeypatch
):
    """登録を待ちきれず 502 を返しても、起こした pid が生きている間は同じ会話を起こさない。"""
    _write_transcript(homes["main_home"], homes["main_cwd"], CARRY_SID)
    calls = _capture_launch(app_module, monkeypatch)
    alive_pid = os.getpid()
    real_launch = app_module.sm.launch_session

    def launch_as_alive(**kwargs):
        # 本物の起動と同じく、返す pid を台帳に書いてから返す
        started = real_launch(**kwargs)
        app_module.sm.record_launched(alive_pid, "trial", kwargs.get("cwd"))
        return dict(started, pid=alive_pid)

    monkeypatch.setattr(app_module.sm, "launch_session", launch_as_alive)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    monkeypatch.setattr(app_module, "wait_for_session", lambda pid: None)
    monkeypatch.setattr(
        app_module.sm, "read_console_screen_isolated", lambda pid, max_lines=40: []
    )
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 502 and body["code"] == "not_registered"
    # 開始時刻は起こした直後に台帳が取ったもの
    recorded = app_module.sm._load_registry()[str(alive_pid)]["create_time"]
    assert app_module._launching == {CARRY_SID: (alive_pid, recorded)}
    status, body = app_module._start_session(
        {"project": "developing/plain-role", "resume": CARRY_SID}
    )
    assert status == 409 and len(calls) == 1
    # pid が別のプロセスに再利用された（開始時刻が違う）なら控えから外れ、起こせる
    app_module._launching[CARRY_SID] = (alive_pid, 1.0)
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 502 and len(calls) == 2
    assert calls[-1]["resume_session_id"] == CARRY_SID
    # 開始時刻を控えられなかったときは、pid が生きている間は控える（今までどおり）
    app_module._launching[CARRY_SID] = (alive_pid, None)
    with app_module._launching_lock:
        assert app_module._is_launching(CARRY_SID) is True
    # 今の開始時刻が読めないときは外す側
    # （差し替えはこの確かめの中だけ。後の段の record_launched まで効くと、台帳の掃除で生きている記録を全部消す・2026-11-02）
    app_module._launching[CARRY_SID] = (alive_pid, 1.0)
    with monkeypatch.context() as patched:
        patched.setattr(app_module.sm, "_process_create_time", lambda pid: None)
        with app_module._launching_lock:
            assert app_module._is_launching(CARRY_SID) is False
    # pid が終われば控えから外れ、起こせる
    app_module._launching[CARRY_SID] = 2**22 + 12345
    monkeypatch.setattr(app_module.sm.psutil, "pid_exists", lambda pid: False)
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 502 and calls[-1]["resume_session_id"] == CARRY_SID
    app_module._launching.clear()


def test_latest_resumable_skips_headless(app_module, monkeypatch):
    """既定の続きは、無人実行（claude -p）の会話を飛ばしていちばん新しい対話の会話にする。"""

    def items(*rows):
        monkeypatch.setattr(
            app_module.sm,
            "list_resumable",
            lambda cwd, **kwargs: [
                {"session_id": sid, "headless": headless, "live": live}
                for sid, headless, live in rows
            ],
        )

    items(("run", True, False), ("talk", False, False))
    assert app_module.latest_resumable_id("C:\\x") == "talk"
    # いちばん新しい対話の会話が動いていれば、古い会話へは戻らない
    items(("run", True, False), ("talk", False, True), ("old", False, False))
    assert app_module.latest_resumable_id("C:\\x") is None
    # 無人実行の会話しか無ければ新しい会話
    items(("run", True, False))
    assert app_module.latest_resumable_id("C:\\x") is None


def test_start_passes_remote_control_name_only_when_asked(
    app_module, homes, monkeypatch
):
    """指定なしは既定（None＝再開なら session_manager が付ける）・true は表示名・false は ""。真偽でなければ 400。"""
    calls = _capture_launch(app_module, monkeypatch)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    app_module._start_session({"project": "developing/plain-role", "new": True})
    assert calls[-1]["remote_control_name"] is None
    app_module._start_session(
        {"project": "developing/plain-role", "new": True, "remote_control": False}
    )
    assert calls[-1]["remote_control_name"] == ""
    app_module._start_session(
        {"project": "developing/plain-role", "new": True, "remote_control": True}
    )
    assert calls[-1]["remote_control_name"] == app_module.build_session_display_name(
        "developing/plain-role"
    )
    status, body = app_module._start_session(
        {"project": "developing/plain-role", "remote_control": "yes"}
    )
    assert status == 400 and body["code"] == "invalid_body"


def test_start_passes_chrome_as_given(app_module, homes, monkeypatch):
    """chrome は値のまま launch_session へ渡す（省けば None＝何も足さない）。真偽でなければ 400。"""
    calls = _capture_launch(app_module, monkeypatch)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    app_module._start_session({"project": "developing/plain-role", "new": True})
    assert calls[-1]["chrome"] is None
    app_module._start_session(
        {"project": "developing/plain-role", "new": True, "chrome": False}
    )
    assert calls[-1]["chrome"] is False
    app_module._start_session(
        {"project": "developing/plain-role", "new": True, "chrome": True}
    )
    assert calls[-1]["chrome"] is True
    count = len(calls)
    status, body = app_module._start_session(
        {"project": "developing/plain-role", "chrome": "true"}
    )
    assert status == 400 and body["code"] == "invalid_body" and len(calls) == count


def test_move_carries_chrome_and_explicit_wins(app_module, homes, monkeypatch):
    """持ち越す移しは元の --chrome を引き継ぎ、chrome: false を明示すれば外せる。文字列は 400。"""
    cwd = homes["main_cwd"]
    _write_transcript(homes["main_home"], cwd, CARRY_SID)
    child = _FakeChild(app_module, monkeypatch, cwd, 77)
    monkeypatch.setattr(
        app_module.sm,
        "launch_options_of",
        lambda pid, dropped=None: {"permission_mode": "dontAsk", "chrome": True},
    )
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct", "chrome": "yes"}
    )
    assert status == 400 and "chrome" in body["error"] and child.stopped == []
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "subacct",
            "carry_session": True,
        }
    )
    assert status == 200 and child.launched[-1]["chrome"] is True
    status, body = app_module.move_account_request(
        {
            "project": "developing/plain-role",
            "account": "mainacct",
            "carry_session": True,
            "chrome": False,
        }
    )
    assert status == 200 and child.launched[-1]["chrome"] is False


def test_start_without_past_conversation_is_new(app_module, homes, monkeypatch):
    calls = _capture_launch(app_module, monkeypatch)
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    status, body = app_module._start_session({"project": "developing/plain-role"})
    assert status == 200 and body["auto_resumed"] is False
    assert calls[-1]["resume_session_id"] is None


def test_move_without_launch(app_module, homes, monkeypatch):
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct", "launch": False}
    )
    assert status == 200 and body["session"] is None
    assert accounts.assigned_name(homes["main_cwd"]) == "subacct"


# --- 割り当ての無い作業場の既定（unassigned_account・2026-11-01 管理役の決め） ----------


def _with_unassigned(homes, name):
    write_settings(dict(homes["settings"], unassigned_account=name))


def test_unassigned_default_goes_to_setting(homes, spawns):
    _with_unassigned(homes, "subacct")
    assert accounts.effective_name(homes["main_cwd"]) == "subacct"
    assert accounts.assigned_name(homes["main_cwd"]) is None
    # 書いていなければ今までどおり本家
    write_settings(homes["settings"])
    assert accounts.account_for(homes["main_cwd"])["name"] == "mainacct"
    _with_unassigned(homes, "subacct")
    result = sm.launch_session(homes["main_cwd"], claude_exe=sys.executable)
    assert result["account"] == "subacct"
    assert spawns["env"]["CLAUDE_CONFIG_DIR"] == homes["sub_cfg"]
    # 起こしたら台帳に書いて固定する（写しに載り、後から本家の記録ができても替わらない）
    assert accounts.resolve(homes["main_cwd"]) == ("subacct", "assigned")
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == ["plain-role"]
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    assert accounts.account_for(homes["main_cwd"])["name"] == "subacct"


def test_main_history_launch_is_not_pinned(homes, spawns):
    _with_unassigned(homes, "subacct")
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    result = sm.launch_session(homes["main_cwd"], claude_exe=sys.executable)
    assert result["account"] == "mainacct"
    assert accounts.resolve(homes["main_cwd"]) == ("mainacct", "main_history")
    assert accounts.state()["assignments"] == {}


def test_resolve_reasons(homes):
    _with_unassigned(homes, "subacct")
    assert accounts.resolve(None) == ("mainacct", "no_cwd")
    assert accounts.resolve(homes["main_cwd"]) == ("subacct", "default")
    accounts.assign(homes["main_cwd"], "developing/plain-role", "mainacct", "move")
    assert accounts.resolve(homes["main_cwd"]) == ("mainacct", "explicit_main")


def test_pending_import_launches_default_without_pin(homes, spawns):
    """取り込み待ちの間は台帳を書けないので、既定で起こす作業場は書かずに起こす（2周目の中1）。"""
    _with_unassigned(homes, "subacct")
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": "C:\\elsewhere", "home": homes["sub_home"], "roles": ["x"]},
            f,
        )
    accounts.set_role_resolver(lambda name: [])
    assert accounts.pending_import_reason()
    result = sm.launch_session(homes["main_cwd"], claude_exe=sys.executable)
    assert result["account"] == "subacct"
    assert not os.path.exists(accounts.STATE_PATH)


def test_pin_waits_for_spawn_checks(homes, spawns, monkeypatch):
    """再開の確かめで断られる起動では台帳を書かない（2周目の低1）。"""
    _with_unassigned(homes, "subacct")

    def refuse(cwd, sid):
        raise sm.SessionError("ない")

    monkeypatch.setattr(sm, "validate_resume_target", refuse)
    with pytest.raises(sm.SessionError):
        sm.launch_session(
            homes["main_cwd"],
            claude_exe=sys.executable,
            resume_session_id="0123abcd-0000-0000-0000-000000000000",
        )
    assert accounts.assigned_name(homes["main_cwd"]) is None


def test_assign_main_works_with_broken_default(homes):
    """既定の設定が誤っていても、本家へ戻す・移すは書ける（明示の本家と書く）。"""
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    _with_unassigned(homes, "nobody")
    accounts.assign(
        homes["sub_cwd"], "developing/sub-role", "mainacct", "move-rollback"
    )
    assert accounts.resolve(homes["sub_cwd"]) == ("mainacct", "explicit_main")


def test_unassigned_default_keeps_main_history(homes):
    """本家に会話の記録がある作業場は、既定を変えても本家のまま（続きが見えなくならない）。"""
    _with_unassigned(homes, "subacct")
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    assert accounts.account_for(homes["main_cwd"])["name"] == "mainacct"
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"


def test_rename_keeps_main_history_workspace_on_main(homes, tmp_path):
    """本家の記録で本家だった作業場は、改名しても既定へ流れず本家のまま（記録だけの低を直した）。"""
    _with_unassigned(homes, "subacct")
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    new_path = str(tmp_path / "work" / "plain-role-renamed")
    moved = accounts.rename_path(
        homes["main_cwd"], new_path, "developing/plain-role-renamed"
    )
    assert moved == 1
    assert accounts.resolve(new_path) == ("mainacct", "explicit_main")
    entry = accounts.state()["assignments"][accounts._norm(new_path)]
    assert (
        entry["by"] == "rename"
        and entry["project_key"] == "developing/plain-role-renamed"
    )
    # 本家と書いた作業場は写しの役に入らない
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == []


def test_rename_keeps_legacy_account(homes, tmp_path):
    """フォルダ名の印で決まっていた作業場は、改名しても同じアカウント（印は新しい名前に効かない）。"""
    state = accounts._empty_state()
    state["legacy_names"] = {"plain-role": "subacct"}
    accounts._save_state(state)
    new_path = str(tmp_path / "work" / "plain-role-2")
    assert (
        accounts.rename_path(homes["main_cwd"], new_path, "developing/plain-role-2")
        == 1
    )
    assert accounts.resolve(new_path) == ("subacct", "assigned")


def test_rename_leaves_default_and_same_result_alone(homes, tmp_path):
    """既定で決まる作業場・改名しても行き先が変わらない作業場は書かない。"""
    _with_unassigned(homes, "subacct")
    new_path = str(tmp_path / "work" / "fresh")
    assert accounts.rename_path(homes["main_cwd"], new_path, "developing/fresh") == 0
    # 既定が本家なら、本家の記録の作業場も新しいパスで本家のまま＝書かない
    write_settings(homes["settings"])
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    assert accounts.rename_path(homes["main_cwd"], new_path, "developing/fresh") == 0
    assert accounts.state()["assignments"] == {}


def test_rename_keeps_main_even_with_broken_default(homes, tmp_path):
    """既定の設定が誤っていても、本家の記録の作業場は改名で本家と書く（黙って流さない）。"""
    _with_unassigned(homes, "nobody")
    os.makedirs(sm.transcript_dir_in(homes["main_home"], homes["main_cwd"]))
    new_path = str(tmp_path / "work" / "plain-role-x")
    assert (
        accounts.rename_path(homes["main_cwd"], new_path, "developing/plain-role-x")
        == 1
    )
    assert accounts.resolve(new_path) == ("mainacct", "explicit_main")


def test_rename_keeps_sub_workspaces_and_counts_both(homes, tmp_path, capsys):
    """配下の作業場も、台帳の行は付け替え、本家の記録だけで決まっていたものは固定する（評価1周目の中）。"""
    _with_unassigned(homes, "subacct")
    parent = tmp_path / "work" / "parent"
    kid_main = parent / "projects" / "kid-main"
    kid_sub = parent / "projects" / "kid-sub"
    grandkid = kid_main / "projects" / "deep"
    for d in (kid_sub, grandkid):
        d.mkdir(parents=True)
    accounts.assign(
        str(kid_sub), "developing/parent/projects/kid-sub", "subacct", "create"
    )
    os.makedirs(sm.transcript_dir_in(homes["main_home"], str(kid_main)))
    os.makedirs(sm.transcript_dir_in(homes["main_home"], str(grandkid)))
    new_parent = tmp_path / "work" / "parent2"
    parent.rename(new_parent)  # フォルダを移した後に呼ばれる形
    moved = accounts.rename_path(str(parent), str(new_parent), "developing/parent2")
    # 付け替え1（kid-sub）＋固定2（kid-main・deep）。親は既定で決まっていたので書かない
    assert moved == 3
    assert accounts.resolve(str(new_parent / "projects" / "kid-sub")) == (
        "subacct",
        "assigned",
    )
    assert accounts.resolve(str(new_parent / "projects" / "kid-main")) == (
        "mainacct",
        "explicit_main",
    )
    deep = accounts.state()["assignments"][
        accounts._norm(str(new_parent / "projects" / "kid-main" / "projects" / "deep"))
    ]
    assert deep["project_key"] == "developing/parent2/projects/kid-main/projects/deep"
    assert accounts.resolve(str(new_parent)) == ("subacct", "default")
    out = capsys.readouterr().out
    assert "前の行き先" in out
    # 付け替えた子（kid-sub）を「改名先の前からの行」と取り違えない
    assert "前からの割り当て" not in out


def test_rename_legacy_role_moves_to_new_name_in_mirror(homes, tmp_path):
    """フォルダ名の印で決まっていた作業場を改名すると、写しの役は新しい名前で載る。"""
    state = accounts._empty_state()
    state["legacy_names"] = {"plain-role": "subacct"}
    accounts._save_state(state)
    new_path = str(tmp_path / "work" / "plain-role-3")
    accounts.rename_path(homes["main_cwd"], new_path, "developing/plain-role-3")
    with open(homes["mirror"], encoding="utf-8") as f:
        assert "plain-role-3" in json.load(f)["roles"]


def test_rename_during_pending_import_is_logged(homes, tmp_path, capsys):
    """取り込み待ちの間は台帳を書けないので付け替えず、そのことを記録に出す（評価1周目の低1）。"""
    with open(homes["mirror"], "w", encoding="utf-8") as f:
        json.dump(
            {"config_dir": "C:\\elsewhere", "home": homes["sub_home"], "roles": ["x"]},
            f,
        )
    accounts.set_role_resolver(lambda name: [])
    assert accounts.pending_import_reason()
    moved = accounts.rename_path(
        homes["main_cwd"], str(tmp_path / "work" / "z"), "developing/z"
    )
    assert moved == 0
    assert "取り込み待ちのため" in capsys.readouterr().out


def test_rename_over_existing_assignment_is_logged(homes, tmp_path, capsys):
    """改名先に別のアカウントの古い行があれば、置き換えることを記録に出す（評価1周目の低2）。"""
    target = tmp_path / "work" / "taken"
    target.mkdir()
    accounts.assign(str(target), "developing/taken", "mainacct", "move")
    _with_unassigned(homes, "subacct")
    accounts.assign(str(target), "developing/taken", "mainacct", "move")
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    accounts.rename_path(homes["sub_cwd"], str(target), "developing/taken")
    assert accounts.resolve(str(target)) == ("subacct", "assigned")
    assert "置き換えます" in capsys.readouterr().out


def test_assign_main_is_explicit_when_default_is_not_main(homes):
    _with_unassigned(homes, "subacct")
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "mainacct", "move")
    entry = accounts.state()["assignments"][accounts._norm(homes["sub_cwd"])]
    assert entry["explicit_main"] is True
    assert accounts.account_for(homes["sub_cwd"])["name"] == "mainacct"
    assert accounts.effective_name(homes["sub_cwd"]) == "mainacct"
    # 本家と書いた作業場は写しの役に入らない
    with open(homes["mirror"], encoding="utf-8") as f:
        assert json.load(f)["roles"] == []
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "move")
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"


def test_unassigned_default_unusable_is_refused(homes, spawns):
    _with_unassigned(homes, "nobody")
    with pytest.raises(accounts.AccountError) as ctx:
        accounts.account_for(homes["main_cwd"])
    assert "unassigned_account" in str(ctx.value)
    with pytest.raises(sm.SessionError):
        sm.launch_session(homes["main_cwd"], claude_exe=sys.executable)
    assert spawns["trusted"] == [] and "route" not in spawns
    # 割り当てのある作業場は既定の誤りに巻き込まれない
    accounts.assign(homes["sub_cwd"], "developing/sub-role", "subacct", "create")
    assert accounts.account_for(homes["sub_cwd"])["name"] == "subacct"


def test_default_choice_prefers_unassigned_account(homes):
    _with_unassigned(homes, "subacct")
    name, reason = accounts.default_choice(
        {
            "mainacct": _both(_known(1), _known(1)),
            "subacct": _both(_known(60), _known(50)),
        }
    )
    assert name == "subacct" and "unassigned_account" in reason
    # 5時間枠の線を超えていれば外して比べる（既定へ固定しない）
    name, _ = accounts.default_choice(
        {
            "mainacct": _both(_known(1), _known(1)),
            "subacct": _both(_known(60), _known(95)),
        }
    )
    assert name == "mainacct"


def test_move_previous_follows_unassigned_default(app_module, homes, monkeypatch):
    _with_unassigned(homes, "subacct")
    monkeypatch.setattr(app_module.sm, "list_sessions", lambda: [])
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "subacct", "launch": False}
    )
    # 割り当てが無くても既定で subacct に居るので、同じアカウントへは移さない
    assert status == 400 and "既定" in body["error"]
    status, body = app_module.move_account_request(
        {"project": "developing/plain-role", "account": "mainacct", "launch": False}
    )
    assert status == 200 and body["previous"] == "subacct"
    assert accounts.account_for(homes["main_cwd"])["name"] == "mainacct"
    # 開始画面の欄: 本家と明示した行は「割り当て」
    client = app_module.app.test_client()
    project = client.get("/api/accounts?project=developing/plain-role").get_json()[
        "project"
    ]
    assert project == {
        "project_key": "developing/plain-role",
        "account": "mainacct",
        "assigned": True,
        "reason": "explicit_main",
    }


def test_api_project_with_broken_default_reports_reason(app_module, homes):
    """既定の設定が誤っていても一覧は 200 で返し、作業場の欄に理由を載せる（記録だけの低を直した）。"""
    _with_unassigned(homes, "nobody")
    client = app_module.app.test_client()
    response = client.get("/api/accounts?project=developing/plain-role")
    assert response.status_code == 200
    body = response.get_json()
    assert {a["name"] for a in body["accounts"]} == {"mainacct", "subacct"}
    project = body["project"]
    assert project["account"] is None and project["reason"] == "error"
    assert "unassigned_account" in project["error"] and "nobody" in project["error"]

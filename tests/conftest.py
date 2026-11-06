# -*- coding: utf-8 -*-
"""pytest の共通の土台。

試験が本物のアカウントの設定（config/settings.json の accounts）と割り当ての台帳
（~/.claude-web-app/account_assignments.json）を読み書きしないよう、どちらも一時フォルダへ向ける。
本物を読むと、本家でないアカウントで動いているセッションが一覧の試験に混ざり、
割り当ての試験が本物の台帳と trial_roles.json の写しを書き換えてしまう。
アカウントの試験（test_accounts.py）は、この一時フォルダに自分の設定を書く。

起こした子の台帳（~/.claude-web-app/launched.json）も一時フォルダへ向ける。本物の launch_session・record_launched を
呼ぶ試験が本物の台帳に試験のプロセスの記録を書き、掃除で生きている子の記録まで消した（2026-11-02・伝言 8bc5f64d28af。
19:25 の学マスの予定タスクが 403 not_managed で断られた）。試験の前後で本物の台帳が変わっていたら、全体を失敗にする。
"""

import os
import sys

import pytest

# テスト対象をインポートできるようプロジェクトルートをパスへ追加する
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import json
import shutil
import tempfile
import time
import uuid

import accounts
import auto_clear
import liaison
import machine_send
import migration_drill
import quota_alert
import quota_resume
import relogin_manager
import self_clear
import send_history
import session_manager

# 本物の台帳の置き場（読み込んだ時点の値。試験はここに書いてはいけない）
REAL_LAUNCHED_REGISTRY = session_manager.LAUNCHED_REGISTRY
# 本物の状態の置き場（~/.claude-web-app）
REAL_APP_STATE_DIR = session_manager.APP_STATE_DIR
# 前後で比べる本物のファイル。本番の 5002 がめったに書かないものだけ（普段から書く記録・状態を入れると、
# 試験と関係なく失敗する。それらは一時の置き場を向いていることを test_registry_isolation.py で確かめる）
GUARDED_REAL_FILES = (
    REAL_LAUNCHED_REGISTRY,
    os.path.join(REAL_APP_STATE_DIR, "keep.json"),
    os.path.join(REAL_APP_STATE_DIR, "hidden_resumable.json"),
    os.path.join(REAL_APP_STATE_DIR, "account_assignments.json"),
)
# タスク経由の起動の受け渡しの置き場（試験は AppData の外の試験専用のフォルダを使う。AppData の下は
# パッケージ版の python では仮想化され、起動役から見えないことがある・session_manager.TASK_LAUNCH_DIR の注記）
TEST_TASK_LAUNCH_ROOT = os.path.join(accounts.USER_DIR, ".claude-web-app-test")


def _fingerprint(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _registry_fingerprint():
    """本物の台帳の（更新時刻, 大きさ）。無ければ None。"""
    return _fingerprint(REAL_LAUNCHED_REGISTRY)


def _guarded_fingerprints():
    return {path: _fingerprint(path) for path in GUARDED_REAL_FILES}


def _load_real_registry(tries=3):
    """本物の台帳の中身。5002 の書き換えと重なって読めなければ少しおいて読み直し、それでも読めなければ None。"""
    for attempt in range(tries):
        try:
            with open(REAL_LAUNCHED_REGISTRY, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            if attempt + 1 < tries:
                time.sleep(0.2)
    return None


def _registry_harm(before, after, started_at=None):
    """試験が本物の台帳を書いた跡（空なら無い）。

    本番の 5002 は同じ間にも子を起こす・取り込む（記録が足される）ので、更新時刻では見分けない
    （2026-11-02 に 5002 が example-price-tool を起こして見張りが失敗した）。試験の跡とみなすのは、
    いまも生きている記録が消えた（掃除で消した）・試験のプロセスや一時フォルダの記録が足された・
    試験の間にこのプロジェクトを作業場とする記録が足された（試験が os.getcwd() で起こす形・評価役の中）、のどれか。
    """
    harm = []
    for pid, entry in before.items():
        if pid not in after and session_manager._entry_matches(pid, entry):
            harm.append(f"生きている記録が消えた pid={pid}（{entry.get('name')}）")
    temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    project = os.path.normcase(os.path.abspath(PROJECT_ROOT))
    for pid, entry in after.items():
        if pid in before:
            continue
        cwd = os.path.normcase(os.path.abspath(entry.get("cwd") or "."))
        during = (
            started_at is not None
            and isinstance(entry.get("launched_at"), (int, float))
            and entry["launched_at"] >= started_at
        )
        if (
            pid == str(os.getpid())
            or cwd.startswith(temp_root)
            or (during and cwd == project)
        ):
            harm.append(
                f"試験の記録が足された pid={pid}（{entry.get('name')}・{entry.get('cwd')}）"
            )
    return harm


def pytest_sessionstart(session):
    session.config._real_guarded_before = _guarded_fingerprints()
    session.config._real_registry_before = _load_real_registry()
    session.config._real_registry_started_at = time.time()


def pytest_sessionfinish(session, exitstatus):
    """試験の間に本物の台帳などを試験が書いた跡があれば、全体を失敗にする。

    台帳は中身で見分ける（_registry_harm）。読めなかったときは判定を見送り、その旨を出す（誤って失敗にしない）。
    ほかの見張るファイル（残す印・会話の一覧から外したもの・アカウントの割り当て）は
    本番の 5002 がめったに書かないので、更新時刻が変われば失敗にする（5002 が同じ間に書いた場合も止まる＝見直す）。
    """
    registry_before = getattr(session.config, "_real_registry_before", None)
    registry_after = _load_real_registry()
    if registry_before is None or registry_after is None:
        print("\n[conftest] 本物の台帳を読めなかったので、台帳の見張りは見送りました")
        harm = []
    else:
        harm = _registry_harm(
            registry_before,
            registry_after,
            getattr(session.config, "_real_registry_started_at", None),
        )
    before = getattr(session.config, "_real_guarded_before", None) or {}
    changed = [
        p
        for p, fp in _guarded_fingerprints().items()
        if p != REAL_LAUNCHED_REGISTRY and fp != before.get(p)
    ]
    if harm or changed:
        print(
            "\n[conftest] 試験が本物の状態に触れた跡があります: "
            + "／".join(harm + [f"{p} が書き換わった" for p in changed])
            + "（本番の 5002 が同じ間に残す印・会話の一覧・割り当ての操作をした場合や、"
            "このプロジェクトの子を起こした・取り込んだ場合も出ます）"
        )
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _no_real_launched_registry(monkeypatch, tmp_path):
    """本アプリの状態の置き場をすべて一時フォルダへ向ける（本物の ~/.claude-web-app を書かない）。

    起こした子の台帳・状態の置き場（inbox・会話の一覧から外したもの・会話のつながり）・自動の区切り・
    管理役への知らせ・枠切れの続きの依頼・機械の口・自分の区切り・送った履歴・ログインの確かめ・タスク経由の起動の受け渡し。
    部品は置き場を使うたびにこれらの定数を引くので、読み込みのときに作られた部品（本アプリの notices 等）にも効く。
    ただし module スコープの fixture で本アプリを読み込むと、読み込みはこの向け直しより先に起きるので、
    部品が作られたときの「読む」だけは本物を読む（書くのは使うたびに引くので一時へ行く）。
    """
    state = tmp_path / "app_state"
    monkeypatch.setattr(session_manager, "APP_STATE_DIR", str(state))
    monkeypatch.setattr(
        session_manager, "LAUNCHED_REGISTRY", str(tmp_path / "launched.json")
    )
    monkeypatch.setattr(auto_clear, "STATE_FILE", str(state / "auto_clear.json"))
    monkeypatch.setattr(auto_clear, "KEEP_FILE", str(state / "keep.json"))
    monkeypatch.setattr(auto_clear, "RESULT_LOG", str(state / "auto_clear.log.jsonl"))
    monkeypatch.setattr(liaison, "STATE_FILE", str(state / "liaison.json"))
    monkeypatch.setattr(quota_resume, "STATE_FILE", str(state / "quota_resume.json"))
    # 週の枠の知らせ: 記録と ntfy の設定（本物の config/ntfy.ini を読んで本物のトピックへ鳴らさない）
    monkeypatch.setattr(quota_alert, "STATE_FILE", str(state / "quota_alert.json"))
    monkeypatch.setattr(quota_alert, "CONFIG_FILE", str(tmp_path / "ntfy.ini"))
    monkeypatch.setattr(machine_send, "STATE_FILE", str(state / "machine_send.json"))
    monkeypatch.setattr(self_clear, "RESULT_LOG", str(state / "self_clear.log.jsonl"))
    monkeypatch.setattr(send_history, "APP_STATE_DIR", str(state))
    monkeypatch.setattr(send_history, "HISTORY_FILE", str(state / "send_history.jsonl"))
    monkeypatch.setattr(relogin_manager, "CHECK_DIR", str(state / "relogin-check"))
    # ログイン更新の claude を起こす作業場（session_manager と relogin_manager の写しを両方）
    monkeypatch.setattr(session_manager, "RELOGIN_DIR", str(state / "relogin"))
    monkeypatch.setattr(relogin_manager, "RELOGIN_DIR", str(state / "relogin"))
    # 移行の練習の記録（migration_drill）
    monkeypatch.setattr(migration_drill, "STATE_DIR", str(state))
    monkeypatch.setattr(
        migration_drill, "LOG_PATH", str(state / "migration_drill.log.jsonl")
    )
    monkeypatch.setattr(
        migration_drill, "PHASE_PATH", str(state / "migration_drill.phase.json")
    )
    launch_dir = os.path.join(TEST_TASK_LAUNCH_ROOT, uuid.uuid4().hex[:12])
    monkeypatch.setattr(session_manager, "TASK_LAUNCH_DIR", launch_dir)
    yield
    shutil.rmtree(launch_dir, ignore_errors=True)
    try:
        os.rmdir(
            TEST_TASK_LAUNCH_ROOT
        )  # 空なら片付ける（ほかの試験が並行して使っていれば残る）
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _no_real_accounts(monkeypatch, tmp_path):
    """本物の設定・台帳を読まない（設定が無い＝本家だけ・台帳も空）。"""
    monkeypatch.setattr(accounts, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setattr(
        accounts, "STATE_PATH", str(tmp_path / "account_assignments.json")
    )
    # 本アプリを読み込んだ試験が登録した取り込みの関数（本物の置き場を探す）を持ち越さない
    monkeypatch.setattr(accounts, "_role_resolver", None)
    monkeypatch.setattr(accounts, "_import_failed", None)
    accounts._usage_cache.clear()

# -*- coding: utf-8 -*-
"""試用（Pro）の口座で起こす役の許可リスト（依頼 de1808326f81・2026-10-27 別の役）。

【なぜ要るか】
host_pc の役を、本家の口座（~/.claude）から試用の口座（CLAUDE_CONFIG_DIR）へ1役ずつ移す。
起動の env は CLAUDE で始まるキーを全部落とす（子セッション扱いを避けるため）ので、
外から CLAUDE_CONFIG_DIR を渡しても消える。そこで「許可リストに載った役だけ」
落とした後の env に試用の3つ（CLAUDE_CONFIG_DIR・HOME・USERPROFILE）を足す。

【置き場】
claude-session-resumer と claude-web-app に同じものを1本ずつ置く（どちらも同じ設定ファイルを読む）。
2本の中身がずれないよう、置いた後に sha256 を照らす（PATCH.md の 0）。

【設定ファイル】既定は C:\\Users\\user1\\claude-trial\\trial_roles.json
（環境変数 CLAUDE_WEB_APP_TRIAL_ROLES で差し替え可）:
    {"config_dir": "C:\\Users\\user1\\claude-trial\\cfg",
     "home": "C:\\Users\\user1\\claude-trial\\home",
     "roles": ["example-analyzer"]}
役の名前は作業場（cwd）の末尾のフォルダ名で照らす（大文字小文字は区別しない）。
役のサブフォルダや worktree で動いたセッションは別の名前になり、本家で起きる（陰性側に倒れる）。
読み直しは呼び出し側が1回の処理の先頭で load() を1度だけ呼び、その conf を各関数へ渡す
（途中で書き換わっても同じ処理の中で判定が割れないように）。

【守り】
- ファイルが無い・roles が空 → 試用の役は無い（本家で起こす）。これは正常。
- ファイルはあるのに使えない（JSON の破れ・形の違い・フォルダが無い等）→ 1回読み直し、
  それでも駄目なら前に読めた設定を使う（このプロセスのもの→隣の trial_roles.lastgood.json）
  （書き換えの途中で試用の役が本家へ黙って流れないように。resumer は呼ぶたびに別のプロセスなのでファイルにも残す）。
  理由は設定の隣の trial_roles.error.txt に残す（読めたら消す）。last_error() でも引ける。
- 本家の置き場の場所（MAIN_USER_DIR）は HOME / USERPROFILE に頼らず固定する
  （resumer や web-app が試用の HOME を継いで起きても、本家と試用を取り違えないように）。
戻すときは roles を空にする（試用の役の会話は cfg の projects に残り、本家からは再開されない）。
"""

import json
import logging
import ntpath
import os
import time

log = logging.getLogger("trial_roles")

ENV_PATH = "CLAUDE_WEB_APP_TRIAL_ROLES"
ENV_MAIN_USER_DIR = "CLAUDE_WEB_APP_MAIN_USER_DIR"
# 本家のユーザーのフォルダ。HOME / USERPROFILE から取らない（試用の HOME を継いだ親でも狂わないように）
MAIN_USER_DIR = os.environ.get(ENV_MAIN_USER_DIR) or (
    r"C:\Users\user1" if os.name == "nt" else os.path.expanduser("~")
)
MAIN_HOME = os.path.join(MAIN_USER_DIR, ".claude")
DEFAULT_PATH = os.path.join(MAIN_USER_DIR, "claude-trial", "trial_roles.json")
ERROR_NAME = "trial_roles.error.txt"
LASTGOOD_NAME = "trial_roles.lastgood.json"

# このプロセスで最後に読めた設定（読めない間の代わり）と、最後の読み失敗の理由
_last_good = None
_last_error = None


class BrokenConfig(ValueError):
    """設定ファイルはあるのに使えない（理由は文面）。"""


def _norm(path):
    """比べるための形（Windows の流儀で区切りと大文字小文字をそろえ、末尾の区切りを落とす）。"""
    return ntpath.normcase(ntpath.normpath(str(path).strip())).rstrip("\\")


def _parse(path):
    """設定を読んで形を確かめる。roles が空なら None。使えなければ BrokenConfig。"""
    try:
        # utf-8-sig: メモ帳や PowerShell 5.1 が付ける BOM があっても読めるように
        with open(path, encoding="utf-8-sig") as f:
            conf = json.load(f)
    except (OSError, ValueError) as e:
        raise BrokenConfig(f"読めない: {e}")
    if not isinstance(conf, dict):
        raise BrokenConfig("形が違う（{} でない）")
    roles = conf.get("roles")
    if roles is None:
        roles = []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise BrokenConfig("roles が文字列の並びでない")
    names = set()
    for r in roles:
        name = r.strip().lower()
        if not name:
            continue
        if any(c in name for c in "\\/:"):
            # 末尾のフォルダ名と照らすので、区切りを含む名前は決して一致しない。黙って捨てると
            # その役が本家で起きるので、設定ごと使えない扱いにして理由を残す
            raise BrokenConfig(f"役の名前 {r!r} に区切りがある（作業場のフォルダ名だけを書く）")
        names.add(name)
    if not names:
        return None
    config_dir = conf.get("config_dir")
    home = conf.get("home")
    if not isinstance(config_dir, str) or not os.path.isdir(config_dir):
        raise BrokenConfig(f"config_dir {config_dir!r} が無い")
    if not isinstance(home, str) or not os.path.isdir(home):
        raise BrokenConfig(f"home {home!r} が無い")
    if _norm(config_dir) == _norm(MAIN_HOME):
        raise BrokenConfig(f"config_dir {config_dir!r} が本家の置き場と同じ")
    return {"config_dir": config_dir, "home": home, "roles": names}


def _note_error(path, message):
    """読めない理由を設定の隣に残す（書けなくても起動は止めない）。"""
    try:
        with open(os.path.join(os.path.dirname(path), ERROR_NAME), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")
    except OSError:
        pass


def _clear_error(path):
    try:
        os.remove(os.path.join(os.path.dirname(path), ERROR_NAME))
    except OSError:
        pass


def _lastgood_path(path):
    return os.path.join(os.path.dirname(path), LASTGOOD_NAME)


def _save_lastgood(path, conf):
    """読めた設定を隣に残す（別のプロセスが壊れた間に使う）。書けなくても止めない。"""
    body = {"config_dir": conf["config_dir"], "home": conf["home"], "roles": sorted(conf["roles"])}
    target = _lastgood_path(path)
    try:
        with open(target, encoding="utf-8") as f:
            if json.load(f) == body:
                return
    except (OSError, ValueError):
        pass
    try:
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        pass


def _read_lastgood(path):
    try:
        return _parse(_lastgood_path(path))
    except BrokenConfig:
        return None


def _drop_lastgood(path):
    try:
        os.remove(_lastgood_path(path))
    except OSError:
        pass


def load(path=None):
    """設定を読む。None＝試用の役は無い（本家で起こす）。

    ファイルはあるのに使えないときは1回読み直し、それでも駄目なら前に読めた設定を返す
    （このプロセスで読めたもの→無ければ隣の trial_roles.lastgood.json→無ければ None）。
    設定ファイルを消した・roles を空にしたときは「試用の役なし」とし、前に読めた設定も捨てる。
    """
    global _last_good, _last_error
    from_env = os.environ.get(ENV_PATH)
    path = path or from_env or DEFAULT_PATH
    if not os.path.isfile(path):
        if from_env and path == from_env:
            # 環境変数で名指したのに無いのは書き損じ。黙って「試用なし」にしない
            _last_error = f"{ENV_PATH} が指す試用の役の設定 {path} が無い"
            log.warning("%s。本家で起こします", _last_error)
            _note_error(path, f"{_last_error}。本家で起こします")
            return None
        # 消して書き直す保存の途中かもしれないので1回だけ見直す。ただし前に読めた設定があるときだけ
        #   （試用の役が無い普段の道＝ファイルも lastgood も無いときに、一覧や送りのたびに待たないように）
        if _last_good or os.path.isfile(_lastgood_path(path)):
            time.sleep(0.2)
        if not os.path.isfile(path):
            _last_good, _last_error = None, None
            _drop_lastgood(path)
            return None
    for attempt in (1, 2):
        try:
            conf = _parse(path)
        except BrokenConfig as e:
            if attempt == 1:
                time.sleep(0.2)  # 書き換えの途中を避けて1回だけ読み直す
                continue
            _last_error = f"試用の役の設定 {path} が使えない（{e}）"
            fallback = _last_good or _read_lastgood(path)
            how = "前に読めた設定を使います" if fallback else "本家で起こします"
            log.warning("%s。%s", _last_error, how)
            _note_error(path, f"{_last_error}。{how}")
            return fallback
        _last_good, _last_error = conf, None
        _clear_error(path)
        if conf:
            _save_lastgood(path, conf)
        else:
            _drop_lastgood(path)
        return conf


def last_error():
    """最後の load() が設定を使えなかった理由（使えたなら None）。"""
    return _last_error


def role_of(cwd):
    """作業場の末尾のフォルダ名（役の名前として照らす）。"""
    if not cwd:
        return ""
    if isinstance(cwd, bytes):
        cwd = cwd.decode("utf-8", "replace")
    # ntpath は / と \ の両方を区切りとして扱う（Windows の cwd がどちらの形で来ても同じ名前に）
    return ntpath.basename(ntpath.normpath(str(cwd).strip())).strip().lower()


def is_trial(cwd, conf):
    """この作業場の役を試用で起こすか（conf は load() の戻り・None なら偽）。"""
    return bool(conf) and role_of(cwd) in conf["roles"]


def apply(env, cwd, conf):
    """起動の env（CLAUDE* を落とした後のもの）の写しを返す。

    試用の役なら試用の3つを足す。ほかの役で HOME / USERPROFILE が試用の home を
    指していたら（親が試用の HOME を継いでいた）、本家のユーザーのフォルダへ戻す。
    """
    out = dict(env)
    if is_trial(cwd, conf):
        out["CLAUDE_CONFIG_DIR"] = conf["config_dir"]
        out["HOME"] = conf["home"]
        out["USERPROFILE"] = conf["home"]
    elif conf:
        for key in ("HOME", "USERPROFILE"):
            if key in out and _norm(out[key]) == _norm(conf["home"]):
                out[key] = MAIN_USER_DIR
    return out


def claude_json_path(cwd, conf):
    """試用の役の信頼登録を書く .claude.json（CLAUDE_CONFIG_DIR の直下）。試用でなければ None（今までどおり本家の物）。"""
    if is_trial(cwd, conf):
        return os.path.join(conf["config_dir"], ".claude.json")
    return None


def home_for(cwd, conf):
    """この作業場の役の claude の置き場（sessions・projects の親）。試用の役なら config_dir、ほかは None（呼び手の本家の置き場）。"""
    if is_trial(cwd, conf):
        return conf["config_dir"]
    return None


def extra_homes(conf):
    """本家のほかに、生きているセッションと会話の記録を探す置き場（試用が無ければ空）。"""
    if conf and _norm(conf["config_dir"]) != _norm(MAIN_HOME):
        return [conf["config_dir"]]
    return []


def claude_homes(conf):
    """会話の記録を拾う置き場の一覧。本家が先・試用があれば後ろに足す。"""
    homes = [MAIN_HOME]
    if conf and _norm(conf["config_dir"]) != _norm(MAIN_HOME):
        homes.append(conf["config_dir"])
    return homes


def record_allowed(cwd, source_home, conf):
    """その置き場で見つけた会話を、この役として再開してよいか。

    試用の役は試用の置き場の会話だけ、ほかの役は本家の会話だけを再開する。
    （移した直後の役が本家の古い会話を --resume して本家の枠を食う・
    　許可リストから外した役が試用の会話を本家で開こうとして失敗する、の2つを防ぐ）
    再開の候補を「cwd ごとに最新の1本」に絞るより前にかけること。
    """
    trial = is_trial(cwd, conf)
    in_trial_home = bool(conf) and _norm(source_home) == _norm(conf["config_dir"])
    return trial == in_trial_home

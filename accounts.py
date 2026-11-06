# -*- coding: utf-8 -*-
"""claude のアカウント（口座）の定義と、作業場ごとの割り当て（正本）。

【なぜ要るか】
同じ機械で claude を2つのアカウント（本家の ~/.claude と、CLAUDE_CONFIG_DIR で分けた別の置き場）で
動かす。どの作業場をどちらで起こすかを本アプリが覚え、起動・一覧・会話の記録・使用量・ログインの期限を
アカウントごとに正しい置き場で扱う（2026-10-28 管理役の依頼。それまでは trial_roles.json の roles が正本で、
1本足すたびに管理者の台本が要り、画面から作ると必ず本家になっていた）。

【定義】config/settings.json の ``accounts``（並び）。置き場を持たない1件が本家:
    "accounts": [
      {"name": "main", "label": "本家", "limits_url": "http://localhost:5006/api/limits"},
      {"name": "sub", "label": "2つ目のアカウント",
       "config_dir": "C:\\\\...\\\\cfg", "home": "C:\\\\...\\\\home", "statusline_dir": "C:\\\\...\\\\statusline"}
    ]
書いていなければ本家1件だけ（選ぶ画面は出さない・今までと同じ動き）。

【割り当て（正本）】~/.claude-web-app/account_assignments.json。作業場のフォルダ（絶対パス）ごとにアカウントの名前。
載っていない作業場は本家。会話の記録はアカウントの置き場ごとに分かれるので、割り当ては「移す」操作
（会話を持ち越すなら記録を写してから起こす）でしか変えない。

【写し】claude-session-resumer は trial_roles.json（役＝作業場のフォルダ名）を読んで起こす。
割り当てを変えるたびに、本家以外の1アカウント分を trial_roles.json の形で書き出す（設定 ``trial_roles_mirror``）。
写しは正本ではない。正本が無い最初の1回だけ、写しの roles を読み込んで正本を作る。
"""

import json
import math
import ntpath
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone


def real_user_dir():
    """本家のユーザーのフォルダ（Windows の既知フォルダ Profile。HOME / USERPROFILE を見ない）。

    本家でないアカウントの子は HOME / USERPROFILE がそのアカウントの home を指すので、
    そこから session_manager を読む道具が expanduser("~") で本家を決めると、置き場を取り違える
    （2026-10-28 に本家でないアカウントの子から試して実測）。取れなければ expanduser に戻す。
    """
    if os.name == "nt":
        try:
            import ctypes
            import uuid

            folder_id = uuid.UUID(
                "{5E6C858F-0E22-4760-9AFE-EA3317B67173}"
            )  # FOLDERID_Profile
            guid = (ctypes.c_ubyte * 16).from_buffer_copy(folder_id.bytes_le)
            path = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if (
                shell32.SHGetKnownFolderPath(
                    ctypes.byref(guid), 0, None, ctypes.byref(path)
                )
                == 0
            ):
                try:
                    if path.value:
                        return path.value
                finally:
                    ctypes.windll.ole32.CoTaskMemFree(path)
        except (OSError, AttributeError, ValueError):
            pass
    return os.path.expanduser("~")


USER_DIR = real_user_dir()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "config", "settings.json")
STATE_PATH = os.path.join(USER_DIR, ".claude-web-app", "account_assignments.json")

# 起動の env に足してよいキー（起動役も同じ並びで照らす）
ENV_KEYS = ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE")

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# 設定に accounts が無いときの本家だけの定義
DEFAULT_ACCOUNTS = [{"name": "main", "label": "既定"}]


# 写しに付ける注記。正本は絶対パスで書く（写しを読む本家以外のアカウントでは ~ が別の home を指すため。
# 2026-10-28 管理役の確かめで指摘）。注記の文面だけの違いは手直しとみなさない（write_mirror）
def mirror_note():
    return (
        f"claude-web-app が書き出す写し。正本は {STATE_PATH}。"
        "手で直さず、web-app の画面か POST /api/accounts/move で変える"
    )


# 5時間枠の線（使用%。これ以上なら新しいセッションの既定にしない）の既定
DEFAULT_FIVE_HOUR_MAX_PCT = 80

# 週次の使用%の取り直しの間隔（秒）と、statusline の値を「いまの値」とみなす長さ（秒）
USAGE_CACHE_SEC = 60
STATUSLINE_FRESH_SEC = 6 * 3600
LIMITS_TIMEOUT_SEC = 3

# 取り込めなかったときに試し直すまでの間（秒）
IMPORT_RETRY_SEC = 30

_lock = threading.RLock()
_usage_cache = {}
# 最後に取り込めなかった (時刻, 理由)
_import_failed = None


class AccountError(Exception):
    """アカウントの定義・割り当ての誤り（文面はそのまま利用者に見せる）。"""


def _norm(path):
    """比べるための形（Windows の流儀で区切りと大文字小文字をそろえ、末尾の区切りを落とす）。"""
    return ntpath.normcase(ntpath.normpath(str(path).strip())).rstrip("\\")


def folder_name(path):
    """作業場の末尾のフォルダ名（小文字。trial_roles の役の名前と同じ照らし方）。"""
    return ntpath.basename(ntpath.normpath(str(path or "").strip())).strip().lower()


# --- 定義 -------------------------------------------------------------------


def _read_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def settings():
    """アカウントに関わる設定（accounts・trial_roles_mirror・mirror_account）。呼ぶたびに読む。"""
    data = _read_settings()
    return {
        "accounts": data.get("accounts"),
        "trial_roles_mirror": data.get("trial_roles_mirror") or "",
        "mirror_account": data.get("mirror_account") or "",
        "five_hour_max_pct": data.get("five_hour_max_pct"),
        "unassigned_account": str(data.get("unassigned_account") or "").strip().lower(),
    }


def definitions():
    """アカウントの定義の並び（本家が先頭）。使えない定義は error を付けて残す（黙って本家へ流さない）。

    本家は ``"main": true`` を付けた1件（置き場 config_dir を書かない）。印が1件も無いときだけ、
    置き場の無い定義がちょうど1件ならそれを本家とする（置き場の無い定義が2件以上なら、
    どれが書き損じか分からないので全部を使えないものにし、名前の無い既定の本家を足す）。
    置き場のキーを書き損じたアカウントが、並び順しだいで本家に化けないようにするため（2026-10-28 評価役の指摘）。

    各要素: name / label / main（本家か）/ config_dir / home / limits_url / statusline_dir / error
    """
    raw = settings()["accounts"]
    if not isinstance(raw, list) or not raw:
        raw = DEFAULT_ACCOUNTS
    entries = []
    names = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        if not NAME_PATTERN.match(name) or name in names:
            continue
        names.add(name)
        entries.append((name, entry))
    flagged = [n for n, e in entries if e.get("main") is True]
    without_dir = [n for n, e in entries if not e.get("config_dir")]
    if flagged:
        main_name = flagged[0] if len(flagged) == 1 else None
    else:
        main_name = without_dir[0] if len(without_dir) == 1 else None
    result = []
    main_seen = False
    for name, entry in entries:
        config_dir = entry.get("config_dir") or None
        account = {
            "name": name,
            "label": str(entry.get("label") or name),
            "main": name == main_name,
            "config_dir": config_dir,
            "home": entry.get("home") or None,
            "limits_url": entry.get("limits_url") or None,
            "statusline_dir": entry.get("statusline_dir") or None,
            "capacity": entry.get("capacity"),
            "five_hour_capacity": entry.get("five_hour_capacity"),
            "five_hour_unlimited": entry.get("five_hour_unlimited"),
            "five_hour_max_pct": entry.get("five_hour_max_pct"),
            "error": None,
        }
        if account["main"]:
            if config_dir:
                # 本家の置き場は ~/.claude に決まっている。書いてあれば何かの取り違え
                account["main"] = False
                account["error"] = "既定のアカウントに置き場（config_dir）は書けません"
            else:
                main_seen = True
        elif name in flagged:
            account["error"] = "既定のアカウント（main: true）が2つ以上あります"
        elif not config_dir:
            account["error"] = (
                "置き場（config_dir）の無い定義が2つ以上あり、どれが既定のアカウントか分かりません"
                '（既定のアカウントに "main": true を付けてください）'
                if not flagged and len(without_dir) > 1
                else "置き場（config_dir）がありません"
            )
        else:
            account["error"] = _check_extra(account)
        if account["error"]:
            # 足す既定の本家と同じ名前のときも、どちらが使えないかを選択肢で見分けられるように
            account["label"] += "（設定の誤り）"
        result.append(account)
    if not main_seen:
        result.insert(
            0,
            dict(
                DEFAULT_ACCOUNTS[0],
                main=True,
                config_dir=None,
                home=None,
                limits_url=None,
                statusline_dir=None,
                capacity=None,
                five_hour_capacity=None,
                five_hour_unlimited=None,
                five_hour_max_pct=None,
                error=None,
            ),
        )
    result.sort(key=lambda a: not a["main"])
    return result


def _check_extra(account):
    """本家でないアカウントの置き場を確かめる。使えなければ理由（使えれば None）。"""
    config_dir, home = account["config_dir"], account["home"]
    if not isinstance(config_dir, str) or not os.path.isdir(config_dir):
        return f"置き場 {config_dir} がありません"
    if not isinstance(home, str) or not os.path.isdir(home):
        return f"home {home} がありません"
    main_home = os.path.join(USER_DIR, ".claude")
    if _norm(config_dir) == _norm(main_home):
        return f"置き場 {config_dir} が既定のアカウントの置き場（~/.claude）と同じです"
    return None


def main_account():
    return definitions()[0]


def get(name):
    """名前からアカウントの定義（無ければ None）。"""
    name = str(name or "").strip().lower()
    return next((a for a in definitions() if a["name"] == name), None)


def require_usable(name):
    """起こせるアカウントの定義を返す。無い・使えないなら AccountError（理由と代わりの選び方つき）。"""
    account = get(name)
    if account is None:
        known = "・".join(a["name"] for a in definitions())
        raise AccountError(
            f"アカウント {name} は設定にありません（選べるのは {known}）"
        )
    if account["error"]:
        raise AccountError(
            f"{account['label']} の設定が使えません（{account['error']}）。"
            f"config/settings.json の accounts を直すか、{main_account()['label']} で起こしてください"
        )
    return account


def claude_home(account, main_home):
    """そのアカウントの claude の置き場（sessions・projects の親）。本家は main_home。"""
    return main_home if account["main"] else account["config_dir"]


def claude_json(account):
    """信頼登録を書く .claude.json（本家は None＝呼び手の既定）。"""
    return (
        None if account["main"] else os.path.join(account["config_dir"], ".claude.json")
    )


def apply_env(env, account):
    """起動の env（CLAUDE* を落とした後のもの）の写しに、そのアカウントの置き場を載せる。

    本家なら、HOME / USERPROFILE がほかのアカウントの home を指していたとき（親が継いでいた）だけ
    本家のユーザーのフォルダへ戻す。
    """
    out = dict(env)
    out.update(env_overrides(account, env))
    return out


def env_overrides(account, env=None):
    """起動役へ渡す env の差分（ENV_KEYS のうち変えるものだけ）。"""
    if not account["main"]:
        return {
            "CLAUDE_CONFIG_DIR": account["config_dir"],
            "HOME": account["home"],
            "USERPROFILE": account["home"],
        }
    env = os.environ if env is None else env
    others = {_norm(a["home"]) for a in definitions() if not a["main"] and a["home"]}
    user_dir = USER_DIR
    fixes = {}
    for key in ("HOME", "USERPROFILE"):
        if key in env and _norm(env[key]) in others:
            fixes[key] = user_dir
    return fixes


# --- 割り当て（正本） -------------------------------------------------------


def _empty_state():
    return {"version": 1, "assignments": {}, "legacy_names": {}}


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise AccountError(f"割り当ての台帳 {STATE_PATH} を読めません: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("assignments"), dict):
        raise AccountError(f"割り当ての台帳 {STATE_PATH} の形が違います")
    data.setdefault("legacy_names", {})
    return data


def _save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


# 写しの役の名前（フォルダ名）を作業場へ解く関数（本アプリが起動時に set_role_resolver で渡す）
_role_resolver = None


def set_role_resolver(resolve):
    """取り込みに使う「役の名前 → [(作業場, プロジェクトキー)]」の関数を渡す。"""
    global _role_resolver
    _role_resolver = resolve


def _mirror_roles():
    """写し（trial_roles.json）にいま書かれている役の名前（無い・読めなければ空）。"""
    path = settings()["trial_roles_mirror"]
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, list):
        return []
    return sorted({str(r).strip().lower() for r in roles if str(r).strip()})


def _current_state():
    """台帳を読む。無ければ、写しに役があるうちは取り込んでから読む（_lock の中で呼ぶ）。

    取り込めないときは、写しの役をフォルダ名の印として持った台帳を返す（ファイルには書かない）。
    台帳が無いまま写しを無視すると、写しに載った役が本家で起き、次の割り当てで写しの役が消えるため
    （2026-10-28 評価役の指摘）。
    """
    global _import_failed
    current = _load_state()
    if current is not None:
        return current
    roles = _mirror_roles()
    if not roles:
        return _empty_state()
    reason = "取り込みの手順が登録されていません（本アプリの外から読んでいます）"
    if _role_resolver is not None:
        # 取り込めなかった直後は、しばらく試し直さない（一覧の配信のたびに置き場を探さないように）
        if _import_failed and time.time() - _import_failed[0] < IMPORT_RETRY_SEC:
            reason = _import_failed[1]
        else:
            report = import_trial_roles(_role_resolver)
            current = _load_state()
            if current is not None:
                _import_failed = None
                return current
            reason = report.get("skipped") or "取り込めませんでした"
            _import_failed = (time.time(), reason)
    account = _mirror_account()
    pending = _empty_state()
    # 取り込み先が決まらないときは、名前の無いアカウントに割り当てた扱い＝起こす前に断られる
    target = account["name"] if account else "(写しのアカウントが決まりません)"
    pending["legacy_names"] = {r: target for r in roles}
    pending["pending_import"] = reason
    return pending


def pending_import_reason():
    """trial_roles.json の役を取り込めずに待っているなら、その理由（待っていなければ None）。

    待っている間は本家以外への割り当て（作成・移す）を書けない。
    """
    return state().get("pending_import") or None


def state():
    """台帳の中身（無ければ空。写しに役があれば取り込んでから）。読めなければ AccountError。"""
    with _lock:
        return _current_state()


def assigned_name(cwd, current=None):
    """作業場に割り当てたアカウントの名前（無ければ None＝本家）。

    台帳が読めないときは AccountError（黙って本家で起こさない）。
    """
    if not cwd:
        return None
    current = state() if current is None else current
    entry = current["assignments"].get(_norm(cwd))
    if entry:
        if entry.get("explicit_main"):
            return None  # フォルダ名の印より優先して本家（同じ名前のほかの作業場の印は残す）
        return entry.get("account")
    # 取り込みのときに作業場へ解けなかった役の名前（フォルダ名で照らす）
    return current["legacy_names"].get(folder_name(cwd))


def unassigned_account():
    """割り当ての無い作業場の既定のアカウント（設定 unassigned_account・書いていなければ本家）。

    名前が設定に無い・使えないときは AccountError（黙って本家で起こさない）。
    2026-11-01 管理役の決め（ユーザーの指示「account_a アカウントは余裕ない」「account_b の枠を使い切りたい」）。
    """
    name = settings()["unassigned_account"]
    if not name:
        return main_account()
    account = get(name)
    if account is None or account["error"]:
        raise AccountError(
            f"割り当ての無い作業場の既定（unassigned_account）の {name} が使えません"
            f"（{account['error'] if account else '設定の accounts にありません'}）。"
            "config/settings.json を確かめてください"
        )
    return account


# 本家の置き場にその作業場の会話の記録があるかを答える関数（session_manager が import 時に set_history_probe で渡す）
_history_probe = None


def set_history_probe(probe):
    """「作業場 → 本家の置き場に会話の記録があるか」の関数を渡す。"""
    global _history_probe
    _history_probe = probe


def _has_main_history(cwd):
    """本家の置き場に、その作業場の会話の記録のフォルダがあるか。"""
    if _history_probe is not None:
        return bool(_history_probe(cwd))
    # 名前の付け方は session_manager.transcript_dir_in と同じ
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))
    return os.path.isdir(os.path.join(USER_DIR, ".claude", "projects", name))


def resolve(cwd, current=None):
    """作業場で起こすアカウントの名前と、そう決めた理由。

    理由: explicit_main（本家と明示）/ assigned（台帳の割り当て）/ legacy（フォルダ名の印）/
    main_history（本家に会話の記録がある）/ default（割り当ての無い作業場の既定）/ no_cwd（作業場が分からない）。
    割り当ての無い作業場は unassigned_account に従う。ただし本家に会話の記録がある作業場は本家のまま
    （既定を変えた途端に、前からの作業場の続きが別の置き場を探して見えなくならないように。
    本家から移すときは「アカウントを移す」で会話を持ち越す）。

    Returns:
        (name, reason)
    """
    if not cwd:
        return main_account()["name"], "no_cwd"
    current = state() if current is None else current
    entry = current["assignments"].get(_norm(cwd))
    if entry and entry.get("explicit_main"):
        return main_account()["name"], "explicit_main"
    if entry and entry.get("account"):
        return entry["account"], "assigned"
    legacy = current["legacy_names"].get(folder_name(cwd))
    if legacy:
        return legacy, "legacy"
    if _has_main_history(cwd):
        return main_account()["name"], "main_history"
    return unassigned_account()["name"], "default"


def effective_name(cwd, current=None):
    """作業場で起こすアカウントの名前（resolve の名前だけ）。"""
    return resolve(cwd, current)[0]


def pin_default(cwd, project_key=None):
    """既定（本家でない unassigned_account）で起こす作業場を、台帳に割り当てとして書いて固定する。

    台帳に書かないと写し（trial_roles.json）に載らず claude-session-resumer と判断が食い違い、
    後から本家に会話の記録ができると黙って本家へ切り替わるため（2026-11-01 評価役の指摘）。
    書いたら割り当ての結果（assign の戻り）、書く必要が無ければ None。書けなければ AccountError / OSError。
    trial_roles.json の役の取り込み待ちの間は台帳を書けないので、書かずに None（起動は既定のまま通す。
    取り込めば写しの役が台帳に入り、それ以外の作業場は今の判定どおりなので、待つ間だけ固定されない）。
    """
    name, reason = resolve(cwd)
    account = get(name)
    if reason != "default" or account is None or account["main"]:
        return None
    if pending_import_reason():
        print(
            f"[Accounts] 取り込み待ちのため、{cwd} の既定の割り当て（{name}）を台帳に書かずに起こします",
            flush=True,
        )
        return None
    return assign(cwd, project_key, name, "default")


def account_for(cwd):
    """作業場で起こすアカウントの定義（割り当てが無ければ effective_name の既定）。"""
    name = assigned_name(cwd)
    if not name:
        # 割り当て・フォルダ名の印が無い（または本家と明示した）作業場
        return get(effective_name(cwd))
    account = get(name)
    if account is None:
        raise AccountError(
            f"作業場 {cwd} の割り当て先 {name} は設定にありません（config/settings.json の accounts を確かめてください）"
        )
    if account["main"]:
        # 割り当ては本家以外にしか書かない。本家に解けたのは設定の取り違え（置き場の書き損じ等）
        raise AccountError(
            f"作業場 {cwd} の割り当て先 {name} の設定が使えません（既定のアカウントとして定義されています。"
            "config/settings.json の accounts を確かめてください）"
        )
    return account


def assign(path, project_key, name, by):
    """作業場の割り当てを書く（本家なら台帳から外す）。写しも書き直す。

    Returns:
        dict: previous（前の名前・本家なら None）/ mirror（写しの結果）
    """
    account = require_usable(name)
    with _lock:
        current = _current_state()
        if current.get("pending_import"):
            raise AccountError(
                "trial_roles.json の役をまだ取り込めていないので、割り当ての台帳を書きません"
                f"（写しの役が消えないように。理由: {current['pending_import']}）。"
                "設定の accounts と trial_roles_mirror を確かめてください"
            )
        before = mirror_body(current)
        key = _norm(path)
        previous = assigned_name(path, current)
        if account["main"]:
            try:
                default_is_main = unassigned_account()["main"]
            except AccountError:
                # 既定の設定が誤っているときも、本家へ戻す・移すは書けるように（明示の本家と書く側へ倒す）
                default_is_main = False
            current["assignments"].pop(key, None)
            if folder_name(path) in current["legacy_names"] or not default_is_main:
                # フォルダ名の印は同じ名前のほかの作業場にも効くので消さず、この作業場だけ本家と書く。
                # 割り当ての無い作業場の既定が本家でないときも、外すと既定へ流れるので本家と書く
                current["assignments"][key] = {
                    "path": os.path.abspath(path),
                    "project_key": project_key,
                    "account": account["name"],
                    "explicit_main": True,
                    "updated_at": _now_text(),
                    "by": by,
                }
        else:
            current["assignments"][key] = {
                "path": os.path.abspath(path),
                "project_key": project_key,
                "account": account["name"],
                "updated_at": _now_text(),
                "by": by,
            }
        _save_state(current)
        return {"previous": previous, "mirror": write_mirror(current, before)}


def rename_path(old_path, new_path, new_key):
    """作業場のフォルダが移った・名前が変わったとき、割り当てを付け替える（配下の作業場も）。

    付け替えないと、移した先の作業場は台帳に無い＝本家で起きてしまう。
    台帳に無く、フォルダ名の印・本家の会話の記録で行き先が決まっていた作業場（配下の projects/ の下も）は、
    新しいパスでは印も記録も効かず既定へ流れるので、前と同じアカウントを新しいパスに書いて固定する
    （改名だけでアカウントを変えない）。

    Returns:
        int: 付け替えた件数と、固定した件数の和
    """
    old, new = _norm(old_path), _norm(new_path)
    if old == new:
        return 0
    with _lock:
        current = _current_state()
        if current.get("pending_import"):
            print(
                f"[Accounts] 取り込み待ちのため、改名 {old_path} -> {new_path} の割り当てを付け替えません"
                f"（理由: {current['pending_import']}）",
                flush=True,
            )
            return 0
        before = mirror_body(current)
        moved = 0
        carried = (
            set()
        )  # いま付け替えた行の鍵（固定の側で「前からの行」と取り違えないため）
        for key in list(current["assignments"]):
            if key == old:
                suffix = ""
            elif key.startswith(old + "\\"):
                suffix = key[len(old) :]
            else:
                continue
            entry = current["assignments"].pop(key)
            tail = os.path.abspath(entry.get("path") or "")[
                len(os.path.abspath(old_path)) :
            ]
            entry.update(
                {
                    "path": os.path.abspath(new_path) + tail,
                    "project_key": (
                        new_key + tail.replace("\\", "/") if suffix else new_key
                    ),
                    "updated_at": _now_text(),
                }
            )
            _warn_overwrite(current, new + suffix, entry, old_path, new_path)
            current["assignments"][new + suffix] = entry
            carried.add(new + suffix)
            moved += 1
        # 台帳に無かった作業場（この作業場と配下の projects/ の下）は、前の行き先が変わるなら固定する
        for tail in [""] + _sub_workspace_tails(old_path, new_path):
            if (
                _norm(old_path + tail) in current["assignments"]
                or _norm(new_path + tail) in carried
            ):
                continue
            kept = current["assignments"].get(_norm(new_path + tail))
            if kept:
                # 改名先に前からの行があれば、その行が勝つ（書き換えない）。記録で決まっていた行き先と違うかもしれないので一言残す
                print(
                    f"[Accounts] 改名 {old_path + tail} -> {new_path + tail}: 改名先に前からの割り当て"
                    f"（{kept.get('account')}）があるので、それを使います",
                    flush=True,
                )
                continue
            moved += _keep_resolved_account(
                current,
                old_path + tail,
                new_path + tail,
                new_key + tail.replace("\\", "/"),
            )
        if moved:
            _save_state(current)
            write_mirror(current, before)
        return moved


# 配下の作業場を置くフォルダの名前（project_manager.PROJECTS_SUBDIR と同じ）
_PROJECTS_SUBDIR = "projects"


def _sub_workspace_tails(old_path, new_path):
    """配下の作業場（<作業場>/projects/<名前>、さらにその下も）の、作業場からの相対の後ろ（"\\projects\\x" の形）。

    フォルダは移した後なら新しいパス、まだなら前のパスにあるので、ある方を辿る。
    """
    base = new_path if os.path.isdir(new_path) else old_path
    tails = []
    pending = [""]
    # リンク（ジャンクション等）で上のフォルダへ戻っても辿り続けないよう、見た実体を覚える
    seen = {os.path.normcase(os.path.realpath(base))}
    while pending:
        tail = pending.pop()
        holder = os.path.join(base + tail, _PROJECTS_SUBDIR)
        try:
            names = sorted(os.listdir(holder))
        except OSError:
            continue
        for name in names:
            path = os.path.join(holder, name)
            real = os.path.normcase(os.path.realpath(path))
            if os.path.isdir(path) and real not in seen:
                seen.add(real)
                child = f"{tail}\\{_PROJECTS_SUBDIR}\\{name}"
                tails.append(child)
                pending.append(child)
    return tails


def _warn_overwrite(current, key, entry, old_path, new_path):
    """付け替え・固定の先に別のアカウントの古い行があれば、置き換えることを記録に出す。"""
    existing = current["assignments"].get(key)
    if not existing:
        return
    if existing.get("account") == entry.get("account") and bool(
        existing.get("explicit_main")
    ) == bool(entry.get("explicit_main")):
        return
    print(
        f"[Accounts] 改名 {old_path} -> {new_path}: {existing.get('path')} にあった割り当て"
        f"（{existing.get('account')}）を {entry.get('account')} で置き換えます",
        flush=True,
    )


def _keep_resolved_account(current, old_path, new_path, new_key):
    """台帳に無い作業場の改名で、前の行き先（印・本家の記録）が新しいパスで変わるなら、新しいパスに書く。

    current を書き換えるだけ（保存は呼び出し側）。書いたら 1、書く必要が無ければ 0。
    既定の設定が誤っていて新しいパスの行き先が読めないときも、前の行き先を書く（黙って流さない）。
    """
    try:
        name, reason = resolve(old_path, current)
    except AccountError:
        return 0
    if reason not in ("legacy", "main_history"):
        return 0
    try:
        if resolve(new_path, current)[0] == name:
            return 0
    except AccountError:
        pass
    account = get(name)
    if account is None:
        return 0
    entry = {
        "path": os.path.abspath(new_path),
        "project_key": new_key,
        "account": account["name"],
        "updated_at": _now_text(),
        "by": "rename",
    }
    if account["main"]:
        entry["explicit_main"] = True
    current["assignments"][_norm(new_path)] = entry
    print(
        f"[Accounts] 改名 {old_path} -> {new_path}: 前の行き先（{name}・{reason}）を台帳に書いて保ちます",
        flush=True,
    )
    return 1


def _now_text():
    return datetime.now().isoformat(timespec="seconds")


# --- trial_roles.json の写しと、最初の1回の取り込み --------------------------


def _mirror_account():
    """写しに書くアカウント（設定 mirror_account、無ければ本家以外の唯一のもの）。"""
    conf = settings()
    extras = [a for a in definitions() if not a["main"]]
    if conf["mirror_account"]:
        return next((a for a in extras if a["name"] == conf["mirror_account"]), None)
    return extras[0] if len(extras) == 1 else None


def mirror_body(current):
    """写しに書く中身（書けないなら None）。"""
    account = _mirror_account()
    if not account or account["error"]:
        return None
    roles = {
        folder_name(e["path"])
        for e in current["assignments"].values()
        if e.get("account") == account["name"]
    }
    roles |= {n for n, a in current["legacy_names"].items() if a == account["name"]}
    return {
        "config_dir": account["config_dir"],
        "home": account["home"],
        "roles": sorted(roles),
        "_note": mirror_note(),
    }


def write_mirror(current, before=None):
    """trial_roles.json の写しを書く（中身が同じなら書かない）。失敗しても割り当ては戻さない。

    Args:
        current: 書き換えた後の台帳
        before: 書き換える前の台帳から作った写しの中身。渡すと、いまのファイルがそれと違う
            （手で直された）ときは上書きせずに error で知らせる（手で足した役を黙って消さない）

    Returns:
        dict: path / written / error
    """
    path = settings()["trial_roles_mirror"]
    if not path:
        return {"path": None, "written": False, "error": None}
    body = mirror_body(current)
    if body is None:
        return {
            "path": path,
            "written": False,
            "error": "写しに書くアカウントが決まりません",
        }
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            actual = json.load(f)
    except FileNotFoundError:
        actual = None
    except (OSError, ValueError):
        actual = "unreadable"
    if actual == body:
        return {"path": path, "written": False, "error": None}
    if (
        before is not None
        and actual is not None
        and _without_note(actual) != _without_note(before)
    ):
        file_roles = (
            set(actual.get("roles") or []) if isinstance(actual, dict) else set()
        )
        only_ledger = sorted(set(body["roles"]) - file_roles)
        only_file = sorted(file_roles - set(body["roles"]))
        return {
            "path": path,
            "written": False,
            "error": "trial_roles.json が手で直されているので書き直していません。"
            "claude-session-resumer はファイルにいま書かれている役で起こすので、"
            f"台帳にだけある役 {only_ledger} は resumer からは既定のアカウントで、"
            f"ファイルにだけある役 {only_file} は resumer からは写しのアカウントで起きます（web-app とは食い違います）。"
            "ファイルにだけある役は画面か POST /api/accounts/move で台帳へ入れてから trial_roles.json を除けると、"
            "次の割り当てで台帳どおりに書き直します",
        }
    try:
        tmp = path + ".tmp-claude-web-app"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        return {"path": path, "written": False, "error": f"書けません: {e}"}
    return {"path": path, "written": True, "error": None}


def _without_note(body):
    """注記を除いた写しの中身（注記の文面を変えても手直しと取り違えないため）。"""
    if not isinstance(body, dict):
        return body
    return {k: v for k, v in body.items() if k != "_note"}


def refresh_mirror_note():
    """写しの注記だけが古いとき、いまの注記で書き直す（役が違えば触らない＝手直しを消さない）。"""
    with _lock:
        try:
            current = _current_state()
        except AccountError as e:
            return {"written": False, "error": str(e)}
        if current.get("pending_import"):
            return {"written": False, "error": None}
        return write_mirror(current, mirror_body(current))


def mirror_status(current=None):
    """写しが正本と合っているか（手で直された・書けていない を見分ける）。"""
    path = settings()["trial_roles_mirror"]
    if not path:
        return {"path": None, "in_sync": None}
    current = state() if current is None else current
    body = mirror_body(current)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            actual = json.load(f)
    except FileNotFoundError:
        actual = None
    except (OSError, ValueError) as e:
        return {"path": path, "in_sync": False, "error": f"読めません: {e}"}
    if body is None:
        return {
            "path": path,
            "in_sync": None,
            "error": "写しに書くアカウントが決まりません",
        }
    file_roles = sorted(actual.get("roles") or []) if isinstance(actual, dict) else []
    return {
        "path": path,
        "in_sync": actual == body,
        "roles": body["roles"],
        "file_roles": file_roles if actual is not None else None,
    }


def import_trial_roles(resolve):
    """正本が無いときだけ、写し（trial_roles.json）の roles を読み込んで正本を作る。

    Args:
        resolve: 役の名前（フォルダ名）→ 作業場の候補 [(path, project_key), ...] を返す関数

    Returns:
        dict: imported（作業場へ解けた役）/ unresolved（解けず名前のまま残した役）/ skipped（理由）
    """
    with _lock:
        if os.path.exists(STATE_PATH):
            return {"skipped": "台帳が既にあります"}
        path = settings()["trial_roles_mirror"]
        if not path or not os.path.isfile(path):
            return {"skipped": "写しの設定・ファイルがありません"}
        account = _mirror_account()
        if not account or account["error"]:
            return {"skipped": "取り込み先のアカウントが使えません"}
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return {"skipped": f"写しを読めません: {e}"}
        roles = data.get("roles") if isinstance(data, dict) else None
        if not isinstance(roles, list):
            return {"skipped": "写しの roles が並びではありません"}
        if (
            isinstance(data, dict)
            and data.get("config_dir")
            and _norm(data["config_dir"]) != _norm(account["config_dir"])
        ):
            return {
                "skipped": f"写しの config_dir が {account['label']} の置き場と違います（取り込みません）"
            }
        current = _empty_state()
        imported, unresolved = [], []
        for role in roles:
            name = str(role).strip().lower()
            if not name:
                continue
            found = resolve(name)
            if len(found) == 1:
                folder, key = found[0]
                current["assignments"][_norm(folder)] = {
                    "path": os.path.abspath(folder),
                    "project_key": key,
                    "account": account["name"],
                    "updated_at": _now_text(),
                    "by": "import",
                }
                imported.append({"role": name, "project_key": key})
            else:
                # 作業場へ1つに解けない名前は、今までどおりフォルダ名で照らす（黙って本家へ流さない）
                current["legacy_names"][name] = account["name"]
                unresolved.append({"role": name, "candidates": [k for _, k in found]})
        current["imported_from"] = {"path": path, "at": _now_text()}
        _save_state(current)
        # 役は同じまま、注記を付けた写しに書き直す（以後は台帳と同じ中身かで手直しを見分ける）
        mirror = write_mirror(current)
        return {"imported": imported, "unresolved": unresolved, "mirror": mirror}


# --- 使用%（週次・5時間枠） ---------------------------------------------------

# 枠の名前（API・画面の鍵）→ 使用量モニターの鍵・statusline の鍵
WINDOWS = {
    "weekly": ("weekly_limit", "seven_day"),
    "five_hour": ("five_hour_limit", "five_hour"),
}
WINDOW_LABELS = {"weekly": "週の枠", "five_hour": "5時間枠"}


def _unknown(reason, detail=None, place=None):
    """取れなかった結果。reason は画面に出す平易な文、detail は API・ログ向けの詳しい理由。

    place は読みに行った置き場（statusline のフォルダ）。画面の注記に添え、どこを見ればよいかを分かるようにする。
    """
    result = {
        "known": False,
        "used_percentage": None,
        "reason": reason,
        "detail": detail or reason,
    }
    if place:
        result["place"] = place
    return result


def _both_unknown(reason, detail=None, place=None):
    return {window: _unknown(reason, detail, place) for window in WINDOWS}


def _from_limits_url(url):
    """使用量モニター（/api/limits）の weekly_limit・five_hour_limit を読む。"""
    try:
        with urllib.request.urlopen(url, timeout=LIMITS_TIMEOUT_SEC) as res:
            data = json.loads(res.read().decode("utf-8"))
    except Exception as e:  # 届かない・壊れた応答はすべて「分からない」
        return _both_unknown(
            "使用量を読む仕組みに繋がりません",
            f"{url} に届きません: {type(e).__name__}",
        )
    if not isinstance(data, dict) or not data.get("available"):
        detail = (
            (data.get("error_message") or data.get("status"))
            if isinstance(data, dict)
            else "形の違う応答"
        )
        return _both_unknown(
            "使用量を読む仕組みが値を返しません", f"{url} が値を返しません: {detail}"
        )
    result = {}
    for window, (key, _) in WINDOWS.items():
        part = data.get(key)
        used = part.get("used_percentage") if isinstance(part, dict) else None
        if not isinstance(used, (int, float)):
            result[window] = _unknown(
                "使用量を読む仕組みが値を返しません",
                f"{url} の値に{WINDOW_LABELS[window]}の%がありません",
            )
            continue
        result[window] = {
            "known": True,
            "used_percentage": float(used),
            "resets_at": part.get("resets_at"),
            "observed_at": data.get("fetched_at"),
            "stale": bool(data.get("is_stale")),
            "source": "limits_url",
        }
    return result


def _from_statusline(directory, now):
    """statusline の写し（claude が渡す rate_limits）の一番新しいものを読む（枠ごとに）。"""
    try:
        names = [n for n in os.listdir(directory) if n.endswith(".json")]
    except OSError as e:
        return _both_unknown(
            "最近の使用量の記録が読めません",
            f"statusline の置き場 {directory} を読めません: {type(e).__name__}",
            directory,
        )
    paths = sorted(
        (os.path.join(directory, n) for n in names), key=_mtime, reverse=True
    )[:50]
    result = {}
    for window, (_, key) in WINDOWS.items():
        result[window] = _from_statusline_window(directory, paths, key, window, now)
    return result


def _from_statusline_window(directory, paths, key, window, now):
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        part = (
            ((data.get("rate_limits") or {}).get(key) or {})
            if isinstance(data, dict)
            else {}
        )
        used = part.get("used_percentage")
        resets = part.get("resets_at")
        if not isinstance(used, (int, float)) or not isinstance(resets, (int, float)):
            continue
        observed = _mtime(path)
        if resets <= now:
            if window == "five_hour":
                # 5時間枠は戻りの時刻を過ぎれば新しい枠。最後の記録より後にこのアカウントで
                # 表示が更新されていない＝使っていないので 0% とみなす（戻りの時刻は次に動かすまで分からない）
                return {
                    "known": True,
                    "used_percentage": 0.0,
                    "resets_at": None,
                    "observed_at": datetime.fromtimestamp(observed).isoformat(
                        timespec="seconds"
                    ),
                    "stale": False,
                    "reset_passed": True,
                    "source": "statusline",
                }
            # 前の週の値しか無い。週が替わってから表示の更新が無いだけかもしれないので 0% とはみなさない
            return _unknown(
                "前の週の値しかありません（このアカウントで動かすと表示されます）",
                "statusline のいちばん新しい値の resets_at が過ぎています",
                directory,
            )
        return {
            "known": True,
            "used_percentage": float(used),
            "resets_at": datetime.fromtimestamp(
                resets, timezone(timedelta(hours=9))
            ).isoformat(),
            "observed_at": datetime.fromtimestamp(observed).isoformat(
                timespec="seconds"
            ),
            "stale": now - observed > STATUSLINE_FRESH_SEC,
            "source": "statusline",
        }
    return _unknown(
        "最近の使用量の記録がありません（このアカウントで動かすと表示されます）",
        f"statusline の置き場 {directory} に{WINDOW_LABELS[window]}の値がありません",
        directory,
    )


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def usage(account, now=None):
    """そのアカウントの使用%（{"weekly": …, "five_hour": …}）。取れなければ known: false と理由（推測しない）。"""
    now = time.time() if now is None else now
    key = account["name"]
    cached = _usage_cache.get(key)
    if cached and now - cached[0] < USAGE_CACHE_SEC:
        return cached[1]
    if account["error"]:
        result = _both_unknown("このアカウントの設定に誤りがあります", account["error"])
    elif account["limits_url"]:
        result = _from_limits_url(account["limits_url"])
    elif account["statusline_dir"]:
        result = _from_statusline(account["statusline_dir"], now)
    else:
        result = _both_unknown(
            "使用量の読み方が設定されていません",
            "設定に limits_url も statusline_dir もありません",
        )
    _usage_cache[key] = (now, result)
    return result


def weekly_usage(account, now=None):
    """そのアカウントの週次の使用%（usage の weekly）。"""
    return usage(account, now)["weekly"]


def five_hour_usage(account, now=None):
    """そのアカウントの5時間枠の使用%（usage の five_hour）。"""
    return usage(account, now)["five_hour"]


# --- 新規作成の既定の選び方 --------------------------------------------------


def policy(account):
    """振り分けの値（目安と枠の大きさ）。設定ファイルで変える（README 3.1節）。

    - capacity: 週の枠の大きさ（アカウントどうしの比。既定 1）。
      同じ作業で消費する%は枠の大きさに反比例するので、残りは「(100 - 使用%) × 枠の大きさ」で比べる。
    - five_hour_capacity: 5時間枠の大きさ（同じくアカウントどうしの比。既定 1）。
      週の枠と5時間枠は倍率が違う（旧 Max 20x は5時間枠が Pro の約20倍・週の枠は約10倍。
      2026-10-30 管理役の訂正）ので、同じ比を使わない。
    - five_hour_unlimited: true なら5時間枠の無いアカウント（Ultra の確認済み等）。
      5時間枠を残りの比べから外す（値が取れない・null でも「分からない」と数えない）。
      使用量モニターは Ultra にも5時間枠の値を返す（2026-10-31 に account_a で 1% を実測）ので、値の有無では決めない。
      値が目安以上のときは、ほかのアカウントと同じに既定から外す（Ultra でも本人確認の前は一時的な上限があるための保険）。
    - 比が週と5時間で違うと、少ないほうを取るときに単位の違う量を比べる（例: capacity 20・five_hour_capacity 1 なら
      ほぼいつも5時間枠が少ないほうになる）。枠の比は公式の説明どおりに書く。
    - five_hour_max_pct: 5時間枠の使用%がこれ以上なら新しいセッションの既定にしない
      （アカウントの定義に書けばそれ、無ければ設定の ``five_hour_max_pct``、それも無ければ 80）。
    書かれた値が使えないときは既定へ退き、ログに一言残す（同じ値は1回だけ）。
    """
    conf = settings()
    capacity = _ratio(account, "capacity")
    five_hour_capacity = _ratio(account, "five_hour_capacity")
    unlimited = account.get("five_hour_unlimited")
    if not isinstance(unlimited, bool):
        if unlimited is not None:
            _warn_policy(
                f"{account['name']} の five_hour_unlimited", unlimited, "false"
            )
        unlimited = False
    line = account.get("five_hour_max_pct")
    if not _valid_pct(line):
        if line is not None:
            _warn_policy(f"{account['name']} の five_hour_max_pct", line, "全体の値")
        line = conf["five_hour_max_pct"]
    if not _valid_pct(line):
        if line is not None:
            _warn_policy("five_hour_max_pct", line, str(DEFAULT_FIVE_HOUR_MAX_PCT))
        line = DEFAULT_FIVE_HOUR_MAX_PCT
    return {
        "capacity": capacity,
        "five_hour_capacity": five_hour_capacity,
        "five_hour_unlimited": unlimited,
        "five_hour_max_pct": float(line),
    }


def _ratio(account, key):
    """枠の大きさの比（正の数）。使えない値は 1 へ退き、ログに一言残す。"""
    raw = account.get(key)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return float(raw)
    if raw is not None:
        _warn_policy(f"{account['name']} の {key}", raw, "1")
    return 1.0


_warned_policy = set()


def _warn_policy(where, value, fallback):
    key = (where, repr(value))
    if key in _warned_policy:
        return
    _warned_policy.add(key)
    print(
        f"[Accounts] 設定の {where} が使えない値です（{value!r}）。{fallback} として扱います",
        flush=True,
    )


def _valid_pct(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 < value <= 100
    )


def _fresh(info):
    return bool(info and info.get("known") and not info.get("stale"))


def round_half_up(value):
    """画面の JS（Math.round）とそろえた四捨五入（round は偶数丸めで 84.5 が 84 になり、画面の 85 と割れる）。"""
    return int(math.floor(value + 0.5))


def over_detail(account, usages):
    """新しいセッションの既定から外す理由と、どの枠か（外さないなら None）。

    Returns:
        {"window": "five_hour" | "weekly", "text": "{label}: …"} か None
    """
    five = (usages or {}).get("five_hour")
    weekly = (usages or {}).get("weekly")
    line = policy(account)["five_hour_max_pct"]
    if _fresh(five) and five["used_percentage"] >= line:
        back = format_reset(five.get("resets_at"))
        return {
            "window": "five_hour",
            "text": f"{account['label']}: 5時間枠 {round_half_up(five['used_percentage'])}% 使用で、"
            f"目安 {round_half_up(line)}% に達しています"
            + (f"（{back} に回復）" if back else ""),
        }
    if _fresh(weekly) and weekly["used_percentage"] >= 100:
        back = format_reset(weekly.get("resets_at"))
        return {
            "window": "weekly",
            "text": f"{account['label']}: 週の枠を使い切っています"
            + (f"（{back} に回復）" if back else ""),
        }
    return None


def over_line(account, usages):
    """新しいセッションの既定から外す理由の文（外さないなら None）。usages はそのアカウントの usage の結果。"""
    detail = over_detail(account, usages)
    return detail["text"] if detail else None


def format_reset(value):
    """回復の時刻（ISO 文字列か epoch 秒）を「10/28 05:59」の形に（読めなければ空）。

    画面に出す時刻の書式はこの1通り（画面側では作らない）。
    """
    if value in (None, "") or isinstance(value, bool):
        return ""
    try:
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, timezone(timedelta(hours=9)))
        else:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                moment = moment.astimezone(timezone(timedelta(hours=9)))
    except (ValueError, OSError, OverflowError):
        return ""
    return moment.strftime("%m/%d %H:%M")


def headroom(account, usages, windows=("weekly", "five_hour")):
    """残りの量（枠ごとの大きさを掛けた値。windows の枠のうち少ないほう）。どれかが分からなければ None。

    5時間枠の無いアカウント（five_hour_unlimited）は週の枠だけで数える。
    """
    rule = policy(account)
    sizes = {"weekly": rule["capacity"], "five_hour": rule["five_hour_capacity"]}
    if rule["five_hour_unlimited"]:
        windows = tuple(w for w in windows if w != "five_hour")
    parts = [(window, (usages or {}).get(window)) for window in windows]
    if not parts or not all(_fresh(part) for _, part in parts):
        return None
    return min((100 - part["used_percentage"]) * sizes[w] for w, part in parts)


def _missing_text(account, info, window):
    state = (
        "古い値" if info and info.get("known") and info.get("stale") else "分からない"
    )
    # 括弧に入れず文で書く（理由の括弧と入れ子にしない・2026-10-28 評価役の指摘）
    return f"{account['label']}の{WINDOW_LABELS[window]}が{state}"


def _because(texts):
    """_missing_text の並びを「…が分からない・…が古い値なので」の形に。"""
    tail = "なので" if texts[-1].endswith("古い値") else "ので"
    return "・".join(texts) + tail


def default_choice(usages):
    """新規作成の既定のアカウント。どの子をどちらに置くかは割り当ての正本で決め、ここは新しく立てるときの既定だけ。

    1. 5時間枠が目安（five_hour_max_pct）以上・週の枠を使い切ったアカウントは外す。
    2. 残りを「(100 - 使用%) × 枠の大きさ」で週の枠・5時間枠の少ないほうとして比べ、多いほう。
       枠の大きさは週（capacity）と5時間（five_hour_capacity）で別に持つ（2026-10-30 管理役の訂正）。
       枠の小さいアカウント（Pro 等）は同じ作業で何倍もの%を食うので、%の比べにしない（2026-10-28 ユーザーの指示）。
       5時間枠の無いアカウント（five_hour_unlimited）は週の枠だけで数える。
    3. 5時間枠が分からない・古いアカウントがあれば、本家に固定せず週の枠だけで比べる（2026-10-28 管理役の決め）。
       5時間枠の無いアカウントの値が無いのは、ここでの「分からない」に入れない。
       古い値を目安超えと数えないのは 1. のとおり。
    4. 週の枠が分からない・古いアカウントがあれば比べず本家（本家が外れていなければ）。

    Args:
        usages: {name: usage の結果（{"weekly": …, "five_hour": …}）}

    Returns:
        (name, reason)
    """
    accounts = [a for a in definitions() if not a["error"]]
    main = main_account()
    if len(accounts) < 2:
        return main["name"], "選べるアカウントが1つだけです"
    blocked = {}
    for a in accounts:
        reason = over_line(a, usages.get(a["name"]))
        if reason:
            blocked[a["name"]] = reason
    open_accounts = [a for a in accounts if a["name"] not in blocked]
    if not open_accounts:
        return (
            main["name"],
            f"どのアカウントも既定から外れるので{main['label']}にしています。"
            f"{'／'.join(blocked.values())}",
        )
    note = f"。外したもの: {'／'.join(blocked.values())}" if blocked else ""
    try:
        preferred = unassigned_account()
    except AccountError:
        preferred = main
    if not preferred["main"] and preferred["name"] in {
        a["name"] for a in open_accounts
    }:
        # 割り当ての無い作業場の既定（unassigned_account）を書いたら、外れていない限りそれを使う
        return (
            preferred["name"],
            f"割り当ての無い作業場の既定（unassigned_account）なので{preferred['label']}にしています{note}",
        )
    if len(open_accounts) == 1:
        only = open_accounts[0]
        return (
            only["name"],
            f"ほかのアカウントが既定から外れるので{only['label']}にしています{note}",
        )
    weekly_missing = []
    five_missing = []
    for a in open_accounts:
        u = usages.get(a["name"]) or {}
        if not _fresh(u.get("weekly")):
            weekly_missing.append(_missing_text(a, u.get("weekly"), "weekly"))
        if not _fresh(u.get("five_hour")) and not policy(a)["five_hour_unlimited"]:
            five_missing.append(_missing_text(a, u.get("five_hour"), "five_hour"))
    if weekly_missing:
        fallback = main if main["name"] not in blocked else open_accounts[0]
        return (
            fallback["name"],
            f"{_because(weekly_missing)}比べられず、"
            f"{fallback['label']}にしています{note}",
        )
    windows = ("weekly",) if five_missing else ("weekly", "five_hour")
    rooms = {
        a["name"]: headroom(a, usages.get(a["name"]), windows) for a in open_accounts
    }
    best = max(open_accounts, key=lambda a: (rooms[a["name"]], a["main"]))
    if five_missing:
        return (
            best["name"],
            f"残りが多いほう（{_because(five_missing)}、週の枠だけで比べた）{note}",
        )
    return best["name"], f"残りが多いほう（枠の大きさを入れて比べた）{note}"


# --- 枠で止まったセッション ---------------------------------------------------

# 会話記録の末尾を読む長さ（バイト）
QUOTA_TAIL_BYTES = 256 * 1024
_quota_cache = {}


def quota_stop(path, now=None):
    """会話記録の最後の応答が枠切れ（error: rate_limit）なら、どの枠で・いつ戻るか。違えば None。

    claude は枠を使い切ると、応答の代わりに ``"error": "rate_limit"`` と ``quotaLimits``
    （rateLimitType: five_hour / seven_day・resetsAt: epoch 秒）を持つ行を書く（2026-10 の会話記録で実測）。
    その後に人の入力があれば止まっていない扱い。
    戻りの時刻を過ぎても、人の次の入力（user 行）があるまでは返し続け、``recovered: True`` を付ける
    （claude は時刻が来ても自分では再開しないので、「続けて」を送るべき時に印が消えると止まった行が
    ただの待機に見えるため・2026-10-28 評価役の指摘）。
    """
    now = time.time() if now is None else now
    try:
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    key = (stat.st_size, stat.st_mtime)
    cached = _quota_cache.get(path)
    if cached and cached[0] == key:
        found = cached[1]
    else:
        found = _scan_quota(path, stat.st_size)
        if len(_quota_cache) > 500:
            _quota_cache.clear()
        _quota_cache[path] = (key, found)
    if not found:
        return None
    resets = found.get("resets_at_epoch")
    # 写しを返す（キャッシュの辞書を呼び手に書き換えさせない）
    return dict(found, recovered=isinstance(resets, (int, float)) and resets <= now)


def _scan_quota(path, size):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - QUOTA_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        kind = record.get("type")
        # サブエージェント・裏の仕事の完了通知（origin.kind=task-notification）は人の入力ではない。
        # 枠切れの直後に何行も載る（2026-10-31 example-blog の会話記録で実測）ので、飛ばしてさかのぼる
        origin = record.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "task-notification":
            continue
        if kind == "user" and not record.get("isMeta"):
            return None
        if kind != "assistant":
            continue
        if record.get("error") != "rate_limit":
            return None
        limits = record.get("quotaLimits") or {}
        window = {"seven_day": "weekly", "five_hour": "five_hour"}.get(
            limits.get("rateLimitType"), limits.get("rateLimitType") or "unknown"
        )
        resets = limits.get("resetsAt")
        return {
            "window": window,
            "window_label": WINDOW_LABELS.get(window, "利用の上限"),
            "resets_at_epoch": resets if isinstance(resets, (int, float)) else None,
            "resets_at": format_reset(resets),
            "at": record.get("timestamp"),
        }
    return None

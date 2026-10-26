# -*- coding: utf-8 -*-
"""claude セッションを本アプリから「切り離して」起動・監視・停止するモジュール。

本アプリはセッションの親ではなく、**管理役**に徹する。

    - 起動: 使い捨てのランチャーを挟んだ**二段起動**で `claude.exe` を起こし、
      ランチャーが即終了することで本アプリとの親子の鎖を切る。
      独立したコンソール（CREATE_NEW_CONSOLE）を与えるだけでは足りない。
      常駐の管理アプリの停止処理は `taskkill /F /T`（プロセスツリー全体）で、
      ParentProcessId の鎖を辿って子孫を皆殺しにするため、鎖が繋がったままだと
      **アプリの再起動で全セッションが道連れになる**（2026-08-21 に稼働中
      セッション5本を失った実障害。ダミープロセスで「直接起動は死ぬ・
      二段起動は生き残る」の両方を実測して本方式にした）。
    - 監視: claude 自身が書いている `~/.claude/sessions/<pid>.json` を読む。
      アプリ側に状態を持たないので、アプリを再起動しても一覧は失われない。
    - 停止: pid を指定して終了させる（本当に claude かを確認してから）。

対話は claude.ai（Remote Control）側で行う想定。最初の指示は起動時の引数として渡す。
起動後に送れるのは、本アプリが起こした子への「/clear」と「本文」だけ
（send_to_session。文脈を空にして次の指示を渡す用途。任意のキー操作は送らない）。
"""

import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from xml.sax.saxutils import escape as xml_escape

import psutil

import safe_console

# --- 定数定義 ---------------------------------------------------------------

CLAUDE_HOME = os.path.join(os.path.expanduser("~"), ".claude")

# claude が起動中セッションの状態を書き出すフォルダ（ファイル名は pid.json）
SESSIONS_DIR = os.path.join(CLAUDE_HOME, "sessions")

# claude 本体の設定ファイル。フォルダの信頼状態もここに記録される。
CLAUDE_CONFIG = os.path.join(os.path.expanduser("~"), ".claude.json")

# 設定ファイルを書き換える前に取っておく控え（上書き事故からの復旧用）
CLAUDE_CONFIG_BACKUP = CLAUDE_CONFIG + ".claude-web-app.bak"

# claude.ai のセッション画面のURL。bridgeSessionId を後ろに繋ぐ。
CLAUDE_WEB_BASE = "https://claude.ai/code/"

# プロセス生成フラグ（Windows）
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# ランチャーが起動報告を返すまでの待ち時間（秒）。
# 実測では1秒未満だが、ディスクが重いときの余裕を持たせる。
LAUNCHER_TIMEOUT_SEC = 30

# pid の再利用による誤認を防ぐため、記録された開始時刻とのズレの許容幅（秒）
START_TIME_TOLERANCE_SEC = 120

# 起動時に選べるモデル（値, 画面の表示名）。値は `claude --model` にそのまま渡す。
# 空文字は「指定しない」＝ ~/.claude/settings.json の model に従う。
# 別名（opus 等）はその時点の最新版を指すので、モデルの世代交代に追随しなくてよい。
MODEL_CHOICES = [
    ("", "既定（settings.json に従う）"),
    ("opus", "Opus"),
    ("opus[1m]", "Opus（1M コンテキスト）"),
    ("sonnet", "Sonnet"),
    ("sonnet[1m]", "Sonnet（1M コンテキスト）"),
    ("fable", "Fable"),
    ("haiku", "Haiku"),
]

# 起動時に選べる権限モード。値は `claude --permission-mode` の選択肢そのもの
# （`claude --help` で確認: acceptEdits / auto / bypassPermissions / manual / dontAsk / plan）。
PERMISSION_MODE_CHOICES = [
    ("", "既定（settings.json に従う）"),
    ("auto", "auto（分類器が判定）"),
    ("acceptEdits", "acceptEdits（編集は自動承認）"),
    ("plan", "plan（計画のみ・変更しない）"),
    ("manual", "manual（毎回確認）"),
    ("dontAsk", "dontAsk（確認が要る操作は拒否）"),
    ("bypassPermissions", "bypassPermissions（全確認を省略・危険）"),
]

# 起動時に選べる思考の深さ（`claude --effort`）
EFFORT_CHOICES = [
    ("", "既定"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "xhigh"),
    ("max", "max"),
]


# 起動先の Windows セッション。
#   0: サービス用のセッション（画面なし）。常駐の管理アプリ配下の本アプリはここで動く。
#   1: ログオン中のデスクトップ。Chrome 連携・クリップボード・GUI 操作が要る作業向け。
# 本アプリと違うセッションへは、使い捨てのタスク経由で起動する（_spawn_via_task）。
# 番号だけではどちらがバックグラウンドか分からないため、表示は役割名を先に出す。
WIN_SESSION_CHOICES = [
    ("", "既定（このアプリと同じ）"),
    ("0", "バックグラウンド（セッション0・画面なし）"),
    ("1", "デスクトップ（セッション1・Chrome 連携可）"),
]

# 起動する claude の権限（Windows の昇格）。値は画面とのやり取り用の名前で、
# タスクの RunLevel へ写す（ELEVATION_RUN_LEVELS）。空文字は従来どおり本アプリと同じ権限。
# 2026-10-11 実測: 本アプリ（常駐の管理アプリ配下・セッション0）は通常権限からは開けない
# （昇格している見込み）。デスクトップ(S1)へタスク経由で起こした子は通常権限（Limited）。
ELEVATION_CHOICES = [
    ("", "既定（起動先の決まりどおり）"),
    ("admin", "管理者"),
    ("standard", "通常"),
]
ELEVATION_RUN_LEVELS = {"admin": "HighestAvailable", "standard": "LeastPrivilege"}

# タスク経由で起動するときの要求・応答ファイルの置き場。
# ★AppData 配下に置かないこと。本アプリの venv は Microsoft Store 版 Python が元で、
#   AppData への書き込みはパッケージ専用フォルダへ仮想化され、schtasks や
#   起動役（別プロセス）からは「パスが見つかりません」になる（2026-09-24 実測）。
TASK_LAUNCH_DIR = os.path.join(
    os.path.expanduser("~"), ".claude-web-app", "task-launch"
)

# タスク経由の起動を待つ時間（秒）。実測では 0.3〜0.5 秒。
TASK_LAUNCH_TIMEOUT_SEC = 30

# 起動役のスクリプト（タスクから呼ばれる）
TASK_LAUNCHER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "session_task_launcher.py"
)


def launch_option_choices():
    """画面に出す起動オプションの選択肢を返す（テンプレートへ渡す用）。"""
    to_list = lambda choices: [{"value": v, "label": l} for v, l in choices]
    return {
        "model": to_list(MODEL_CHOICES),
        "permission_mode": to_list(PERMISSION_MODE_CHOICES),
        "effort": to_list(EFFORT_CHOICES),
        "win_session": to_list(WIN_SESSION_CHOICES),
        "elevation": to_list(ELEVATION_CHOICES),
        "current_win_session": process_win_session(os.getpid()),
        "current_elevated": process_elevation(os.getpid()),
    }


def build_launch_args(model=None, permission_mode=None, effort=None):
    """起動オプションを検証して claude のコマンドライン引数へ変換する。

    画面から届いた値をそのまま argv に積まないよう、定義済みの選択肢に
    無い値は拒否する（`--` で始まる値を渡されて別の引数として解釈される等を防ぐ）。

    Args:
        model: モデル名（MODEL_CHOICES の値。空・None なら指定しない）
        permission_mode: 権限モード（PERMISSION_MODE_CHOICES の値）
        effort: 思考の深さ（EFFORT_CHOICES の値）

    Returns:
        list[str]: 追加する引数

    Raises:
        SessionError: 選択肢に無い値が指定された場合
    """
    args = []
    for flag, value, choices, label in (
        ("--model", model, MODEL_CHOICES, "モデル"),
        ("--permission-mode", permission_mode, PERMISSION_MODE_CHOICES, "モード"),
        ("--effort", effort, EFFORT_CHOICES, "思考の深さ"),
    ):
        if not value:
            continue
        if value not in {v for v, _ in choices}:
            raise SessionError(f"{label}の指定が不正です: {value}")
        args += [flag, value]
    return args


def launch_options_of(pid, dropped=None):
    """動いている claude のコマンドラインから、引き継げる起動オプションを読む。

    起こし直し（--resume）で元と同じモデル・モード・思考の深さにするため。
    選択肢に無い値（フルのモデル名など）は引き継がない（既定に任せる）。
    読めなければ空の dict。途中で /model で変えたモデルは引き継げない。

    Args:
        dropped: リストを渡すと、引き継がない引数（--add-dir など）をそこへ足す

    Returns:
        dict: model / permission_mode / effort のうち読めたもの
    """
    try:
        argv = psutil.Process(int(pid)).cmdline()
    except (psutil.Error, TypeError, ValueError):
        if dropped is not None:
            dropped.append("（コマンドラインを読めなかった）")
        return {}
    found = {}
    for flag, key, choices in (
        ("--model", "model", MODEL_CHOICES),
        ("--permission-mode", "permission_mode", PERMISSION_MODE_CHOICES),
        ("--effort", "effort", EFFORT_CHOICES),
    ):
        for i, arg in enumerate(argv):
            if arg == "--":
                break
            value = None
            if arg == flag and i + 1 < len(argv):
                value = argv[i + 1]
            elif arg.startswith(flag + "="):
                value = arg.split("=", 1)[1]
            if value in {v for v, _ in choices}:
                found[key] = value
    head = argv[: argv.index("--")] if "--" in argv else argv
    if "permission_mode" not in found and "--dangerously-skip-permissions" in head:
        found["permission_mode"] = "bypassPermissions"
    if dropped is not None:
        carried = (
            "--model",
            "--permission-mode",
            "--effort",
            "--dangerously-skip-permissions",
            "-n",
            "--name",
            "--resume",
            "-r",
        )
        for arg in head[1:]:
            if arg.startswith("-") and arg.split("=", 1)[0] not in carried:
                dropped.append(arg.split("=", 1)[0])
    return found


class SessionError(Exception):
    """セッションの起動・停止に失敗したことを表す例外。"""


class SessionNotManagedError(SessionError):
    """本アプリが起こした子ではないため、操作を断ったことを表す例外。"""


class SessionBusyError(SessionError):
    """相手が作業中（入力待ちでない）のため、送らなかったことを表す例外。"""


class SessionDraftError(SessionError):
    """台帳に無い相手の入力欄に文字があるため、消さずに送らなかったことを表す例外。

    人がターミナルで書きかけている文を Ctrl+U で消さないための断り。
    """


# --- 環境変数 ---------------------------------------------------------------


def sanitize_env(env=None):
    """Claude Code 由来のマーカーを取り除いた環境変数を作る。

    Claude Code の中から（あるいは Claude Code に起動されたプロセスから）
    `claude` を起こすと、`CLAUDE_CODE_CHILD_SESSION` などを引き継いで
    「子セッション」と判定され、**会話の記録も claude.ai へのブリッジも
    無効化される**（実測: `Transcript saving is off` の警告が出る）。
    切り離して起動する以上、これらは必ず落とす。

    Args:
        env: 元にする環境変数。省略時は現在の環境

    Returns:
        マーカーを除いた環境変数の辞書
    """
    source = os.environ if env is None else env
    return {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("CLAUDE")
    }


# --- フォルダの信頼登録 -----------------------------------------------------


def _load_claude_config():
    """claude 本体の設定ファイルを読み込む。無ければ空の辞書を返す。"""
    if not os.path.exists(CLAUDE_CONFIG):
        return {}
    with open(CLAUDE_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)


def config_key_for(path):
    """設定ファイル内でフォルダを指すキー表記へ変換する。

    claude はスラッシュ区切りの絶対パス（例: ``C:/work/develop/...``）を
    キーにしている。Windows の区切り文字のまま書くと別物として扱われる。
    """
    return os.path.abspath(path).replace("\\", "/")


def is_trusted(path, config=None):
    """フォルダが claude に信頼済みとして登録されているかを返す。"""
    config = _load_claude_config() if config is None else config
    entry = (config.get("projects") or {}).get(config_key_for(path))
    return bool(entry and entry.get("hasTrustDialogAccepted"))


def ensure_trusted(path):
    """フォルダを信頼済みとして登録する（未登録のときだけ書き込む）。

    切り離して起動したセッションは画面が見えないため、初回起動時に出る
    「このフォルダを信頼しますか？」の確認に誰も答えられず、そのまま
    止まってしまう。本アプリから起動するフォルダは利用者自身のものなので、
    起動前にここで信頼済みにしておく。

    設定ファイルは claude 自身も書き換えるため、
    **控えを取ってから一時ファイル経由で置き換える**（書き込みを不可分にする）。

    Args:
        path: 対象フォルダの絶対パス

    Returns:
        True なら今回書き込んだ（False は既に信頼済みで何もしなかった）

    Raises:
        SessionError: 設定ファイルの読み書きに失敗した場合
    """
    key = config_key_for(path)
    try:
        config = _load_claude_config()
    except (OSError, ValueError) as e:
        raise SessionError(f"claude の設定ファイルを読めませんでした: {e}")

    projects = config.setdefault("projects", {})
    entry = projects.get(key)
    if entry and entry.get("hasTrustDialogAccepted"):
        return False

    if entry is None:
        entry = {}
        projects[key] = entry
    entry["hasTrustDialogAccepted"] = True

    try:
        # 既存の内容の控えを残してから書き換える
        if os.path.exists(CLAUDE_CONFIG):
            shutil.copyfile(CLAUDE_CONFIG, CLAUDE_CONFIG_BACKUP)

        temp_path = CLAUDE_CONFIG + ".tmp-claude-web-app"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(temp_path, CLAUDE_CONFIG)
    except OSError as e:
        raise SessionError(f"claude の設定ファイルを更新できませんでした: {e}")

    return True


# --- 起動 -------------------------------------------------------------------


def resolve_claude_exe(configured=None):
    """claude 本体の実行ファイルの絶対パスを解決する。

    Args:
        configured: 設定で明示された実行ファイルのパス（任意）

    Returns:
        実行ファイルの絶対パス

    Raises:
        SessionError: 見つからない場合
    """
    if configured:
        if os.path.isfile(configured):
            return configured
        raise SessionError(f"claude の実行ファイルが見つかりません: {configured}")

    found = shutil.which("claude.exe") or shutil.which("claude")
    if not found:
        raise SessionError(
            "claude の実行ファイルが見つかりません。"
            "PATH を確認するか、config/settings.json の claude_exe に絶対パスを設定してください。"
        )
    return found


# ランチャー本体。stdin から JSON（argv / cwd / env）を受け取って対象を起動し、
# pid を1行出力して**即座に終了する**。このプロセスが終了した瞬間、
# 対象プロセスの ParentProcessId は死んだ pid を指すようになり、
# `taskkill /F /T` のツリー探索が本アプリ側から到達できなくなる。
_LAUNCHER_CODE = (
    "import json,subprocess,sys\n"
    "req=json.load(sys.stdin)\n"
    "si=subprocess.STARTUPINFO()\n"
    "si.dwFlags|=subprocess.STARTF_USESHOWWINDOW\n"
    "si.wShowWindow=subprocess.SW_HIDE\n"
    "p=subprocess.Popen(req['argv'],cwd=req['cwd'],env=req['env'],"
    "creationflags=0x210,"  # CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
    "startupinfo=si,close_fds=True)\n"
    "print(p.pid,flush=True)\n"
)


def _spawn_detached(argv, cwd, env):
    """本アプリのプロセスツリーから切り離して argv を起動し、pid を返す。

    直接 Popen すると起動したプロセスは本アプリの子孫になり、
    常駐の管理アプリの停止処理（`taskkill /F /T` ＝ プロセスツリー全体）に
    巻き込まれて死ぬ（2026-08-21 の実障害）。使い捨てのランチャーを挟み、
    ランチャーが起動報告と同時に終了することで親子の鎖を切る。

    Args:
        argv: 起動するコマンドライン（リスト）
        cwd: 作業ディレクトリ
        env: 環境変数の辞書（呼び出し側で sanitize_env 済みであること）

    Returns:
        int: 起動したプロセスの pid

    Raises:
        SessionError: ランチャーの失敗・応答なし・pid が読めない場合
    """
    request = json.dumps({"argv": argv, "cwd": cwd, "env": env})
    try:
        launcher = subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER_CODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # ランチャーの stderr には OS 由来の cp932 メッセージが混ざりうる。
            # decode 失敗で SessionError 以外の例外を漏らさないよう置換する。
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
        out, err = launcher.communicate(request, timeout=LAUNCHER_TIMEOUT_SEC)
    except OSError as e:
        raise SessionError(f"ランチャーの起動に失敗しました: {e}")
    except subprocess.TimeoutExpired:
        launcher.kill()
        raise SessionError(
            f"ランチャーが {LAUNCHER_TIMEOUT_SEC} 秒以内に応答しませんでした"
        )
    if launcher.returncode != 0:
        raise SessionError(
            f"ランチャーが異常終了しました (code={launcher.returncode}): "
            f"{(err or '').strip()[:500]}"
        )
    try:
        return int(out.strip())
    except ValueError:
        raise SessionError(f"ランチャーの応答が読めません: {out.strip()[:200]}")


# --- Windows セッション -----------------------------------------------------


def process_win_session(pid):
    """プロセスが属する Windows セッション番号を返す（取れなければ None）。"""
    sid = wt.DWORD()
    try:
        if ctypes.windll.kernel32.ProcessIdToSessionId(int(pid), ctypes.byref(sid)):
            return sid.value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def process_elevation(pid):
    """プロセスが管理者の権限を持って動いているかを返す（True/False。読めなければ None）。

    トークンのグループに Administrators（S-1-5-32-544）が**有効**な形で入っているかで見る。
    TokenElevation（昇格の印）は、SAFER で絞ったトークンでも True のまま残るため使わない
    （評価役の指摘。gsudo の作者の回答 so60454914）。UAC で絞られたトークンと SAFER の
    「通常の利用者」トークンでは、Administrators は拒否専用（deny-only）になり、ここでは False。
    通常権限のプロセスから昇格したプロセスは開けないことがある（2026-10-11 実測:
    アクセス拒否）ため、その場合は None。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.restype = wt.HANDLE
    process_query_limited_information = 0x1000
    token_query = 0x0008
    token_groups = 2
    se_group_enabled = 0x4
    se_group_use_for_deny_only = 0x10
    try:
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, int(pid)
        )
    except (TypeError, ValueError, OverflowError, ctypes.ArgumentError):
        return None
    if not handle:
        return None
    token = wt.HANDLE()
    admin_sid = ctypes.c_void_p()
    try:
        if not advapi32.OpenProcessToken(handle, token_query, ctypes.byref(token)):
            return None
        needed = wt.DWORD()
        advapi32.GetTokenInformation(token, token_groups, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, token_groups, buffer, needed, ctypes.byref(needed)
        ):
            return None
        if not advapi32.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(admin_sid)):
            return None
        # TOKEN_GROUPS: DWORD GroupCount のあとに SID_AND_ATTRIBUTES の配列（ポインタの境界に揃う）
        count = wt.DWORD.from_buffer(buffer).value
        offset = ctypes.sizeof(ctypes.c_void_p)
        groups = (_SidAndAttributes * count).from_buffer(buffer, offset)
        for group in groups:
            if group.Sid and advapi32.EqualSid(ctypes.c_void_p(group.Sid), admin_sid):
                return bool(group.Attributes & se_group_enabled) and not (
                    group.Attributes & se_group_use_for_deny_only
                )
        return False
    finally:
        if admin_sid:
            kernel32.LocalFree(admin_sid)
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def _current_user_name():
    """`コンピューター名\\ユーザー名` 形式の実行ユーザー名を返す（S4U タスクの指定用）。"""
    size = wt.ULONG(256)
    buffer = ctypes.create_unicode_buffer(size.value)
    # NameSamCompatible = 2
    if ctypes.windll.secur32.GetUserNameExW(2, buffer, ctypes.byref(size)):
        return buffer.value
    return f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"


def _run_schtasks(args):
    """schtasks.exe を実行し、失敗したら SessionError を投げる。"""
    try:
        completed = subprocess.run(
            ["schtasks"] + args,
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SessionError(f"タスクスケジューラを呼び出せませんでした: {e}")
    if completed.returncode != 0:
        # schtasks のメッセージは OS の既定コードページ（cp932）で出る
        detail = (completed.stderr or completed.stdout).decode("cp932", "replace")
        raise SessionError(f"タスクスケジューラの操作に失敗しました: {detail.strip()}")


def _build_task_xml(target, command, arguments, run_level="LeastPrivilege"):
    """起動用の使い捨てタスクの定義（XML）を作る。

    トリガーは持たせず、`schtasks /run` で1回だけ走らせる。
      - target "1": 対話トークン。ログオン中の利用者のデスクトップで動く
        （パスワード不要。ログオンしていなければ起動できない）。
      - target "0": S4U。画面を持たないセッション0で動く（パスワード不要）。
    run_level は "LeastPrivilege"（通常権限）か "HighestAvailable"（管理者。
    利用者が管理者なら昇格して動く。登録には本アプリ自身の昇格が要る）。
    """
    if run_level not in ELEVATION_RUN_LEVELS.values():
        raise SessionError(f"権限の指定が不正です: {run_level}")
    if target == "1":
        principal = "<LogonType>InteractiveToken</LogonType>"
    else:
        principal = (
            f"<UserId>{xml_escape(_current_user_name())}</UserId>"
            "<LogonType>S4U</LogonType>"
        )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>claude-web-app がセッション起動に使う使い捨てタスク（起動後に自動削除）</Description></RegistrationInfo>
  <Principals><Principal id="Author">{principal}<RunLevel>{run_level}</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>Parallel</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{xml_escape(command)}</Command><Arguments>{xml_escape(arguments)}</Arguments></Exec>
  </Actions>
</Task>
"""


def _spawn_via_task(argv, cwd, target, run_level="LeastPrivilege"):
    """使い捨てのタスク経由で、指定の Windows セッションに argv を起動する。

    セッション0の本アプリからセッション1へプロセスを作るには、本来 SYSTEM
    権限（WTSQueryUserToken）が要る。タスクスケジューラに「対話トークン」の
    タスクを登録して走らせれば、同じ利用者の権限のままデスクトップ側で起動できる。
    起動役（session_task_launcher.py）が起動報告を書いて即終了するので、
    起動した claude はどのプロセスツリーにも属さない。

    Args:
        argv: 起動するコマンドライン（リスト）
        cwd: 作業ディレクトリ
        target: 起動先セッション（"0" または "1"）
        run_level: タスクの RunLevel（"LeastPrivilege" / "HighestAvailable"）

    Returns:
        int: 起動したプロセスの pid

    Raises:
        SessionError: タスクの登録・実行に失敗した、または時間内に応答が無い場合
    """
    os.makedirs(TASK_LAUNCH_DIR, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    task_name = f"claude-web-app-launch-{token}"
    request_path = os.path.join(TASK_LAUNCH_DIR, f"{token}.request.json")
    response_path = os.path.join(TASK_LAUNCH_DIR, f"{token}.response.json")
    xml_path = os.path.join(TASK_LAUNCH_DIR, f"{token}.task.xml")

    # 窓を出さないよう pythonw で起動役を走らせる
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable

    registered = False
    try:
        with open(request_path, "w", encoding="utf-8") as f:
            json.dump(
                {"argv": argv, "cwd": cwd, "response": response_path},
                f,
                ensure_ascii=False,
            )
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(
                _build_task_xml(
                    target,
                    pythonw,
                    f'"{TASK_LAUNCHER_SCRIPT}" "{request_path}"',
                    run_level,
                )
            )
        _run_schtasks(["/create", "/tn", task_name, "/xml", xml_path, "/f"])
        registered = True
        _run_schtasks(["/run", "/tn", task_name])

        deadline = time.time() + TASK_LAUNCH_TIMEOUT_SEC
        while time.time() < deadline:
            if os.path.exists(response_path):
                with open(response_path, "r", encoding="utf-8") as f:
                    response = json.load(f)
                break
            time.sleep(0.2)
        else:
            hint = (
                "（デスクトップにログオンしていない可能性があります）"
                if target == "1"
                else ""
            )
            raise SessionError(
                f"セッション{target}での起動が {TASK_LAUNCH_TIMEOUT_SEC} 秒以内に"
                f"完了しませんでした{hint}"
            )
    except OSError as e:
        raise SessionError(f"タスク経由の起動の準備に失敗しました: {e}")
    finally:
        if registered:
            try:
                _run_schtasks(["/delete", "/tn", task_name, "/f"])
            except SessionError as e:
                safe_console.say(f"[Session] 使い捨てタスクを削除できませんでした: {e}")
        for path in (request_path, response_path, xml_path):
            try:
                os.remove(path)
            except OSError:
                pass

    if response.get("error"):
        raise SessionError(
            f"セッション{target}での起動に失敗しました: {response['error']}"
        )
    actual = response.get("win_session")
    if (target == "0") != (actual == 0):
        raise SessionError(
            f"セッション{target}を指定しましたが、セッション{actual}で起動されました"
        )
    return int(response["pid"])


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD),
        ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD),
        ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD),
        ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD),
        ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]


def _last_error_text():
    """直前の Windows API の失敗を「error=番号: 意味」の形にする（番号だけでは原因が読めない）。"""
    code = ctypes.get_last_error()
    return f"error={code}: {ctypes.FormatError(code).strip()}"


def _spawn_restricted(argv, cwd):
    """本アプリのトークンから「通常の利用者」相当のトークンを作り、起動役を起こして pid を返す。

    セッション0の S4U タスクは LeastPrivilege を指定しても昇格したまま動く
    （2026-10-11 本番で実測: 「通常」を選んでも elevated=True）。そこで、
    `runas /trustlevel:0x20000` と同じ SAFER の「通常の利用者」レベルでトークンを作り、
    整合性レベルを Medium に下げて起動役（session_task_launcher.py）を起こす。
    同じログオンのままなので、S4U と違い保存した資格情報も使える。
    起動役は claude を起こして pid を書いたら終わるので、親子の鎖は切れる。

    Raises:
        SessionError: トークンの作成・起動に失敗した、または時間内に応答が無い場合
    """
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    safer_scopeid_user = 2
    safer_levelid_normaluser = 0x20000
    safer_level_open = 1
    token_integrity_level = 25
    se_group_integrity = 0x20
    create_no_window = 0x08000000

    os.makedirs(TASK_LAUNCH_DIR, exist_ok=True)
    token_name = uuid.uuid4().hex[:12]
    request_path = os.path.join(TASK_LAUNCH_DIR, f"{token_name}.request.json")
    response_path = os.path.join(TASK_LAUNCH_DIR, f"{token_name}.response.json")
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable

    level = wt.HANDLE()
    token = wt.HANDLE()
    sid = ctypes.c_void_p()
    try:
        with open(request_path, "w", encoding="utf-8") as f:
            json.dump(
                {"argv": argv, "cwd": cwd, "response": response_path},
                f,
                ensure_ascii=False,
            )
        if not advapi32.SaferCreateLevel(
            safer_scopeid_user,
            safer_levelid_normaluser,
            safer_level_open,
            ctypes.byref(level),
            None,
        ):
            raise SessionError(
                f"通常権限のトークンを作れませんでした（SaferCreateLevel {_last_error_text()}）"
            )
        if not advapi32.SaferComputeTokenFromLevel(
            level, None, ctypes.byref(token), 0, None
        ):
            raise SessionError(
                f"通常権限のトークンを作れませんでした（SaferComputeTokenFromLevel {_last_error_text()}）"
            )
        # 整合性レベルを Medium（S-1-16-8192）に下げる。下げないと High のまま残る
        if not advapi32.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid)):
            raise SessionError(
                f"整合性レベルを用意できませんでした（{_last_error_text()}）"
            )
        label = _SidAndAttributes(sid, se_group_integrity)
        if not advapi32.SetTokenInformation(
            token,
            token_integrity_level,
            ctypes.byref(label),
            ctypes.sizeof(label) + advapi32.GetLengthSid(sid),
        ):
            raise SessionError(
                f"整合性レベルを下げられませんでした（{_last_error_text()}）"
            )

        command_line = ctypes.create_unicode_buffer(
            subprocess.list2cmdline([pythonw, TASK_LAUNCHER_SCRIPT, request_path])
        )
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        info = _ProcessInformation()
        if not advapi32.CreateProcessAsUserW(
            token,
            None,
            command_line,
            None,
            None,
            False,
            create_no_window,
            None,
            os.path.dirname(TASK_LAUNCHER_SCRIPT),
            ctypes.byref(startup),
            ctypes.byref(info),
        ):
            raise SessionError(
                f"通常権限で起動役を起こせませんでした（CreateProcessAsUser {_last_error_text()}）"
            )
        kernel32.CloseHandle(ctypes.c_void_p(info.hThread))
        launcher = ctypes.c_void_p(info.hProcess)

        # 起動役が応答を書かずに終わったら、時間切れを待たずに終了コードを添えて知らせる
        deadline = time.time() + TASK_LAUNCH_TIMEOUT_SEC
        exited_at = None
        try:
            while not os.path.exists(response_path):
                if exited_at is None and kernel32.WaitForSingleObject(launcher, 0) == 0:
                    exited_at = time.time()
                if exited_at is not None and time.time() - exited_at > 1.0:
                    code = wt.DWORD()
                    kernel32.GetExitCodeProcess(launcher, ctypes.byref(code))
                    raise SessionError(
                        f"通常権限の起動役が、応答を書かずに終了しました（終了コード {code.value}）"
                    )
                if time.time() >= deadline:
                    raise SessionError(
                        f"通常権限での起動が {TASK_LAUNCH_TIMEOUT_SEC} 秒以内に完了しませんでした"
                    )
                time.sleep(0.2)
        finally:
            kernel32.CloseHandle(launcher)
        with open(response_path, "r", encoding="utf-8") as f:
            response = json.load(f)
    except (OSError, ValueError) as e:
        raise SessionError(f"通常権限での起動の準備に失敗しました: {e}")
    finally:
        if sid:
            kernel32.LocalFree(sid)
        if token:
            kernel32.CloseHandle(token)
        if level:
            advapi32.SaferCloseLevel(level)
        for path in (request_path, response_path):
            try:
                os.remove(path)
            except OSError:
                pass

    if response.get("error"):
        raise SessionError(f"通常権限での起動に失敗しました: {response['error']}")
    return int(response["pid"])


def launch_session(
    cwd,
    name=None,
    prompt=None,
    claude_exe=None,
    trust=True,
    model=None,
    permission_mode=None,
    effort=None,
    win_session=None,
    elevation=None,
    resume_session_id=None,
):
    """本アプリから切り離した独立プロセスとして claude セッションを起動する。

    ランチャー経由の二段起動で親子の鎖を切るため、本アプリを終了・再起動
    しても（常駐の管理アプリの `taskkill /F /T` でも）セッションは生き続ける。
    起動後の操作は claude.ai 側で行う。

    Args:
        cwd: 作業ディレクトリ（プロジェクトの絶対パス）
        name: セッション表示名（省略時はフォルダ名）
        prompt: 最初の指示。渡すと起動と同時に自動送信される
        claude_exe: claude 実行ファイルのパス（省略時は PATH から解決）
        trust: True ならフォルダを信頼済みとして登録してから起動する
        model: モデル（MODEL_CHOICES の値。省略時は settings.json に従う）
        permission_mode: 権限モード（PERMISSION_MODE_CHOICES の値）
        effort: 思考の深さ（EFFORT_CHOICES の値）
        win_session: 起動先の Windows セッション（"0"/"1"。省略時は本アプリと同じ）
        elevation: 権限（"admin"/"standard"。省略時は起動先の決まりどおり）。
            本アプリと違う権限を指定したときは、同じセッションでも使い捨てのタスク経由で
            起動する（本アプリと同じセッション・同じ権限なら二段起動のまま）
        resume_session_id: 続きから再開する過去の会話の sessionId（`--resume`）。
            そのプロジェクトの会話記録にあり、いま動いていないものに限る

    Returns:
        dict: ``pid``（起動したプロセスID）、``name``、``trusted``（今回登録したか）、
        ``win_session``（実際に起動したセッション番号）、``elevated``（実際に
        昇格しているか。読めなければ None）、``resumed``（再開した sessionId か None）

    Raises:
        SessionError: 起動に失敗した場合
    """
    if not cwd or not os.path.isdir(cwd):
        raise SessionError(f"作業ディレクトリが見つかりません: {cwd}")

    # 不正な指定は、信頼登録などの副作用を起こす前に弾く
    option_args = build_launch_args(model, permission_mode, effort)
    target = str(win_session) if win_session not in (None, "") else ""
    if target and target not in {v for v, _ in WIN_SESSION_CHOICES}:
        raise SessionError(f"起動先セッションの指定が不正です: {win_session}")
    elevation = elevation or ""
    if elevation and elevation not in ELEVATION_RUN_LEVELS:
        raise SessionError(f"権限の指定が不正です: {elevation}")
    own_session = process_win_session(os.getpid())
    if (
        elevation == "standard"
        and target == "0"
        and own_session is not None
        and own_session != 0
    ):
        # 別のセッション0へはタスク（S4U）でしか起こせず、S4U は LeastPrivilege でも
        # 昇格したまま動く（2026-10-11 本番で実測）。通常にならないので起こさずに断る
        raise SessionError(
            "このアプリがデスクトップ側で動いているため、バックグラウンド（セッション0）へは"
            "通常権限で起動できません。起動先をデスクトップにするか、権限を既定にしてください"
        )
    if resume_session_id:
        validate_resume_target(cwd, resume_session_id)

    exe = resolve_claude_exe(claude_exe)
    display_name = name or os.path.basename(os.path.normpath(cwd))

    trusted_now = False
    if trust:
        trusted_now = ensure_trusted(cwd)

    if resume_session_id:
        # 再開では -n を付けない（付けると元の会話の題名を上書きしてしまう）
        argv = [exe, "--resume", resume_session_id] + option_args
    else:
        argv = [exe, "-n", display_name] + option_args
    if prompt:
        # 最初の指示は引数として渡す。TUI へ文字を流し込む必要がない。
        # 指示が `-` で始まっても引数と誤認されないよう `--` で区切る。
        argv += ["--", prompt]

    current = process_win_session(os.getpid())
    same_session = not target or (target == "0") == (current == 0)
    # 本アプリと同じセッションで、本アプリと同じ権限を選んだときは、タスクを通さない
    # （セッション0のタスクは S4U ＝保存した資格情報を持たないログオンになり、
    #  ネットワーク共有や資格情報マネージャーが使えなくなる見込みのため。評価役の指摘）
    own_elevated = process_elevation(os.getpid()) is True
    own_matches = bool(elevation) and own_elevated == (elevation == "admin")
    if elevation == "standard" and same_session and own_elevated:
        # 昇格した本アプリから同じセッションへ通常権限で起こす。タスク（セッション0は S4U）だと
        # LeastPrivilege でも昇格したまま動くため（2026-10-11 本番で実測）、SAFER で絞ったトークンで起こす
        pid = _spawn_restricted(argv, cwd)
    elif elevation and not (same_session and own_matches):
        # 二段起動は本アプリの権限をそのまま受け継ぐため、違う権限はタスクの RunLevel で起こす
        destination = target or ("0" if current == 0 else "1")
        pid = _spawn_via_task(argv, cwd, destination, ELEVATION_RUN_LEVELS[elevation])
    elif not same_session:
        # 本アプリと別のセッションへは、使い捨てのタスク経由で起動する
        pid = _spawn_via_task(argv, cwd, target)
    else:
        # ランチャー経由の二段起動。直接 Popen してはいけない
        # （本アプリの子孫になり、taskkill /F /T の巻き添えで死ぬ）。
        pid = _spawn_detached(argv, cwd, sanitize_env())

    # 文字を送れる相手（send_to_session）を、本アプリが起こした子に限るための台帳
    record_launched(pid, display_name, cwd)

    return {
        "pid": pid,
        "name": display_name,
        "trusted": trusted_now,
        "win_session": process_win_session(pid),
        "elevated": process_elevation(pid),
        "resumed": resume_session_id or None,
    }


# --- 一覧・監視 -------------------------------------------------------------


def _read_session_file(path):
    """セッション状態ファイルを読み込む。壊れていれば None を返す。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_session_alive(session):
    """セッションのプロセスが生きているかを、pid の再利用も考慮して判定する。

    pid は使い回されるため、「pid が存在する」だけでは足りない。
    プロセス名と開始時刻の一致まで確認する。
    """
    pid = session.get("pid")
    if not pid:
        return False
    try:
        proc = psutil.Process(int(pid))
        if "claude" not in (proc.name() or "").lower():
            return False
        started_at = session.get("startedAt")
        if started_at:
            if abs(proc.create_time() - float(started_at) / 1000.0) > (
                START_TIME_TOLERANCE_SEC
            ):
                return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def is_relogin_session(session):
    """ログイン更新の専用の claude か（cwd がその専用フォルダか）。

    ログイン用の claude の画面には認証コードが映りうるため、一覧・送信・取り込み・
    一斉指示の対象から外す（エラー文の画面の末尾を通じてコードが漏れないように）。
    """
    return _same_path(session.get("cwd") or "", RELOGIN_DIR)


def list_sessions():
    """稼働中の claude セッションの一覧を返す。

    情報源は claude 自身が書いている ``~/.claude/sessions/<pid>.json`` で、
    本アプリは何も保持しない。したがってアプリを再起動しても、他の手段
    （PCのターミナル等）で起動されたセッションも含めて一覧できる。

    Returns:
        list[dict]: 更新時刻の新しい順。各要素は次のキーを持つ。
            pid / session_id / name / cwd / status / waiting_for /
            status_updated_at / started_at / updated_at /
            url（claude.ai の画面。無ければ None）
    """
    if not os.path.isdir(SESSIONS_DIR):
        return []

    sessions = []
    for entry in os.listdir(SESSIONS_DIR):
        if not entry.endswith(".json"):
            continue
        data = _read_session_file(os.path.join(SESSIONS_DIR, entry))
        if not data or not is_session_alive(data) or is_relogin_session(data):
            continue

        bridge_id = data.get("bridgeSessionId")
        sessions.append(
            {
                "pid": data.get("pid"),
                "session_id": data.get("sessionId"),
                "name": data.get("name") or "",
                "cwd": data.get("cwd") or "",
                "status": data.get("status") or "",
                # status が waiting のときの待ちの種類（"permission prompt" / "input needed"
                # 等。claude 2.1.288 の本体で確認）。それ以外のときは空
                "waiting_for": data.get("waitingFor") or "",
                "status_updated_at": data.get("statusUpdatedAt"),
                "kind": data.get("kind") or "",
                "win_session": process_win_session(data.get("pid")),
                "started_at": data.get("startedAt"),
                "updated_at": data.get("updatedAt"),
                "url": (CLAUDE_WEB_BASE + bridge_id) if bridge_id else None,
                # 本アプリが起こした子か（確認なしで文字を送れる相手か）
                "launched_by_app": is_launched_by_app(data.get("pid")),
                # 管理者として昇格しているか（読めなければ None）
                "elevated": process_elevation(data.get("pid")),
            }
        )

    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return sessions


def find_session_by_pid(pid):
    """pid から稼働中セッションを1件探す（見つからなければ None）。"""
    for session in list_sessions():
        if str(session.get("pid")) == str(pid):
            return session
    return None


# --- 過去の会話（resume） ---------------------------------------------------

# 会話の番号（sessionId）の形。--resume に渡す前に必ず照合する。
SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# 一覧に出す過去の会話の最大件数（新しい順）
RESUMABLE_LIMIT = 30

# 会話記録の先頭から読む行数（最初の指示と cwd を探す）と、末尾から読む大きさ（最新の題名）
TRANSCRIPT_HEAD_LINES = 400
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def transcript_dir_for(cwd):
    """作業フォルダの会話記録が置かれる ``~/.claude/projects/<名前>`` を返す。

    名前は作業フォルダのパスの英数字以外をすべて ``-`` にしたもの（2026-10-11 実測:
    ``my_app_02`` → ``my-app-02``、``AB資料`` → ``AB--``）。
    別のパスが同じ名前になりうるため、中身の cwd でも照合すること。
    """
    return os.path.join(
        CLAUDE_HOME, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))
    )


def _same_path(a, b):
    return os.path.normcase(os.path.normpath(a or "")) == os.path.normcase(
        os.path.normpath(b or "")
    )


# 他のセッションから届いた指示（会話記録では isMeta 付きの利用者の発言になる）
CROSS_SESSION_PATTERN = re.compile(
    r'<cross-session-message[^>]*?from-name="([^"]*)"[^>]*>(.*?)(?:</cross-session-message>|\Z)',
    re.DOTALL,
)


def _summarize_transcript(path):
    """会話記録から、題名・最初の指示・cwd・返答の有無を読む（全体は読まない）。

    最初の指示は、利用者が打った発言を優先し、無ければ他のセッションから届いた指示
    （管理用のセッションからの依頼など）の本文で代える（2026-10-11 実測: ある管理用の子の会話の
    約3割は /clear の直後に他のセッションからの指示で始まり、利用者の発言が無い）。

    Returns:
        dict: title / first_prompt / cwd（見つからなければ空文字）/
        has_reply（claude の返答があるか）
    """
    title, first_prompt, cwd = "", "", ""
    relayed_prompt = ""
    has_reply = False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(TRANSCRIPT_HEAD_LINES):
            line = f.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            if kind == "custom-title":
                title = record.get("customTitle") or title
            elif kind == "assistant":
                has_reply = True
            elif kind == "user":
                cwd = cwd or record.get("cwd") or ""
                content = (record.get("message") or {}).get("content")
                if not isinstance(content, str):
                    continue
                if (
                    not first_prompt
                    and not record.get("isMeta")
                    and not content.lstrip().startswith("<")
                ):
                    first_prompt = " ".join(content.split())[:160]
                elif not relayed_prompt:
                    relayed = CROSS_SESSION_PATTERN.search(content)
                    if relayed:
                        body = " ".join(relayed.group(2).split())
                        relayed_prompt = f"（{relayed.group(1)}から）{body}"[:160]
    # 題名は途中で付け直されることがあるので、末尾の最新のものを優先する
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if '"custom-title"' in line:
                try:
                    title = json.loads(line).get("customTitle") or title
                    break
                except ValueError:
                    continue
    except OSError:
        pass
    return {
        "title": title,
        "first_prompt": first_prompt or relayed_prompt,
        "cwd": cwd,
        "has_reply": has_reply,
    }


_hidden_resumable_lock = threading.Lock()


def _hidden_resumable_path():
    # APP_STATE_DIR は下で定義する（呼ぶのは読み込みの後なので問題ない）
    return os.path.join(APP_STATE_DIR, "hidden_resumable.json")


def _load_hidden_resumable():
    try:
        with open(_hidden_resumable_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def hide_resumable(cwd, session_id):
    """過去の会話を resume の一覧から外す（試験で起こした会話など）。

    会話の記録（jsonl）には触れず、外す会話の番号を ~/.claude-web-app/hidden_resumable.json に足すだけ。

    Returns:
        bool: 今回外したか（すでに外していれば False）

    Raises:
        SessionError: 番号の形が不正・そのプロジェクトに記録が無い場合
    """
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionError(f"会話の指定が不正です: {session_id}")
    if not os.path.isfile(os.path.join(transcript_dir_for(cwd), f"{session_id}.jsonl")):
        raise SessionError(
            f"このプロジェクトに、その会話の記録が見つかりません: {session_id}"
        )
    # 読んでから書くまでを排他にする（同時に2件外すと片方が消えるため）
    with _hidden_resumable_lock:
        hidden = _load_hidden_resumable()
        if session_id in hidden:
            return False
        hidden[session_id] = int(time.time() * 1000)
        os.makedirs(APP_STATE_DIR, exist_ok=True)
        temp_path = _hidden_resumable_path() + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(hidden, f, indent=1)
        os.replace(temp_path, _hidden_resumable_path())
    return True


def list_resumable(cwd, limit=RESUMABLE_LIMIT):
    """作業フォルダの過去の会話を、新しい順に返す（resume の候補）。

    Returns:
        list[dict]: session_id / title / first_prompt / updated_at（ミリ秒）/
        size_kb / live（いまどれかのプロセスで動いているか）
    """
    folder = transcript_dir_for(cwd)
    if not os.path.isdir(folder):
        return []
    live_ids = {s.get("session_id") for s in list_sessions()}
    hidden = _load_hidden_resumable()
    entries = []
    for entry in os.listdir(folder):
        session_id = entry[: -len(".jsonl")] if entry.endswith(".jsonl") else ""
        if not SESSION_ID_PATTERN.fullmatch(session_id) or session_id in hidden:
            continue
        path = os.path.join(folder, entry)
        try:
            entries.append((os.path.getmtime(path), session_id, path))
        except OSError:
            continue
    entries.sort(reverse=True)

    results = []
    for mtime, session_id, path in entries:
        if len(results) >= limit:
            break
        try:
            summary = _summarize_transcript(path)
            size_kb = round(os.path.getsize(path) / 1024)
        except OSError:
            continue
        # 中身の無い会話（起動しただけ）と、別のフォルダの会話（名前の衝突）は出さない
        if not (summary["first_prompt"] or summary["has_reply"]) or not _same_path(
            summary["cwd"], cwd
        ):
            continue
        results.append(
            {
                "session_id": session_id,
                "title": summary["title"],
                "first_prompt": summary["first_prompt"],
                "updated_at": int(mtime * 1000),
                "size_kb": size_kb,
                "live": session_id in live_ids,
            }
        )
    return results


def validate_resume_target(cwd, session_id, ignore_pid=None):
    """--resume に渡してよい会話かを確かめる（形・置き場所・いま動いていないこと）。

    ignore_pid を渡すと、そのプロセスで動いている分は「動いている」と数えない
    （起こし直しで、止める前に確かめるため）。

    Raises:
        SessionError: 渡せない場合
    """
    # fullmatch: `$` だと末尾の改行を通してしまう
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionError(f"再開する会話の指定が不正です: {session_id}")
    path = os.path.join(transcript_dir_for(cwd), f"{session_id}.jsonl")
    if not os.path.isfile(path):
        raise SessionError(
            f"このプロジェクトに、その会話の記録が見つかりません: {session_id}"
        )
    if not _same_path(_summarize_transcript(path)["cwd"], cwd):
        raise SessionError(f"その会話は別のフォルダのものです: {session_id}")
    for session in list_sessions():
        if session.get("session_id") == session_id and (
            ignore_pid is None or str(session.get("pid")) != str(ignore_pid)
        ):
            raise SessionError(
                f"その会話はいま動いています（pid={session.get('pid')}）。"
                "同じ会話を2つで動かすと記録が壊れるため、再開できません"
            )


# --- 起動した子の台帳 -------------------------------------------------------

# 本アプリの状態を置くフォルダ（AppData の外。TASK_LAUNCH_DIR の注記を参照）
APP_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude-web-app")

# ログイン更新（relogin_manager）が claude を起こす専用フォルダ。ここで動く claude は
# 一覧・送信・取り込み・一斉指示の対象から外す（is_relogin_session）
RELOGIN_DIR = os.path.join(APP_STATE_DIR, "relogin")

# 本アプリが起こした子の台帳。{pid: {create_time, name, cwd, launched_at}}
LAUNCHED_REGISTRY = os.path.join(APP_STATE_DIR, "launched.json")

# 台帳の開始時刻と実プロセスの開始時刻のズレの許容幅（秒）。pid の再利用を見分ける。
LAUNCHED_TIME_TOLERANCE_SEC = 2.0

_registry_lock = threading.Lock()


def _process_create_time(pid):
    """プロセスの開始時刻（エポック秒）。取れなければ None。"""
    try:
        return psutil.Process(int(pid)).create_time()
    except (psutil.Error, ValueError, TypeError):
        return None


def _load_registry():
    try:
        with open(LAUNCHED_REGISTRY, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _entry_matches(pid, entry):
    """台帳の記録が、いま生きている同じプロセスを指しているか。"""
    actual = _process_create_time(pid)
    recorded = (entry or {}).get("create_time")
    return (
        actual is not None
        and recorded is not None
        and abs(actual - float(recorded)) <= LAUNCHED_TIME_TOLERANCE_SEC
    )


def record_launched(pid, name, cwd, adopted=False):
    """本アプリが起こした子として台帳に記録する（死んだ記録はついでに掃除する）。

    台帳に載っていない相手へは send_to_session で文字を送れない。
    台帳の書き込みに失敗しても起動そのものは成功扱いにする（送れないだけ）。
    """
    with _registry_lock:
        registry = {
            key: entry
            for key, entry in _load_registry().items()
            if _entry_matches(key, entry)
        }
        registry[str(pid)] = {
            "create_time": _process_create_time(pid),
            "name": name,
            "cwd": cwd,
            "launched_at": time.time(),
        }
        if adopted:
            # 起動ではなく、後から取り込んだ記録であることを残す（adopt_session）
            registry[str(pid)]["adopted"] = True
        try:
            os.makedirs(APP_STATE_DIR, exist_ok=True)
            temp_path = LAUNCHED_REGISTRY + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, LAUNCHED_REGISTRY)
        except OSError as e:
            safe_console.say(f"[Session] 起動した子の台帳を書けませんでした: {e}")


def _parent_alive(pid):
    """親プロセスが生きているか（pid の再利用は開始時刻で見分ける）。

    本アプリの子は二段起動・タスク経由で起こすため、起動役がすぐ終わり親が居なくなる。
    ターミナルで人が起こした claude は親のシェルが生きている（2026-10-01 実測:
    人が起こした子の親は生きた powershell、台帳の子3本はいずれも親なし）。
    """
    try:
        proc = psutil.Process(int(pid))
        parent = psutil.Process(proc.ppid())
        return parent.create_time() <= proc.create_time()
    except psutil.Error:
        return False


def adopt_session(pid):
    """台帳に無い稼働中の子を、本アプリの子として台帳に取り込む。

    台帳の機能（2026-10-01 21:10 反映）より前に起こした子は台帳に載っておらず、
    send_to_session が 403 で断る。その救済のための明示的な操作で、自動では取り込まない。
    人が使っているターミナルへ打ち込まないよう、親プロセスが生きている相手は断る。

    Returns:
        dict: pid / name / cwd / adopted（今回取り込んだか。既に台帳にあれば False）

    Raises:
        SessionError: 稼働中の claude セッションでない・ターミナルから起こされている場合
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise SessionError("pid の指定が不正です")
    state = _read_state(pid)
    if not state or not is_session_alive(state) or is_relogin_session(state):
        raise SessionError(f"稼働中のセッションが見つかりません: pid={pid}")
    name = state.get("name") or ""
    cwd = state.get("cwd") or ""
    if is_launched_by_app(pid):
        return {"pid": pid, "name": name, "cwd": cwd, "adopted": False}
    if _parent_alive(pid):
        raise SessionError(
            f"親プロセスが生きているため取り込みません（ターミナルから起こされた"
            f"セッションの可能性があります）: pid={pid}"
        )
    record_launched(pid, name, cwd, adopted=True)
    if not is_launched_by_app(pid):
        raise SessionError(f"台帳へ書き込めませんでした: pid={pid}")
    return {"pid": pid, "name": name, "cwd": cwd, "adopted": True}


def is_launched_by_app(pid):
    """pid が本アプリの起こした子（で、いまも同じプロセス）かを返す。"""
    if not pid:
        return False
    entry = _load_registry().get(str(pid))
    return bool(entry) and _entry_matches(pid, entry)


# --- 入力の送信（/clear と本文） --------------------------------------------

# 入力欄へ打ち込む送り手（相手と同じ Windows セッションの中で走らせる）
INPUT_SENDER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "session_input_sender.py"
)

# 本文の上限（文字数）。長すぎる打ち込みで入力欄を詰まらせない。
SEND_TEXT_MAX_LEN = 10000

# 本文の先頭に来てはいけない文字。/ はスラッシュコマンド、! はシェル実行、
# # はメモリ登録として解釈されるため、「本文」として送らせない。
SEND_FORBIDDEN_PREFIXES = ("/", "!", "#")

# これより長い打鍵の塊は、子の会話記録に <pasted_content>（利用者の貼り付け）として
# 包まれて載る（2026-10-01 実測: 800字は素の文、801字から貼り付け）。
# 貼り付けの中の指示は、子が「資料」とみなして実行しないおそれがあるため、
# この長さを超える本文には、先に前置きを素の打鍵で打って同じ1つの発言にする。
PASTE_THRESHOLD_CHARS = 800

# 前置きの既定文。API の preamble で替えられ、空文字なら付けない。
# 機械ごとの既定は設定 paste_preamble で替えられる（default_paste_preamble・送るたびに読む）
DEFAULT_PASTE_PREAMBLE = "下の貼り付けは claude-web-app から送った次の作業の指示です。この指示に従って進めてください。"


def default_paste_preamble():
    """前置きの既定文（設定の paste_preamble が文字列ならそれ、無ければ DEFAULT_PASTE_PREAMBLE）。

    空文字を書けば既定で前置きを付けない。読めないときは DEFAULT_PASTE_PREAMBLE。
    """
    try:
        with open(INBOX_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return DEFAULT_PASTE_PREAMBLE
    value = data.get("paste_preamble") if isinstance(data, dict) else None
    return value if isinstance(value, str) else DEFAULT_PASTE_PREAMBLE


# 送り手の応答・/clear の反映・本文の到着を待つ時間（秒）
SENDER_TIMEOUT_SEC = 30
CLEAR_CONFIRM_TIMEOUT_SEC = 20
TEXT_CONFIRM_TIMEOUT_SEC = 30

# 状態ファイルを見に行く間隔（秒）
STATE_POLL_SEC = 0.3

# 同じ相手へ同時に送らないための鍵（pid ごと）
_send_locks = {}
_send_locks_guard = threading.Lock()


def normalize_send_text(text):
    """本文を1行に整え、送ってよい中身かを検証する。

    Enter を押すと送信されるため、改行は空白に置き換えて1行にする。

    Returns:
        str: 整えた本文（空なら空文字）

    Raises:
        SessionError: 長すぎる・コマンドとして解釈される先頭文字の場合
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    single = " ".join(line for line in lines if line)
    if len(single) > SEND_TEXT_MAX_LEN:
        raise SessionError(
            f"本文が長すぎます（{len(single)} 文字。上限 {SEND_TEXT_MAX_LEN}）"
        )
    if single.startswith(SEND_FORBIDDEN_PREFIXES):
        raise SessionError(
            "本文を / ・ ! ・ # で始めることはできません"
            "（コマンドとして解釈されるため。/clear は clear の指定で送ってください）"
        )
    return single


def _read_state(pid):
    """``~/.claude/sessions/<pid>.json`` を読む（無い・壊れていれば None）。"""
    return _read_session_file(os.path.join(SESSIONS_DIR, f"{int(pid)}.json"))


def _wait_state(pid, predicate, timeout):
    """状態ファイルが predicate を満たすまで待つ。満たさなければ None。"""
    deadline = time.time() + timeout
    while True:
        state = _read_state(pid)
        if state and predicate(state):
            return state
        if time.time() >= deadline:
            return None
        time.sleep(STATE_POLL_SEC)


def check_input_area(pid):
    """相手の入力欄に人の書きかけが無いかだけを確かめる（何も送らない）。

    作業後の提案は書きかけとみなさない（1文字打って見分け、その1文字は消す）。

    Raises:
        SessionDraftError: 書きかけがある
        SessionError: 入力欄が見えない・確かめられない
    """
    _run_input_sender(pid, "", check_only=True)


def _run_input_sender(pid, text, prefix=None, keep_draft=False, check_only=False):
    """送り手を相手と同じ Windows セッションで走らせ、結果を返す。

    別の Windows セッションからの AttachConsole はアクセス拒否（エラー5）に
    なるため（2026-09-30 実測）、相手が本アプリと違うセッションにいるときは
    使い捨てのタスク経由で送り手を起こす。相手が管理者として動いている
    （または権限を読めない）ときは、送り手も管理者（HighestAvailable）で起こす
    （通常権限のプロセスからは昇格したプロセスのコンソールへ入れないため）。
    keep_draft が真なら、入力欄に文字があるときは消さずに断る（SessionDraftError）。

    Raises:
        SessionDraftError: keep_draft で、入力欄に文字があった場合
        SessionError: 送り手が失敗・応答しなかった場合
    """
    os.makedirs(TASK_LAUNCH_DIR, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    request_path = os.path.join(TASK_LAUNCH_DIR, f"{token}.send-request.json")
    response_path = os.path.join(TASK_LAUNCH_DIR, f"{token}.send-response.json")
    argv = [sys.executable, INPUT_SENDER_SCRIPT, request_path]
    target_session = process_win_session(pid)
    try:
        with open(request_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pid": int(pid),
                    "text": text,
                    "prefix": prefix or "",
                    "keep_draft": bool(keep_draft),
                    "check_only": bool(check_only),
                    "response": response_path,
                },
                f,
                ensure_ascii=False,
            )
        if target_session == process_win_session(os.getpid()):
            try:
                subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=SENDER_TIMEOUT_SEC,
                    creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise SessionError(f"送り手を起動できませんでした: {e}")
        else:
            _spawn_via_task(
                argv,
                os.path.dirname(INPUT_SENDER_SCRIPT),
                "0" if target_session == 0 else "1",
                (
                    "LeastPrivilege"
                    if process_elevation(pid) is False
                    else "HighestAvailable"
                ),
            )

        deadline = time.time() + SENDER_TIMEOUT_SEC
        while not os.path.exists(response_path):
            if time.time() >= deadline:
                raise SessionError(
                    f"送り手が {SENDER_TIMEOUT_SEC} 秒以内に応答しませんでした"
                )
            time.sleep(0.2)
        with open(response_path, "r", encoding="utf-8") as f:
            response = json.load(f)
    except OSError as e:
        raise SessionError(f"送信の準備に失敗しました: {e}")
    finally:
        for path in (request_path, response_path):
            try:
                os.remove(path)
            except OSError:
                pass

    if not response.get("ok"):
        message = (
            f"送信できませんでした（{response.get('code')}）: {response.get('error')}"
        )
        if response.get("code") == "draft_present":
            raise SessionDraftError(message)
        raise SessionError(message)


# 会話記録との照合に使う本文の先頭の文字数。全文で照合すると、長い本文
# （貼り付け扱いになるもの）の記録のされ方に左右されるため、先頭だけで見る。
TRANSCRIPT_MATCH_CHARS = 40


def _transcript_paths(session_id):
    pattern = os.path.join(CLAUDE_HOME, "projects", "*", f"{session_id}.jsonl")
    return glob.glob(pattern)


def _transcript_sizes(session_id):
    """会話記録のファイルごとの大きさ（送る前の位置を覚えておくため）。"""
    sizes = {}
    for path in _transcript_paths(session_id):
        try:
            sizes[path] = os.path.getsize(path)
        except OSError:
            continue
    return sizes


def _transcript_has_prompt(session_id, text, offsets=None):
    """会話記録（transcript）の、送る前より後ろに本文の先頭が載ったか。

    offsets（送る前の大きさ）より後ろだけを見るので、同じ本文を続けて送っても
    前回の分と取り違えない。照合は本文の先頭 TRANSCRIPT_MATCH_CHARS 字で行う。
    """
    head = text[:TRANSCRIPT_MATCH_CHARS]
    needle = json.dumps(head, ensure_ascii=False)[1:-1].encode("utf-8")
    offsets = offsets or {}
    for path in _transcript_paths(session_id):
        try:
            with open(path, "rb") as f:
                f.seek(offsets.get(path, 0))
                if needle in f.read():
                    return True
        except OSError:
            continue
    return False


def _acquire_send_lock(pid):
    with _send_locks_guard:
        lock = _send_locks.setdefault(str(pid), threading.Lock())
    return lock if lock.acquire(blocking=False) else None


def paste_prefix_for(body, preamble=None):
    """本文の前に素の打鍵で打つ前置きを返す（付けないなら空文字）。

    本文が PASTE_THRESHOLD_CHARS 字以下なら貼り付けにならないので付けない。
    preamble が None なら既定文（default_paste_preamble）、空文字なら付けない。

    Raises:
        SessionError: 前置きがコマンドとして解釈される・長すぎて自身が貼り付けになる場合
    """
    if len(body or "") <= PASTE_THRESHOLD_CHARS:
        return ""
    if preamble is None:
        preamble = default_paste_preamble()
    if not preamble:
        return ""
    try:
        prefix = normalize_send_text(preamble)
    except SessionError as e:
        raise SessionError(f"前置き（preamble）が不正です: {e}")
    if len(prefix) > PASTE_THRESHOLD_CHARS:
        raise SessionError(
            f"前置き（preamble）が長すぎます（{len(prefix)} 文字。"
            f"上限 {PASTE_THRESHOLD_CHARS}。それ自体が貼り付け扱いになるため）"
        )
    return prefix


# --- 受け箱（方式 A・mod は docs/web-app-inbox-mod/） --------------------
# 子に mod web-app-inbox が入っていれば、打鍵の代わりに受け箱のファイルで /clear と本文を渡す。
# mod は <sessionId>.alive.json を10秒ごとに書く。これが新しい相手にだけ受け箱で送り、
# 無ければ今の打鍵で送る（併存）。mod には消す口が無いので、req を消すのは本アプリ。

# False にすると、mod が入っていても打鍵で送る（試験・呼び手が上書きする口）。
# True のときも、設定 config/settings.json の inbox_enabled が false なら打鍵で送る
# （本アプリの外から session_manager を import して送る道具にも効かせるため、ここで読む）
INBOX_ENABLED = True

INBOX_SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "settings.json"
)
# 設定ファイルの更新時刻ごとに読んだ値を覚える（送るたびに開き直さない）
_inbox_settings_cache = {"mtime": None, "enabled": True}


def inbox_enabled():
    """受け箱で送ってよいか（INBOX_ENABLED と設定の inbox_enabled の両方を見る）。"""
    if not INBOX_ENABLED:
        return False
    try:
        mtime = os.path.getmtime(INBOX_SETTINGS_FILE)
    except OSError:
        return True
    if _inbox_settings_cache["mtime"] != mtime:
        enabled = True
        try:
            with open(INBOX_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # JSON の false だけで止める（"false" のような文字列は既定のまま）
            enabled = not (
                isinstance(data, dict) and data.get("inbox_enabled") is False
            )
        except (OSError, ValueError):
            enabled = True
        _inbox_settings_cache.update(mtime=mtime, enabled=enabled)
    return _inbox_settings_cache["enabled"]


# alive がこれより古ければ mod は居ないとみなす（mod は10秒ごとに書く）
INBOX_ALIVE_FRESH_SEC = 30

# req を書いてから mod の受け取り（ack）を待つ時間（mod の見回りは2秒ごと）
INBOX_QUEUE_TIMEOUT_SEC = 10

# 受け取りが無く req を下げた後、すき間で mod が拾っていないかを見るまでの待ち
INBOX_WITHDRAW_GRACE_SEC = 2.5

# 古い受け箱のファイル（落ちた子の alive 等）を片付ける基準と間隔
INBOX_STALE_SEC = 24 * 3600
INBOX_CLEANUP_INTERVAL_SEC = 3600
_inbox_last_cleanup = 0.0


def _inbox_dir():
    # APP_STATE_DIR は試験で差し替えるので、呼ぶたびに組み立てる
    return os.path.join(APP_STATE_DIR, "inbox")


def _inbox_path(session_id, kind):
    """受け箱のファイルのパス（kind は req / ack / alive）。"""
    if not session_id or not SESSION_ID_PATTERN.match(str(session_id)):
        raise SessionError(f"会話の番号が不正です: {session_id!r}")
    return os.path.join(_inbox_dir(), f"{session_id}.{kind}.json")


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # 無い・mod が書いている途中で読めない（次の周で読み直す）
        return None
    return data if isinstance(data, dict) else None


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def inbox_alive(session_id):
    """相手の会話に mod web-app-inbox が入っていて、いま見回っているか。

    Returns:
        dict | None: 新しい alive の中身（版・時刻）。無い・古い・読めないなら None
    """
    if not session_id or not inbox_enabled():
        return None
    try:
        path = _inbox_path(session_id, "alive")
    except SessionError:
        return None
    data = _read_json_file(path)
    if not data or data.get("sid") != session_id:
        return None
    try:
        age = time.time() - float(data.get("at")) / 1000.0
    except (TypeError, ValueError):
        return None
    # 時計のずれで少し未来になることはあるので、大きく外れたものだけ捨てる
    if age > INBOX_ALIVE_FRESH_SEC or age < -INBOX_ALIVE_FRESH_SEC:
        return None
    return data


def cleanup_inbox(now=None):
    """古い受け箱のファイル（落ちた子の alive・残った ack・tmp）を片付ける。"""
    now = time.time() if now is None else now
    try:
        names = os.listdir(_inbox_dir())
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not (name.endswith(".json") or name.endswith(".tmp")):
            continue
        path = os.path.join(_inbox_dir(), name)
        try:
            if now - os.path.getmtime(path) <= INBOX_STALE_SEC:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def _maybe_cleanup_inbox():
    global _inbox_last_cleanup
    now = time.time()
    if now - _inbox_last_cleanup < INBOX_CLEANUP_INTERVAL_SEC:
        return
    _inbox_last_cleanup = now
    cleanup_inbox(now)


def draft_mode_for(managed, discard_draft=False, protect_draft=False):
    """受け箱で送るときの書きかけの扱い（利用者の決定 (b)・2026-10-19）。

    protect_draft（自分を区切る予約）＝断る / discard_draft＝消して送る /
    台帳の子＝書きかけを残したまま送る / 台帳に無い子＝断る。
    """
    if protect_draft:
        return "refuse"
    if discard_draft:
        return "discard"
    return "keep" if managed else "refuse"


CLEARED_PREFIX = "/clear は済みました（会話は新しくなっています）が、"


def _inbox_error(cls, message, cleared, session_id_after):
    """受け箱の経路の例外を作る（/clear が済んでいれば文言の頭でも伝える）。

    画面は cleared の属性（API の応答の cleared）で /clear のチェックを外す。
    文言の頭の CLEARED_PREFIX は、読む人と古い画面のため。
    """
    error = cls((CLEARED_PREFIX + message) if cleared else message)
    error.cleared = bool(cleared)
    error.session_id_after = session_id_after
    error.via = "inbox"
    return error


def _cleared_session(pid, session_id, ack_data=None):
    """/clear が済んでいれば新しい会話の番号を返す（ack の newSessionId か、状態ファイルで見る）。"""
    new_sid = (ack_data or {}).get("newSessionId")
    if new_sid and new_sid != session_id:
        return new_sid
    state = _read_state(pid) or {}
    if state.get("sessionId") not in (None, session_id):
        return state.get("sessionId")
    return None


def _inbox_error_from_ack(ack_data, session_id, pid=None):
    """ack が refused なら、今の打鍵と同じ種類の例外にして返す（refused でなければ None）。"""
    if not ack_data or ack_data.get("stage") != "refused":
        return None
    reason = str(ack_data.get("reason") or "")
    new_sid = (
        _cleared_session(pid, session_id, ack_data)
        if pid is not None
        else (
            ack_data.get("newSessionId")
            if ack_data.get("newSessionId") not in (None, session_id)
            else None
        )
    )
    cleared = bool(new_sid)
    after = new_sid or session_id
    if reason == "draft_present":
        return _inbox_error(
            SessionDraftError,
            "本文は送っていません。入力欄に人の書きかけがあるため、消さずに断りました。"
            "書きかけを送るか消してから送り直してください（画面では「入力欄の文字を消して送る」）",
            cleared,
            after,
        )
    if reason == "no_input_area":
        return _inbox_error(
            SessionError,
            "入力欄が見えないため、書きかけを消せませんでした。"
            "ダイアログやメニューが開いているかもしれません。閉じてから送り直してください",
            cleared,
            after,
        )
    version = ack_data.get("version")
    return _inbox_error(
        SessionError,
        f"受け箱の mod（web-app-inbox{' ' + version if version else ''}）の中で失敗しました: "
        f"{reason or '理由なし'}",
        cleared,
        after,
    )


def _write_inbox_req(path, data):
    """req を書く（mod が書きかけを読まないよう、別名に書いてから置き換える）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        _remove_quietly(tmp)


def _withdraw_inbox_req(path, data):
    """受け取られなかった req を下げる（消せなければ cancelled を立てる）。

    Returns:
        bool: 下げられたか（False なら mod が後で拾いうるので、打鍵で送り直してはいけない）
    """
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        pass
    try:
        _write_inbox_req(path, dict(data, cancelled=True))
        return True
    except OSError:
        return False


# 時間切れの直後に ack を読み直す回数（mod が書いている途中の ack を読むことがあるため）
INBOX_ACK_REREAD_TIMES = 3


def _send_via_inbox(pid, session_id, clear, body, draft):
    """受け箱で /clear と本文を渡し、確かめる。

    mod が受け取らなかった（INBOX_QUEUE_TIMEOUT_SEC 内に ack が無い）ときは req を下げて
    (None, 理由) を返す（何も打たれていないので、呼び手は今の打鍵で送り直してよい）。
    受け取った（queued 以降を見た）後は取り消さず、今と同じ確かめで待つ:
    /clear は sessionId が変わったこと（本文つきなら、子はすぐ本文の作業に入るので
    入力待ちまでは求めない）、本文は会話記録に先頭が載ったこと。

    Returns:
        tuple: (送った後の会話の番号, None)。受け取られなかったら (None, 打鍵へ戻った理由)

    Raises:
        SessionDraftError / SessionError: mod が断った・確かめが時間切れ
            （cleared・session_id_after・via の属性つき）
    """
    _maybe_cleanup_inbox()
    req_path = _inbox_path(session_id, "req")
    ack_path = _inbox_path(session_id, "ack")
    req = {
        "id": uuid.uuid4().hex,
        "clear": bool(clear),
        "text": body or "",
        "created": int(time.time() * 1000),
        "cancelled": False,
        "draft": draft,
    }

    def read_ack():
        data = _read_json_file(ack_path)
        return data if data and data.get("id") == req["id"] else None

    # 本文だけを送るときは、送る前の会話記録の位置を覚える（/clear の後は新しい記録なので不要）
    offsets = {} if clear else _transcript_sizes(session_id)
    try:
        _write_inbox_req(req_path, req)
    except OSError as e:
        return None, f"req を書けませんでした: {e}"
    new_session_id = session_id
    try:
        deadline = time.time() + INBOX_QUEUE_TIMEOUT_SEC
        ack_data = read_ack()
        while not ack_data:
            if time.time() >= deadline:
                withdrawn = _withdraw_inbox_req(req_path, req)
                # 下げる直前に mod が拾っていれば、queued が書かれる
                time.sleep(INBOX_WITHDRAW_GRACE_SEC)
                for _ in range(INBOX_ACK_REREAD_TIMES):
                    ack_data = read_ack()
                    if ack_data:
                        break
                    time.sleep(STATE_POLL_SEC)
                if ack_data:
                    break
                if not withdrawn:
                    # 下げられなかった req は mod が後で拾いうる。打鍵で送ると二重になる
                    raise _inbox_error(
                        SessionError,
                        "受け箱の mod が受け取らず、req も下げられませんでした。"
                        "遅れて届くことがあるので、送り直す前に相手の会話を見てください",
                        False,
                        session_id,
                    )
                return (
                    None,
                    f"mod が {INBOX_QUEUE_TIMEOUT_SEC} 秒以内に受け取りませんでした",
                )
            time.sleep(STATE_POLL_SEC)
            ack_data = read_ack()

        error = _inbox_error_from_ack(ack_data, session_id, pid)
        if error:
            raise error

        if clear:
            deadline = time.time() + CLEAR_CONFIRM_TIMEOUT_SEC
            while True:
                ack_data = read_ack() or ack_data
                error = _inbox_error_from_ack(ack_data, session_id, pid)
                if error:
                    raise error
                state = _read_state(pid)
                if (
                    state
                    and state.get("sessionId") not in (None, session_id)
                    and (body or state.get("status") == "idle")
                ):
                    new_session_id = state.get("sessionId")
                    break
                if time.time() >= deadline:
                    stage = (ack_data or {}).get("stage")
                    # 入力待ちに戻らないだけで /clear は済んでいることがある（ack の newSessionId・状態ファイル）
                    cleared_sid = _cleared_session(pid, session_id, ack_data)
                    raise _inbox_error(
                        SessionError,
                        (
                            f"入力待ちに戻ったことを確認できませんでした（mod の段: {stage}）"
                            + (
                                "。本文は遅れて届くことがあるので、送り直す前に相手の会話を見てください"
                                if body
                                else ""
                            )
                            if cleared_sid
                            else f"/clear を受け箱で渡しましたが、会話が新しくなったことを確認できませんでした"
                            f"（mod の段: {stage}）。mod は受け取り済みなので遅れて /clear と本文が届くことがあります。"
                            "送り直す前に相手の会話を見てください"
                        ),
                        bool(cleared_sid),
                        cleared_sid or session_id,
                    )
                time.sleep(STATE_POLL_SEC)

        if body:
            deadline = time.time() + TEXT_CONFIRM_TIMEOUT_SEC
            while not _transcript_has_prompt(new_session_id, body, offsets):
                ack_data = read_ack() or ack_data
                error = _inbox_error_from_ack(ack_data, session_id, pid)
                if error:
                    raise error
                if time.time() >= deadline:
                    stage = (ack_data or {}).get("stage")
                    raise _inbox_error(
                        SessionError,
                        f"本文を受け箱で渡しましたが、会話記録に載ったことを確認できませんでした"
                        f"（mod の段: {stage}）。遅れて届くことがあるので、送り直す前に相手の会話を見てください",
                        bool(clear),
                        new_session_id,
                    )
                time.sleep(STATE_POLL_SEC)
        return new_session_id, None
    finally:
        _remove_quietly(req_path)
        if new_session_id != session_id:
            # /clear の後は古い会話の alive・ack は使わない
            _remove_quietly(_inbox_path(session_id, "alive"))
            _remove_quietly(ack_path)


def send_to_session(
    pid,
    clear=False,
    text=None,
    wait_idle_sec=0,
    preamble=None,
    allow_unmanaged=False,
    discard_draft=False,
    protect_draft=False,
):
    """稼働中のセッションへ、/clear と本文を送る（プロセスは止めない）。

    既定では本アプリが起こした子（台帳にあるもの）だけに送る。allow_unmanaged を
    明示したときだけ、台帳に無い相手（ターミナルで人が起こしたもの等）にも送る。
    台帳に無い相手は人が入力欄を使っているかもしれないため、入力欄に人の書きかけが
    あれば消さずに断る（SessionDraftError。作業後の提案は1文字打って見分け、上書きする）。
    discard_draft を明示したときだけ書きかけも消して送る。

    送れる中身は「/clear」と「本文（1行・コマンドではない文）」だけで、
    任意のキー操作は送らせない。手順:
        1. 相手が本アプリの起こした子で、入力待ち（idle）であることを確かめる。
           作業中なら wait_idle_sec 秒まで待ち、それでも作業中なら断る。
        2. clear なら、書きかけを消して /clear を打ち、会話の番号（sessionId）が
           変わって入力待ちに戻ったことを確かめる。
        3. text なら、書きかけを消して本文を打ち、会話記録に載ったことを確かめる。
    pid と claude.ai の画面（bridgeSessionId）は /clear の前後で変わらない
    （2026-09-30 に S0・S1 の両方で実測）。
    相手に mod web-app-inbox が入っていれば（alive が新しければ）、2. 3. を打鍵の
    代わりに受け箱で渡す（_send_via_inbox）。書きかけは draft_mode_for のとおりで、
    台帳の子は書きかけを残したまま届ける。mod が受け取らなければ打鍵で送る。

    Args:
        pid: 相手のプロセスID
        clear: True なら先に /clear を送る
        text: 続けて送る本文（省略可）
        wait_idle_sec: 相手が作業中のとき、入力待ちになるまで待つ秒数（0 なら待たずに断る）
        preamble: 本文が貼り付け扱いになる長さ（PASTE_THRESHOLD_CHARS 超）のとき、
            先に素の打鍵で打つ前置き。None なら既定文、空文字なら付けない
        allow_unmanaged: True なら台帳に無い相手にも送る（画面・API で相手を明示した場合）
        discard_draft: True なら、台帳に無い相手の入力欄の文字も消して送る
        protect_draft: True なら、台帳にある相手でも入力欄に書きかけがあれば消さずに断る
            （自分を区切る予約 self_clear から使う。discard_draft より優先する）

    Returns:
        dict: pid / managed（台帳にある相手か）/ cleared / text_sent / preamble_used /
        via（inbox: 受け箱 / console: 打鍵）/ session_id_before / session_id_after /
        bridge_session_id / bridge_unchanged（送る前に画面が未登録なら None）/
        elapsed_sec

    Raises:
        SessionNotManagedError: 台帳に無い相手で、allow_unmanaged が無い場合
        SessionBusyError: 作業中・別の送信が進行中の場合
        SessionDraftError: 台帳に無い相手の入力欄に文字があった場合（何も打っていない）
        SessionError: 入力の不備・送信や確認に失敗した場合
    """
    started = time.time()
    try:
        pid = int(pid)
        wait_idle_sec = max(0.0, min(float(wait_idle_sec or 0), 600.0))
    except (TypeError, ValueError):
        raise SessionError("pid・wait_idle_sec の指定が不正です")
    body = normalize_send_text(text)
    if not clear and not body:
        raise SessionError("clear か本文（text）のどちらかを指定してください")
    prefix = paste_prefix_for(body, preamble)

    state = _read_state(pid)
    # ログイン用の claude には送らない（画面に認証コードが映りうるため）
    if not state or not is_session_alive(state) or is_relogin_session(state):
        raise SessionError(f"稼働中のセッションが見つかりません: pid={pid}")
    managed = is_launched_by_app(pid)
    if not managed and not allow_unmanaged:
        raise SessionNotManagedError(
            f"このアプリが起動したセッションではないため送れません: pid={pid}"
            "（送るときは相手を確かめたうえで allow_unmanaged を指定してください）"
        )
    # 台帳に無い相手は人が入力欄を使っているかもしれないので、打鍵の前に
    # 入力欄に人の書きかけがあれば消さずに断る
    keep_draft = protect_draft or (not managed and not discard_draft)

    lock = _acquire_send_lock(pid)
    if lock is None:
        raise SessionBusyError(f"このセッションへの別の送信が進行中です: pid={pid}")
    # まだどちらの口でも打っていない間（作業中で断った等）は None
    via = None
    try:
        if state.get("status") != "idle":
            state = (
                _wait_state(pid, lambda s: s.get("status") == "idle", wait_idle_sec)
                if wait_idle_sec
                else None
            )
            if not state:
                raise SessionBusyError(
                    f"セッションが作業中のため送りませんでした（pid={pid}）。"
                    "入力待ちになってから送るか、wait_idle_sec を指定してください"
                )

        bridge_before = state.get("bridgeSessionId")
        session_id_before = state.get("sessionId")
        session_id = session_id_before

        via = "console"
        inbox_fallback = None
        if inbox_alive(session_id_before):
            # mod が入っていれば受け箱で渡す。受け取られなければ（None）何も起きていないので打鍵で送る
            session_id_inbox, inbox_fallback = _send_via_inbox(
                pid,
                session_id_before,
                clear,
                body,
                draft_mode_for(managed, discard_draft, protect_draft),
            )
            if session_id_inbox is not None:
                via = "inbox"
                session_id = session_id_inbox
                state = _read_state(pid) or state
            else:
                # 受け取りを待った間（最大十数秒）に人が打ち始めたかもしれないので、打鍵の前に見直す
                # （待つ指定があれば、その残りの時間だけ入力待ちに戻るのを待つ）
                remaining = wait_idle_sec - (time.time() - started)
                again = _read_state(pid)
                if again and again.get("status") != "idle" and remaining > 0:
                    again = _wait_state(
                        pid, lambda s: s.get("status") == "idle", remaining
                    )
                if (
                    not again
                    or again.get("status") != "idle"
                    or again.get("sessionId") != session_id_before
                ):
                    error = SessionBusyError(
                        f"受け箱の mod が受け取らず、待っている間にセッションが作業へ戻ったため送りませんでした（pid={pid}）"
                    )
                    error.via = "console"
                    raise error
                state = again

        if via == "console" and clear:
            _run_input_sender(pid, "/clear", keep_draft=keep_draft)
            after = _wait_state(
                pid,
                lambda s: s.get("sessionId") != session_id_before
                and s.get("status") == "idle",
                CLEAR_CONFIRM_TIMEOUT_SEC,
            )
            if not after:
                raise SessionError(
                    "/clear を送りましたが、会話が新しくなったことを確認できませんでした"
                )
            state = after
            session_id = after.get("sessionId")

        if via == "console" and body:
            offsets = _transcript_sizes(session_id)
            try:
                # /clear の直後でも、その数秒に人が打ち始めているかもしれないので毎回確かめる
                try:
                    _run_input_sender(pid, body, prefix, keep_draft=keep_draft)
                except SessionDraftError as e:
                    if clear:
                        # 何が済んだかを伝える（もう一度 /clear から送り直さなくてよい）
                        raise SessionDraftError(
                            f"/clear は済みました（会話は新しくなっています）が、本文は送っていません。{e}"
                        )
                    raise
                deadline = time.time() + TEXT_CONFIRM_TIMEOUT_SEC
                while not _transcript_has_prompt(session_id, body, offsets):
                    if time.time() >= deadline:
                        raise SessionError(
                            "本文を送りましたが、会話記録に載ったことを確認できませんでした"
                        )
                    time.sleep(STATE_POLL_SEC)
            except SessionError as e:
                # 呼び手（self_clear）が「/clear だけ済んだ」を見分けて本文を送り直せるように
                e.cleared = bool(clear)
                e.session_id_after = session_id
                raise
            state = _read_state(pid) or state
    except SessionError as e:
        # どちらの口で失敗したかを応答・ログに出す（受け箱の経路の例外は自分で付けている）
        if getattr(e, "via", None) is None:
            e.via = via
        raise
    finally:
        lock.release()

    return {
        "pid": pid,
        "managed": managed,
        "cleared": bool(clear),
        "text_sent": bool(body),
        # 受け箱では前置きを付けない（貼り付け扱いへの手当てなので要らない）
        "preamble_used": bool(body and prefix and via == "console"),
        # どちらの口で渡したか（inbox: mod の受け箱 / console: 打鍵）
        "via": via,
        # mod が居るのに受け取らず打鍵で送ったときの理由（mod が壊れていないかの手がかり）
        "inbox_fallback": inbox_fallback,
        "session_id_before": session_id_before,
        "session_id_after": state.get("sessionId"),
        "bridge_session_id": state.get("bridgeSessionId"),
        # 起動直後は claude.ai への登録前で bridgeSessionId が無いことがある
        # （2026-10-01 S1 で実測）。比べる相手が無いときは「判定できない」= None
        "bridge_unchanged": (
            None if not bridge_before else state.get("bridgeSessionId") == bridge_before
        ),
        "elapsed_sec": round(time.time() - started, 2),
    }


# --- 停止 -------------------------------------------------------------------


def stop_session(pid, timeout=10):
    """指定した claude セッションを終了させる。

    誤って別のプロセスを落とさないよう、対象が本当に稼働中の claude
    セッションかを確認してから終了する。

    Args:
        pid: 対象のプロセスID
        timeout: 終了を待つ秒数。過ぎたら強制終了する

    Returns:
        dict: ``pid`` と ``forced``（強制終了したか）

    Raises:
        SessionError: 対象が見つからない、または終了できない場合
    """
    session = find_session_by_pid(pid)
    if not session:
        raise SessionError(f"稼働中のセッションが見つかりません: pid={pid}")

    try:
        proc = psutil.Process(int(pid))
        children = proc.children(recursive=True)
        proc.terminate()
        forced = False
        try:
            proc.wait(timeout=timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            forced = True
        # 取り残された子プロセス（ワーカー等）も片付ける
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.NoSuchProcess:
        return {"pid": pid, "forced": False}
    except psutil.Error as e:
        raise SessionError(f"セッションを終了できませんでした: {e}")

    return {"pid": pid, "forced": forced}


# --- 画面ののぞき見（読み取り専用） -----------------------------------------


def read_console_screen(pid, max_lines=200):
    """切り離したセッションのコンソール画面を読み取る（入力はできない）。

    非表示のコンソールに AttachConsole して、スクリーンバッファの文字だけを
    取り出す。色や装飾は取れないが、いま何を表示しているかは分かる。

    ※ 呼び出したプロセス自身のコンソールを一時的に付け替えるため、
      別プロセス（サブプロセス）から呼ぶこと。

    Args:
        pid: 対象のプロセスID
        max_lines: 取得する最大行数

    Returns:
        list[str]: 画面の各行

    Raises:
        SessionError: コンソールに接続できない場合
    """

    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", ctypes.c_short),
            ("Top", ctypes.c_short),
            ("Right", ctypes.c_short),
            ("Bottom", ctypes.c_short),
        ]

    class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", COORD),
            ("dwCursorPosition", COORD),
            ("wAttributes", ctypes.c_ushort),
            ("srWindow", SMALL_RECT),
            ("dwMaximumWindowSize", COORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(int(pid)):
        raise SessionError(f"セッションのコンソールに接続できませんでした（pid={pid}）")

    try:
        handle = kernel32.CreateFileW(
            "CONOUT$", 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None
        )
        info = CONSOLE_SCREEN_BUFFER_INFO()
        kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info))

        lines = []
        top = max(0, info.srWindow.Top)
        bottom = min(info.srWindow.Bottom, top + max_lines - 1)
        width = info.dwSize.X
        for y in range(top, bottom + 1):
            buffer = ctypes.create_unicode_buffer(width)
            read = wt.DWORD(0)
            kernel32.ReadConsoleOutputCharacterW(
                handle, buffer, width, COORD(0, y), ctypes.byref(read)
            )
            lines.append(buffer.value.rstrip())
        return lines
    finally:
        kernel32.FreeConsole()


def read_console_screen_isolated(pid, max_lines=60, timeout=15):
    """`read_console_screen` を別プロセスで実行して結果だけ受け取る。

    `AttachConsole` は呼び出したプロセス自身のコンソールを付け替えてしまうため、
    Webサーバーの中から直接呼ぶと自分のコンソールを失う。子プロセスに任せる。

    起動したはずのセッションが記録に現れないとき、「画面に何が出ているのか」を
    利用者へそのまま見せるために使う（信頼確認の残りやエラー表示など）。

    Args:
        pid: 対象のプロセスID
        max_lines: 取得する最大行数
        timeout: 子プロセスの待ち時間（秒）

    Returns:
        list[str]: 画面の各行。取得できなければ空リスト
    """
    import sys

    code = (
        "import json,sys;"
        "sys.path.insert(0, r'%s');"
        "import session_manager as sm;"
        # ASCII だけの JSON で返す。本番（常駐の管理アプリ配下）は PYTHONIOENCODING が無く
        # 標準出力が cp932 になり、❯ や罫線を書けずに落ちて、結果が空になっていた（2026-10-11 実測）
        "print(json.dumps(sm.read_console_screen(%d, %d), ensure_ascii=True))"
        % (os.path.dirname(os.path.abspath(__file__)), int(pid), int(max_lines))
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NEW_CONSOLE,
        )
        if completed.returncode != 0 or not completed.stdout:
            return []
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []

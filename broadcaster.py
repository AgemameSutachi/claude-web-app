# -*- coding: utf-8 -*-
"""全稼働 claude セッションへの一斉指示（ブロードキャスト）モジュール。

スマートフォンから「この PC を再起動するので作業を片付けて」のような指示を、
稼働中の全セッションへ一度に届けるための仕組み。

配送の仕組み:
    ヘッドレスの ``claude -p`` を1回だけ起動し、SendMessage ツールで
    各宛先セッション（``~/.claude/sessions/*.json`` の ``name``）へ
    同じメッセージを送らせる。アイドルのセッションは起こされて実行し、
    作業中のセッションには次のツールラウンドで届く（enqueue）。

設計上の注意:
    - ``claude -p`` は宛先数×数秒＋起動十数秒かかるため、実行は
      バックグラウンドスレッドで行い、開始要求へは即座に応答する。
    - 二重実行はしない（実行中に開始要求が来たら BroadcastBusyError）。
    - 起動時は session_manager.sanitize_env() で CLAUDE* 環境変数を
      除去する（子セッション判定を避けるため）。
"""

import datetime
import os
import re
import subprocess
import threading
import uuid

import session_manager as sm

# --- 定数定義 ---------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 実行ログの保存先（1回のブロードキャストごとに追記する）
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "broadcast.log")

# コンソールウィンドウを新しく出さずに子プロセスを起動するフラグ（Windows）
CREATE_NO_WINDOW = 0x08000000

# claude -p の待ち時間（秒）。起動に十数秒＋宛先1件あたり数秒かかるため、
# 固定値ではなく宛先数に応じて延ばす。
TIMEOUT_BASE_SEC = 120
TIMEOUT_PER_TARGET_SEC = 30

# 定型文。message が明示されていればそちらを優先する。
# {machine} は機械の名前（設定 machine_name）に置き換えてから使う（preset_message）
PRESET_MESSAGES = {
    "reboot": (
        "【一斉指示】{machine} をまもなく再起動します。"
        "今の作業を安全に区切り、"
        "(1)未保存の変更をコミットまたは保存、"
        "(2)進行状況・次にやることを README（または進行中ドキュメント）へ記録、"
        "(3)再開時に続きから始められる状態にしてください。"
        "完了したら『再起動準備完了』と発言して待機してください。"
    ),
}


def preset_message(preset, machine="この PC"):
    """定型文に機械の名前を入れて返す（未知の定型文なら空文字）。"""
    template = PRESET_MESSAGES.get(preset, "")
    return template.replace("{machine}", machine or "この PC")


# 宛先ごとの成否行を取り出すパターン（例: ``名前: SENT`` / ``- 名前: FAILED 理由``）
RESULT_LINE_RE = re.compile(r"^\s*(?:[-*・]\s*)?(.+?)\s*[:：]\s*(SENT|FAILED\b.*)\s*$")


class BroadcastError(Exception):
    """ブロードキャストを開始できないことを表す例外（宛先なし等）。"""


class BroadcastBusyError(BroadcastError):
    """前回のブロードキャストがまだ実行中であることを表す例外。"""


# --- ジョブ状態（メモリ保持） -----------------------------------------------

# 最後に実行した（または実行中の）ジョブ。アプリを再起動すると消えるが、
# 実行ログは LOG_FILE に残る。
_current_job = None
_job_lock = threading.Lock()


def _log(text):
    """実行ログへ1行追記する。ログ書き込みの失敗で本処理は止めない。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except OSError as e:
        print(f"[Broadcast] ログの書き込みに失敗しました: {e}")


# --- 宛先の決定 -------------------------------------------------------------


def list_target_names(exclude=None):
    """生存セッションから、ブロードキャストの宛先名リストを作る。

    生存判定は session_manager.list_sessions()（pid の生存・プロセス名・
    開始時刻の一致まで確認する既存ロジック）をそのまま使う。
    SendMessage の宛先はセッションの ``name`` なので、名前が無いものは
    宛先にできず除外する。同名が複数あると宛先が曖昧になるため1つに畳む。

    Args:
        exclude: 除外するセッション名のリスト（任意）

    Returns:
        list[str]: 宛先セッション名（重複なし）
    """
    excluded = {str(name) for name in (exclude or [])}
    names = []
    for session in sm.list_sessions():
        name = (session.get("name") or "").strip()
        if not name or name in excluded or name in names:
            continue
        names.append(name)
    return names


# --- プロンプトの組み立てと結果の解釈 ---------------------------------------


def build_prompt(targets, message):
    """``claude -p`` へ渡す配送指示のプロンプトを組み立てる。

    Args:
        targets: 宛先セッション名のリスト
        message: 各セッションへ届けるメッセージ本文

    Returns:
        配送指示のプロンプト文字列
    """
    target_list = ", ".join(targets)
    return (
        "あなたはメッセージの配送係です。以下の作業だけを行ってください。\n"
        "SendMessage ツールで、次の各宛先セッションに同じメッセージを送ってください。\n"
        f"宛先: {target_list}\n"
        "メッセージ本文（この区切り線の間を一言一句そのまま送ること）:\n"
        "----------\n"
        f"{message}\n"
        "----------\n"
        "全宛先へ送ったら、宛先ごとの成否を1行ずつ、次の形式だけで出力して終わってください。\n"
        "成功した宛先: 名前: SENT\n"
        "失敗した宛先: 名前: FAILED 理由\n"
        "コードブロックや引用符などの装飾は付けず、"
        "上記以外の文章・前置き・要約も出力しないでください。"
    )


def parse_results(targets, output):
    """``claude -p`` の出力から宛先ごとの成否を取り出す。

    出力に現れなかった宛先は ``UNKNOWN`` とする（送れたともいえず、
    失敗したともいえないため、そのまま利用者へ見せる）。

    Args:
        targets: 宛先セッション名のリスト
        output: claude -p の標準出力

    Returns:
        dict: 宛先名 → ``SENT`` / ``FAILED <理由>`` / ``UNKNOWN``
    """
    results = {name: "UNKNOWN" for name in targets}
    for line in (output or "").splitlines():
        # モデルが行全体や名前を ` や " で飾ることがあるため、先に剥がしておく
        # （実測: 「`名前: SENT`」と行ごとバッククォートで包まれ、照合に失敗した）
        match = RESULT_LINE_RE.match(line.strip().strip("`\"'"))
        if not match:
            continue
        name = match.group(1).strip().strip("`\"'")
        if name in results:
            results[name] = match.group(2).strip().rstrip("`\"'")
    return results


# --- 実行 -------------------------------------------------------------------


def _run_job(job, claude_exe):
    """バックグラウンドスレッドで ``claude -p`` を実行し、結果をジョブへ反映する。"""
    prompt = build_prompt(job["targets"], job["message"])
    timeout = TIMEOUT_BASE_SEC + TIMEOUT_PER_TARGET_SEC * len(job["targets"])

    error = None
    raw_output = ""
    returncode = None
    try:
        completed = subprocess.run(
            # SendMessage だけを許可し、配送以外の操作の許可待ちで止まらないようにする。
            # ★ --allowedTools は可変長オプションのため、後ろに置くとプロンプトまで
            #   ツール名として飲み込んでしまう。プロンプトは必ず -p の直後に置くこと
            #   （実測: 後置すると "Input must be provided..." で異常終了した）。
            [claude_exe, "-p", prompt, "--allowedTools", "SendMessage"],
            cwd=BASE_DIR,
            env=sm.sanitize_env(),  # CLAUDE* を落とす（子セッション判定の回避）
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        raw_output = (completed.stdout or "").strip()
        returncode = completed.returncode
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            error = f"claude -p が異常終了しました（code={completed.returncode}）: {stderr[:500]}"
    except subprocess.TimeoutExpired:
        error = f"claude -p が {timeout} 秒以内に終わりませんでした"
    except OSError as e:
        error = f"claude -p を起動できませんでした: {e}"

    results = parse_results(job["targets"], raw_output)

    with _job_lock:
        job["results"] = results
        job["raw_output"] = raw_output
        job["returncode"] = returncode
        job["error"] = error
        job["status"] = "failed" if error else "done"
        job["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")

    summary = ", ".join(f"{name}={value}" for name, value in results.items())
    _log(
        f"job={job['id']} finished status={job['status']} results=({summary}) error={error}"
    )

    # 完了を通知するコールバック（Socket.IO への emit など）。失敗しても握りつぶす。
    callback = job.get("_on_complete")
    if callback:
        try:
            callback(get_status())
        except Exception as e:
            print(f"[Broadcast] 完了通知のコールバックに失敗しました: {e}")


def start_broadcast(message, exclude=None, claude_exe=None, on_complete=None):
    """ブロードキャストをバックグラウンドで開始する。

    Args:
        message: 各セッションへ届けるメッセージ本文
        exclude: 除外するセッション名のリスト（任意）
        claude_exe: claude 実行ファイルのパス（省略時は PATH から解決）
        on_complete: 完了時に呼ぶコールバック。引数はジョブのスナップショット

    Returns:
        dict: 開始したジョブのスナップショット（``id`` / ``targets`` など）

    Raises:
        BroadcastBusyError: 前回のブロードキャストがまだ実行中の場合
        BroadcastError: 宛先が1件も無い場合、または claude が見つからない場合
    """
    global _current_job

    message = (message or "").strip()
    if not message:
        raise BroadcastError("メッセージが空です")

    targets = list_target_names(exclude)
    if not targets:
        raise BroadcastError("宛先となる稼働中セッションがありません")

    exe = sm.resolve_claude_exe(claude_exe)

    # 配送係の作業ディレクトリはアプリ自身のフォルダ。未信頼なら信頼登録しておく
    # （信頼確認ダイアログにヘッドレスでは誰も答えられないため）。
    sm.ensure_trusted(BASE_DIR)

    with _job_lock:
        if _current_job and _current_job["status"] == "running":
            raise BroadcastBusyError("前回の一斉指示がまだ実行中です")

        job = {
            "id": uuid.uuid4().hex[:12],
            "status": "running",
            "message": message,
            "targets": targets,
            "results": {},
            "raw_output": "",
            "returncode": None,
            "error": None,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "_on_complete": on_complete,
        }
        _current_job = job

    _log(f"job={job['id']} started targets={targets} " f"message={message[:120]!r}")

    thread = threading.Thread(target=_run_job, args=(job, exe), daemon=True)
    thread.start()

    return get_status()


def get_status():
    """最後に実行した（または実行中の）ジョブのスナップショットを返す。

    Returns:
        dict: ジョブ情報のコピー。まだ一度も実行していなければ None
    """
    with _job_lock:
        if _current_job is None:
            return None
        snapshot = dict(_current_job)
    # 内部用のコールバックは外へ出さない
    snapshot.pop("_on_complete", None)
    snapshot["results"] = dict(snapshot.get("results") or {})
    return snapshot

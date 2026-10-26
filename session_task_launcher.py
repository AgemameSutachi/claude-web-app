# -*- coding: utf-8 -*-
"""claude を起動して pid を報告し、すぐ終わる起動役。呼ばれ方は2つある。

1. タスクスケジューラから（session_manager._spawn_via_task）。本アプリ（server_manager
   配下＝セッション0）から、ログオン中のデスクトップ（セッション1）へ直接プロセスを
   作ることはできない（SYSTEM 権限が要る）。そこで「対話トークン（セッション1）」または
   「S4U（セッション0）」で動く使い捨てのタスクを登録し、このスクリプトを走らせる。
2. 本アプリから、SAFER で絞った「通常の利用者」トークンで直接（session_manager._spawn_restricted）。
   昇格した本アプリから同じセッションへ通常権限で起こすとき。環境変数は本アプリのものを
   受け継ぐ（Claude Code 由来のマーカーはここで落とす）。

    使い方: pythonw.exe session_task_launcher.py <要求ファイル.json>

要求ファイル: ``{"argv": [...], "cwd": "...", "response": "<応答ファイル>"}``
応答ファイル: ``{"pid": 1234, "win_session": 1}`` または ``{"error": "..."}``

このプロセスは起動報告を書いた直後に終了するため、起動した claude の
親は死んだ pid を指し、どのプロセスツリーにも属さない（二段起動と同じ理屈）。
"""

import ctypes
import json
import os
import subprocess
import sys

# CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
CREATION_FLAGS = 0x00000010 | 0x00000200


def current_win_session():
    """このプロセスが属する Windows セッション番号を返す。"""
    sid = ctypes.c_ulong()
    ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return sid.value


def sanitized_env():
    """Claude Code 由来のマーカーを除いた環境変数（session_manager.sanitize_env と同じ規則）。"""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE")}


def write_response(path, payload):
    """応答を一時ファイル経由で書く（読み手に書きかけを見せない）。"""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(temp_path, path)


def main():
    if len(sys.argv) != 2:
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        request = json.load(f)
    response_path = request["response"]
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        # Claude Code 由来のマーカーは落とす（タスク経由ならこのセッションの利用者の環境、
        # SAFER 経由なら本アプリの環境を受け継いでいる）
        proc = subprocess.Popen(
            request["argv"],
            cwd=request["cwd"],
            env=sanitized_env(),
            creationflags=CREATION_FLAGS,
            startupinfo=si,
            close_fds=True,
        )
        write_response(
            response_path, {"pid": proc.pid, "win_session": current_win_session()}
        )
    except Exception as e:  # 応答が無いと呼び出し側は時間切れまで待たされるため全て返す
        write_response(response_path, {"error": f"{type(e).__name__}: {e}"})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

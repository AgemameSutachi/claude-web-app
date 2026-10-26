# -*- coding: utf-8 -*-
"""別の機械へ配る束（system_update.zip と同じ形）を作る。

共有の CreateUpdateZip.ps1（server_manager の正本から写される）は作業フォルダの
ファイルを全部拾うため、本アプリでは projects/（別プロジェクトの作業ツリー）まで
入ってしまう。ここでは **最後のコミット（HEAD）に載っているものだけ** を束ね、
次のものは入れない。

    config/    機械ごとの設定（settings.json の host・合言葉、slackapi.ini 等）。
               配った先では、初回の起動で既定値（127.0.0.1 で待ち受け）の settings.json が作られる
    projects/  別プロジェクトの作業ツリー（git では追跡していないので元から入らない）

server_manager の更新の形に合わせ、直下に update_target.txt（"claude-web-app"）と
commit_message.txt（最後のコミットのメッセージ）を置く。request_self_clear.py
（区切りの予約の道具）も直下に入る。

使い方:
    <python> make_bundle.py --out <書き出す zip のパス>
"""

import argparse
import io
import os
import subprocess
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 束に入れない最上位のフォルダ・ファイル
EXCLUDE_TOP = ("config", "projects")

# server_manager がどのアプリの更新かを見分ける名前
TARGET_NAME = "claude-web-app"

# 束に必ず入っていてほしいもの（無ければ束を作らずに止める）
REQUIRED_FILES = (
    "claude-web-app.py",
    "access_guard.py",
    "request_self_clear.py",
    "requirements.txt",
)


class BundleError(Exception):
    """束を作れない（git が無い・必要なファイルが無い等）。"""


def _git(args, repo):
    try:
        result = subprocess.run(
            ["git", "-C", repo] + args, capture_output=True, check=False
        )
    except OSError as e:
        raise BundleError(f"git を起動できません: {e}")
    if result.returncode != 0:
        raise BundleError(
            f"git {' '.join(args)} が失敗しました: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def is_excluded(name):
    """束に入れない道か（最上位の config/ と projects/）。"""
    top = name.replace("\\", "/").split("/", 1)[0]
    return top in EXCLUDE_TOP


def build_bundle(out_path, repo=BASE_DIR, ref="HEAD"):
    """束を out_path へ書き、入れたファイルの名前の並びを返す。"""
    archive = _git(["archive", "--format=zip", ref], repo)
    message = _git(["log", "-1", "--pretty=%B", ref], repo).decode("utf-8").strip()
    names = []
    with zipfile.ZipFile(io.BytesIO(archive)) as src:
        members = [
            m for m in src.infolist() if not m.is_dir() and not is_excluded(m.filename)
        ]
        present = {m.filename for m in members}
        missing = [f for f in REQUIRED_FILES if f not in present]
        if missing:
            raise BundleError(f"束に入るはずのファイルがありません: {missing}")
        tmp_path = out_path + ".tmp"
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
                for member in members:
                    dst.writestr(member, src.read(member))
                    names.append(member.filename)
                dst.writestr("update_target.txt", TARGET_NAME)
                dst.writestr("commit_message.txt", message)
            os.replace(tmp_path, out_path)
        finally:
            # 途中で失敗したときの書きかけを残さない（自分で作った一時ファイルだけ）
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="配る束（system_update.zip の形）を作る"
    )
    parser.add_argument("--out", required=True, help="書き出す zip のパス")
    parser.add_argument("--ref", default="HEAD", help="束ねるコミット（既定 HEAD）")
    args = parser.parse_args(argv)
    try:
        names = build_bundle(os.path.abspath(args.out), ref=args.ref)
    except BundleError as e:
        print(f"束を作れませんでした: {e}", file=sys.stderr)
        return 1
    print(f"{len(names)} 件を束ねました: {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""サブプロジェクト（projects/ 配下）の作成・管理モジュール。

本モジュールは claude-web-app の「プロジェクト作成機能」を担当する。

主な責務:
    1. 利用者が最初に書いた指示文を ollama に渡し、プロジェクト名を自動生成する
    2. 親プロジェクトの ``projects/`` フォルダ配下に新規プロジェクトを作成する
    3. 作成したプロジェクトのメタデータを SQLite に記録し、
       作成日順・更新日順のソートや、全プロジェクト横断の内容検索を可能にする

外部（claude-web-app.py）からは、この 3 つの用途に対応する
公開関数のみを利用する想定である。
"""

import json
import os
import re
import shutil
import sqlite3
import threading
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime

# --- 定数定義 ---------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# サブプロジェクトを格納する固定フォルダ名。
# 親プロジェクト直下にこの名前のフォルダを作り、その中に各プロジェクトを納める。
PROJECTS_SUBDIR = "projects"

# メタデータを保存する SQLite データベースの絶対パス
DB_FILE = os.path.join(BASE_DIR, "config", "projects.db")

# 生成するプロジェクト名の最大長（Windowsのパス長を考慮して短めに制限）
MAX_NAME_LENGTH = 30

# 記録・命名に用いる指示文の最大文字数（末尾から切り出す）
MAX_CHAT_CHARS = 3000

# 手で改名されたフォルダを旧名の記録と同じものとみなす、作成時刻のずれの許容幅（秒）。
# 記録の created_at は秒で切り捨て・アプリ作成ならフォルダ作成の直後に取るため、
# 実測のずれは1秒未満（2026-10-23: temp-session → temp-session-develop で 0.63 秒）。
RENAME_MATCH_TOLERANCE_SEC = 3.0

# Windows で使用できない予約デバイス名（フォルダ名として使うとエラーになる）
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul"}
WINDOWS_RESERVED_NAMES.update(f"com{i}" for i in range(1, 10))
WINDOWS_RESERVED_NAMES.update(f"lpt{i}" for i in range(1, 10))

# ollama へ与える命名指示プロンプト。
# 小型モデルでも破綻しないよう、出力形式を強く限定している。
NAMING_PROMPT_TEMPLATE = """You are a naming assistant. Read the project request below and output ONE short project folder name.

Rules:
- lowercase ASCII letters, digits and hyphens only (kebab-case)
- 2 to 4 words, max 30 characters
- describe the purpose of the work, not the technology stack alone
- output ONLY the name, no quotes, no explanation, no markdown

Project request:
---
{chat}
---

Project folder name:"""

# SQLite への同時書き込みを直列化するためのロック
_db_lock = threading.Lock()

# 一覧のソート指定と実際の ORDER BY 句の対応表。
# 外部から渡された文字列をそのままSQLへ埋め込まないための固定マッピング。
SORT_ORDERS = {
    "updated": "updated_at DESC, name ASC",
    "created": "created_at DESC, name ASC",
    "name": "name ASC",
}


# --- データベース -----------------------------------------------------------


def _connect():
    """SQLite への接続を生成する。

    Flask-SocketIO の threading モードでは複数スレッドから呼ばれるため、
    接続を使い回さず都度生成・都度クローズする短命接続方式としている。
    """
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """メタデータ用テーブルを作成する（既に存在する場合は何もしない）。"""
    with _db_lock:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sub_projects (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_key  TEXT NOT NULL,
                    name        TEXT NOT NULL,
                    project_key TEXT NOT NULL UNIQUE,
                    abs_path    TEXT NOT NULL,
                    summary     TEXT NOT NULL DEFAULT '',
                    source_chat TEXT NOT NULL DEFAULT '',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sub_projects_parent "
                "ON sub_projects (parent_key)"
            )
            conn.commit()
        finally:
            conn.close()


# --- テキスト処理 -----------------------------------------------------------


def to_single_line(text):
    """複数行のテキストを、対話型CLIへ流し込める1行の文字列へ畳む。

    対話型CLIの入力欄では改行が「送信」として解釈されることがあるため、
    改行・タブは空白へ置き換え、連続する空白を1つにまとめる。
    """
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def sanitize_project_name(raw_name):
    """任意の文字列を、フォルダ名として安全な kebab-case へ正規化する。

    生成モデルが説明文や引用符、アンダースコアを混ぜて返すことがあるため、
    ここで機械的に整形する。正規化の結果が空になった場合は空文字を返し、
    呼び出し側でフォールバック名を割り当てる。
    """
    if not raw_name:
        return ""

    name = str(raw_name).strip()

    # モデルが複数行や説明文を返した場合、最初の意味のある行だけを採用する
    for line in name.splitlines():
        line = line.strip()
        if line:
            name = line
            break

    # 全角英数字を半角へ変換してから小文字化する
    name = unicodedata.normalize("NFKC", name).lower()

    # マークダウンの装飾・引用符・バッククォートを除去する
    name = name.strip("`\"'*#: 　")

    # 区切りとして使われうる文字をハイフンへ寄せる
    name = re.sub(r"[\s_./\\]+", "-", name)

    # 英数字とハイフン以外（日本語・記号など）を除去する
    name = re.sub(r"[^a-z0-9-]", "", name)

    # 連続ハイフンの圧縮と前後のハイフン除去
    name = re.sub(r"-{2,}", "-", name).strip("-")

    # 長すぎる場合は単語（ハイフン）境界で切り詰める
    if len(name) > MAX_NAME_LENGTH:
        truncated = name[:MAX_NAME_LENGTH]
        if "-" in truncated:
            truncated = truncated.rsplit("-", 1)[0]
        name = truncated.strip("-")

    # Windows の予約デバイス名はフォルダ名に使えないため退避させる
    if name in WINDOWS_RESERVED_NAMES:
        name = f"{name}-project"

    return name


def fallback_project_name():
    """名前の自動生成に失敗した場合に使う、日時ベースのフォールバック名を返す。"""
    return datetime.now().strftime("project-%Y%m%d-%H%M%S")


def resolve_unique_name(parent_projects_dir, base_name):
    """同名フォルダが既に存在する場合に、連番を付与した一意な名前を返す。"""
    if not base_name:
        base_name = fallback_project_name()

    candidate = base_name
    index = 2
    while os.path.exists(os.path.join(parent_projects_dir, candidate)):
        suffix = f"-{index}"
        # 連番を足しても最大長を超えないよう、必要なら基底名を削る
        trimmed = base_name[: MAX_NAME_LENGTH - len(suffix)].strip("-")
        candidate = f"{trimmed}{suffix}"
        index += 1
    return candidate


# --- ollama によるプロジェクト名の自動生成 ----------------------------------


def generate_project_name(chat_text, settings=None):
    """利用者が最初に書いた指示文を ollama へ渡し、プロジェクト名を自動生成する。

    ollama が停止している、モデルが未取得であるなどの理由で失敗した場合は
    例外を送出せず、日時ベースのフォールバック名を返して処理を継続させる。

    Args:
        chat_text: 命名の材料となる指示文
        settings: ollama の接続設定（url / model / timeout）を含む辞書

    Returns:
        (生成された名前, エラーメッセージ or None) のタプル。
        エラーメッセージが None でない場合、名前はフォールバック名である。
    """
    settings = settings or {}
    ollama_url = settings.get("ollama_url", "http://localhost:11434")
    model = settings.get("ollama_model", "qwen3.5:9b")
    timeout = settings.get("ollama_timeout", 60)

    chat_text = (chat_text or "").strip()
    if not chat_text:
        return fallback_project_name(), "内容が空のため自動命名できませんでした"

    payload = {
        "model": model,
        "prompt": NAMING_PROMPT_TEMPLATE.format(chat=chat_text),
        "stream": False,
        # Qwen3.5 などは思考（thinking）が既定で有効で、num_predict 32 では思考で出力が
        # 埋まり名前が返らない。命名に思考は要らないため明示的に切る
        "think": False,
        # 命名のブレを抑えるため温度は低め、出力長も短く制限する
        "options": {"temperature": 0.2, "num_predict": 32},
    }

    try:
        request = urllib.request.Request(
            f"{ollama_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        raw_name = body.get("response", "")
    except urllib.error.HTTPError as e:
        # HTTPError は URLError の一種。つながった上での失敗（モデル未取得の 404 等）を
        # 「接続できない」と取り違えないよう先に拾い、ollama の error 本文を見せる
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error") or e.reason
        except Exception:
            detail = e.reason
        return (
            fallback_project_name(),
            f"ollama が命名を断りました（HTTP {e.code}・モデル {model}）: {detail}",
        )
    except urllib.error.URLError as e:
        return (
            fallback_project_name(),
            f"ollama へ接続できませんでした（{ollama_url}）: {e.reason}",
        )
    except Exception as e:
        return fallback_project_name(), f"名前の自動生成に失敗しました: {e}"

    name = sanitize_project_name(raw_name)
    if not name:
        return (
            fallback_project_name(),
            "モデルの応答から有効な名前を抽出できませんでした",
        )
    return name, None


def build_summary(chat_text, max_chars=120):
    """会話テキストの先頭から、一覧表示用の短い要約文を作る。"""
    text = re.sub(r"\s+", " ", (chat_text or "")).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "…"
    return text


# --- プロジェクトの作成 -----------------------------------------------------


def create_project(dest_dir, key_prefix, name, source_chat="", parent_key=None):
    """指定した置き場（フォルダ）へ新規プロジェクトフォルダを作成する。

    置き場は「一時フォルダ（親プロジェクトの ``projects/`` 配下）」と
    「``developing`` 直下」のどちらもあり得るため、パスとキーの前半分を
    呼び出し側から受け取る形にしている。

    Args:
        dest_dir: 作成先フォルダの絶対パス（例: ``C:/work/developing``）
        key_prefix: プロジェクトキーの前半分（例: ``developing``）
        name: 作成するプロジェクト名（正規化前でよい）
        source_chat: 命名の元となった会話テキスト（検索用に保存する）
        parent_key: DBに記録する所属。一時フォルダなら親プロジェクトのキー、
            トップレベルならグループ名（``developing`` / ``working``）

    Returns:
        作成したプロジェクトのメタデータ辞書

    Raises:
        ValueError: 名前が不正な場合
        OSError: フォルダ作成に失敗した場合
    """
    name = sanitize_project_name(name)
    if not name:
        raise ValueError(
            "プロジェクト名が空です。半角英数字を含む名前を指定してください。"
        )

    os.makedirs(dest_dir, exist_ok=True)

    # 既存フォルダとの衝突を避けて一意な名前を確定させる
    name = resolve_unique_name(dest_dir, name)
    abs_path = os.path.join(dest_dir, name)
    os.makedirs(abs_path, exist_ok=False)

    summary = build_summary(source_chat)
    now = datetime.now().isoformat(timespec="seconds")
    project_key = f"{key_prefix}/{name}"
    parent_key = parent_key or key_prefix

    # 作成直後のフォルダが空だと何のプロジェクトか分からなくなるため、
    # 命名の根拠となった会話を README.md として残しておく。
    try:
        readme_path = os.path.join(abs_path, "README.md")
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\n")
            f.write(f"- 作成日時: {now}\n")
            f.write(f"- 親プロジェクト: {parent_key}\n\n")
            f.write("## 最初の指示（このプロジェクトを始めたときの依頼内容）\n\n")
            f.write("```\n")
            f.write((source_chat or "（記録なし）").strip() + "\n")
            f.write("```\n")
    except OSError as e:
        # README の書き込み失敗はプロジェクト作成そのものを妨げない
        print(f"[ProjectManager] README.md の作成に失敗しました: {e}")

    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO sub_projects
                    (parent_key, name, project_key, abs_path,
                     summary, source_chat, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parent_key,
                    name,
                    project_key,
                    abs_path,
                    summary,
                    (source_chat or "")[-MAX_CHAT_CHARS:],
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "name": name,
        "project_key": project_key,
        "parent_key": parent_key,
        "abs_path": abs_path,
        "summary": summary,
        "created_at": now,
        "updated_at": now,
    }


def create_sub_project(parent_key, parent_dir, name, source_chat=""):
    """親プロジェクトの projects/ 配下に新規プロジェクトフォルダを作成する。

    一時的な作業場所（既定の作成先）向けの薄いラッパー。実体は
    :func:`create_project` で、置き場だけをここで決めている。

    Args:
        parent_key: 親プロジェクトのキー（例: ``developing/claude-web-app``）
        parent_dir: 親プロジェクトの絶対パス
        name: 作成するプロジェクト名
        source_chat: 命名の元となった会話テキスト

    Returns:
        作成したプロジェクトのメタデータ辞書
    """
    return create_project(
        dest_dir=os.path.join(parent_dir, PROJECTS_SUBDIR),
        key_prefix=f"{parent_key}/{PROJECTS_SUBDIR}",
        name=name,
        source_chat=source_chat,
        parent_key=parent_key,
    )


def move_project(project_key, src_path, dest_dir, key_prefix, parent_key=None):
    """プロジェクトフォルダを別の置き場へ移動する。

    「一時フォルダで作って試し、育ったら developing へ、運用が始まったら
    working へ」という流れを想定している。フォルダの実体を移し、DBに
    メタデータ（作成時の指示など）が残っていればそれも新しい場所へ書き換える。

    Args:
        project_key: 移動元のプロジェクトキー
        src_path: 移動元の絶対パス（呼び出し側で実在を検証済みであること）
        dest_dir: 移動先フォルダの絶対パス
        key_prefix: 移動後のプロジェクトキーの前半分
        parent_key: 移動後にDBへ記録する所属

    Returns:
        移動後のメタデータ辞書（``previous_key`` に移動前のキーを含む）

    Raises:
        ValueError: すでに同じ置き場にある場合
        OSError: 移動に失敗した場合（使用中のフォルダなど）
    """
    src_path = os.path.abspath(src_path)
    name = os.path.basename(src_path)
    current_dir = os.path.dirname(src_path)

    if os.path.normcase(current_dir) == os.path.normcase(os.path.abspath(dest_dir)):
        raise ValueError("すでにこの置き場にあります。")

    os.makedirs(dest_dir, exist_ok=True)

    # 移動先に同名フォルダがあれば連番を付けて退避させる（上書きは絶対にしない）
    new_name = resolve_unique_name(dest_dir, name)
    dest_path = os.path.join(dest_dir, new_name)

    # shutil.move は同一ドライブなら os.rename と同じく瞬時に完了する。
    # 使用中（PTYの作業ディレクトリになっている等）の場合はここで OSError となる。
    shutil.move(src_path, dest_path)

    new_key = f"{key_prefix}/{new_name}"
    parent_key = parent_key or key_prefix
    now = datetime.now().isoformat(timespec="seconds")

    # DBにメタデータがあれば、消さずに新しい場所へ付け替える。
    # こうしておくと「何を作ろうとしたか」による横断検索が移動後も効く。
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """
                UPDATE sub_projects
                   SET parent_key = ?, name = ?, project_key = ?,
                       abs_path = ?, updated_at = ?
                 WHERE project_key = ?
                """,
                (parent_key, new_name, new_key, dest_path, now, project_key),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "name": new_name,
        "project_key": new_key,
        "previous_key": project_key,
        "parent_key": parent_key,
        "abs_path": dest_path,
        "renamed": new_name != name,
        "updated_at": now,
    }


# --- プロジェクトの一覧・検索 -----------------------------------------------


def _parse_iso_epoch(text):
    """ISO 形式の日時文字列をエポック秒へ。読めなければ None。"""
    try:
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def _pair_renamed(vanished, appeared):
    """消えた記録と現れたフォルダを、作成時刻で1対1に突き合わせる。

    Windows の作成時刻はフォルダの改名・同じドライブの中の移動で変わらないため、記録の
    created_at（作成時にフォルダとほぼ同時に書く・手動の取り込みでは作成時刻そのもの）と
    近いものを「同じフォルダの改名」とみなす。候補が2つ以上ある・相手からも
    一意に決まらないものは対にしない（取り違えて別のプロジェクトの指示を付けないため）。

    Args:
        vanished: {記録の識別子（project_key 等）: created_at のエポック秒（無ければ None）}
        appeared: {フォルダ名: 作成時刻のエポック秒（無ければ None）}

    Returns:
        list[tuple[str, str]]: (記録の識別子, 新しいフォルダ名) の組
    """

    def close(a, b):
        return (
            a is not None and b is not None and abs(a - b) <= RENAME_MATCH_TOLERANCE_SEC
        )

    pairs = []
    for new_name, ctime in appeared.items():
        olds = [old for old, created in vanished.items() if close(created, ctime)]
        if len(olds) != 1:
            continue
        news = [n for n, c in appeared.items() if close(vanished[olds[0]], c)]
        if news == [new_name]:
            pairs.append((olds[0], new_name))
    return pairs


def _folder_birth_time(path):
    """フォルダの作成時刻（エポック秒）。

    Windows の ``st_ctime`` は作成時刻だが、将来「メタデータの変更時刻」へ変わる予定
    （3.12 で非推奨）。改名で変わらない値が要るため ``st_birthtime`` を優先する。
    """
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime)


def _list_folder_names(folder):
    """直下のフォルダ名（. で始まるものを除く）の集合。読めなければ OSError。"""
    return {
        entry
        for entry in os.listdir(folder)
        if os.path.isdir(os.path.join(folder, entry)) and not entry.startswith(".")
    }


def _adopt_renamed(conn, dest_dir, key_prefix, parent_key, candidate_names):
    """記録の無いフォルダを、実体の消えた記録と作成時刻で突き合わせて付け替える。

    実体の消えた記録は、同じ階層（一時フォルダ側どうし／developing・working 直下どうし）
    なら親を問わず候補にする。親フォルダごとの改名や、エクスプローラーでの親をまたぐ移動
    でも、作成時の指示（検索の手がかり）を残すため。階層をまたがせないのは、子と同じ頃に
    作った親フォルダ（記録が無い）を、子の改名と取り違えないため（試験で実際に起きた）。

    Args:
        conn: 開いている接続（呼び出し側が _db_lock を持っていること）
        dest_dir: candidate_names が並んでいるフォルダ
        key_prefix: 付け替え後のキーの前半分
        parent_key: 付け替え後の所属
        candidate_names: dest_dir 直下の、記録の無いフォルダ名

    Returns:
        tuple[list[tuple[str, str]], set[str]]: (旧キー, 新キー) の組と、付け替えたフォルダ名
    """
    renamed, adopted = [], set()
    if not candidate_names:
        return renamed, adopted

    # 所属に / を含まない（developing / working）ものが直下の記録
    top_level = "/" not in parent_key
    vanished = {
        row["project_key"]: _parse_iso_epoch(row["created_at"])
        for row in conn.execute(
            "SELECT project_key, parent_key, abs_path, created_at FROM sub_projects"
        )
        if ("/" not in row["parent_key"]) == top_level
        and not os.path.isdir(row["abs_path"] or "")
    }
    if not vanished:
        return renamed, adopted

    appeared = {}
    for name in candidate_names:
        try:
            appeared[name] = _folder_birth_time(os.path.join(dest_dir, name))
        except OSError:
            appeared[name] = None

    now = datetime.now().isoformat(timespec="seconds")
    for old_key, new_name in _pair_renamed(vanished, appeared):
        new_key = f"{key_prefix}/{new_name}"
        try:
            conn.execute(
                """
                UPDATE sub_projects
                   SET parent_key = ?, name = ?, project_key = ?, abs_path = ?,
                       updated_at = ?
                 WHERE project_key = ?
                """,
                (
                    parent_key,
                    new_name,
                    new_key,
                    os.path.join(dest_dir, new_name),
                    now,
                    old_key,
                ),
            )
        except sqlite3.IntegrityError:
            # 新しいキーの記録がすでにある（別の経路で取り込み済み）。付け替えない
            continue
        renamed.append((old_key, new_key))
        adopted.add(new_name)
        print(f"[ProjectManager] 改名を記録に反映しました: {old_key} -> {new_key}")
    return renamed, adopted


def sync_sub_projects(parent_key, parent_dir, prune=True):
    """実フォルダの状態を SQLite へ同期する。

    Webアプリを介さず手動で作成されたフォルダを一覧に取り込み、
    削除済みのフォルダをDBから取り除く。また、更新日順のソートを
    実態に合わせるため、フォルダの最終更新時刻を updated_at に反映する。

    エクスプローラー等で改名・移動されたフォルダは、作成時刻で実体の消えた記録と
    突き合わせ、削除＋取り込みではなく記録の付け替えにする（作成時の指示を残すため）。

    Args:
        parent_key: 親プロジェクトのキー
        parent_dir: 親プロジェクトの絶対パス
        prune: 実体の消えたこの親の記録を消すか。複数の親をまとめて同期するときは
            False にして、最後に :func:`prune_vanished_sub_projects` を呼ぶ
            （先に消すと、別の親へ移ったフォルダが記録を引き継げないため）

    Returns:
        list[tuple[str, str]]: 改名とみなした (旧キー, 新キー) の組
            （呼び出し側が起動回数などキーで持つ記録を付け替えるため）
    """
    projects_dir = os.path.join(parent_dir, PROJECTS_SUBDIR)
    key_prefix = f"{parent_key}/{PROJECTS_SUBDIR}"
    existing_names = set()
    if os.path.isdir(projects_dir):
        try:
            existing_names = _list_folder_names(projects_dir)
        except OSError as e:
            print(f"[ProjectManager] projects フォルダの走査に失敗しました: {e}")
            return []

    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sub_projects WHERE parent_key = ?", (parent_key,)
            ).fetchall()
            known_names = {row["name"] for row in rows}

            # 改名・移動されたフォルダは、記録を新しい場所へ付け替える
            renamed, adopted = _adopt_renamed(
                conn,
                projects_dir,
                key_prefix,
                parent_key,
                existing_names - known_names,
            )
            known_names |= adopted

            # DBに存在しない実フォルダを取り込む（手動作成分）
            for name in existing_names - known_names:
                abs_path = os.path.join(projects_dir, name)
                try:
                    created = datetime.fromtimestamp(
                        _folder_birth_time(abs_path)
                    ).isoformat(timespec="seconds")
                except OSError:
                    created = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sub_projects
                        (parent_key, name, project_key, abs_path,
                         summary, source_chat, created_at, updated_at)
                    VALUES (?, ?, ?, ?, '', '', ?, ?)
                    """,
                    (
                        parent_key,
                        name,
                        f"{key_prefix}/{name}",
                        abs_path,
                        created,
                        created,
                    ),
                )

            # 実体が消えたフォルダをDBから除去する
            if prune:
                for name in known_names - existing_names:
                    conn.execute(
                        "DELETE FROM sub_projects WHERE parent_key = ? AND name = ?",
                        (parent_key, name),
                    )

            # フォルダの最終更新時刻を updated_at へ反映する（変わったものだけ書く）
            for name in existing_names:
                abs_path = os.path.join(projects_dir, name)
                try:
                    mtime = datetime.fromtimestamp(
                        os.path.getmtime(abs_path)
                    ).isoformat(timespec="seconds")
                except OSError:
                    continue
                conn.execute(
                    "UPDATE sub_projects SET updated_at = ? "
                    "WHERE parent_key = ? AND name = ? AND updated_at != ?",
                    (mtime, parent_key, name, mtime),
                )

            # 変化が無ければ書かない（検索のたびに DB ファイルを書き換えないため）
            if conn.total_changes:
                conn.commit()
        finally:
            conn.close()
    return renamed


def sync_top_level(group_key, group_dir):
    """developing / working 直下で改名されたプロジェクトの記録を付け替える。

    直下には記録の無いフォルダが多い（アプリを介さず作ったもの）ため、取り込みと削除は
    しない。記録の無いフォルダのうち、実体の消えた記録と作成時刻で対になったものだけを
    付け替える。

    Returns:
        list[tuple[str, str]]: 改名とみなした (旧キー, 新キー) の組
    """
    try:
        names = _list_folder_names(group_dir)
    except OSError as e:
        print(f"[ProjectManager] {group_key} の走査に失敗しました: {e}")
        return []

    with _db_lock:
        conn = _connect()
        try:
            known_paths = {
                os.path.normcase(os.path.abspath(row["abs_path"]))
                for row in conn.execute("SELECT abs_path FROM sub_projects")
            }
            candidates = {
                name
                for name in names
                if os.path.normcase(os.path.abspath(os.path.join(group_dir, name)))
                not in known_paths
            }
            renamed, _ = _adopt_renamed(
                conn, group_dir, group_key, group_key, candidates
            )
            if conn.total_changes:
                conn.commit()
        finally:
            conn.close()
    return renamed


def prune_vanished_sub_projects():
    """実体の消えたサブプロジェクト（一時フォルダ側）の記録を消す。

    トップレベル（所属が developing / working）の記録は消さない。従来どおり残し、
    検索の結果からは実体の有無で外す。

    Returns:
        int: 消した件数
    """
    with _db_lock:
        conn = _connect()
        try:
            gone = [
                row["project_key"]
                for row in conn.execute(
                    "SELECT project_key, parent_key, abs_path FROM sub_projects"
                )
                if "/" in row["parent_key"] and not os.path.isdir(row["abs_path"] or "")
                # 親の projects/ ごと見当たらない行は残す（付け替えに失敗しても指示を失わないため）
                and os.path.isdir(os.path.dirname(row["abs_path"] or ""))
            ]
            for key in gone:
                conn.execute("DELETE FROM sub_projects WHERE project_key = ?", (key,))
            if gone:
                conn.commit()
        finally:
            conn.close()
    return len(gone)


def list_sub_projects(
    parent_key=None, parent_dir=None, query=None, sort="updated", limit=None
):
    """サブプロジェクトの一覧を返す。

    ``parent_key`` を省略すると、親を問わず**全プロジェクトを横断**して検索する。

    Args:
        parent_key: 親プロジェクトのキー。None なら全件が対象
        parent_dir: 親プロジェクトの絶対パス（指定時は実フォルダと同期してから返す。
            この経路では改名の組を捨てるため、起動回数の記録は付け替わらない。
            アプリからは claude-web-app.py の sync_sub_projects_of で同期してから呼ぶ）
        query: 検索文字列（名前・要約・最初の指示内容を対象に部分一致）
        sort: ``updated`` / ``created`` / ``name`` のいずれか
        limit: 返す最大件数

    Returns:
        メタデータ辞書のリスト
    """
    if parent_key and parent_dir:
        sync_sub_projects(parent_key, parent_dir)

    order_by = SORT_ORDERS.get(sort, SORT_ORDERS["updated"])
    sql = (
        "SELECT name, project_key, parent_key, abs_path, summary, "
        "created_at, updated_at FROM sub_projects"
    )
    conditions = []
    params = []

    if parent_key:
        conditions.append("parent_key = ?")
        params.append(parent_key)

    if query:
        # 名前・要約・最初の指示内容の3列を対象に部分一致で絞り込む
        conditions.append("(name LIKE ? OR summary LIKE ? OR source_chat LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like, like])

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += f" ORDER BY {order_by}"

    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))

    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    return [dict(row) for row in rows]


def count_sub_projects_by_parent():
    """親プロジェクトごとのサブプロジェクト件数を辞書で返す（一覧のバッジ表示用）。"""
    with _db_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT parent_key, COUNT(*) AS cnt FROM sub_projects GROUP BY parent_key"
            ).fetchall()
        finally:
            conn.close()
    return {row["parent_key"]: row["cnt"] for row in rows}


def delete_sub_project_record(project_key):
    """サブプロジェクトのメタデータのみをDBから削除する（フォルダは削除しない）。"""
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM sub_projects WHERE project_key = ?", (project_key,)
            )
            conn.commit()
        finally:
            conn.close()

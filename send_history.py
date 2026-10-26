# -*- coding: utf-8 -*-
"""本アプリから送ったもの（起動・/clear・本文・一斉指示・自分を区切る予約の送信）の履歴。

台帳に無い相手（allow_unmanaged で送った先）への送信も、断られた・失敗した送信も残す。
1件1行の JSON（``~/.claude-web-app/send_history.jsonl``）に追記する。

本文は先頭 TEXT_HEAD_CHARS 字だけを残す。本文にトークン類が入りうるため、
よく知られた形の秘密（sk-ant-…・ghp_…・Bearer … 等）は伏せてから書く
（伏せきれない形もありうるので、画面は本アプリの他の画面と同じく Tailscale の内側だけに出す）。
"""

import json
import os
import re
import threading
from datetime import datetime

import safe_console

APP_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude-web-app")
HISTORY_FILE = os.path.join(APP_STATE_DIR, "send_history.jsonl")

# 残す本文の長さ（字数）
TEXT_HEAD_CHARS = 200

# これより大きくなったら1世代だけ残して作り直す（読むたびに全部を読むため）
MAX_FILE_BYTES = 5 * 1024 * 1024

# 画面へ返す最大件数
MAX_LIST = 1000

# 種類の識別子と表示名（画面の絞り込みにも使う）
KIND_LABELS = {
    "launch": "起動",
    "resume": "再開",
    "clear": "/clear",
    "text": "本文",
    "clear_text": "/clear＋本文",
    "self_clear": "区切りの予約",
    "auto_clear": "自動の区切りの依頼",
    "broadcast": "一斉指示",
}

# 秘密を表すキーの名前。pass・pwd だけは前に英字が来る語（bypass・compass）を除く。
# apiToken・dbPassword・clientSecret のようなキャメルケースは伏せる（評価役の指摘）
_SECRET_KEY = (
    r"(?:(?<![A-Za-z])(?:pass|pwd)|(?-i:(?<=[a-z])(?:Pass|Pwd))|password|passwd|passphrase|"
    r"パスワード|token|secret(?:[_-]?key)?|api[_-]?key|access[_-]?key|private[_-]?key)"
)
# キーと値の区切り。日本語の「パスワードは …」も区切りとして認める
_SECRET_SEP = r"[\"']?\s*(?:[:=：＝]|(?<=パスワード)は)\s*"

# 伏せる秘密の形。値の部分だけを [伏せ字] にする
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    # OpenAI 形式（sk-proj-… のようにハイフン・下線を含む）
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]{16,}"),
    # Basic 認証の値。英語の "basic authentication" を伏せないよう、数字か = を含む値に限る
    re.compile(r"(?i)(basic\s+)(?=[A-Za-z0-9+/=]*[0-9=])[A-Za-z0-9+/=]{12,}"),
    # Authorization: Basic の後ろは数字が無くても伏せる（短い資格情報ほど数字を含まない）。
    # JSON・辞書の形（"Authorization": "Basic …"）も含める
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?basic\s+)[A-Za-z0-9+/=]{8,}"),
    # キーのあとに引用符で囲んだ値（空白を含んでも閉じ引用符まで伏せる）
    re.compile(
        r"(?i)("
        + _SECRET_KEY
        + _SECRET_SEP
        + r")"
        # 「」の囲みは ASCII に限る（日本語では画面の文言「再設定」を示すことが多い・評価役の指摘）
        r"(?:\"[^\"]{3,}\"|'[^']{3,}'|「[\x20-\x7e]{3,}」)"
    ),
    # キーのあとに引用符なしの値。値は ASCII の印字可能文字に限る
    # （空白の無い日本語の指示文「パスワード：を入力する画面を…」を丸ごと伏せないため・評価役の指摘）
    re.compile(r"(?i)(" + _SECRET_KEY + _SECRET_SEP + r")(?![\"'「])[\x21-\x7e]{4,}"),
    # コマンドの引数として空白で区切って渡す形（--password 値・--db-password 値）。
    # 値は引用符で囲んだものか ASCII に限る（「--password オプションを追加して」を伏せない）
    re.compile(
        r"(?i)(--[\w-]*(?:password|passwd|passphrase|token|secret|api[_-]?key)\s+)"
        r"(?:\"[^\"]{3,}\"|'[^']{3,}'|(?![\"'-])[\x21-\x7e]{4,})"
    ),
    # curl -u 利用者:パスワード の形（-uadmin:… ・--user=admin:… も）
    re.compile(r"(\bcurl\b[^\n]*?\s(?:-u\s*|--user[=\s]+)[\"']?[^:\s\"']+:)[^\s\"']+"),
    # mysql -p<パスワード> の形
    re.compile(r"(\bmysql\b[^\n]*?\s-p)[^\s]{4,}"),
    # URL に埋め込んだ利用者名とパスワード（https://user:pass@host）
    re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"),
]

_lock = threading.Lock()


def mask_secrets(text):
    """よく知られた形の秘密を伏せる。前置き（Bearer・token= 等）は残す。"""
    text = str(text or "")
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: m.group(1) + "[伏せ字]" + m.group(2), text)
        elif pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "[伏せ字]", text)
        else:
            text = pattern.sub("[伏せ字]", text)
    return text


def text_head(text):
    """履歴に残す本文の先頭（秘密を伏せ、改行を空白にして TEXT_HEAD_CHARS 字まで）。"""
    text = mask_secrets(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > TEXT_HEAD_CHARS:
        return text[:TEXT_HEAD_CHARS] + "…"
    return text


def send_kind(clear, text):
    """/clear と本文の有無から、送信の種類を決める。"""
    has_text = bool(str(text or "").strip())
    if clear and has_text:
        return "clear_text"
    if clear:
        return "clear"
    return "text"


def record(
    kind,
    ok,
    target=None,
    text=None,
    source=None,
    error=None,
    code=None,
    detail=None,
    now=None,
):
    """1件を追記する。書けなくても呼び手を止めない（送信そのものは済んでいるため）。

    Args:
        kind: KIND_LABELS の識別子
        ok: 成功したか（開始しただけで結果が後で出るものは None）
        target: 宛先 dict（pid / name / cwd / session_id / managed / project の任意の組）
            または宛先名のリスト（一斉指示）
        text: 送った本文（先頭だけ・秘密を伏せて残す）
        source: どこから送ったか（screen / api / socket / self_clear / broadcast 等）
        error: 失敗の理由（先頭 300 字）
        code: 失敗の種類（busy / not_managed 等）
        detail: そのほか残したい短い情報（dict）
    """
    entry = {
        "ts": (now or datetime.now().astimezone()).isoformat(timespec="seconds"),
        "kind": kind,
        "ok": ok,
        "target": target,
        "text": text_head(text) if text else "",
        "source": source or "",
    }
    if error:
        entry["error"] = mask_secrets(str(error))[:300]
    if code:
        entry["code"] = code
    if detail:
        entry["detail"] = detail
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock:
            os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
            try:
                if os.path.getsize(HISTORY_FILE) > MAX_FILE_BYTES:
                    os.replace(HISTORY_FILE, HISTORY_FILE + ".1")
            except OSError:
                pass
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        safe_console.say(f"[History] 送った履歴を書けませんでした: {e}", flush=True)
    return entry


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _target_text(target):
    """絞り込み用に、宛先を1つの文字列にする。"""
    if isinstance(target, dict):
        return " ".join(str(v) for v in target.values() if v is not None)
    if isinstance(target, list):
        return " ".join(str(v) for v in target)
    return str(target or "")


def list_entries(limit=200, kind=None, query=None, failed_only=False):
    """新しい順に返す。kind（識別子）・query（宛先・本文・失敗理由の部分一致）・
    failed_only（失敗だけ）で絞れる。"""
    try:
        limit = max(1, min(int(limit or 200), MAX_LIST))
    except (TypeError, ValueError):
        limit = 200
    with _lock:
        entries = _read_file(HISTORY_FILE + ".1") + _read_file(HISTORY_FILE)
    query = (query or "").strip().lower()
    result = []
    for entry in reversed(entries):
        if kind and entry.get("kind") != kind:
            continue
        if failed_only and entry.get("ok") is not False:
            continue
        if query:
            haystack = " ".join(
                [
                    _target_text(entry.get("target")),
                    str(entry.get("text") or ""),
                    str(entry.get("error") or ""),
                ]
            ).lower()
            if query not in haystack:
                continue
        result.append(entry)
        if len(result) >= limit:
            break
    return result


def target_of(session, managed=None):
    """セッションの dict（list_sessions の1要素）から履歴の宛先を作る。"""
    session = session or {}
    target = {
        "pid": session.get("pid"),
        "name": session.get("name") or "",
        "cwd": session.get("cwd") or "",
        "session_id": session.get("session_id") or "",
    }
    if managed is None:
        managed = session.get("launched_by_app")
    if managed is not None:
        target["managed"] = bool(managed)
    return target

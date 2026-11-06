# -*- coding: utf-8 -*-
"""会話の末尾（人の発言と応答の文だけ）を会話記録から読む（2026-11-01・伝言 49cc8aa92134 の (a)）。

スマホから「いま何をしていて、どこで止まったか」を読むため。Claude に要約させるとトークンが
かかるので、会話記録（jsonl）の末尾を読んで文の部分だけを抜く（道具の呼び出しと結果・考えの部分・
サブエージェントの記録は抜かない。道具は名前だけ1行で残す）。
本文には秘密が入りうるので、送った履歴と同じ形で伏せる（send_history.mask_secrets）。
"""

import json

import send_history

# 末尾から読む長さ（バイト）
TAIL_BYTES = 768 * 1024
# 返す件数の既定と上限
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# 1件の文の上限（字）
MAX_CHARS = 3000


def _text_of(content):
    """content（文字列か、type 付きの塊の並び）から (文, 道具の名前の並び) を取る。"""
    if isinstance(content, str):
        return content, []
    texts, tools = [], []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "tool_use":
            tools.append(str(part.get("name") or "?"))
    return "\n".join(texts), tools


def _is_command_echo(text):
    # /clear などローカルのコマンドの記録（会話の中身ではない）
    head = text.lstrip()[:40]
    return head.startswith(("<command-name>", "<local-command-", "<command-message>"))


def _clip(text):
    text = send_history.mask_secrets(text.strip())
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + "…（以下略）"
    return text


def read_tail(path, limit=DEFAULT_LIMIT):
    """会話記録の末尾から、新しいものを最後にした最大 limit 件の並びを返す。

    1件は {"role": "user" | "assistant", "at": 時刻の文字列, "text": 文, "tools": [道具の名前]}。
    道具だけの応答（文が無い）は、続く同じ側の応答とまとめて道具の名前だけを持つ。
    読めなければ OSError（呼び手が 404 等にする）。
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read()
    lines = data.splitlines()
    if size > TAIL_BYTES and lines:
        # 途中から読んだ最初の行は切れているので捨てる
        lines = lines[1:]
    items = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        role = record.get("type")
        if role not in ("user", "assistant") or record.get("isMeta"):
            continue
        if record.get("isCompactSummary"):
            continue
        message = record.get("message") or {}
        text, tools = _text_of(message.get("content"))
        if role == "user" and (not text.strip() or _is_command_echo(text)):
            # 道具の結果だけの記録・コマンドの記録は出さない
            continue
        origin = record.get("origin")
        if role == "user" and isinstance(origin, dict):
            if origin.get("kind") == "task-notification":
                # サブエージェント・裏の仕事の完了通知（XML のままでは読めない）は1行にする
                text = "（サブエージェント・裏の仕事の完了通知）"
        if role == "assistant" and record.get("error") == "rate_limit":
            # 本文は英語の定型（You've hit your session limit · resets …）なので置き換える
            text = "（枠切れで応答できませんでした）"
        elif role == "assistant" and record.get("error"):
            text = text or f"（応答の失敗: {record.get('error')}）"
        if not text.strip() and not tools:
            continue
        at = str(record.get("timestamp") or "")
        if items and items[-1]["role"] == role == "assistant" and not items[-1]["text"]:
            # 道具だけの応答が続くときは1件にまとめる
            items[-1]["tools"].extend(tools)
            if text.strip():
                items[-1]["text"] = _clip(text)
            items[-1]["at"] = at
            continue
        items.append(
            {
                "role": role,
                "at": at,
                "text": _clip(text) if text.strip() else "",
                "tools": tools,
            }
        )
    for item in items:
        # 道具の名前は多すぎると読めないので、数をまとめる
        if len(item["tools"]) > 8:
            item["tools"] = item["tools"][:8] + [f"ほか {len(item['tools']) - 8} 件"]
    return items[-limit:]

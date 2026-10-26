# -*- coding: utf-8 -*-
"""稼働中の claude セッションの入力欄へ、1行ぶんの文字と Enter を打ち込む送り手。

session_manager.send_to_session から、**相手と同じ Windows セッションの中で**
呼ばれる（別セッションからの AttachConsole はアクセス拒否＝エラー5 になるため、
本アプリと違うセッションの相手には、使い捨てのタスク経由でこのスクリプトを走らせる）。

    使い方: python session_input_sender.py <要求ファイル.json>

要求ファイル: ``{"pid": 1234, "text": "/clear", "prefix": "", "response": "<応答ファイル>"}``
（prefix は長い本文の前に別の塊で打つ前置き。空なら打たない）
応答ファイル: ``{"ok": true}`` または ``{"ok": false, "code": "...", "error": "..."}``

手順（2026-09-30 の実測に基づく）:
    1. 相手のコンソールへ AttachConsole する
    1.5 画面とカーソルを読み、カーソルの所に入力欄が無ければ1打鍵も送らずに返す
       （2026-10-14: mod の帯・ペインが入力欄のまわりに何を描いても、claude はカーソルを
       入力欄の打つ位置に置く。形を画面の下から探すと、ダイアログの上に残る過去の発言
       「❯ /model」＋罫線を入力欄と取り違えた。input_area / _input_area_at_cursor）
    2. Ctrl+U を CLEAR_REPEAT 回送り、書きかけの入力を消す
       （Ctrl+U は「見た目の1行」ぶんしか消さないため、折り返し・複数行の
       書きかけに備えて複数回送る。空の欄に余分に送っても害はない）
    3. 画面を読み、入力欄が空であることを確かめる。入力欄が見えない
       （確認ダイアログ等が出ている）・書きかけが残っている場合は打たずに返す
    （keep_draft のとき＝台帳に無い相手）2 の前に入力欄を読み、文字があれば「x」を1文字打って
       読み直す。欄が「x」だけになれば作業後の提案（続ける）、そうでなければ人の書きかけなので
       Backspace で x だけ消して断る（Ctrl+U は使わない）。vim モードのノーマルモードでは x が
       1文字削除になるため、この確かめは vim モードを前提にしていない
    4. 文字を WriteConsoleInputW で流し込み、画面が落ち着いてから Enter を別に打つ
       （一度に流すと長い本文は貼り付け扱いになり Enter が飲み込まれる。2026-10-01 実測）。
       入力欄に本文が残っていれば Enter を打ち直す

このスクリプトは標準ライブラリだけで動く（タスク経由でも依存を増やさないため）。
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import sys
import time
import unicodedata

# 書きかけを消すために送る Ctrl+U の回数
CLEAR_REPEAT = 20

# 本文を打った後、画面が落ち着いたかを見る間隔と上限（秒）
STABLE_POLL_SEC = 0.3
STABLE_TIMEOUT_SEC = 10

# Enter を打ってから、入力欄が空いた（送信された）かを見るまでの時間と、打ち直す回数
ENTER_CHECK_SEC = 1.0
ENTER_RETRY = 3

# 入力欄の行頭の記号（claude の TUI）。バックグラウンド(S0)では「❯」だが、
# デスクトップ(S1)の非表示コンソールでは ASCII の「>」で描かれる（2026-09-30 実測）。
PROMPT_MARKS = ("❯", ">")

# 入力欄の下の罫線。入力欄の記号の直後にこれがあることを「空の入力欄」の条件にする
# （「>」は会話中の引用などにも現れるため、記号だけで入力欄と決めない）。
BORDER_CHAR = "─"  # ─

# 空の入力欄に出る案内文（例: `Try "fix typecheck errors"`）
PLACEHOLDER_PATTERN = re.compile(r'^Try ".*"$')

KEY_EVENT = 0x0001
LEFT_CTRL_PRESSED = 0x0008
VK_RETURN = 0x0D
VK_U = 0x55
VK_BACK = 0x08

# 入力欄に見えている文字が、作業後の提案か人の書きかけかを見分けるために打つ1文字
PROBE_CHAR = "x"


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("uChar", wt.WCHAR),
        ("dwControlKeyState", wt.DWORD),
    ]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EVENT)]


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


class SendError(Exception):
    """送れなかったことを表す例外。code は呼び出し側が分岐に使う識別子。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def key_records(char, virtual_key=0, ctrl=False):
    """1打鍵ぶん（押す・離す）の入力レコードを作る。"""
    records = []
    for down in (True, False):
        record = INPUT_RECORD()
        record.EventType = KEY_EVENT
        event = record.Event.KeyEvent
        event.bKeyDown = down
        event.wRepeatCount = 1
        event.wVirtualKeyCode = virtual_key
        event.uChar = char
        event.dwControlKeyState = LEFT_CTRL_PRESSED if ctrl else 0
        records.append(record)
    return records


def input_area_state(lines, cursor=None):
    """画面の行から入力欄の状態を判定する（cursor の扱いは input_area と同じ）。

    入力欄は「記号の行 →（書きかけの折り返し・複数行なら字下げした続きの行）→ 罫線」
    という形をしている。記号だけで決めると、会話中の引用（> で始まる行）を
    入力欄と取り違えるため、直後に罫線が来ることまで確かめる。

    Returns:
        "empty"（空）・"draft"（書きかけあり）・"missing"（入力欄が見えない）
    """
    return input_area(lines, cursor)[0]


def input_area(lines, cursor=None):
    """入力欄の状態と、欄に見えている文字（記号の後ろ・続きの行を含む）を返す。

    cursor（画面の行・桁。行は lines の添字）を渡すと、カーソルのある所の入力欄だけを見る
    （_input_area_at_cursor）。mod（Claude Code の拡張）が入力欄の上に帯を、横にペインを描いても、
    claude はカーソルを入力欄の打つ位置に置くため（2026-10-14 実測・3本とも）。
    渡さなければ、画面の下から形で探す（カーソルを読めない代役のための従来の判定）。

    Returns:
        (状態, 文字列)。状態は input_area_state と同じ。見えなければ ("missing", "")
    """
    if cursor is not None:
        return _input_area_at_cursor(lines, cursor)
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].lstrip()
        mark = next((m for m in PROMPT_MARKS if stripped.startswith(m)), None)
        if mark is None:
            continue
        rest = stripped[len(mark) :]
        # 記号の直後は空白（NBSP を含む）か行末であること（">>" 等を除く）
        if rest and rest[0] not in " \xa0":
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith("  ") and lines[j].strip():
            j += 1
        if j >= len(lines) or not lines[j].lstrip().startswith(BORDER_CHAR):
            if _is_shifted_input_line(lines, i):
                # 下の罫線が消えた崩れた画面。続きの行と状態行を見分けられないため、
                # 下の中身のある行が状態行1行だけのときに限り、記号の行の文字で判定する。
                # 2行以上あれば、複数行の書きかけかもしれないので書きかけとみなす（安全側）
                content = rest.strip("\xa0 ")
                below = [line.strip() for line in lines[i + 1 :] if line.strip()]
                if len(below) > 1:
                    return "draft", "\n".join([content] + below)
                if content and not PLACEHOLDER_PATTERN.match(content):
                    return "draft", content
                return "empty", content
            continue
        content = rest.strip("\xa0 ")
        has_draft = (content and not PLACEHOLDER_PATTERN.match(content)) or j > i + 1
        text = "\n".join([content] + [line.strip() for line in lines[i + 1 : j]])
        return ("draft" if has_draft else "empty"), text
    return "missing", ""


def _is_shifted_input_line(lines, i):
    """下の罫線が無い記号の行 i が、崩れて描かれた入力欄かどうか。

    入力欄の上に帯（「メモなし … メモを編集 ファイル [-]」）が出た画面で、描画が1行ずれ、
    上の罫線の左端に記号が重なり、下の罫線が消えた形を実測した（2026-10-14）:
    「> ──── 名前 ─」→「>」→「  ⏵⏵ auto mode on …」。
    会話中の引用・確認ダイアログの選択肢・候補メニュー・スクロールバックの区切り線と
    取り違えないよう、すぐ上の行が実測した重なりの形（左端から記号＋罫線）であり、
    下には字下げした状態行か空行しか無い（＝画面の最下部の枠）ことまで確かめる。
    書きかけのある崩れた画面は未実測（文字が上の行に重なれば missing になり、安全側）。
    """
    if i == 0:
        return False
    above = lines[i - 1]
    mark = next((m for m in PROMPT_MARKS if above.startswith(m)), None)
    if mark is None or not above[len(mark) :].lstrip("\xa0 ").startswith(BORDER_CHAR):
        return False
    return all(not line.strip() or line.startswith("  ") for line in lines[i + 1 :])


# カーソルの行から記号の行まで遡る、書きかけの続きの行の上限
MAX_DRAFT_LINES = 60

# 罫線とみなす「─」の続く長さ（入力欄の罫線は欄の幅いっぱい。短い区切りと取り違えない）
MIN_RULE_RUN = 3

# 横に並んだペインとの仕切りに使われうる縦線（ここから右は入力欄ではない）
PANE_SEPARATORS = "│┃║▏▕"


def _mark_rest(line):
    """0桁目が入力欄の記号なら、記号の後ろの文字を返す。違えば None。

    字下げした記号は受け入れない。入力欄の記号は実物の写しすべてで0桁目にあり、
    ダイアログの選ばれた選択肢（「  ❯ 5.  Haiku」）は字下げされている（2026-10-14 実測）。
    """
    mark = next((m for m in PROMPT_MARKS if line.startswith(m)), None)
    if mark is None:
        return None
    rest = line[len(mark) :]
    # 記号の直後は空白（NBSP を含む）か行末であること（">>" 等を除く）
    if rest and rest[0] not in " \xa0":
        return None
    return rest


def _is_rule(line):
    """入力欄の上下の罫線か（字下げなしで「─」が続く。上の罫線には右寄せの名前が入る）。"""
    return line.startswith(BORDER_CHAR * MIN_RULE_RUN)


def _is_overlapped_rule(line):
    """崩れた画面で、上の罫線の左端に記号が重なった行か（「> ──── 名前 ─」）。"""
    rest = _mark_rest(line)
    return rest is not None and rest.lstrip("\xa0 ").startswith(
        BORDER_CHAR * MIN_RULE_RUN
    )


def _cell_width(char):
    """端末での1文字の幅（全角は2桁）。"""
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _cut_columns(line, columns):
    """行の左から columns 桁ぶんを返す（横に並んだペインの中身を落とすため）。"""
    used = 0
    for index, char in enumerate(line):
        used += _cell_width(char)
        if used > columns:
            return line[:index]
    return line


def _rule_columns(line):
    """罫線の行から入力欄の幅（桁）を読む。右にペインがあれば、仕切りの手前まで。"""
    for separator in PANE_SEPARATORS:
        if separator in line:
            line = line[: line.index(separator)]
    line = line.rstrip(" \xa0")
    return sum(_cell_width(char) for char in line)


def _find_mark_line(lines, row):
    """row から上へ、書きかけの続きの行（字下げ・中身あり）を遡って記号の行を探す。無ければ None。"""
    k = row
    while 0 <= k < len(lines) and row - k <= MAX_DRAFT_LINES:
        if _mark_rest(lines[k]) is not None:
            return k
        if not (lines[k].startswith("  ") and lines[k].strip()):
            return None
        k -= 1
    return None


def _box_at(lines, m, row):
    """記号の行 m を入力欄として読む。入力欄の形でなければ None。

    row はカーソルの行。下の罫線のある普通の欄では、カーソルが欄の中（記号の行〜続きの行）に
    あることを求める。1行のずれを許すのは、下の罫線が消えた崩れた画面だけ。

    形の条件（帯やペインが何行あっても変わらない、入力欄そのものの形だけを見る）:
      - すぐ上が罫線（崩れた画面では、記号の重なった罫線）
      - 記号の行と字下げした続きの行の下に罫線（崩れた画面では下の罫線が消えるので、
        下に字下げの行か空行しか無いこと。_is_shifted_input_line と同じ条件）
    横にペインが並んでいれば、罫線の幅で切ってから読む。
    """
    if m == 0:
        return None
    above = lines[m - 1]
    shifted = _is_overlapped_rule(above)
    if not (_is_rule(above) or shifted):
        return None
    # 欄の幅は下の罫線（無ければ上の罫線）で決める
    j = m + 1
    while j < len(lines) and lines[j].startswith("  ") and lines[j].strip():
        j += 1
    has_bottom = j < len(lines) and _is_rule(lines[j])
    columns = _rule_columns(lines[j] if has_bottom else above)
    box = [_cut_columns(line, columns) if columns else line for line in lines]
    content = _mark_rest(box[m]).strip("\xa0 ")
    if has_bottom:
        if not m <= row < j:
            return None
        continued = [line.strip() for line in box[m + 1 : j]]
        has_draft = bool(content and not PLACEHOLDER_PATTERN.match(content)) or bool(
            continued
        )
        return ("draft" if has_draft else "empty"), "\n".join([content] + continued)
    if not shifted:
        return None
    below = box[m + 1 :]
    if not all(not line.strip() or line.startswith("  ") for line in below):
        return None
    # 崩れた画面では続きの行と状態行を見分けられない。下の中身のある行が2行以上なら
    # 複数行の書きかけかもしれないので書きかけとみなす（安全側。input_area の注記と同じ）
    rows = [line.strip() for line in below if line.strip()]
    if len(rows) > 1:
        return "draft", "\n".join([content] + rows)
    if content and not PLACEHOLDER_PATTERN.match(content):
        return "draft", content
    return "empty", content


def _input_area_at_cursor(lines, cursor):
    """カーソルのある入力欄の状態と文字を返す。カーソルの所に入力欄の形が無ければ missing。

    claude はカーソルを入力欄の打つ位置に置く（2026-10-14 実測: 帯つきの3本とも、記号の2桁右）。
    画面の下から探す従来の判定と違い、mod の帯・ペインに引用（>）や罫線が何行あっても、
    カーソルの所しか見ないので取り違えない。崩れた画面（描画が1行ずれる）に備えて、
    カーソルの1行上・下も記号の行の候補にする（下の罫線がある欄では _box_at が欄の外を断る）。

    限界: mod のペインがキーを受け取っていても、カーソルは入力欄に残る（file-browser のペインを
    開いた直後に実測）。そのペインが文字の hotkey を持てば、打った文字がボタンを押しうる。
    これは画面からは見分けられない。
    """
    row, _column = cursor
    if not 0 <= row < len(lines):
        return "missing", ""
    for start in (row, row - 1, row + 1):
        m = _find_mark_line(lines, start)
        if m is None:
            continue
        found = _box_at(lines, m, row)
        if found is not None:
            return found
    return "missing", ""


class Console:
    """相手のコンソールに接続して、入力と画面の読み取りを行う。"""

    def __init__(self, pid):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.restype = wt.HANDLE
        self.kernel32.FreeConsole()
        if not self.kernel32.AttachConsole(int(pid)):
            raise SendError(
                "attach_failed",
                f"セッションのコンソールに接続できませんでした"
                f"（pid={pid}, error={ctypes.get_last_error()}）",
            )
        self.input_handle = self._open("CONIN$")
        self.output_handle = self._open("CONOUT$")

    def _open(self, name):
        handle = self.kernel32.CreateFileW(
            name, 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0, None
        )
        if not handle or handle == wt.HANDLE(-1).value:
            raise SendError("attach_failed", f"{name} を開けませんでした")
        return handle

    def write(self, records):
        array = (INPUT_RECORD * len(records))(*records)
        written = wt.DWORD(0)
        ok = self.kernel32.WriteConsoleInputW(
            self.input_handle, array, len(records), ctypes.byref(written)
        )
        if not ok or written.value != len(records):
            raise SendError(
                "write_failed",
                f"入力を書き込めませんでした（{written.value}/{len(records)}）",
            )

    def read_lines(self):
        return self.read_screen()[0]

    def read_screen(self):
        """見えている画面の行と、カーソルの位置（行は返す行の添字・桁）を返す。"""
        info = CONSOLE_SCREEN_BUFFER_INFO()
        self.kernel32.GetConsoleScreenBufferInfo(self.output_handle, ctypes.byref(info))
        width = info.dwSize.X
        top = max(0, info.srWindow.Top)
        lines = []
        for y in range(top, info.srWindow.Bottom + 1):
            buffer = ctypes.create_unicode_buffer(width)
            read = wt.DWORD(0)
            self.kernel32.ReadConsoleOutputCharacterW(
                self.output_handle, buffer, width, COORD(0, y), ctypes.byref(read)
            )
            lines.append(buffer.value.rstrip())
        cursor = (info.dwCursorPosition.Y - top, info.dwCursorPosition.X)
        return lines, cursor

    def close(self):
        for handle in (self.input_handle, self.output_handle):
            self.kernel32.CloseHandle(handle)
        self.kernel32.FreeConsole()


def look(console):
    """画面を読み、(行, 入力欄の状態, 欄の文字) を返す。

    カーソルを読める相手（本物の Console）はカーソルの所の入力欄だけを見る。
    読めない代役（試験の FakeConsole 等）は従来どおり形で探す。
    """
    reader = getattr(console, "read_screen", None)
    if reader is None:
        lines, cursor = console.read_lines(), None
    else:
        lines, cursor = reader()
    state, text = input_area(lines, cursor)
    return lines, state, text


def _screen_tail(lines):
    """原因を画面から読めるよう、画面の末尾の中身のある行を添える。"""
    tail = [line for line in lines if line.strip()][-6:]
    return " / ".join(tail) if tail else f"（画面が空。{len(lines)} 行）"


def _no_input_area(lines):
    return SendError(
        "no_input_area",
        "入力欄が見えません（確認ダイアログやメニューが開いている"
        f"可能性があります）。画面の末尾: {_screen_tail(lines)}",
    )


def text_records(text):
    """文字列ぶんの打鍵レコード（Enter は含まない）。"""
    records = []
    for char in text:
        records += key_records(char)
    return records


def send_line(pid, text, settle_sec=0.8, prefix="", keep_draft=False, check_only=False):
    """書きかけを消し、入力欄が空なのを確かめてから（prefix と）text と Enter を打つ。

    keep_draft が真なら、消す前に入力欄を読み、人の書きかけがあれば消さずに断る
    （台帳に無い相手＝人がターミナルで使っているかもしれない相手の書きかけを守る）。
    作業後の提案は、1文字打つと置き換わることで見分けて、そのまま送る。
    check_only が真なら、書きかけの確かめ（keep_draft と同じ）だけをして何も送らない
    （確かめに打った1文字は Backspace で消す。起こし直しの前に、人の書きかけを止めないため）。
    """
    for part in (text if not check_only else "x", prefix or "x"):
        if not part or "\n" in part or "\r" in part:
            raise SendError("invalid_text", "送る文字は改行を含まない1行にしてください")
    console = Console(pid)
    try:
        # 何か打つ前に、カーソルの所に入力欄があることを確かめる。無ければ1打鍵も送らない
        # （ダイアログや mod のペインがキーを受け取っている所へ Ctrl+U や確かめの1文字を流さない）
        lines, state, seen = look(console)
        if state == "missing":
            raise _no_input_area(lines)
        if check_only:
            if state == "draft":
                console.write(key_records(PROBE_CHAR))
                time.sleep(settle_sec)
                _, _, probed = look(console)
                console.write(key_records("\b", VK_BACK))
                if probed.strip() != PROBE_CHAR or seen.strip() == PROBE_CHAR:
                    raise SendError(
                        "draft_present",
                        "入力欄に書きかけの文字があります（人が書きかけているかもしれません）。"
                        f"入力欄: {seen}",
                    )
            return
        if keep_draft:
            if state == "draft":
                # 作業後の提案か、人の書きかけかを、消さずに見分ける。1文字打つと、
                # 提案はその1文字に置き換わり、書きかけには1文字が足される。
                # 書きかけなら Backspace で足した1文字だけ消して断る（Ctrl+U は使わない）。
                console.write(key_records(PROBE_CHAR))
                time.sleep(settle_sec)
                lines, state, probed = look(console)
                # 元の文が「x」そのものだと、描画が間に合わなかったときに見分けられないので書きかけ扱い
                if probed.strip() != PROBE_CHAR or seen.strip() == PROBE_CHAR:
                    console.write(key_records("\b", VK_BACK))
                    raise SendError(
                        "draft_present",
                        "入力欄に書きかけの文字があるため、消さずに止めました"
                        f"（人が書きかけているかもしれません）。入力欄: {seen}",
                    )
            if state == "missing":
                raise _no_input_area(lines)
        clear_keys = []
        for _ in range(CLEAR_REPEAT):
            clear_keys += key_records("\x15", VK_U, ctrl=True)
        console.write(clear_keys)
        time.sleep(settle_sec)  # 画面の再描画を待つ

        lines, state, seen = look(console)
        if state == "draft":
            # 作業を終えた直後の入力欄には、次の指示の提案（例「中身を確認して」）が
            # 薄字で出る。画面の文字も色属性も本物の書きかけと同じで、Ctrl+U でも消えない
            # （2026-10-02 実測）。1文字打って消し、同じ文が戻れば提案とみなして空として扱う
            # （本物の書きかけは消せば戻らない）。提案は本文を打つと置き換わる。
            console.write(key_records("x"))
            time.sleep(settle_sec)
            console.write(clear_keys)
            time.sleep(settle_sec)
            lines, state, again = look(console)
            if state == "draft" and again == seen:
                state = "empty"
        if state != "empty":
            # 何が見えていたかを添える（原因を画面から読めるように）
            if state == "missing":
                raise _no_input_area(lines)
            raise SendError(
                "draft_remains",
                f"書きかけの入力を消せませんでした。画面の末尾: {_screen_tail(lines)}",
            )

        if prefix:
            # 前置きは素の文として入るよう、本文とは別の塊で打ち、描かれるのを待つ。
            # 続く長い本文は貼り付けとして同じ入力欄に足され、1つの発言になる。
            console.write(text_records(prefix))
            wait_screen_stable(console)
        console.write(text_records(text))

        # 本文と Enter を一度に流し込むと、長い本文（約100字〜）は TUI に「貼り付け」と
        # みなされ、末尾の Enter が送信にならず飲み込まれる（2026-10-01 実測：本文は
        # 入力欄に残り、会話記録には載らなかった）。そこで、本文が入力欄に描かれて
        # 画面が落ち着くのを待ってから、Enter を別に打つ。
        wait_screen_stable(console)
        _, _, typed = look(console)
        enter = key_records("\r", VK_RETURN)
        for _ in range(ENTER_RETRY):
            console.write(enter)
            time.sleep(ENTER_CHECK_SEC)
            # 打ち直すのは、打った文字がそのまま残っているときだけ。返答がすぐ終わって
            # 提案が出ていても、それに Enter を打って送ってしまわないようにする。
            _, state, now = look(console)
            if state != "draft" or now != typed:
                return
        lines, _, _ = look(console)
        raise SendError(
            "enter_not_accepted",
            "本文は入力欄に入りましたが、Enter で送信されませんでした。"
            f"画面の末尾: {_screen_tail(lines)}",
        )
    finally:
        console.close()


def wait_screen_stable(console, timeout=STABLE_TIMEOUT_SEC):
    """打った文字が描かれ（入力欄が空でなくなり）、画面が続けて同じになるまで待つ。

    時間内に落ち着かなくても例外にはしない（後の Enter と確認で判定する）。
    """
    deadline = time.time() + timeout
    time.sleep(STABLE_POLL_SEC)
    previous = None
    while time.time() < deadline:
        lines, state, _ = look(console)
        # /clear の直後は候補メニューが開いて入力欄が「見えない」判定になることが
        # あるため、空でなければ（何か打たれていれば）状態は問わない
        if lines == previous and state != "empty":
            return
        previous = lines
        time.sleep(STABLE_POLL_SEC)


def write_response(path, payload):
    """応答を一時ファイル経由で書く（読み手に書きかけを見せない）。"""
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(temp_path, path)


def type_raw(pid, text, enter=True):
    """確かめをせずに、text（と Enter）をそのまま打つ。ログイン更新の専用。

    ログイン画面（方式の選択・認証コードの入力欄）は通常の入力欄の形ではないため、
    send_line の確かめは使えない。打つ相手は relogin_manager が起こしたログイン用の
    claude に限られる（呼び出し側で pid を照合する）。
    """
    console = Console(pid)
    try:
        if text:
            console.write(text_records(text))
            # 長い文字列は貼り付け扱いになり、すぐ打った Enter が飲まれる（send_line の注記）。
            # 画面が2回続けて同じになるまで待ってから Enter を打つ
            deadline = time.time() + STABLE_TIMEOUT_SEC
            previous = None
            time.sleep(STABLE_POLL_SEC)
            while time.time() < deadline:
                lines = console.read_lines()
                if lines == previous:
                    break
                previous = lines
                time.sleep(STABLE_POLL_SEC)
        if enter:
            console.write(key_records("\r", VK_RETURN))
    finally:
        console.close()


def main_stdin():
    """要求を標準入力の JSON で受け、結果を標準出力の JSON で返す（ファイルに書かない）。

    認証コードのように、ディスク・コマンドラインに残したくない文字を打つための入口。
    要求: ``{"pid": 1234, "raw": "文字", "enter": true}``
    応答: ``{"ok": true}`` または ``{"ok": false, "code": "...", "error": "..."}``
    （エラー文に打った文字は入れない）
    """
    request = json.loads(sys.stdin.read() or "{}")
    try:
        type_raw(
            int(request["pid"]),
            request.get("raw") or "",
            bool(request.get("enter", True)),
        )
        payload = {"ok": True}
    except SendError as e:
        payload = {"ok": False, "code": e.code, "error": str(e)}
    except Exception as e:
        payload = {"ok": False, "code": "unexpected", "error": type(e).__name__}
    # ASCII だけの JSON で返す（呼び出し元の環境によっては標準出力が cp932 になり、書けずに落ちるため）
    sys.stdout.write(json.dumps(payload, ensure_ascii=True))
    return 0 if payload["ok"] else 1


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--stdin":
        return main_stdin()
    if len(sys.argv) != 2:
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        request = json.load(f)
    try:
        send_line(
            int(request["pid"]),
            request["text"],
            prefix=request.get("prefix") or "",
            keep_draft=bool(request.get("keep_draft")),
            check_only=bool(request.get("check_only")),
        )
        payload = {"ok": True}
    except SendError as e:
        payload = {"ok": False, "code": e.code, "error": str(e)}
    except Exception as e:  # 応答が無いと呼び出し側は時間切れまで待たされるため全て返す
        payload = {
            "ok": False,
            "code": "unexpected",
            "error": f"{type(e).__name__}: {e}",
        }
    write_response(request["response"], payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

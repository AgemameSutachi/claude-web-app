"""標準出力・標準エラーへ書けなくなっても落ちないようにする包み。

server_manager は子の標準出力をパイプで受けている。server_manager が再起動すると
読み手の居ないパイプが残り、print() のたびに OSError [Errno 22] が出る
（2026-10-22 に、区切りの予約が 500 を返し、自動の区切りの見回りが止まった）。

install() は sys.stdout / sys.stderr を SafeStream に替え、
- 元の出力へ書いて失敗しても例外を出さない（失敗した出力にはそれ以降書かない）
- 同じ内容を行頭に時刻を付けてログファイル（既定 ~/.claude-web-app/server.log）へも書く
ようにする。ログファイルが書けないときも黙って続け、少し後に開き直す
（出力のせいで処理を止めない）。
"""

import datetime
import logging
import os
import sys
import threading
import time

LOG_MAX_BYTES = 5 * 1024 * 1024
# ログファイルが書けなかった後、開き直しを試すまでの秒数
LOG_RETRY_SEC = 60


class LogFile:
    """複数の SafeStream が共有する、行頭に時刻を付けて追記するログファイル。

    LOG_MAX_BYTES を超えたら <path>.1 へ退かせて書き直す（1世代だけ残す）。
    退かせられないとき（.1 を別のプロセスが開いている等）は同じファイルへ書き続け、
    1割ほど書いた後に試し直す。書けなかったときは LOG_RETRY_SEC 後に開き直す。
    """

    def __init__(self, path, max_bytes=LOG_MAX_BYTES, clock=time.monotonic):
        self.path = path
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._file = None
        self._size = 0
        self._rotate_at = max_bytes
        self._at_line_start = True
        self._retry_at = None

    @property
    def broken(self):
        """書けずに休んでいる最中か。"""
        return self._retry_at is not None and self._clock() < self._retry_at

    def _open(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # 改行は LF にそろえる（大きさを tell() と書いた量で食い違わせない）
        self._file = open(
            self.path, "a", encoding="utf-8", errors="backslashreplace", newline="\n"
        )
        self._size = self._file.tell()

    def _rotate(self):
        self._file.close()
        self._file = None
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        self._open()
        if self._size > self.max_bytes:
            # 退かせられなかった。書き続けて、少し後に試し直す
            self._rotate_at = self._size + max(self.max_bytes // 10, 1)
        else:
            self._rotate_at = self.max_bytes

    def _close_file(self):
        if self._file is not None:
            try:
                self._file.close()
            except (OSError, ValueError):
                pass
            self._file = None

    def write(self, text):
        if not text or self.broken:
            return
        with self._lock:
            try:
                if self._file is None:
                    self._open()
                if self._size > self._rotate_at:
                    self._rotate()
                stamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
                out = []
                for piece in text.splitlines(keepends=True):
                    if self._at_line_start:
                        out.append(stamp)
                    out.append(piece)
                    self._at_line_start = piece.endswith(("\n", "\r"))
                self._file.write("".join(out))
                self._file.flush()
                self._size = self._file.tell()
                self._retry_at = None
            except (OSError, ValueError):
                # 書けないログのために毎回やり直して遅くしない（少し後に開き直す）
                self._close_file()
                self._retry_at = self._clock() + LOG_RETRY_SEC

    def close(self):
        with self._lock:
            self._close_file()


class SafeStream:
    """元の出力（stream）とログファイル（log）の両方へ書き、書く失敗を飲み込む。

    元の出力が一度でも失敗したら、以降は元の出力へは書かない（読み手の居ない
    パイプは戻らないため）。それ以外の属性は元の出力へ委ねる。
    """

    def __init__(self, stream, log=None):
        self._stream = stream
        self._log = log
        self.stream_broken = stream is None

    def write(self, text):
        if self._log is not None:
            self._log.write(text)
        if not self.stream_broken:
            try:
                try:
                    self._stream.write(text)
                except UnicodeEncodeError:
                    # 出力の文字コードで書けない文字だけ \\uXXXX の形にして書き直す
                    enc = getattr(self._stream, "encoding", None) or "utf-8"
                    self._stream.write(
                        text.encode(enc, "backslashreplace").decode(enc, "replace")
                    )
            except (OSError, ValueError):
                self.stream_broken = True
        return len(text)

    def flush(self):
        if self.stream_broken:
            return
        try:
            self._stream.flush()
        except (OSError, ValueError):
            self.stream_broken = True

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def isatty(self):
        if self.stream_broken:
            return False
        try:
            return self._stream.isatty()
        except (OSError, ValueError):
            return False

    def __getattr__(self, name):
        if name in ("_stream", "_log"):
            # copy や pickle で __init__ を通らずに作られたときの無限再帰を防ぐ
            raise AttributeError(name)
        return getattr(self._stream, name)


def _repoint_stream_handlers(replaced):
    """install より前に作られた logging の StreamHandler を、包んだ出力へ付け替える。

    socketio / engineio は import の時点で StreamHandler を足し、元の sys.stderr を
    掴んだままにする（それだと server.log に残らず、壊れた後は Logging error が出る）。

    Args:
        replaced: {id(元の出力): 包んだ出力}
    """
    loggers = [logging.getLogger()] + [
        lg
        for lg in list(logging.Logger.manager.loggerDict.values())
        if isinstance(lg, logging.Logger)
    ]
    for logger in loggers:
        for handler in list(logger.handlers):
            if type(handler) is not logging.StreamHandler:
                continue
            target = replaced.get(id(getattr(handler, "stream", None)))
            if target is not None:
                # setStream は古い出力を flush するので、壊れたパイプだとここで例外になる
                handler.acquire()
                try:
                    handler.stream = target
                finally:
                    handler.release()


def install(log_path):
    """sys.stdout / sys.stderr を SafeStream に替える。

    既に包んである出力はそのままにする（二度呼んでも包みは1重・ログファイルも共有）。

    Returns:
        LogFile: 共有のログファイル
    """
    log = None
    for name in ("stdout", "stderr"):
        current = getattr(sys, name)
        if isinstance(current, SafeStream) and current._log is not None:
            log = current._log
    if log is None:
        log = LogFile(log_path)
    replaced = {}
    for name in ("stdout", "stderr"):
        current = getattr(sys, name)
        if isinstance(current, SafeStream) and current._log is not None:
            continue
        if isinstance(current, SafeStream):
            current = current._stream
        wrapped = SafeStream(current, log)
        replaced[id(current)] = wrapped
        setattr(sys, name, wrapped)
    _repoint_stream_handlers(replaced)
    return log


def say(*args, **kwargs):
    """print と同じ。ただし出力の失敗では例外を出さない（包みの外でも使える保険）。"""
    try:
        print(*args, **kwargs)
    except Exception:
        pass

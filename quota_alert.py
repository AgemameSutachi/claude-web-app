# -*- coding: utf-8 -*-
"""子が週の枠（seven_day）で止まったら、すぐ ntfy で人へ1通知らせる（2026-11-03・管理役の伝言 cc46caadb535）。

5時間枠の停止は数時間で戻り、戻ったら quota_resume.py が続きを頼むので知らせない。週の枠は戻りが
最長7日先で、続きの依頼では取り戻せない（11-02 22:52 の例）。止まったことをすぐ知れば、人がアカウントを
替える・枠を足す等を決められる。

同じ停止で何度も鳴らさない: 鍵は（アカウント, 戻りの時刻）。同じアカウントの子が同じ週の枠で次々に止まっても
1通だけにする（その見回りで見つけた子を全部書く）。後から止まった子は記録に残すが鳴らさない。
送れなかったら間をおいて MAX_ATTEMPTS 回まで送り直す。記録はファイルに残し、再起動しても鳴らし直さない。
見つけたときに既に戻っている（recovered）停止は知らせない。

設定:
    config/ntfy.ini（送るたびに読む・再起動は要らない・リポジトリには入れない）
        [DEFAULT] NTFY_SERVER・NTFY_TOPIC・NTFY_TOKEN・NTFY_FALLBACK_SERVER・NTFY_FALLBACK_TOKEN・NTFY_TIMEOUT_SEC
        無い・トピックが空なら知らせない（記録には「設定なし」と残す）。
    config/settings.json の quota_alert_mode: on（既定）/ off
"""

import configparser
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import accounts
import safe_console
import session_manager as sm

MODES = ("off", "on")
DEFAULT_MODE = "on"

# 知らせる枠（accounts.quota_stop の window）
ALERT_WINDOWS = ("weekly",)

CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config", "ntfy.ini"
)
STATE_FILE = os.path.join(sm.APP_STATE_DIR, "quota_alert.json")

# 送れなかったときの送り直し（秒×回数）と上限の回数
RETRY_SEC = 5 * 60
MAX_ATTEMPTS = 3
# 記録を残す日数（週の枠の戻りより長く）
KEEP_DAYS = 10
# 主サーバーの待ち時間の既定（秒）・予備の待ち時間（秒。経路が長い）
DEFAULT_TIMEOUT_SEC = 5
FALLBACK_TIMEOUT_SEC = 15
# 1通に書く子の数の上限（多いときは「ほか n 本」）
MAX_LISTED = 10

USER_AGENT = "claude-web-app-quota-alert/1.0"


def mode_of(settings):
    mode = (settings or {}).get("quota_alert_mode", DEFAULT_MODE)
    return mode if mode in MODES else DEFAULT_MODE


def load_config(path=None):
    """ntfy の設定を読む。無い・トピックが空なら None。"""
    path = path or CONFIG_FILE
    if not os.path.isfile(path):
        return None
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as e:
        safe_console.say(f"[QuotaAlert] {path} を読めません: {e}", flush=True)
        return None
    conf = parser["DEFAULT"]
    topic = (conf.get("NTFY_TOPIC") or "").strip()
    server = (conf.get("NTFY_SERVER") or "").strip()
    if not topic or not server:
        return None
    try:
        timeout = float(conf.get("NTFY_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SEC
    return {
        "server": server,
        "topic": topic,
        "token": (conf.get("NTFY_TOKEN") or "").strip(),
        "fallback_server": (conf.get("NTFY_FALLBACK_SERVER") or "").strip(),
        "fallback_token": (conf.get("NTFY_FALLBACK_TOKEN") or "").strip(),
        "timeout": timeout if timeout > 0 else DEFAULT_TIMEOUT_SEC,
    }


def post_to_ntfy(config, title, message, priority=3, tags=("warning",)):
    """ntfy へ1通送る。主サーバー → 予備の順に試し、送れたサーバーを返す。どちらも駄目なら RuntimeError。

    JSON 形式で送る（ヘッダー方式は日本語のタイトルが壊れる）。User-Agent を名乗る（公開 URL の
    Cloudflare は urllib の既定の名前を 403 で弾く）。
    """
    data = json.dumps(
        {
            "topic": config["topic"],
            "title": title,
            "message": message,
            "priority": priority,
            "tags": list(tags),
        },
        ensure_ascii=False,
    ).encode("utf-8")

    def send(server, token, timeout):
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            server.rstrip("/") + "/", data=data, headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as res:
            res.read()

    # ValueError は URL の書き間違い（scheme が無い等）。予備へ回す（評価1周目の低3）
    errors = (urllib.error.URLError, OSError, ValueError)
    try:
        send(config["server"], config["token"], config["timeout"])
        return config["server"]
    except errors as e:
        if not config.get("fallback_server"):
            raise RuntimeError(f"ntfy へ送れませんでした: {e}") from e
        first = e
    try:
        send(config["fallback_server"], config["fallback_token"], FALLBACK_TIMEOUT_SEC)
        return config["fallback_server"]
    except errors as e:
        raise RuntimeError(f"ntfy へ送れませんでした（主: {first}／予備: {e}）") from e


def _account_of(session):
    return session.get("account") or "本家"


def _label(session):
    return session.get("name") or os.path.basename(session.get("cwd") or "") or "?"


def compose(account, stop, sessions):
    """知らせの題と本文。"""
    names = [f"{_label(s)}（pid {s.get('pid')}）" for s in sessions[:MAX_LISTED]]
    rest = len(sessions) - len(names)
    if rest > 0:
        names.append(f"ほか {rest} 本")
    window = stop.get("window_label") or "週の枠"
    title = f"{account} が{window}で止まった"
    message = (
        f"アカウント {account} の子が{window}を使い切って止まりました。"
        f"戻りは {stop.get('resets_at') or '不明'} です。\n"
        f"止まった子: {'、'.join(names)}\n"
        "戻るまで、この子たちは続きの依頼でも動きません。アカウントを替えるか枠を足すかを決めてください。"
        "（claude-web-app の自動の知らせ・同じ停止では二度鳴らしません）"
    )
    return title, message


class QuotaAlert:
    """週の枠で止まった子を ntfy で知らせる見回り。"""

    def __init__(
        self,
        transcript_path_for,
        quota_stop=None,
        poster=None,
        config_loader=None,
        state_file=None,
        notifier=None,
    ):
        self.transcript_path_for = transcript_path_for
        # 鳴らせなかった（設定なし・諦めた）ことを管理役へ知らせる関数（kind, key, text）。None なら知らせない
        self.notifier = notifier
        # 試験で差し替える
        self.quota_stop = quota_stop or accounts.quota_stop
        self.poster = poster or post_to_ntfy
        self.config_loader = config_loader or load_config
        # None なら使うたびに STATE_FILE を引く（試験が置き場を差し替えられるように）
        self._state_file = state_file
        self._lock = threading.Lock()
        self._state = self._load()
        self._last_tick = None
        self._mode = DEFAULT_MODE

    @property
    def state_file(self):
        return self._state_file or STATE_FILE

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"alerts": {}}

    def _save(self, now=None):
        cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
        alerts = self._state["alerts"]
        for key in [k for k, v in alerts.items() if v.get("first_seen", 0) < cutoff]:
            del alerts[key]
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_file)
        except OSError as e:
            safe_console.say(f"[QuotaAlert] 記録を書けませんでした: {e}", flush=True)

    def tick(self, sessions, settings, now=None):
        """1回の見回り。送った通数を返す。"""
        now = time.time() if now is None else now
        self._mode = mode_of(settings)
        self._last_tick = datetime.fromtimestamp(now).isoformat(timespec="seconds")
        if self._mode == "off":
            return 0
        # 鍵（アカウント|戻りの時刻）→ (枠切れ, この見回りで見つけた子)
        found = {}
        for session in sessions:
            if (session.get("kind") or "interactive") != "interactive":
                continue
            if sm.is_relogin_session(session):
                continue
            session_id = session.get("session_id")
            if not session_id:
                continue
            try:
                stop = self.quota_stop(
                    self.transcript_path_for(session.get("cwd"), session_id), now
                )
            except Exception as e:
                safe_console.say(f"[QuotaAlert] 会話記録を読めません: {e}", flush=True)
                continue
            if not stop or stop.get("window") not in ALERT_WINDOWS:
                continue
            if stop.get("recovered"):
                # 見つけたときに既に戻っている（本アプリが止まっていた等）。今から鳴らしても遅い
                continue
            key = f"{_account_of(session)}|{stop.get('resets_at_epoch')}"
            found.setdefault(key, (stop, []))[1].append(session)
        changed = False
        sent = 0
        # 記録の書き換えは錠の中で行う（GET の status() が同時に写しても壊れないように・評価1周目の低2）。
        # ntfy への送信（最大20秒）だけは錠の外
        for key, (stop, children) in found.items():
            with self._lock:
                record = self._state["alerts"].get(key)
                if record is None:
                    record = {
                        "first_seen": now,
                        "account": _account_of(children[0]),
                        "window": stop.get("window"),
                        "resets_at": stop.get("resets_at"),
                        "status": "waiting",
                        "attempts": 0,
                        "children": [],
                    }
                    self._state["alerts"][key] = record
                    changed = True
                for child in children:
                    entry = f"{_label(child)}|{child.get('session_id')}"
                    if entry not in record["children"]:
                        record["children"].append(entry)
                        changed = True
                status = record["status"]
                tell_gave_up = status == "failed" and not record.get("told_gave_up")
                if status == "sent" or (status == "failed" and not tell_gave_up):
                    continue
                if status != "failed" and now < record.get("next_try_epoch", 0):
                    continue
            if tell_gave_up:
                # 諦めた知らせが前に溜まらなかった（管理役への知らせが無効だった等）。見回りのたびに出し直す
                changed |= self._tell_gave_up(key, record, stop)
                continue
            config = self.config_loader()
            if not config:
                # 設定が置かれたら鳴らせるよう、間をおいて読み直す（送り直しの回数には数えない）
                with self._lock:
                    first = record["status"] != "no_config"
                    record["status"] = "no_config"
                    record["next_try_epoch"] = now + RETRY_SEC
                    told = record.get("told_no_config")
                changed = True
                if first:
                    safe_console.say(
                        f"[QuotaAlert] {key} を知らせたいが ntfy の設定（config/ntfy.ini）が無い",
                        flush=True,
                    )
                if not told and self._tell(
                    key + "|no_config",
                    f"{record['account']} が週の枠で止まった（戻り {stop.get('resets_at')}）が、"
                    "ntfy の設定（claude-web-app の config/ntfy.ini）が無いので人へ鳴らせていない。"
                    "置けば5分以内に鳴らす",
                ):
                    with self._lock:
                        record["told_no_config"] = True
                continue
            title, message = compose(record["account"], stop, children)
            with self._lock:
                record["attempts"] += 1
                attempts = record["attempts"]
                # 送っている間は waiting（設定が置かれた後に no_config のまま見えないように）
                record["status"] = "waiting"
            changed = True
            try:
                server = self.poster(config, title, message)
            except Exception as e:
                with self._lock:
                    gave_up = attempts >= MAX_ATTEMPTS
                    if gave_up:
                        record["status"] = "failed"
                    else:
                        record["next_try_epoch"] = now + RETRY_SEC * attempts
                    record["error"] = str(e)[:300]
                safe_console.say(
                    f"[QuotaAlert] {key} を知らせられませんでした（{attempts} 回目）: {e}",
                    flush=True,
                )
                if gave_up:
                    self._tell_gave_up(key, record, stop)
                continue
            with self._lock:
                record["status"] = "sent"
                record["sent_at"] = now
                record["server"] = server
                record.pop("error", None)
            sent += 1
            safe_console.say(f"[QuotaAlert] 知らせた: {title}", flush=True)
        if changed:
            with self._lock:
                self._save(now)
        return sent

    def _tell_gave_up(self, key, record, stop):
        """諦めたことを管理役へ知らせ、溜まったら印を付ける。記録を変えたら True。"""
        told = self._tell(
            key + "|gave_up",
            f"{record['account']} が週の枠で止まった（戻り {stop.get('resets_at')}）が、"
            f"ntfy へ {record['attempts']} 回送れず諦めた: {str(record.get('error') or '')[:150]}",
        )
        if told:
            with self._lock:
                record["told_gave_up"] = True
        return bool(told)

    def _tell(self, key, text):
        """人へ鳴らせなかったことを管理役へ知らせる（notifier）。溜まった（または前に溜めた）なら True。

        鍵は場面ごとに分ける（設定なし・諦めた）。管理役への知らせは同じ（種類, 鍵）を二度は溜めないので、
        同じ鍵だと設定なしの後の諦めが捨てられる（評価2周目の中1）。notifier が False を返したとき
        （管理役への知らせが無効等）は溜まっていないので、呼び手は印を付けず次の見回りで出し直す。
        """
        if not self.notifier:
            return False
        try:
            return self.notifier("quota_alert_failed", key, text) is not False
        except Exception as e:  # 知らせの失敗で見回りを止めない
            safe_console.say(f"[QuotaAlert] 管理役への知らせに失敗: {e}", flush=True)
            return False

    def status(self):
        with self._lock:
            alerts = {
                k: dict(v, children=list(v.get("children") or []))
                for k, v in self._state["alerts"].items()
            }
        return {"mode": self._mode, "last_tick": self._last_tick, "alerts": alerts}

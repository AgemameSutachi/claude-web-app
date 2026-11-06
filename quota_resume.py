# -*- coding: utf-8 -*-
"""枠切れで止まった子へ、枠が戻ったら自動で続きを頼む（2026-11-01・管理役の伝言 49cc8aa92134 の (c)）。

claude は5時間枠・週の枠を使い切ると、応答の代わりに ``error: rate_limit`` の行を会話記録に書いて
止まり、枠が戻っても自分では再開しない（accounts.quota_stop）。誰も「続けて」と言わなければ
止まったままになる。そこで見回りで次の全部に当たる子にだけ、続きを頼む文を1回送る:

    - 会話記録の最後の応答が枠切れで、その後に人の発言が無い（accounts.quota_stop。人・管理役が
      止めた子、もともと待機していた子は、最後の応答が枠切れではないので当たらない）
    - 戻りの時刻（resetsAt）から GRACE_SEC たち、MAX_AGE_SEC はたっていない
      （それより古いものは、人が置いておくと決めたかもしれないので送らない）
    - 入力待ち（idle）が IDLE_STABLE_SEC 続いている
    - 入力欄に書きかけが無い（protect_draft。人が打っている所へは割り込まない）

ユーザー（原文・10-31）「続けてって文字だけだと、止めてた作業まで再開する恐れがあるから注意」。
送る文は「枠切れで中断した作業だけを続け、それより前に止めるよう言われていた作業・返事待ちの作業は
再開しない。続けるものが無ければ待つ」と書く。

同じ枠切れ（会話の番号と枠切れの行の時刻の組）へは一度だけ送る（記録はファイルに残し、再起動しても
送り直さない）。送れなかったときは間をおいて MAX_ATTEMPTS 回まで送り直し、それでも駄目なら諦めて
知らせる（notifier。管理役への伝言・liaison.py）。

動き方は設定 quota_resume_mode で選ぶ: off / dry_run（当たりを記録するだけ）/ on（送る。既定）。
"""

import json
import os
import threading
import time
from datetime import datetime

import accounts
import safe_console
import send_history
import session_manager as sm

MODES = ("off", "dry_run", "on")
DEFAULT_MODE = "on"

# 戻りの時刻から、これだけたってから送る（秒。時計のずれと、戻った直後の取りこぼしを見込む）
GRACE_SEC = 120
# 戻りの時刻からこれより後は送らない（秒）。本アプリが止まっていた等で大きく遅れた分
MAX_AGE_SEC = 6 * 3600
# 入力待ちがこれだけ続いてから送る（秒）
IDLE_STABLE_SEC = 60
# 戻りの時刻を過ぎても入力待ちにならない状態がこれだけ続いたら、管理役へ1回知らせる（秒）。
# 知らせないと MAX_AGE_SEC（6時間）まで黙ったままになる（5.31節の評価で残した低・2026-11-02）
NOT_IDLE_NOTIFY_SEC = 30 * 60
# 送れなかったときの送り直し（秒×回数）と上限の回数
RETRY_SEC = 5 * 60
MAX_ATTEMPTS = 3
# 書きかけがあったときに見直すまでの間（秒。回数には数えない）
DRAFT_RETRY_SEC = 10 * 60
# 見回りの間隔（秒）
POLL_SEC = 60
# 記録を残す日数
KEEP_DAYS = 14
# 画面・GET に返す直近の結果の件数
RECENT_MAX = 50

STATE_FILE = os.path.join(sm.APP_STATE_DIR, "quota_resume.json")

# これ以上は送らない記録の状態（again: 一度頼んだ後に同じ戻りの時刻でまた枠切れ）
# handed: 見つけたときに試しの設定で「本アプリは頼まない」と管理役へ知らせたもの。後で on に戻っても頼まない
# （管理役が戻りの後に手で頼むと、続きの依頼が2回届くため・2026-11-02 評価役の中）
FINAL_STATUSES = ("sent", "failed", "would_send", "too_old", "again", "handed")

# 送る本文。頭は変えないこと（送った履歴と会話記録の照合は先頭で行う）。
# {window} は「5時間枠」「週の枠」など
# 頭に【…】の見出しは付けない（全体の決まりは機械の伝言の見出しの形だけを信じ、形の崩れた見出しには
# 従わないとしているので、似た見出しは「確かめられていない伝言」と読まれうる・評価役の指摘）
RESUME_TEXT = (
    "{window}が戻りました（{resets} に戻る予定でした。本アプリ claude-web-app からの自動の知らせで、"
    "ユーザーの発言ではありません）。"
    "枠切れで中断した作業だけを、続きから進めてください。"
    "人・管理役・ほかの役から止めるよう言われた作業（枠切れの前でも、枠切れの間に届いた指示でも）と、"
    "人・管理役・ほかの役の返事を待っていた作業（まだ返事が来ていないもの）は再開しないでください。"
    "枠切れの間に届いてまだ応えていない依頼・伝言は、止める指示に反しない限り扱ってください。"
    "続ける作業が無ければ、何もせずに次の依頼を待ってください。"
)

# 管理役が手で頼むときの形（ただの「続けて」は止めていた作業まで再開させうる・/clear すると続きが分からなくなる）
MANUAL_HINT = (
    "頼むなら『枠切れで中断した作業だけ続けて。止めるよう言われた作業（枠切れの間の指示も）・"
    "返事待ちの作業は再開しない』の形で（ただの『続けて』は不可・/clear はしない）"
)

# 同じ戻りの時刻へ頼んだのにまた枠切れ（戻りの時刻を過ぎても同じ枠がまだ戻っていなかった）のとき、
# もう1回だけ送るまでの間（秒）。2回頼んでも駄目なら諦める（積もるのは最大2通）
AGAIN_RETRY_SEC = 30 * 60
AGAIN_MAX_SENDS = 2


def resume_text(stop):
    """送る本文（どの枠が・いつ戻ったか入り）。"""
    return RESUME_TEXT.format(
        window=stop.get("window_label") or "利用の上限",
        resets=stop.get("resets_at") or "予定の時刻",
    )


def mode_of(settings):
    mode = (settings or {}).get("quota_resume_mode", DEFAULT_MODE)
    return mode if mode in MODES else DEFAULT_MODE


def stop_key(session_id, stop):
    """同じ枠切れを見分ける鍵（会話の番号と、枠切れの行の時刻）。"""
    return f"{session_id}|{stop.get('at') or stop.get('resets_at_epoch')}"


def _now_text():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def judge(session, stop, now):
    """送るか・送らないなら理由（試験・GET 用）。(送る, 理由)。"""
    if not stop:
        return False, "not_stopped"
    resets = stop.get("resets_at_epoch")
    if not isinstance(resets, (int, float)):
        return False, "reset_unknown"
    if now < resets + GRACE_SEC:
        return False, "not_yet"
    if now - resets > MAX_AGE_SEC:
        return False, "too_old"
    if not sm.is_input_ready(session):
        # 裏の仕事を持った入力待ち（shell）も入力待ちとして続きを頼む
        return False, "not_idle"
    updated = session.get("status_updated_at")
    if isinstance(updated, (int, float)) and now - updated / 1000.0 < IDLE_STABLE_SEC:
        return False, "settling"
    return True, ""


class QuotaResume:
    """枠切れの子へ続きを頼む見回り。"""

    def __init__(
        self,
        transcript_path_for,
        sender=None,
        notifier=None,
        state_file=None,
        quota_stop=None,
    ):
        self.transcript_path_for = transcript_path_for
        # 試験で差し替える
        self.sender = sender or sm.send_to_session
        self.quota_stop = quota_stop or accounts.quota_stop
        # 知らせ（kind, key, text）を受ける関数。None なら知らせない
        self.notifier = notifier
        # None なら使うたびに STATE_FILE を引く（試験が置き場を差し替えられるように・2026-11-02）
        self._state_file = state_file
        self._lock = threading.Lock()
        self._state = self._load()
        self._recent = []
        self._last_tick = None
        self._mode = DEFAULT_MODE

    # --- 記録 ---

    @property
    def state_file(self):
        return self._state_file or STATE_FILE

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("stops"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"stops": {}}

    def _save(self):
        cutoff = time.time() - KEEP_DAYS * 86400
        stops = self._state["stops"]
        for key in [k for k, v in stops.items() if v.get("at_epoch", 0) < cutoff]:
            del stops[key]
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_file)
        except OSError as e:
            safe_console.say(f"[QuotaResume] 記録を書けませんでした: {e}", flush=True)

    def _note(self, session, stop, outcome, detail=""):
        entry = {
            "ts": _now_text(),
            "outcome": outcome,
            "pid": session.get("pid"),
            "name": session.get("name") or "",
            "cwd": session.get("cwd") or "",
            "session_id": session.get("session_id"),
            "account": session.get("account") or "",
            "window": stop.get("window"),
            "resets_at": stop.get("resets_at"),
            "detail": detail,
        }
        self._recent.append(entry)
        del self._recent[:-RECENT_MAX]
        safe_console.say(
            f"[QuotaResume] {outcome} pid={entry['pid']} {entry['name'] or entry['cwd']}"
            f" {entry['window']}: {detail}",
            flush=True,
        )
        return entry

    def _tell(self, kind, key, text):
        if not self.notifier:
            return
        try:
            self.notifier(kind, key, text)
        except Exception as e:  # 知らせの失敗で見回りを止めない
            safe_console.say(f"[QuotaResume] 知らせに失敗: {e}", flush=True)

    def _watch_not_idle(self, session, stop, record, now, who):
        """戻ったのに入力待ちにならない子を、NOT_IDLE_NOTIFY_SEC 続いたら管理役へ1回知らせる。記録を変えたら True。

        確かな許可待ち・答え待ち（attention.certain）の子は liaison が別に知らせるので、ここでは知らせない
        （確かでない印で黙ると、どちらからも知らされない・評価役の中）。
        知らせた後も入力待ちになれば今までどおり続きを頼む（記録は終わりの状態にしない）。
        数える起点は、戻った後に初めて入力待ちでないと見た時刻（行き来しても戻さない）。
        """
        changed = False
        if "not_idle_since" not in record:
            record["not_idle_since"] = now
            changed = True
        if record.get("not_idle_told"):
            return changed
        if now - record["not_idle_since"] < NOT_IDLE_NOTIFY_SEC:
            return changed
        if (session.get("attention") or {}).get("certain"):
            return changed
        record["not_idle_told"] = True
        minutes = int((now - record["not_idle_since"]) // 60)
        self._note(
            session,
            stop,
            "not_idle",
            f"戻ってから {minutes} 分、入力待ちにならず続きを頼めていない（状態 {session.get('status')}）",
        )
        self._tell(
            "quota_not_idle",
            stop_key(session.get("session_id"), stop) + "|not_idle",
            f"{who}の{stop.get('window_label')}は {stop.get('resets_at')} に戻ったが、"
            f"戻ってから {minutes} 分、入力待ちにならず（いまの状態 {session.get('status')}）本アプリは続きを頼めていない。"
            f"入力待ちになれば頼む（6時間を過ぎたら頼まない）。固まっているなら手で確かめて。{MANUAL_HINT}",
        )
        return True

    # --- 見回り ---

    def tick(self, sessions, settings, now=None):
        """1回の見回り。送った（dry_run なら当たった）件数を返す。"""
        now = time.time() if now is None else now
        self._mode = mode_of(settings)
        self._last_tick = _now_text()
        if self._mode == "off":
            return 0
        count = 0
        changed = False
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
                safe_console.say(f"[QuotaResume] 会話記録を読めません: {e}", flush=True)
                continue
            if not stop:
                continue
            key = stop_key(session_id, stop)
            with self._lock:
                record = self._state["stops"].get(key)
            label = session.get("name") or os.path.basename(session.get("cwd") or "")
            who = f"{label}（pid {session.get('pid')}・{session.get('account') or '本家'}）"
            # 戻るまでに送られた文がまた枠切れになる（同じ戻りの時刻で何行も載る・
            # 2026-10-31 example-blog で4行）ので、管理役への知らせは戻りの時刻ごとに1回
            resets_key = f"{session_id}|{stop.get('resets_at_epoch')}"
            fresh = record is None
            if fresh:
                # 初めて見た枠切れ（戻る前でも記録する）
                record = {
                    "at_epoch": now,
                    "status": "waiting",
                    "attempts": 0,
                    "pid": session.get("pid"),
                    "name": session.get("name") or "",
                    "session_id": session_id,
                    "window": stop.get("window"),
                    "resets_at": stop.get("resets_at"),
                    "resets_at_epoch": stop.get("resets_at_epoch"),
                }
                if self._handed_before(session_id, stop):
                    # 同じ戻りの時刻の前の枠切れを管理役に任せていれば、この行も任せる（管理役が戻る前に手で頼み、
                    # また枠切れになった形。本アプリが頼むと二重になる・評価役の中）
                    record["handed_by_mode"] = True
                with self._lock:
                    self._state["stops"][key] = record
                changed = True
                # 本アプリが頼む形なら管理役の手は要らないので知らせない（管理役を起こすと
                # トークンを食う・評価役の指摘）。頼まない形のときだけ、手で頼む形を添えて知らせる
                plan = self._plan_text(stop, now)
                if plan:
                    self._tell(
                        "quota_gave_up",
                        resets_key,
                        f"{who}が{stop.get('window_label')}で止まった。{plan}",
                    )
                    if self._mode != "on":
                        record["handed_by_mode"] = True
            if record.get("status") in FINAL_STATUSES:
                continue
            if record.get("handed_by_mode") and self._mode == "on":
                # 試しの設定で見つけて管理役へ任せた枠切れ。知らせたとおり本アプリは頼まない
                record["status"] = "handed"
                changed = True
                self._note(
                    session,
                    stop,
                    "handed",
                    "見つけたときに試しの設定で管理役へ任せたので、on に戻っても頼まない",
                )
                continue
            if now < record.get("next_try_epoch", 0):
                continue
            ok, why = judge(session, stop, now)
            if not ok:
                if why == "too_old":
                    record["status"] = "too_old"
                    changed = True
                    self._note(
                        session,
                        stop,
                        "too_old",
                        "戻りの時刻から時間がたちすぎたので送らない",
                    )
                    # 見つけたときに既に過ぎていたものは、上の知らせで済んでいる
                    if not fresh:
                        self._tell(
                            "quota_gave_up",
                            resets_key,
                            f"{who}の{stop.get('window_label')}は {stop.get('resets_at')} に戻ったが、"
                            f"6時間を過ぎたので本アプリは続きを頼まない。止まったまま。{MANUAL_HINT}",
                        )
                elif why == "not_idle" and self._mode == "on":
                    # dry_run では頼まない（見つけたときに「試しなので頼まない」と知らせ済み）ので、
                    # 「入力待ちになれば頼む」と知らせると実際と食い違う（評価役の中）
                    changed |= self._watch_not_idle(session, stop, record, now, who)
                continue
            sent_before = self._already_sent(session_id, stop, key)
            if sent_before >= AGAIN_MAX_SENDS:
                # 同じ戻りの時刻へ2回頼んでも、また枠切れになった。鍵（枠切れの行の時刻）が変わるたびに
                # 送り直すと同じ文が積もるので、もう頼まない
                record["status"] = "again"
                changed = True
                self._note(
                    session, stop, "again", "2回頼んだ後もまた同じ戻りの時刻で枠切れ"
                )
                self._tell(
                    "quota_gave_up",
                    resets_key + "|again",
                    f"{who}へ{stop.get('window_label')}が戻ったと続きを {sent_before} 回頼んだが、"
                    f"戻りの時刻（{stop.get('resets_at')}）を過ぎても同じ枠がまだ戻っていなかった。"
                    f"本アプリはもう頼まない。止まったまま。{MANUAL_HINT}",
                )
                continue
            if sent_before and not record.get("again_wait_until"):
                # 戻りの時刻の直後に頼んだが、まだ戻っていなかった（時計のずれ・サーバー側の遅れ）。
                # 間をおいてもう1回だけ頼む
                until = self._inherited_wait(session_id, stop) or now + AGAIN_RETRY_SEC
                record["again_wait_until"] = until
                record["next_try_epoch"] = until
                changed = True
                self._note(
                    session,
                    stop,
                    "again_wait",
                    f"頼んだ後もまだ枠切れ。{AGAIN_RETRY_SEC // 60}分あけてもう1回頼む",
                )
                continue
            if self._mode == "dry_run":
                record["status"] = "would_send"
                changed = True
                self._note(session, stop, "would_send", "送るはずだった（dry_run）")
                # 見つけたときは on で知らせず、その後に試しの設定へ切り替わったなら、誰にも知らされず止まったままになる。
                # 見つけたときに同じ鍵で知らせ済みなら liaison が二度は送らない（5.31節の評価で残した低・2026-11-02）
                self._tell(
                    "quota_gave_up",
                    resets_key,
                    f"{who}の{stop.get('window_label')}は {stop.get('resets_at')} に戻ったが、"
                    f"試しの設定（quota_resume_mode=dry_run）なので本アプリは（これ以上）続きを頼まない。止まったまま。{MANUAL_HINT}",
                )
                count += 1
                continue
            if self._send(session, stop, record, key, now):
                count += 1
            changed = True
        if changed:
            with self._lock:
                self._save()
        return count

    def _plan_text(self, stop, now):
        """本アプリが頼まない形なら、管理役への知らせの後半（手で頼む形）。頼む形なら空。"""
        resets = stop.get("resets_at_epoch")
        if not isinstance(resets, (int, float)):
            return "戻る時刻が分からないので本アプリは頼まない。" + MANUAL_HINT
        when = f"戻り {stop.get('resets_at')}。"
        if self._mode != "on":
            return (
                when
                + f"試しの設定（quota_resume_mode={self._mode}）なので本アプリは頼まない。"
                + MANUAL_HINT
            )
        if now - resets > MAX_AGE_SEC:
            # 本アプリが止まっていた等で、見つけたときには6時間を過ぎていた
            return when + "6時間を過ぎていたので本アプリは頼まない。" + MANUAL_HINT
        return ""

    def _handed_before(self, session_id, stop):
        """同じ会話・同じ戻りの時刻の枠切れを、試しの設定で管理役に任せた記録があるか。"""
        resets = stop.get("resets_at_epoch")
        with self._lock:
            return any(
                v.get("handed_by_mode")
                and v.get("session_id") == session_id
                and v.get("resets_at_epoch") == resets
                for v in self._state["stops"].values()
            )

    def _already_sent(self, session_id, stop, key):
        """同じ会話・同じ戻りの時刻へ、別の枠切れの行として既に頼んだ回数。"""
        resets = stop.get("resets_at_epoch")
        with self._lock:
            return sum(
                1
                for k, v in self._state["stops"].items()
                if k != key
                and v.get("status") == "sent"
                and v.get("session_id") == session_id
                and v.get("resets_at_epoch") == resets
            )

    def _send(self, session, stop, record, key, now):
        pid = session.get("pid")
        session_id = session.get("session_id")
        text = resume_text(stop)
        target = None
        offsets = {}
        try:
            # ここで落ちても、その回の残りの子の見回りと記録の保存を止めない
            target = send_history.target_of(session)
            offsets = sm._transcript_sizes(session_id)
            result = self.sender(
                pid, text=text, preamble="", allow_unmanaged=True, protect_draft=True
            )
        except sm.SessionBusyError:
            # 送る直前に作業へ戻った（人が話しかけた等）。次の見回りで見直す
            return False
        except sm.SessionDraftError:
            # 人が入力欄に書いている。消さずに間をおく（回数には数えない）
            record["next_try_epoch"] = now + DRAFT_RETRY_SEC
            self._note(
                session, stop, "draft_present", "入力欄に書きかけがあるので後で送る"
            )
            return False
        except sm.SessionError as e:
            if not sm._transcript_has_prompt(session_id, text, offsets):
                return self._failed(session, stop, record, key, now, target, text, e)
            result = {"via": None}
        except Exception as e:  # 見回りを止めない
            # 届いていたのに例外になった1通を「送れなかった」と数えると、頼み直しの数え（again）から漏れる
            try:
                landed = bool(offsets) and sm._transcript_has_prompt(
                    session_id, text, offsets
                )
            except Exception:
                landed = False
            if not landed:
                return self._failed(session, stop, record, key, now, target, text, e)
            result = {"via": None}
        record["status"] = "sent"
        record["sent_epoch"] = now
        record["attempts"] = record.get("attempts", 0) + 1
        send_history.record(
            "quota_resume",
            ok=True,
            target=target,
            text=text,
            source="quota_resume",
            detail={"window": stop.get("window"), "via": (result or {}).get("via")},
        )
        detail = f"{stop.get('window_label')}が戻ったので、枠切れで中断した作業の続きを頼んだ"
        self._note(
            session, stop, "sent", f"{detail}（via {(result or {}).get('via')}）"
        )
        # 頼めたことは管理役へは知らせない（手は要らない。GET /api/quota-resume と送った履歴で見られる）
        return True

    def _failed(self, session, stop, record, key, now, target, text, error):
        record["attempts"] = record.get("attempts", 0) + 1
        reason = str(error).split("画面の末尾:")[0][:200]
        send_history.record(
            "quota_resume",
            ok=False,
            target=target,
            text=text,
            source="quota_resume",
            error=reason,
            code=type(error).__name__,
        )
        if record["attempts"] >= MAX_ATTEMPTS:
            record["status"] = "failed"
            self._note(
                session,
                stop,
                "failed",
                f"{record['attempts']} 回送れず諦めた: {reason}",
            )
            label = session.get("name") or os.path.basename(session.get("cwd") or "")
            self._tell(
                "quota_resume_failed",
                key,
                f"{label}（pid {session.get('pid')}）へ枠が戻った知らせを {record['attempts']} 回"
                f"送れなかった（{reason}）。止まったまま。" + MANUAL_HINT,
            )
        else:
            record["next_try_epoch"] = now + RETRY_SEC * record["attempts"]
            self._note(
                session, stop, "send_failed", f"送れなかった（後で送り直す）: {reason}"
            )
        return False

    def state_for(self, session_id, stop):
        """その枠切れへの続きの依頼の状態（waiting / again_wait / sent / failed / would_send / too_old / again。
        見ていなければ None）。again_wait は、一度頼んだ後にまた枠切れになり、もう1回頼むまで待っているもの。
        """
        if not session_id or not stop:
            return None
        with self._lock:
            record = self._state["stops"].get(stop_key(session_id, stop))
        if not record:
            return None
        if record.get("status") == "waiting" and record.get("again_wait_until"):
            return "again_wait"
        return record.get("status")

    def again_wait_text(self, session_id, stop):
        """again_wait のとき、もう1回頼む時刻（画面の文用・例 23:40）。それ以外は None。"""
        with self._lock:
            record = self._state["stops"].get(stop_key(session_id, stop)) or {}
        until = record.get("again_wait_until")
        if record.get("status") != "waiting" or not until:
            return None
        return datetime.fromtimestamp(until).strftime("%H:%M")

    def _inherited_wait(self, session_id, stop):
        """同じ会話・同じ戻りの時刻の、前の行に付いた頼み直しの待ちの期限（無ければ None）。

        待ちの間に届いた文がまた枠切れになり新しい行が載るたびに、30分を一から数え直さないため。
        """
        resets = stop.get("resets_at_epoch")
        with self._lock:
            waits = [
                v["again_wait_until"]
                for v in self._state["stops"].values()
                if v.get("again_wait_until")
                and v.get("session_id") == session_id
                and v.get("resets_at_epoch") == resets
            ]
        return max(waits) if waits else None

    def status(self):
        with self._lock:
            # 記録ごとに写す（見回りが錠の外で記録の dict に鍵を足すので、共有したまま返さない）
            stops = {k: dict(v) for k, v in self._state["stops"].items()}
        return {
            "mode": self._mode,
            "last_tick": self._last_tick,
            "stops": stops,
            "recent": list(self._recent),
        }

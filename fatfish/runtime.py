"""Runtime: two lives on one loop, one memory, one set of rules."""
from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import time
from pathlib import Path

from . import guard
from .bilibili import BiliClient, BiliError, BiliRisk
from .config import Config
from .llm import LLM
from .memory import Memory
from .mind import Brain
from .persona import Persona
from .policy import Policy
from .qq import QQService
from .report import daily_report
from .state import StateStore
from .util import clean_text, day_str, human_ago, in_window, now, pid_alive, setup_logging


class Runtime:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = setup_logging(cfg.path("paths.log_dir"), str(cfg.get("logging.level", "INFO")))
        self.data_dir: Path = cfg.path("paths.data_dir")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.memory = Memory(cfg.path("paths.db_file"))
        self.persona = Persona.load(cfg.path("identity.persona_file"))
        self.persona.seed_interests(self.memory)
        self.state = StateStore(self.memory)
        self.llm = LLM(cfg, self.memory, self.log)
        self.brain = Brain(cfg, self.persona, self.memory, self.state, self.llm, self.log)
        self.policy = Policy(cfg, self.memory)
        self.bili = BiliClient(cfg, self.memory, self.persona, self.log)
        self.qq: QQService | None = None
        if cfg.get("qq.enabled"):
            self.qq = QQService(cfg, self.memory, self.policy, self.brain, self.state, self.log)
            self.qq.bili = self.bili

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self.last_bili_at = 0
        self.lock_path = self.data_dir / "fish.lock"

    def acquire_instance_lock(self) -> None:
        """Refuse to run two fish at once: duplicate bots double every write."""
        if self.lock_path.exists():
            try:
                other = int(self.lock_path.read_text(encoding="utf-8").strip() or 0)
            except Exception:
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                raise RuntimeError(f"已经有一个大肥鱼在运行（PID {other}）。先停止它，或删除 {self.lock_path}。")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    def release_instance_lock(self) -> None:
        try:
            if self.lock_path.exists() and self.lock_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                self.lock_path.unlink()
        except OSError:
            pass

    # ----------------------------------------------------------- startup
    def _next_bili_at(self) -> float:
        value = float(self.memory.kv_get("next_bili_at", 0) or 0)
        if not value:
            value = time.time() + random.uniform(90, 240)
            self.memory.kv_set("next_bili_at", value)
        return value

    def _schedule_bili(self, minutes: tuple[float, float] | None = None) -> None:
        lo, hi = minutes or (
            float(self.cfg.get("bilibili.browse.min_gap_minutes", 40)),
            float(self.cfg.get("bilibili.browse.max_gap_minutes", 95)),
        )
        if hi < lo:
            hi = lo
        self.memory.kv_set("next_bili_at", time.time() + random.uniform(lo, hi) * 60)

    # -------------------------------------------------------------- run
    async def run(self) -> None:
        self.acquire_instance_lock()
        self.log.info("=" * 64)
        self.log.info(
            "大肥鱼启动 | mode=%s | live=%s | LLM=%s | 人设=%s",
            self.cfg.mode,
            self.cfg.live,
            self.cfg.get("llm.model") if self.llm.available else "离线",
            self.persona.name,
        )
        self.log.info(
            "B站=%s（登录：%s）| QQ=%s（白名单：%s）",
            self.cfg.get("bilibili.enabled"),
            "是" if self.bili.logged_in else "否，只读",
            self.cfg.get("qq.enabled"),
            self.cfg.get("qq.groups") or "空（只听不说）",
        )
        for problem in self.cfg.validate():
            self.log.warning("配置提醒：%s", problem)
        try:
            await self.bili.maybe_refresh()
            await self.bili.ensure_buvid()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("B 站 cookie 初始化跳过：%s", exc)
        self.log.info("=" * 64)

        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, lambda *_: asyncio.get_event_loop().call_soon_threadsafe(self._stop.set))
            except Exception:
                pass

        self._tasks = [
            asyncio.create_task(self.bili_loop(), name="bilibili"),
            asyncio.create_task(self.mind_loop(), name="mind"),
        ]
        if self.cfg.get("bilibili.comments.enabled", True):
            self._tasks.append(asyncio.create_task(self.comment_loop(), name="comment-replies"))
        if self.cfg.get("bilibili.notifications.enabled", True):
            self._tasks.append(asyncio.create_task(self.bili_notify_loop(), name="bili-notifications"))
        if self.cfg.get("bilibili.dm.enabled", True) and int(self.cfg.get("bilibili.dm.user_mid", 0) or 0):
            self._tasks.append(asyncio.create_task(self.bili_dm_loop(), name="bili-dm"))
        if (
            self.cfg.get("bilibili.proactive.enabled", True)
            and int(self.cfg.get("bilibili.dm.user_mid", 0) or 0)
            and self.bili.logged_in
        ):
            self._tasks.append(asyncio.create_task(self.bili_proactive_loop(), name="bili-proactive"))
        if self.qq and self.cfg.get("qq.proactive.enabled", True):
            self._tasks.append(asyncio.create_task(self.proactive_loop(), name="qq-proactive"))
        if self.qq and self.cfg.get("qq.enabled"):
            await self.qq.start()

        try:
            await self._stop.wait()
        finally:
            try:
                await self.shutdown()
            finally:
                self.release_instance_lock()

    async def shutdown(self) -> None:
        self.log.info("正在收摊……")
        for t in self._tasks:
            t.cancel()
        if self.qq:
            await self.qq.stop()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        await self.llm.close()
        self.state.save()
        self.memory.close()
        self.log.info("已收摊。")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------- bilibili
    async def proactive_loop(self) -> None:
        """Every few hours, send one private opener if the owner is quiet."""
        first = float(self.cfg.get("qq.proactive.first_delay_minutes", 15) or 15) * 60
        await asyncio.sleep(first)
        while not self._stop.is_set():
            delay = 30 * 60
            try:
                start, end = (self.cfg.get("qq.proactive.active_hours") or ["09:30", "23:30"])[:2]
                if self.qq and in_window(now(), start, end):
                    sent = await self.qq.proactive_private()
                    if sent:
                        lo = float(self.cfg.get("qq.proactive.min_gap_minutes", 120) or 120)
                        hi = float(self.cfg.get("qq.proactive.max_gap_minutes", 240) or 240)
                        delay = random.uniform(min(lo, hi), max(lo, hi)) * 60
                        self.log.info("下一次主动私聊约 %.0f 分钟后", delay / 60)
                    else:
                        delay = 15 * 60
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("主动私聊循环失败：%s", exc)
                delay = 15 * 60
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def bili_proactive_loop(self) -> None:
        """Randomly open a Bilibili DM when the owner has been quiet."""
        mid = int(self.cfg.get("bilibili.dm.user_mid", 0) or 0)
        first = random.uniform(5, max(6, float(self.cfg.get("bilibili.proactive.first_delay_minutes", 10) or 10))) * 60
        await asyncio.sleep(first)
        while not self._stop.is_set():
            delay = random.uniform(20, 40) * 60
            try:
                start, end = (self.cfg.get("bilibili.proactive.active_hours") or ["09:00", "23:30"])[:2]
                if not in_window(now(), start, end):
                    delay = 30 * 60
                elif await self.bili_proactive_once(mid):
                    lo = float(self.cfg.get("bilibili.proactive.min_gap_minutes", 45) or 45)
                    hi = float(self.cfg.get("bilibili.proactive.max_gap_minutes", 150) or 150)
                    delay = random.uniform(min(lo, hi), max(lo, hi)) * 60
                    self.log.info("下一次 B 站主动私信约 %.0f 分钟后", delay / 60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("B 站主动私信循环失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def bili_proactive_once(self, mid: int) -> bool:
        if not self.bili.logged_in:
            return False
        target = f"bili_dm:{mid}"
        recent = self.memory.recent_group_msgs(target, 10)
        for m in recent:
            m["is_self"] = bool(self.bili.uid and str(m.get("user_id")) == str(self.bili.uid))
        user_msgs = [m for m in recent if not m.get("is_self")]
        last_user = int(user_msgs[-1].get("ts") or 0) if user_msgs else 0
        idle = float(self.cfg.get("bilibili.proactive.min_idle_minutes", 60) or 0)
        if last_user and (time.time() - last_user) < idle * 60:
            return False
        cap = int(self.cfg.get("bilibili.proactive.max_per_day", 4) or 0)
        used = self.memory.count_actions("bili_dm_proactive", day=day_str(), channel="bilibili")
        if cap > 0 and used >= cap:
            return False
        gap = float(self.cfg.get("bilibili.proactive.min_gap_minutes", 45) or 0)
        last = self.memory.last_action_ts("bili_dm_proactive", target=str(mid))
        if gap and last and (time.time() - last) < gap * 60:
            return False
        text = await self.brain.proactive_private(recent)
        if not text:
            return False
        try:
            await self.bili.send_dm(mid, text)
        except (BiliRisk, BiliError) as exc:
            self.log.warning("B 站主动私信发送失败：%s", exc)
            return False
        self.memory.add_group_msg(target, str(self.bili.uid or "self"), "大肥鱼", text, direct_at=True)
        self.memory.add_episode(
            channel="bilibili",
            kind="say",
            summary=text,
            ref=target,
            title="B站私信",
            reaction="主动找你",
            meta={"tool": "bili_dm_proactive"},
        )
        self.memory.record_action("bilibili", "bili_dm_proactive", target=str(mid), detail=text, result="ok")
        self.state.after_speak(len(text))
        self.log.info("B 站主动私信：%d 字", len(text))
        return True

    async def bili_dm_loop(self) -> None:
        mid = int(self.cfg.get("bilibili.dm.user_mid", 0) or 0)
        interval = max(20, int(self.cfg.get("bilibili.dm.check_seconds", 60) or 60))
        while not self._stop.is_set():
            try:
                await self.check_bili_dm(mid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("B 站私信轮询失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def check_bili_dm(self, mid: int) -> None:
        # The session-list endpoint updates before fetch_history; poll it first.
        latest_hint = None
        try:
            latest_hint = await self.bili.dm_latest(mid)
        except BiliError as exc:
            self.log.debug("B 站私信会话刷新失败：%s", exc)
        messages = await self.bili.dm_messages(mid)
        parsed: list[dict] = []
        for m in messages:
            if int(m.get("msg_type") or 0) != 1:
                continue
            raw = str(m.get("content") or "")
            try:
                body = json.loads(raw)
                text = str(body.get("content") or "") if isinstance(body, dict) else raw
            except Exception:
                text = raw
            text = clean_text(text, 500)
            if not text:
                continue
            parsed.append(
                {
                    "sender": int(m.get("sender_uid") or 0),
                    "ts": int(m.get("timestamp") or 0),
                    "text": text,
                }
            )
        if latest_hint and latest_hint.get("text"):
            if not any(
                m["ts"] == latest_hint["ts"] and m["text"] == latest_hint["text"] for m in parsed
            ):
                parsed.append(latest_hint)
                self.log.info("B 站私信：会话列表先拿到新消息")
        if not parsed:
            return
        parsed.sort(key=lambda m: m["ts"])
        last = int(self.memory.kv_get("bili_dm_last_ts", 0) or 0)
        newest = max(m["ts"] for m in parsed)
        if not last:
            self.memory.kv_set("bili_dm_last_ts", newest)
            self.log.info("B 站私信：从 %s 开始监听", time.strftime("%H:%M:%S", time.localtime(newest)))
            return
        fresh = [m for m in parsed if m["ts"] > last]
        if not fresh:
            return
        target = f"bili_dm:{mid}"
        for m in fresh:
            is_self = m["sender"] == int(self.bili.uid or 0)
            self.memory.add_group_msg(
                target,
                str(m["sender"]),
                "大肥鱼" if is_self else "主人B站",
                m["text"],
                ts=m["ts"],
                direct_at=True,
            )
        latest = fresh[-1]
        if latest["sender"] == int(self.bili.uid or 0):
            self.memory.kv_set("bili_dm_last_ts", max(last, newest))
            return
        recent = self.memory.recent_group_msgs(target, 10)
        for m in recent:
            m["is_self"] = bool(self.bili.uid and str(m.get("user_id")) == str(self.bili.uid))
        reply = await self.brain.dm_reply("主人B站", latest["text"], recent)
        if not reply:
            # Do not mark it read: the next poll must retry this same message.
            return
        parts = [p.strip() for p in reply.split("||") if p.strip()][:2] or [reply]
        try:
            for i, part in enumerate(parts):
                await self.bili.send_dm(mid, part)
                if i == 0 and len(parts) > 1:
                    await asyncio.sleep(random.uniform(1.2, 2.6))
        except (BiliRisk, BiliError) as exc:
            self.log.warning("B 站私信回复失败：%s", exc)
            return
        self.memory.kv_set("bili_dm_last_ts", max(last, newest))
        self.memory.add_group_msg(target, str(self.bili.uid or "self"), "大肥鱼", " || ".join(parts), direct_at=True)
        self.memory.add_episode(
            channel="bilibili",
            kind="say",
            summary=reply,
            ref=target,
            title="B站私信",
            reaction=f"回复：{latest['text'][:60]}",
            meta={"tool": "bili_dm"},
        )
        self.memory.record_action("bilibili", "bili_dm", target=str(mid), detail=reply, result="ok")
        self.state.after_speak(len(reply))
        self.log.info("B 站私信回复：%d 字", len(reply))

    def _comment_worthy_today(self) -> int:
        threshold = float(self.cfg.get("bilibili.write.min_score.comment", 70))
        return sum(
            1
            for e in self.memory.episodes_for_day(day_str())
            if e["channel"] == "bilibili"
            and e["kind"] == "watch"
            and float(e.get("score") or 0) >= threshold
        )

    async def bili_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.cfg.get("bilibili.enabled"):
                    await asyncio.sleep(300)
                    continue
                due = self._next_bili_at()
                if time.time() < due:
                    await asyncio.sleep(min(60, max(5, due - time.time())))
                    continue
                start, end = (self.cfg.get("bilibili.browse.active_hours") or ["09:30", "23:30"])[:2]
                if not in_window(now(), start, end):
                    self.log.debug("不在活跃时段（%s-%s），继续睡", start, end)
                    await asyncio.sleep(600)
                    continue
                worthy_cap = int(self.cfg.get("bilibili.browse.max_comment_worthy_per_day", 30))
                if worthy_cap > 0 and self._comment_worthy_today() >= worthy_cap:
                    self.log.info("今天值得评论的视频已经 %d 个，收工", worthy_cap)
                    self._schedule_bili((120, 240))
                    continue
                if self.memory.watched_count_today(day_str()) >= int(self.cfg.get("bilibili.browse.max_videos_per_day", 60)):
                    self.log.info("今天看够了，明天再说")
                    self._schedule_bili((120, 240))
                    continue
                ok, why = self.policy.llm_budget_ok()
                if not ok:
                    self.log.warning(why)
                    self._schedule_bili((180, 300))
                    continue
                batch = max(1, int(self.cfg.get("bilibili.browse.batch_size", 3) or 1))
                for _ in range(batch):
                    if worthy_cap > 0 and self._comment_worthy_today() >= worthy_cap:
                        break
                    if self.memory.watched_count_today(day_str()) >= int(
                        self.cfg.get("bilibili.browse.max_videos_per_day", 60)
                    ):
                        break
                    result = await self.bili_cycle()
                    if not result.get("watched"):
                        break
                self._schedule_bili()
            except asyncio.CancelledError:
                raise
            except BiliRisk as exc:
                hours = float(self.cfg.get("safety.risk_pause_hours", 12))
                self.policy.pause_for_risk(hours)
                self.log.error("触发 B 站风控，暂停 %s 小时：%s", hours, exc)
                self._schedule_bili((240, 420))
            except BiliError as exc:
                self.log.warning("B 站这轮没跑成：%s", exc)
                self._schedule_bili((45, 90))
            except Exception as exc:  # noqa: BLE001
                self.log.exception("B 站循环出错，稍后再试：%s", exc)
                self._schedule_bili((45, 90))
        self.log.info("B 站循环结束")

    async def bili_cycle(self, force_observe: bool | None = None, bvid: str | None = None) -> dict:
        self.policy.force_observe_bili = bool(force_observe) if force_observe is not None else False
        self.last_bili_at = int(time.time())
        await self.bili.ensure_buvid()
        if bvid:
            pick = {"bvid": bvid, "pick_why": "手动指定"}
            detail = await self.bili.fetch_detail(pick)
        else:
            cands = await self.bili.candidates()
            self.log.info("候选 %d 条", len(cands))
            picks = await self.brain.pick(cands)
            if not picks:
                self.memory.add_episode(channel="bilibili", kind="skip", summary="这轮没有想看的", meta={"candidates": len(cands)})
                return {"watched": None, "actions": []}
            pick = picks[0]
            detail = await self.bili.fetch_detail(pick)
        exp = await self.brain.experience(detail)
        score = float(exp.get("genuine_interest", 0))
        self.log.info(
            "看《%s》%s | 兴趣 %.0f | %s | 互动：%s",
            detail["title"],
            exp.get("how_much", ""),
            score,
            exp.get("reaction", "")[:60],
            {k: v for k, v in (exp.get("interact") or {}).items() if v},
        )

        self.memory.upsert_video(
            detail["bvid"],
            title=detail["title"],
            up_name=detail["up"],
            up_mid=detail.get("up_mid", 0),
            duration=detail.get("duration", 0),
            pubdate=detail.get("pubdate", 0),
            topics=exp.get("topics") or [],
        )
        if detail.get("up_mid"):
            self.memory.touch_peer("bilibili", str(detail["up_mid"]), detail["up"], delta=0.05)
        self.memory.mark_watched(detail["bvid"], "watched", score, exp.get("topics") or [])
        self.memory.kv_set("last_watched_bvid", detail["bvid"])
        self.memory.add_episode(
            channel="bilibili",
            kind="watch",
            ref=detail["bvid"],
            title=detail["title"],
            summary=exp.get("what_it_is") or "",
            reaction=exp.get("reaction") or "",
            score=score,
            topics=exp.get("topics") or [],
            meta={
                "how_much": exp.get("how_much"),
                "evidence": exp.get("evidence"),
                "interact_reason": exp.get("interact_reason"),
                "pick_why": pick.get("pick_why"),
                "up": detail["up"],
                "duration": detail.get("duration"),
                "url": f"https://www.bilibili.com/video/{detail['bvid']}",
            },
        )
        self._learn_topics(exp.get("topics") or [], score)
        actions = await self._apply_interactions(detail, exp)
        social = await self._reply_other_fish(detail, score)
        if not social:
            social = await self._join_comment(detail, score)
        actions += social
        self.state.after_watch(score, bool(actions))
        self.policy.force_observe_bili = False
        return {"watched": detail["title"], "score": score, "actions": actions}

    async def _join_comment(self, detail: dict, score: float) -> list[str]:
        """Proactively join one other user's comment thread, with a strict daily cap."""
        if not self.cfg.get("bilibili.join_comments.enabled", True):
            return []
        if not self.cfg.live or self.policy.force_observe_bili or not self.bili.logged_in:
            return []
        minimum = float(self.cfg.get("bilibili.join_comments.min_video_score", 65) or 65)
        if score < minimum:
            return []
        cap = int(self.cfg.get("bilibili.join_comments.max_per_day", 6) or 0)
        used = self.memory.count_actions("join_comment", day=day_str(), channel="bilibili")
        if cap > 0 and used >= cap:
            return []
        gap = float(self.cfg.get("bilibili.join_comments.gap_minutes", 20) or 0)
        last = self.memory.last_action_ts("join_comment")
        if gap and last and (time.time() - last) < gap * 60:
            return []
        owner = {str(x).strip().lower() for x in self.persona.special_names()}
        ai_related = self.brain._ai_related(detail)
        for c in detail.get("comments") or []:
            name = str(c.get("user") or "")
            rpid = int(c.get("rpid") or 0)
            if not name or not rpid or name.strip().lower() in owner:
                continue
            if int(c.get("mid") or 0) == int(self.bili.uid or 0):
                continue
            if self.memory.kv_get(f"comment_joined:{rpid}"):
                continue
            text = await self.brain.join_comment(str(detail.get("title") or ""), c, ai_related=ai_related)
            if not text:
                continue
            findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
            if findings:
                self.log.info("楼中楼接话含风险词，跳过")
                return []
            try:
                res = await self.bili.reply_comment(int(detail.get("aid") or 0), rpid, rpid, text)
            except (BiliRisk, BiliError) as exc:
                self.log.warning("楼中楼接话失败：%s", exc)
                return []
            self.memory.kv_set(f"comment_joined:{rpid}", int(time.time()))
            self.memory.record_action(
                "bilibili", "join_comment", str(detail.get("bvid") or ""), text,
                result=res.get("status", "ok"),
            )
            self.memory.add_episode(
                channel="bilibili",
                kind="join_comment",
                ref=str(detail.get("bvid") or ""),
                title=f"接 {name} 的评论",
                summary=text,
                reaction=f"对方：{str(c.get('text') or '')[:80]}",
                meta={"rpid": rpid, "target": name},
            )
            self.log.info("主动接评论 %s：%s", name, text)
            return ["join_comment"]
        return []

    async def _reply_other_fish(self, detail: dict, score: float) -> list[str]:
        """Reply once to another '大肥鱼/鱼鱼' comment, guarded against bot loops."""
        if not self.cfg.get("bilibili.other_fish.enabled", True):
            return []
        if not self.cfg.live or self.policy.force_observe_bili or not self.bili.logged_in:
            return []
        minimum = float(self.cfg.get("bilibili.other_fish.min_video_score", 70) or 70)
        if score < minimum:
            return []
        day = day_str()
        cap = int(self.cfg.get("bilibili.other_fish.max_per_day", 8) or 0)
        used = self.memory.count_actions("other_fish_reply", day=day, channel="bilibili")
        if cap > 0 and used >= cap:
            return []
        gap = float(self.cfg.get("bilibili.other_fish.gap_minutes", 20) or 0)
        last = self.memory.last_action_ts("other_fish_reply")
        if gap and last and (time.time() - last) < gap * 60:
            return []
        patterns = [str(x) for x in (self.cfg.get("bilibili.other_fish.names") or []) if str(x)]
        if not patterns:
            return []
        owner = {str(x).strip().lower() for x in self.persona.special_names()}
        for c in detail.get("comments") or []:
            name = str(c.get("user") or "")
            if not name or name.strip().lower() in owner:
                continue
            if int(c.get("mid") or 0) == int(self.bili.uid or 0):
                continue
            if not any(p in name for p in patterns):
                continue
            rpid = int(c.get("rpid") or 0)
            if not rpid or self.memory.kv_get(f"other_fish_replied:{rpid}"):
                continue
            text = await self.brain.other_fish_reply(str(detail.get("title") or ""), c)
            if not text:
                continue
            findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
            if findings:
                self.log.info("同类评论回复含风险词，跳过")
                return []
            try:
                res = await self.bili.reply_comment(
                    int(detail.get("aid") or 0), rpid, rpid, text
                )
            except (BiliRisk, BiliError) as exc:
                self.log.warning("同类评论回复失败：%s", exc)
                return []
            self.memory.kv_set(f"other_fish_replied:{rpid}", int(time.time()))
            self.memory.record_action(
                "bilibili", "other_fish_reply", str(detail.get("bvid") or ""), text,
                result=res.get("status", "ok"),
            )
            self.memory.add_episode(
                channel="bilibili",
                kind="other_fish_reply",
                ref=str(detail.get("bvid") or ""),
                title=f"回复 {name}",
                summary=text,
                reaction=f"对方：{str(c.get('text') or '')[:80]}",
                meta={"rpid": rpid, "target": name},
            )
            self.log.info("回复同类 %s：%s", name, text)
            return ["other_fish_reply"]
        return []

    def _learn_topics(self, topics: list[str], score: float) -> None:
        known = {row["topic"]: row for row in self.persona.interest_rows(self.memory)}
        delta = max(-0.04, min(0.04, (score - 50) / 1250))
        hit = False
        for t in topics:
            for topic in known:
                if t and (t in topic or topic in t):
                    self.memory.bump_interest(topic, delta, hit=True)
                    hit = True
                    break
        if not hit:
            # Keep the strongest signal alive even when the model uses a new label.
            best = max(known.values(), key=lambda r: float(r.get("weight", 0)), default=None)
            if best and score >= 65:
                self.memory.bump_interest(best["topic"], delta * 0.5, hit=True)

    async def _apply_interactions(self, detail: dict, exp: dict) -> list[str]:
        want = exp.get("interact") or {}
        score = float(exp.get("genuine_interest", 0))
        done: list[str] = []
        bvid = detail["bvid"]

        if not self.bili.logged_in and self.cfg.live and not self.policy.force_observe_bili and any(
            want.get(k) for k in ("like", "coin", "favorite", "danmaku", "comment", "follow")
        ):
            self.log.info("B 站未登录，这轮只想不做")
            for action in ("like", "coin", "favorite", "danmaku", "comment", "follow"):
                if want.get(action):
                    self.memory.record_action(
                        "bilibili", action, bvid, "未登录，跳过", result="skipped", dry_run=True
                    )
            return done

        async def attempt(action: str, text: str = "") -> None:
            findings = (
                guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
                if text
                else []
            )
            verdict = self.policy.check_bili_write(action, bvid, score, findings, text=text)
            if not verdict.allowed:
                self.memory.record_action("bilibili", action, bvid, verdict.reason, result="blocked", dry_run=True)
                self.log.info("不做 %s：%s", action, verdict.reason)
                return
            if verdict.dry_run:
                self.memory.record_action(
                    "bilibili", action, bvid, text or exp.get("interact_reason", ""), result="would", dry_run=True
                )
                self.log.info("[observe] 本来想 %s：%s", action, text or "")
                return
            try:
                if action == "like":
                    res = await self.bili.like(bvid)
                elif action == "coin":
                    res = await self.bili.coin(bvid, 1)
                elif action == "favorite":
                    res = await self.bili.favorite(bvid, int(self.cfg.get("bilibili.favorite_media_id", 0)))
                elif action == "danmaku":
                    at = self._danmaku_moment(detail)
                    res = await self.bili.danmaku(bvid, int(detail["cid"]), text, at=at)
                elif action == "comment":
                    res = await self.bili.send_comment(bvid, int(detail["aid"]), text)
                elif action == "follow":
                    res = await self.bili.follow(int(detail.get("up_mid") or 0))
                else:
                    return
                self.memory.record_action("bilibili", action, bvid, text or res.get("detail", ""), result=res.get("status", "ok"))
                if res.get("status") == "ok":
                    done.append(action)
                    if action == "comment" and res.get("rpid"):
                        self.memory.add_my_comment(bvid, int(detail["aid"]), int(res["rpid"]), text)
                    self.log.info("已 %s：%s", action, text or "")
                else:
                    self.log.info("%s 跳过：%s", action, res.get("detail"))
            except BiliRisk:
                raise
            except BiliError as exc:
                self.memory.record_action("bilibili", action, bvid, str(exc), result="error", dry_run=True)
                self.log.warning("%s 失败：%s", action, exc)

        order = [
            ("like", bool(want.get("like"))),
            ("coin", bool(want.get("coin"))),
            ("favorite", bool(want.get("favorite"))),
            ("danmaku", bool(want.get("danmaku"))),
            ("comment", bool(want.get("comment"))),
            ("follow", bool(want.get("follow"))),
        ]
        for action, proposed in order:
            if not proposed:
                continue
            text = ""
            if action in ("danmaku", "comment"):
                text = str(want.get(action) or "").strip()
                if not text:
                    continue
                if action == "comment":
                    mention = str(self.cfg.get("bilibili.write.comment_mention") or "").strip()
                    if mention and mention not in text:
                        text = f"@{mention} {text}"
            await attempt(action, text)
        return done

    def _danmaku_moment(self, detail: dict) -> float:
        dms = detail.get("danmaku") or []
        if dms:
            return float(random.choice(dms)["t"])
        duration = int(detail.get("duration") or 60)
        return float(random.uniform(3, max(6, min(duration * 0.7, 300))))

    # ------------------------------------------------- comment replies
    async def bili_notify_loop(self) -> None:
        interval = max(1, int(self.cfg.get("bilibili.notifications.check_minutes", 5) or 5)) * 60
        await asyncio.sleep(10)
        while not self._stop.is_set():
            try:
                await self.check_bili_mentions()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("B 站 @ 检查失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def check_bili_mentions(self) -> None:
        if not self.bili.logged_in:
            return
        items = await self.bili.at_notifications()
        if not items:
            return
        seen = int(self.memory.kv_get("last_bili_at_ts", 0) or 0)
        fresh = [i for i in items if int(i.get("at_time") or 0) > seen]
        if not fresh:
            return
        max_ts = max(int(i.get("at_time") or 0) for i in fresh)
        owner_mid = int(
            self.cfg.get("bilibili.comments.owner_mid", 0)
            or self.cfg.get("bilibili.dm.user_mid", 0)
            or 0
        )
        for item in sorted(fresh, key=lambda x: int(x.get("at_time") or 0)):
            if int(item.get("business_id") or 0) != 1:
                continue
            if not owner_mid or int(item.get("user_mid") or 0) != owner_mid:
                continue
            if not item.get("subject_id") or not item.get("source_id"):
                continue
            text = await self.brain.bili_at_reply(
                str(item.get("title") or ""), str(item.get("text") or ""), str(item.get("user") or "")
            )
            if not text:
                continue
            findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
            if findings:
                self.log.info("B 站 @ 回复被内容闸门拦下：%s", findings)
                continue
            root = int(item.get("root_id") or item["source_id"])
            try:
                res = await self.bili.reply_comment(
                    int(item["subject_id"]), root, int(item["source_id"]), text
                )
            except BiliRisk:
                raise
            except BiliError as exc:
                self.log.warning("回复 B 站 @ 失败：%s", exc)
                break
            self.memory.record_action(
                "bilibili", "bili_at_reply", str(item.get("uri") or ""), text, result=res.get("status", "ok")
            )
            self.memory.add_episode(
                channel="bilibili",
                kind="bili_at_reply",
                ref=str(item.get("uri") or ""),
                title=str(item.get("title") or ""),
                summary=text,
                reaction=f"回复 @{item.get('user', '')}",
                meta={"mention": item.get("text", "")},
            )
            self.log.info("回复 B 站 @（%s）：%s", item.get("user", ""), text)
            break
        self.memory.kv_set("last_bili_at_ts", max_ts)

    async def comment_loop(self) -> None:
        """Low-frequency check of replies under *my own* comments only.

        Deliberately does not crawl the whole comment section: one thread is
        fetched at a time, and only one reply is generated per run.  This keeps
        token use low and avoids reading every stranger's reply.
        """
        interval = max(10, int(self.cfg.get("bilibili.comments.check_minutes", 60) or 60)) * 60
        await asyncio.sleep(20)
        while not self._stop.is_set():
            try:
                await self.check_comment_replies()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("评论回复检查失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass

    async def check_comment_replies(self) -> None:
        if not self.bili.logged_in:
            return
        day = day_str()
        cap = int(self.cfg.get("bilibili.comments.max_replies_per_day", 6) or 0)
        used = self.memory.count_actions("comment_reply", day=day, channel="bilibili")
        if used >= cap:
            return
        gap = float(self.cfg.get("bilibili.comments.reply_gap_minutes", 60) or 0)
        last = self.memory.last_action_ts("comment_reply")
        if gap and last and (time.time() - last) < gap * 60:
            return

        rows = self.memory.my_comments(limit=int(self.cfg.get("bilibili.comments.max_threads", 5) or 5))
        owner_mid = int(
            self.cfg.get("bilibili.comments.owner_mid", 0)
            or self.cfg.get("bilibili.dm.user_mid", 0)
            or 0
        )
        for row in rows:
            checked = int(row.get("checked_ts") or 0)
            try:
                replies = await self.bili.sub_comments(int(row["aid"]), int(row["rpid"]))
            except BiliRisk:
                raise
            except BiliError as exc:
                self.log.debug("读取评论回复失败 %s：%s", row["rpid"], exc)
                continue
            fresh = [
                r
                for r in replies
                if r.get("rpid")
                and int(r.get("ctime") or 0) > checked
                and int(r.get("mid") or 0) != int(self.bili.uid or 0)
            ]
            if not fresh:
                self.memory.mark_comment_checked(int(row["rpid"]), int(time.time()))
                continue

            user_replies = [r for r in fresh if owner_mid and int(r.get("mid") or 0) == owner_mid]
            outsiders = [r for r in fresh if not owner_mid or int(r.get("mid") or 0) != owner_mid]
            selected = None
            is_user = False
            if user_replies:
                selected, is_user = user_replies[0], True
            elif outsiders and bool(self.cfg.get("bilibili.comments.reply_to_outsiders", True)):
                selected = outsiders[0]

            if selected is None:
                self.memory.mark_comment_checked(int(row["rpid"]), int(time.time()))
                continue

            text = await self.brain.comment_reply(
                str(row.get("text") or ""), selected, is_user=is_user, video_ref=str(row.get("bvid") or "")
            )
            if not text:
                self.memory.mark_comment_checked(int(row["rpid"]), int(selected.get("ctime") or time.time()))
                continue
            findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
            if findings:
                self.log.info("评论回复被内容闸门拦下：%s", findings)
                self.memory.mark_comment_checked(int(row["rpid"]), int(selected.get("ctime") or time.time()))
                continue
            try:
                res = await self.bili.reply_comment(
                    int(row["aid"]), int(row["rpid"]), int(selected["rpid"]), text
                )
            except BiliRisk:
                raise
            except BiliError as exc:
                self.log.warning("回复评论失败：%s", exc)
                return
            self.memory.record_action(
                "bilibili", "comment_reply", row["bvid"], text, result=res.get("status", "ok")
            )
            self.memory.add_episode(
                channel="bilibili",
                kind="comment_reply",
                ref=row["bvid"],
                title=row.get("text") or "",
                summary=text,
                reaction=f"回复 {selected.get('user', '')}",
                meta={"reply_to": selected.get("text", ""), "is_user": is_user},
            )
            self.memory.mark_comment_checked(
                int(row["rpid"]), max(int(selected.get("ctime") or 0), int(time.time()))
            )
            self.log.info("回复评论（%s）：%s", selected.get("user", ""), text)
            return

    # ------------------------------------------------------------ mind
    async def mind_loop(self) -> None:
        last_journal = ""
        last_prune = 0
        # If the process was off at 23:30, write yesterday's diary on startup.
        try:
            from datetime import timedelta

            yesterday = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
            await self._write_daily(yesterday)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("补写昨日日记跳过：%s", exc)
        while not self._stop.is_set():
            try:
                self.state.decay()
                self.state.maybe_shift_mood()
                clock = now()
                if time.time() - last_prune > 6 * 3600:
                    self.memory.prune_group_msgs(7)
                    last_prune = time.time()
                # Diary after 23:30.
                if clock.hour == 23 and clock.minute >= 30 and last_journal != day_str():
                    await self._write_daily(day_str())
                    last_journal = day_str()
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.exception("mind loop 出错：%s", exc)
                await asyncio.sleep(60)

    async def _write_daily(self, day: str) -> None:
        if self.memory.get_journal(day):
            return
        if not self.memory.episodes_for_day(day):
            return
        text = await self.brain.journal(day)
        if not text or text.startswith("离线"):
            return
        self.memory.set_journal(day, text)
        out = self.cfg.path("paths.report_dir") / f"{day}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(daily_report(self.memory, self.state.state, day), encoding="utf-8")
        self.log.info("日记和日报写好了：%s", out)

    # ---------------------------------------------------------- status
    def status(self) -> dict:
        st = self.state.state
        next_at = float(self.memory.kv_get("next_bili_at", 0) or 0)
        qq_connected = bool(self.qq and self.qq.client.connected)
        if not qq_connected:
            qq_state = self.memory.kv_get("qq_status") or {}
            if isinstance(qq_state, dict) and qq_state.get("connected"):
                # This CLI process is not the running bot; trust a recent handshake.
                if time.time() - int(qq_state.get("ts", 0) or 0) < 300:
                    qq_connected = True
        return {
            "mode": self.cfg.mode,
            "live": self.cfg.live,
            "llm": "online" if self.llm.available else "offline",
            "bili_logged_in": self.bili.logged_in,
            "qq_connected": qq_connected,
            "mood": st.mood,
            "energy": round(st.energy),
            "sociability": round(st.sociability),
            "curiosity": round(st.curiosity),
            "watches_today": self.memory.watched_count_today(day_str()),
            "qq_msgs_today": self.memory.count_actions("qq_msg", day=day_str(), channel="qq"),
            "next_bili_in": max(0, int(next_at - time.time())) if next_at else 0,
            "last_bili": human_ago(self.last_bili_at or st.last_watch_at),
            "busy": {"llm_budget": self.policy.llm_budget_ok()},
        }

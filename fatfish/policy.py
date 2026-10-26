"""Deterministic write gate.

Every like/coin/comment/danmaku/group message must pass here, no matter what
the model said.  This is the main reason a prompt injection in a video title
or a group message cannot turn into an unwanted public action: the model can
only *propose*, and proposals are checked against fixed budgets.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import guard
from .config import Config
from .guard import Finding
from .memory import Memory
from .util import day_str, in_quiet_hours, now


@dataclass
class Verdict:
    allowed: bool
    reason: str
    dry_run: bool = False
    action: str = ""
    target: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Policy:
    def __init__(self, cfg: Config, memory: Memory):
        self.cfg = cfg
        self.memory = memory
        self.force_observe_bili = False

    # ------------------------------------------------------------ common
    def risk_paused(self) -> tuple[bool, str]:
        until = int(self.memory.kv_get("risk_pause_until", 0) or 0)
        if until > time.time():
            mins = int((until - time.time()) / 60)
            return True, f"风控暂停中，还有 {mins} 分钟"
        return False, ""

    def quiet_now(self) -> tuple[bool, str]:
        windows = self.cfg.get("safety.quiet_hours") or []
        if in_quiet_hours(now(), windows):
            return True, "现在是安静时段"
        return False, ""

    # ---------------------------------------------------------- bilibili
    def check_bili_write(
        self,
        action: str,
        bvid: str,
        score: float,
        findings: list[Finding] | None = None,
        text: str = "",
    ) -> Verdict:
        if self.cfg.get("safety.minor_mode", True) and text:
            findings = list(findings or []) + guard.scan_minor_unsafe(text)
        if not self.cfg.get("bilibili.enabled"):
            return Verdict(False, "B 站功能未启用", action=action, target=bvid)

        paused, why = self.risk_paused()
        if paused:
            return Verdict(False, why, action=action, target=bvid)
        quiet, why = self.quiet_now()
        if quiet:
            return Verdict(False, why, action=action, target=bvid)

        enabled = set(self.cfg.get("bilibili.write.enabled_actions") or [])
        if action not in enabled:
            return Verdict(False, f"{action} 未在 enabled_actions 中启用", action=action, target=bvid)

        if findings:
            return Verdict(False, "待写入内容含风险词，放弃", action=action, target=bvid)

        min_score = float(self.cfg.get(f"bilibili.write.min_score.{action}", 101))
        if score < min_score:
            return Verdict(False, f"兴趣分 {score:.0f} 低于 {action} 门槛 {min_score:.0f}", action=action, target=bvid)

        daily_cap = int(self.cfg.get(f"bilibili.write.daily_caps.{action}", 0))
        used = self.memory.count_actions(action, day=day_str(), channel="bilibili")
        if daily_cap > 0 and used >= daily_cap:
            return Verdict(False, f"{action} 今日额度已用完（{used}/{daily_cap}）", action=action, target=bvid)

        weekly_cap = int(self.cfg.get(f"bilibili.write.weekly_caps.{action}", -1))
        if weekly_cap > 0:
            used_week = self.memory.count_actions(
                action, since_ts=int(time.time()) - 7 * 86400, channel="bilibili"
            )
            if used_week >= weekly_cap:
                return Verdict(False, f"{action} 本周额度已用完（{used_week}/{weekly_cap}）", action=action, target=bvid)

        gap_min = float(self.cfg.get(f"bilibili.write.min_gap_minutes.{action}", 0))
        last = self.memory.last_action_ts(action)
        if gap_min and last and (time.time() - last) < gap_min * 60:
            wait = int((gap_min * 60 - (time.time() - last)) / 60) + 1
            return Verdict(False, f"{action} 冷却中，约 {wait} 分钟后可再做", action=action, target=bvid)

        if self.memory.last_action_ts(action, target=bvid) and action in ("like", "favorite", "comment", "danmaku"):
            return Verdict(False, f"这个视频的 {action} 已经做过了", action=action, target=bvid)

        if not self.cfg.live or self.force_observe_bili:
            return Verdict(True, "通过（observe 只记录）", dry_run=True, action=action, target=bvid)

        return Verdict(True, "通过", action=action, target=bvid)

    def pause_for_risk(self, hours: float) -> None:
        until = int(time.time() + max(1.0, hours) * 3600)
        self.memory.kv_set("risk_pause_until", until)

    # ---------------------------------------------------------------- qq
    def check_qq_private(
        self, user_id: str, text: str, findings: list[Finding] | None = None
    ) -> Verdict:
        target = f"private:{user_id}"
        if self.cfg.get("safety.minor_mode", True) and text:
            findings = list(findings or []) + guard.scan_minor_unsafe(text)
        if not self.cfg.get("qq.enabled"):
            return Verdict(False, "QQ 功能未启用", action="send", target=target)
        paused, why = self.risk_paused()
        if paused:
            return Verdict(False, why, action="send", target=target)
        # Private messages from the owner are allowed at night too.
        if not self.cfg.get("qq.private_ignore_quiet", True):
            quiet, why = self.quiet_now()
            if quiet:
                return Verdict(False, why, action="send", target=target)
        if not text or not text.strip():
            return Verdict(False, "空消息", action="send", target=target)
        if findings:
            return Verdict(False, "消息含风险词，放弃", action="send", target=target)
        cap = int(self.cfg.get("qq.private_max_msgs_per_day", 0))
        used = self.memory.count_actions("qq_msg", day=day_str(), channel="qq", target=target)
        if cap > 0 and used >= cap:
            return Verdict(False, f"今天私聊已经说了 {used} 句，够了", action="send", target=target)
        gap = float(self.cfg.get("qq.private_min_gap_seconds", 0) or 0)
        last = self.memory.last_action_ts("qq_msg", target=target)
        if gap and last and (time.time() - last) < gap:
            return Verdict(False, "私聊还在冷却", action="send", target=target)
        if not self.cfg.live:
            return Verdict(True, "通过（observe 只记录）", dry_run=True, action="send", target=target)
        return Verdict(True, "通过", action="send", target=target)

    def check_qq_send(
        self,
        group_id: str,
        text: str,
        findings: list[Finding] | None = None,
        ignore_gap: bool = False,
    ) -> Verdict:
        if self.cfg.get("safety.minor_mode", True) and text:
            findings = list(findings or []) + guard.scan_minor_unsafe(text)
        if not self.cfg.get("qq.enabled"):
            return Verdict(False, "QQ 功能未启用", action="send", target=str(group_id))

        groups = [str(g) for g in (self.cfg.get("qq.groups") or [])]
        if str(group_id) not in groups:
            return Verdict(False, "该群不在 qq.groups 白名单里", action="send", target=str(group_id))

        paused, why = self.risk_paused()
        if paused:
            return Verdict(False, why, action="send", target=str(group_id))
        quiet, why = self.quiet_now()
        if quiet:
            return Verdict(False, why, action="send", target=str(group_id))

        if not text or len(text.strip()) < 1:
            return Verdict(False, "空消息", action="send", target=str(group_id))
        if findings:
            return Verdict(False, "消息含风险词，放弃", action="send", target=str(group_id))

        day = day_str()
        per_group = int(self.cfg.get("qq.max_msgs_per_day_per_group", 0))
        used_group = self.memory.count_actions("qq_msg", day=day, channel="qq", target=str(group_id))
        if used_group >= per_group:
            return Verdict(False, f"这个群今天已经说了 {used_group} 句，够了", action="send", target=str(group_id))

        total_cap = int(self.cfg.get("qq.max_msgs_per_day_total", 0))
        used_total = self.memory.count_actions("qq_msg", day=day, channel="qq")
        if used_total >= total_cap:
            return Verdict(False, f"今天群聊总发言数已到 {total_cap}", action="send", target=str(group_id))

        window = int(self.cfg.get("qq.window_seconds", 600) or 600)
        window_cap = int(self.cfg.get("qq.max_msgs_per_window", 20) or 20)
        used_window = self.memory.count_actions(
            "qq_msg", since_ts=int(time.time()) - window, channel="qq", target=str(group_id)
        )
        if used_window >= window_cap:
            return Verdict(
                False,
                f"{window // 60} 分钟内已经说了 {used_window} 句，到上限了",
                action="send",
                target=str(group_id),
            )

        gap = float(self.cfg.get("qq.min_gap_seconds", 0))
        last = self.memory.last_action_ts("qq_msg", target=str(group_id))
        if gap and last and not ignore_gap and (time.time() - last) < gap:
            wait = int((gap - (time.time() - last)) / 60) + 1
            return Verdict(False, f"群发言冷却中，约 {wait} 分钟后", action="send", target=str(group_id))

        if not self.cfg.live:
            return Verdict(True, "通过（observe 只记录）", dry_run=True, action="send", target=str(group_id))

        return Verdict(True, "通过", action="send", target=str(group_id))

    # ------------------------------------------------------------ budget
    def llm_budget_ok(self) -> tuple[bool, str]:
        cap = int(self.cfg.get("safety.max_daily_llm_tokens", 0) or 0)
        if not cap:
            return True, ""
        used = int(self.memory.kv_get(f"llm_tokens:{day_str()}", 0) or 0)
        if used >= cap:
            return False, f"今日 LLM token 预算已用完（{used}/{cap}）"
        return True, ""

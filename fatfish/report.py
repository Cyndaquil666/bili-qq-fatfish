"""Human-readable daily report and status text."""
from __future__ import annotations

from .memory import Memory
from .state import MindState
from .util import day_str, now


def daily_report(memory: Memory, state: MindState, day: str | None = None) -> str:
    day = day or day_str()
    episodes = memory.episodes_for_day(day)
    actions = memory.actions_for_day(day)
    journal = memory.get_journal(day)

    watches = [e for e in episodes if e["channel"] == "bilibili" and e["kind"] == "watch"]
    skips = [e for e in episodes if e["channel"] == "bilibili" and e["kind"] == "skip"]
    says = [e for e in episodes if e["channel"] == "qq" and e["kind"] == "say"]
    would_says = [e for e in episodes if e["channel"] == "qq" and e["kind"] == "would_say"]
    stickers = [e for e in episodes if e["channel"] == "qq" and e["kind"] == "sticker"]
    lurks = [e for e in episodes if e["channel"] == "qq" and e["kind"] == "lurk"]

    lines = [f"# 大肥鱼 · {day}", ""]
    lines.append(f"状态：{state.mood}｜精力 {state.energy:.0f}｜社交电量 {state.sociability:.0f}｜好奇 {state.curiosity:.0f}")
    lines.append("")

    lines.append(f"## B 站（{len(watches)} 个视频）")
    if not watches:
        lines.append("今天没点开什么。" if skips else "今天没刷。")
    for e in watches:
        meta = e.get("meta") or {}
        lines.append(f"- **{e['title']}**（{meta.get('up', '?')}，{meta.get('duration', 0) // 60} 分钟）")
        lines.append(f"  - 兴趣 {e['score']:.0f}｜{meta.get('how_much', '?')}｜{e['summary']}")
        if e.get("reaction"):
            lines.append(f"  - 感觉：{e['reaction']}")
        if meta.get("evidence"):
            lines.append(f"  - 记住的点：{'；'.join(meta['evidence'][:2])}")
        if meta.get("pick_why"):
            lines.append(f"  - 为什么点开：{meta['pick_why']}")
    if skips:
        lines.append("")
        lines.append("划过去的：" + "；".join(e["summary"] or "没感觉" for e in skips[-5:]))

    lines.append("")
    lines.append("## 写操作")
    if not actions:
        lines.append("今天什么都没写。")
    for a in actions:
        what = {"like": "点赞", "coin": "投币", "favorite": "收藏", "danmaku": "弹幕", "comment": "评论", "follow": "关注", "qq_msg": "群消息"}.get(a["action"], a["action"])
        status = {"ok": "已做", "would": "只记录（observe）", "blocked": "被拦下", "skipped": "跳过", "already": "之前做过"}.get(a["result"], a["result"])
        target = a["target"] or ""
        detail = (a["detail"] or "").replace("\n", " ")[:80]
        lines.append(f"- {what} · {status} · {target} {detail}")

    lines.append("")
    lines.append(
        f"## QQ（说了 {len(says)} 句，表情包 {len(stickers)} 个，想说没发 {len(would_says)} 条，忍住 {len(lurks)} 次）"
    )
    for e in says:
        lines.append(f"- 「{e['summary']}」")
    for e in stickers:
        lines.append(f"- [表情包] {e['summary']}")
    for e in would_says:
        lines.append(f"- （observe 想说）「{e['summary']}」")
    if lurks:
        lines.append("")
        lines.append("忍住没说的理由（抽样）：")
        for e in lurks[-5:]:
            lines.append(f"- {e['summary'] or '没有具体的点'}")

    if journal:
        lines.append("")
        lines.append("## 日记")
        lines.append(journal)

    lines.append("")
    lines.append("---")
    lines.append(f"生成于 {now().strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


def status_text(runtime) -> str:
    s = runtime.status()
    return (
        f"模式：{s['mode']}（live={s['live']}）｜LLM：{s['llm']}｜B站登录：{s['bili_logged_in']}｜QQ：{'已连接' if s['qq_connected'] else '未连接'}\n"
        f"状态：{s['mood']}｜精力 {s['energy']}｜社交 {s['sociability']}｜好奇 {s['curiosity']}\n"
        f"今天：看 {s['watches_today']} 个｜群消息 {s['qq_msgs_today']} 条\n"
        f"下一轮 B 站：{s['next_bili_in'] // 60} 分钟后｜上次看：{s['last_bili']}"
    )

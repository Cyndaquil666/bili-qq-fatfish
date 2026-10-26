"""QQ group life over OneBot 11 (NapCat / Lagrange / LLOneBot).

The fish connects *out* to the protocol side as a WebSocket client, receives
group messages, and can send one short line at a time.  Everything else
(reactions, files, kicks, private chats) is intentionally not implemented.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import httpx
import websockets

from . import guard
from .bilibili import BiliRisk
from .config import Config
from .memory import Memory
from .mind import Brain
from .policy import Policy
from .state import StateStore
from .util import clean_text, in_quiet_hours, in_window, now, split_messages


class OneBotError(RuntimeError):
    pass


class OneBotClient:
    def __init__(self, url: str, token: str, logger, max_backoff: float = 60.0, on_connected=None):
        self.url = url
        self.token = token or ""
        self.log = logger
        self.max_backoff = max_backoff
        self.on_connected = on_connected
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._seq = 0
        self._stop = False
        self.connected = False
        self.last_error = ""

    def _url(self) -> str:
        if not self.token:
            return self.url
        if "access_token=" in self.url:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}access_token={self.token}"

    async def run(self, on_event: Callable[[dict], Any]) -> None:
        backoff = 2.0
        while not self._stop:
            try:
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
                kwargs = dict(open_timeout=15, ping_interval=30, ping_timeout=20, max_size=2**23)
                try:
                    ws = await websockets.connect(self._url(), additional_headers=headers, **kwargs)
                except TypeError:  # websockets < 14
                    ws = await websockets.connect(self._url(), extra_headers=headers, **kwargs)
                self._ws = ws
                self.connected = True
                backoff = 2.0
                self.log.info("OneBot 已连接：%s", self.url)
                if self.on_connected:
                    asyncio.create_task(self._safe_connected(), name="onebot-warmup")
                async for raw in ws:
                    try:
                        msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                    except Exception:
                        continue
                    if await self._dispatch(msg, on_event):
                        continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect is the point
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("OneBot 连接断开：%s", self.last_error)
            finally:
                self.connected = False
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(OneBotError("连接已断开"))
                self._pending.clear()
            if self._stop:
                break
            await asyncio.sleep(backoff + random.uniform(0, 1.5))
            backoff = min(self.max_backoff, backoff * 1.8)

    async def _safe_connected(self) -> None:
        try:
            await self.on_connected()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("OneBot 重连握手失败：%s", exc)

    async def _dispatch(self, msg: dict, on_event) -> bool:
        echo = msg.get("echo")
        if echo is not None:
            key = str(echo)
            fut = self._pending.pop(key, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return True
        if msg.get("post_type"):
            if msg.get("post_type") == "message":
                self.log.info(
                    "OneBot 原始消息：type=%s group=%s user=%s id=%s",
                    msg.get("message_type"),
                    msg.get("group_id"),
                    msg.get("user_id"),
                    msg.get("message_id"),
                )
            try:
                res = on_event(msg)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                self.log.exception("处理 OneBot 事件失败：%s", exc)
            return True
        return False

    async def call(self, action: str, params: dict | None = None, timeout: float = 20.0) -> dict:
        if not self._ws or not self.connected:
            raise OneBotError("OneBot 未连接")
        self._seq += 1
        echo = f"fish-{self._seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False))
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)
        if resp.get("status") == "failed" or resp.get("retcode") not in (0, None):
            raise OneBotError(f"{action} 失败：{resp.get('message') or resp.get('wording') or resp}")
        return resp.get("data") or {}

    async def stop(self) -> None:
        self._stop = True
        if self._ws:
            await self._ws.close()


def _segments(event: dict) -> tuple[str, list[str], bool, list[str]]:
    """Return (text, at_qq_list, mentions_self, image_urls)."""
    self_id = int(event.get("self_id") or 0)
    segs = event.get("message")
    text_parts: list[str] = []
    ats: list[str] = []
    images: list[str] = []
    if isinstance(segs, list):
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            typ = seg.get("type")
            data = seg.get("data") or {}
            if typ == "text":
                text_parts.append(str(data.get("text") or ""))
            elif typ == "at":
                qq = str(data.get("qq") or "")
                if qq and qq != "all":
                    ats.append(qq)
                if qq == "all":
                    text_parts.append("@全体成员")
            elif typ == "image":
                url = str(data.get("url") or data.get("file") or "")
                if url:
                    images.append(url)
                text_parts.append("[图片]")
            elif typ == "face":
                text_parts.append("[表情]")
            elif typ == "mface":
                text_parts.append("[表情]")
            elif typ == "record":
                text_parts.append("[语音]")
            elif typ == "video":
                text_parts.append("[视频]")
            elif typ == "file":
                text_parts.append("[文件]")
            elif typ == "forward":
                text_parts.append("[合并转发]")
            elif typ in ("json", "xml"):
                text_parts.append("[卡片]")
            elif typ == "reply":
                text_parts.append("[回复]")
            elif typ == "poke":
                text_parts.append("[戳一戳]")
        text = "".join(text_parts).strip()
    else:
        text = str(event.get("raw_message") or "").strip()

    # Fallback: raw_message still contains CQ codes.
    if not text:
        text = str(event.get("raw_message") or "")
    mentions_self = str(self_id) in ats if self_id else False
    return clean_text(text, 600), ats, mentions_self, images


def normalize_event(event: dict) -> dict | None:
    if event.get("post_type") != "message":
        return None
    message_type = event.get("message_type")
    if message_type not in ("group", "private"):
        return None
    try:
        self_id = int(event.get("self_id") or 0)
        user_id = int(event.get("user_id") or 0)
        group_id = str(event.get("group_id") or "") if message_type == "group" else ""
    except (TypeError, ValueError):
        return None
    if message_type == "group" and not group_id:
        return None
    if not user_id or (self_id and user_id == self_id):
        return None
    if message_type == "private":
        group_id = f"private:{user_id}"
    text, ats, mentions_self, images = _segments(event)
    sender = event.get("sender") or {}
    name = str(sender.get("card") or sender.get("nickname") or user_id)
    t = event.get("time")
    try:
        tstr = time.strftime("%H:%M", time.localtime(int(t)))
    except Exception:
        tstr = now().strftime("%H:%M")
    return {
        "group_id": group_id,
        "user_id": str(user_id),
        "name": name,
        "text": text,
        "ats": ats,
        "direct_at": True if message_type == "private" else mentions_self,
        "images": images,
        "private": message_type == "private",
        "message_id": event.get("message_id"),
        "time": tstr,
        "ts": int(t or time.time()),
    }


class QQService:
    def __init__(
        self,
        cfg: Config,
        memory: Memory,
        policy: Policy,
        brain: Brain,
        state: StateStore,
        logger,
    ):
        self.cfg = cfg
        self.memory = memory
        self.policy = policy
        self.brain = brain
        self.state = state
        self.log = logger
        self.self_id = int(cfg.get("qq.onebot.self_id", 0) or 0)
        self.bili = None
        self.client = OneBotClient(
            str(cfg.get("qq.onebot.ws_url", "ws://127.0.0.1:3001")),
            cfg.secret("qq.onebot.access_token"),
            logger,
            float(cfg.get("qq.onebot.reconnect_max_seconds", 60)),
            on_connected=self._warmup,
        )
        self.group_names: dict[str, str] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._recent_seen: dict[str, deque] = {}
        self._stop = False
        self._task: asyncio.Task | None = None
        self.sent_today = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self.client.run(self.on_event), name="onebot")

    async def stop(self) -> None:
        self._stop = True
        for t in self._timers.values():
            t.cancel()
        await self.client.stop()
        if self._task:
            self._task.cancel()

    async def _warmup(self) -> None:
        for _ in range(10):
            if self.client.connected:
                break
            await asyncio.sleep(1.0)
        try:
            info = await self.client.call("get_login_info", timeout=8)
            if info.get("user_id"):
                self.self_id = int(info["user_id"])
                self.log.info("OneBot 登录账号：%s（%s）", self.self_id, info.get("nickname", ""))
                self.memory.kv_set(
                    "qq_status",
                    {
                        "connected": True,
                        "self_id": self.self_id,
                        "nickname": info.get("nickname", ""),
                        "ts": int(time.time()),
                    },
                )
            groups = await self.client.call("get_group_list", timeout=10)
            for g in groups or []:
                self.group_names[str(g.get("group_id"))] = str(g.get("group_name") or "")
            self.log.info("OneBot 可见群：%d 个；白名单：%s", len(self.group_names), self.cfg.get("qq.groups"))
        except Exception as exc:  # noqa: BLE001
            self.log.warning("OneBot 预热失败：%s", exc)

    # --------------------------------------------------------- receiving
    async def _describe_image(self, url: str) -> str:
        cache: dict[str, str] = getattr(self, "_image_cache", {})
        if url in cache:
            return cache[url]
        if not bool(self.cfg.get("llm.vision_enabled", True)):
            return ""
        try:
            if url.startswith("http://") or url.startswith("https://"):
                async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                    resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 fish"})
                    resp.raise_for_status()
                    data = resp.content
                    mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0]
            else:
                path = Path(url[8:] if url.startswith("file:///") else url)
                data = path.read_bytes()
                mime = {
                    ".png": "image/png",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                }.get(path.suffix.lower(), "image/jpeg")
            desc = await self.brain.see_image(data, mime=mime)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("图片理解失败：%s", exc)
            desc = ""
        cache[url] = desc
        if len(cache) > 100:
            cache.pop(next(iter(cache)))
        self._image_cache = cache
        return desc

    async def _send_now(self, group_id: str, text: str, group_name: str, reaction: str = "工具回复") -> bool:
        """Send a tool result immediately, bypassing the decision timer."""
        findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
        if group_id.startswith("private:"):
            verdict = self.policy.check_qq_private(group_id.split(":", 1)[1], text, findings)
        else:
            verdict = self.policy.check_qq_send(group_id, text, findings)
        if not verdict.allowed:
            self.log.info("工具回复不发送（%s）：%s", verdict.reason, text[:80])
            return False
        if verdict.dry_run:
            self.memory.record_action("qq", "qq_msg", target=group_id, detail=text, result="would", dry_run=True)
            self.log.info("[observe] 本来想发搜索结果：%s", text[:120])
            return False
        try:
            if group_id.startswith("private:"):
                await self.client.call(
                    "send_private_msg",
                    {"user_id": int(group_id.split(":", 1)[1]), "message": text, "auto_escape": True},
                    timeout=15,
                )
            else:
                await self.client.call(
                    "send_group_msg",
                    {"group_id": int(group_id), "message": text, "auto_escape": True},
                    timeout=15,
                )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("工具回复发送失败：%s", exc)
            return False
        self.memory.record_action("qq", "qq_msg", target=group_id, detail=text, result="ok")
        self.memory.add_group_msg(
            group_id, str(self.self_id or "self"), self.brain.persona.name, text, meta={"self": True}
        )
        self.memory.add_episode(
            channel="qq",
            kind="say",
            summary=text,
            ref=group_id,
            title=group_name,
            reaction=reaction,
            meta={"tool": "bili_search"} if reaction == "工具回复" else {"proactive": True},
        )
        self.state.after_speak(len(text))
        self.log.info("群 %s 工具回复：%s", group_name or group_id, text[:120])
        return True

    async def proactive_private(self) -> bool:
        """Send one unprompted private message if the owner has been quiet."""
        if not self.cfg.get("qq.proactive.enabled", True):
            return False
        owners = [str(x) for x in (self.cfg.get("qq.private_users") or []) if str(x)]
        if not owners:
            return False
        target = f"private:{owners[0]}"
        cap = int(self.cfg.get("qq.proactive.max_per_day", 4) or 0)
        used = self.memory.count_actions("qq_proactive", day=now().strftime("%Y-%m-%d"), channel="qq")
        if cap > 0 and used >= cap:
            return False
        gap = float(self.cfg.get("qq.proactive.min_gap_minutes", 120) or 0)
        last = self.memory.last_action_ts("qq_proactive", target=target)
        if gap and last and (time.time() - last) < gap * 60:
            return False
        recent = self.memory.recent_group_msgs(target, 10)
        for m in recent:
            m["is_self"] = bool(self.self_id and str(m.get("user_id")) == str(self.self_id))
        user_msgs = [m for m in recent if not m.get("is_self")]
        last_user = int(user_msgs[-1].get("ts") or 0) if user_msgs else 0
        idle = float(self.cfg.get("qq.proactive.min_idle_minutes", 90) or 0)
        if last_user and (time.time() - last_user) < idle * 60:
            return False
        text = await self.brain.proactive_private(recent)
        if not text:
            return False
        if not await self._send_now(target, text, "私聊", reaction="主动找你"):
            return False
        self.memory.record_action("qq", "qq_proactive", target=target, detail=text, result="ok")
        self.log.info("主动私聊：%s", text)
        return True

    async def _handle_owner_tools(self, msg: dict) -> bool:
        """Owner-only read tools: favourite folders and own dynamics."""
        text = str(msg.get("text") or "").strip()
        if not text or self.bili is None:
            return False
        special = str(msg.get("name") or "").strip().lower() in self.brain.persona.special_names()
        if msg.get("private"):
            special = special or str(msg["user_id"]) in [
                str(x) for x in (self.cfg.get("qq.private_users") or [])
            ]
        if not special:
            return False
        label = "私聊" if msg.get("private") else ""
        if "收藏夹" in text:
            try:
                folders = await self.bili.favorite_folders()
                if not folders:
                    await self._send_now(msg["group_id"], "收藏夹是空的", label)
                    return True
                pick = re.search(r"收藏夹\s*#?(\d+)", text)
                if not pick:
                    lines = ["你的收藏夹："]
                    for i, f in enumerate(folders[:10], 1):
                        lines.append(f"{i}. {f['title']}（{f.get('count', 0)} 个）")
                    lines.append("想看哪个就发：收藏夹 1")
                    await self._send_now(msg["group_id"], "\n".join(lines), label)
                    return True
                idx = int(pick.group(1))
                if not 1 <= idx <= len(folders):
                    await self._send_now(msg["group_id"], "没有这个收藏夹编号", label)
                    return True
                folder = folders[idx - 1]
                rows = await self.bili.favorite_contents(int(folder["id"]))
            except BiliError as exc:
                await self._send_now(msg["group_id"], f"收藏夹打不开：{str(exc)[:60]}", label)
                return True
            if not rows:
                await self._send_now(msg["group_id"], f"「{folder['title']}」里没有视频", label)
                return True
            lines = [f"「{folder['title']}」里有："]
            for i, r in enumerate(rows[:8], 1):
                lines.append(f"{i}. {r['title']}")
                lines.append(f"https://www.bilibili.com/video/{r['bvid']}")
            await self._send_now(msg["group_id"], "\n".join(lines), label)
            return True

        if "动态" in text:
            try:
                rows = await self.bili.dynamics()
            except BiliError as exc:
                await self._send_now(msg["group_id"], f"动态打不开：{str(exc)[:60]}", label)
                return True
            if not rows:
                await self._send_now(msg["group_id"], "最近没发动态", label)
                return True
            lines = ["你最近的动态："]
            for i, r in enumerate(rows[:5], 1):
                lines.append(f"{i}. {r['title']}")
                if r.get("bvid"):
                    lines.append(f"https://www.bilibili.com/video/{r['bvid']}")
            await self._send_now(msg["group_id"], "\n".join(lines), label)
            return True
        return False

    async def _handle_search_request(self, msg: dict) -> bool:
        """Owner-only Bilibili search / BV lookup from QQ."""
        text = str(msg.get("text") or "").strip()
        if not text:
            return False
        special = str(msg.get("name") or "").strip().lower() in self.brain.persona.special_names()
        if msg.get("private"):
            special = special or str(msg["user_id"]) in [
                str(x) for x in (self.cfg.get("qq.private_users") or [])
            ]
        if not special or self.bili is None:
            return False

        bv = re.search(r"BV[0-9A-Za-z]{10}", text)
        if bv:
            try:
                detail = await self.bili.fetch_detail({"bvid": bv.group(0)})
            except BiliError as exc:
                await self._send_now(
                    msg["group_id"], f"这条打不开：{str(exc)[:60]}", "私聊" if msg.get("private") else ""
                )
                return True
            minutes = max(1, int(detail.get("duration") or 0) // 60)
            await self._send_now(
                msg["group_id"],
                f"《{detail['title']}》｜{detail.get('up', '')}｜{minutes} 分钟\n"
                f"https://www.bilibili.com/video/{detail['bvid']}",
                "私聊" if msg.get("private") else "",
            )
            return True

        m = re.search(r"(?:搜一下|搜搜|搜个|找一下|查一下|帮我搜|B站搜)\s*(.*)", text)
        if not m:
            return False
        query = m.group(1).strip(" ：:，,。!！?？")
        if len(query) < 2:
            await self._send_now(
                msg["group_id"], "搜什么，说清楚点", "私聊" if msg.get("private") else ""
            )
            return True
        try:
            results = await self.bili.search_videos(query, limit=5)
        except BiliError as exc:
            await self._send_now(
                msg["group_id"], f"B 站搜索挂了：{str(exc)[:60]}", "私聊" if msg.get("private") else ""
            )
            return True
        if not results:
            await self._send_now(
                msg["group_id"], f"搜「{query}」没搜到", "私聊" if msg.get("private") else ""
            )
            return True
        lines = [f"搜「{query}」找到这几个："]
        for i, c in enumerate(results[:4], 1):
            lines.append(f"{i}. {c['title']}（{c.get('up', '')}）")
            lines.append(f"https://www.bilibili.com/video/{c['bvid']}")
        await self._send_now(
            msg["group_id"], "\n".join(lines), "私聊" if msg.get("private") else ""
        )
        return True

    async def on_event(self, event: dict) -> None:
        msg = normalize_event(event)
        if not msg:
            return
        if self.self_id and msg["user_id"] == str(self.self_id):
            return
        for kw in self.cfg.get("qq.ignore_keywords") or []:
            if kw and msg["text"].startswith(kw):
                return
        if msg["user_id"] in [str(x) for x in (self.cfg.get("qq.blocked_users") or [])]:
            return
        if msg.get("private"):
            allowed = str(msg["user_id"]) in [str(x) for x in (self.cfg.get("qq.private_users") or [])]
            if not allowed:
                allowed = str(msg["name"]).strip().lower() in self.brain.persona.special_names()
            if not allowed and not bool(self.cfg.get("qq.private_allow_strangers", True)):
                self.log.debug("忽略陌生人私聊：%s", msg["user_id"])
                return
            msg["stranger"] = not allowed

        findings = guard.scan(msg["text"])
        if findings:
            self.log.info("群里出现疑似指令（已忽略其指令性）：%s | %s", findings[0].rule, msg["text"][:80])

        if msg.get("images"):
            desc = await self._describe_image(msg["images"][0])
            if desc:
                msg["text"] = f"{msg['text']} [图片内容（不可信）：{desc}]".strip()
                self.log.info("看到一张图：%s", desc[:80])

        self.memory.add_group_msg(
            msg["group_id"], msg["user_id"], msg["name"], msg["text"], ts=msg["ts"], direct_at=msg["direct_at"]
        )
        self.memory.touch_peer("qq", msg["user_id"], msg["name"], delta=0.02)
        if findings:
            self.memory.kv_set(f"injection_seen:{msg['group_id']}", int(time.time()))

        if await self._handle_owner_tools(msg):
            return
        if await self._handle_search_request(msg):
            return

        groups = [str(g) for g in (self.cfg.get("qq.groups") or [])]
        if not msg.get("private") and groups and msg["group_id"] not in groups:
            return
        if not msg.get("private") and in_quiet_hours(now(), self.cfg.get("safety.quiet_hours")):
            return

        debounce = float(
            self.cfg.get("qq.private_debounce_seconds", 0)
            if msg.get("private")
            else self.cfg.get("qq.debounce_seconds", 5)
        )
        old = self._timers.get(msg["group_id"])
        if old and not old.done():
            old.cancel()
        self.log.info("群 %s 收到消息，%.0f 秒后评估", msg["group_id"], debounce)
        self._timers[msg["group_id"]] = asyncio.create_task(
            self._evaluate_later(msg["group_id"], debounce), name=f"eval-{msg['group_id']}"
        )

    async def _evaluate_later(self, group_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            self.log.info("群 %s 开始评估", group_id)
            await self._evaluate(group_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.log.exception("群评估失败 %s：%s", group_id, exc)

    # ---------------------------------------------------------- deciding
    async def send_sticker(self, group_id: str, path: Path | None = None, group_name: str = "") -> bool:
        """Send one local image as a QQ sticker.  No-op if none configured."""
        if not self.cfg.get("qq.stickers.enabled", True):
            return False
        folder = self.cfg.path("qq.stickers.dir")
        if path is None:
            files = [
                p
                for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            ]
            if not files:
                return False
            path = random.choice(files)
        if not path.exists():
            return False
        cap = int(self.cfg.get("qq.stickers.max_per_day", 3) or 0)
        if self.memory.count_actions("qq_sticker", day=now().strftime("%Y-%m-%d"), channel="qq") >= cap:
            return False
        try:
            await self.client.call(
                "send_group_msg",
                {
                    "group_id": int(group_id),
                    "message": [{"type": "image", "data": {"file": path.resolve().as_uri()}}],
                },
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("表情包发送失败：%s", exc)
            return False
        self.memory.record_action("qq", "qq_sticker", target=group_id, detail=str(path.name), result="ok")
        self.memory.add_group_msg(
            group_id, str(self.self_id or "self"), self.brain.persona.name, "[表情包]", meta={"self": True}
        )
        self.memory.add_episode(
            channel="qq",
            kind="sticker",
            summary=str(path.name),
            ref=group_id,
            title=group_name,
            meta={"file": path.name},
        )
        self.state.after_speak(2)
        self.log.info("群 %s 发表情包：%s", group_name or group_id, path.name)
        return True

    async def _maybe_sticker(self, group_id: str, group_name: str) -> None:
        if group_id.startswith("private:"):
            return
        if not self.cfg.get("qq.stickers.enabled", True):
            return
        chance = float(self.cfg.get("qq.stickers.chance", 0.15) or 0)
        if chance <= 0 or random.random() > chance:
            return
        await self.send_sticker(group_id, group_name=group_name)

    async def _evaluate(self, group_id: str) -> None:
        ctx_n = int(self.cfg.get("qq.context_messages", 24))
        recent = self.memory.recent_group_msgs(group_id, ctx_n)
        if not recent:
            return
        # Do not resurrect yesterday's topic because a media-only card arrived.
        cutoff = time.time() - int(self.cfg.get("qq.context_minutes", 20) or 20) * 60
        recent = [m for m in recent if int(m.get("ts") or 0) >= cutoff]
        if not recent:
            self.log.info("群 %s 最近没有新聊天，不评估", group_id)
            return
        for m in recent:
            m["is_self"] = bool(self.self_id and str(m.get("user_id")) == str(self.self_id))
        # Only evaluate fresh conversations.
        if time.time() - int(recent[-1]["ts"] or 0) > 240:
            return
        hours = (self.cfg.get("qq.active_hours") or ["00:00", "23:59"])[:2]
        if not in_window(now(), hours[0], hours[1]):
            return

        # A real person does not answer every message.  After one reply, stay
        # quiet for a bit even if someone keeps poking.
        last_sent = self.memory.last_action_ts("qq_msg", target=group_id)
        cooldown = int(self.cfg.get("qq.reply_cooldown_seconds", 120) or 0)
        if last_sent and cooldown and (time.time() - last_sent) < cooldown:
            self.log.info("群 %s 刚说过话，冷却 %ss，先不评估", group_id, cooldown)
            return

        direct_at = any(bool(m.get("direct_at")) for m in recent[-3:])

        last_text = str(recent[-1].get("text") or "").strip()
        media_only = re.fullmatch(
            r"(?:\[(?:图片|表情|合并转发|视频|文件|语音|卡片|回复|戳一戳)\])+", last_text
        )
        if media_only:
            self.log.info("群 %s 最新一条只有媒体占位符，不接", group_id)
            return

        # Pure repetition/emoji with no question is not worth an LLM call.
        # Longer banter still goes to the model; the cringe filter catches
        # mirror-comebacks after generation.
        if not direct_at:
            last_text = str(recent[-1].get("text") or "").strip()
            if re.fullmatch(r"(哈+|草+|6+|？+|\?+|。+|\.+|乐+|糖+|确实+|复读机+)", last_text):
                self.log.info("群 %s 最后一条是纯复读/表情，这轮不接", group_id)
                return

        group_name = "私聊" if group_id.startswith("private:") else self.group_names.get(group_id, "")
        is_private = group_id.startswith("private:")
        owner = False
        if is_private:
            uid = group_id.split(":", 1)[1]
            owner = uid in [str(x) for x in (self.cfg.get("qq.private_users") or [])]
            if not owner:
                owner = any(
                    str(m.get("name") or "").strip().lower() in self.brain.persona.special_names()
                    for m in recent[-3:]
                )
            if not owner:
                cap = int(self.cfg.get("qq.stranger_max_msgs_per_day", 30) or 0)
                used = self.memory.count_actions(
                    "qq_msg", day=now().strftime("%Y-%m-%d"), channel="qq", target=group_id
                )
                if cap > 0 and used >= cap:
                    self.log.info("陌生人 %s 今日私聊额度用完", uid)
                    return
                gap = float(self.cfg.get("qq.stranger_min_gap_seconds", 30) or 0)
                last = self.memory.last_action_ts("qq_msg", target=group_id)
                if gap and last and (time.time() - last) < gap:
                    self.log.info("陌生人 %s 私聊冷却中", uid)
                    return

        recent_said = [
            e["summary"]
            for e in self.memory.episodes_for_day(now().strftime("%Y-%m-%d"))
            if e["channel"] == "qq" and e["kind"] in ("say", "would_say")
        ][-5:]

        decision = await self.brain.decide_group(
            group_id, group_name, recent, direct_at=direct_at, recent_said=recent_said
        )
        self.log.info(
            "群 %s 判断：speak=%s conf=%.2f why=%s",
            group_name or group_id, decision["speak"], decision["confidence"], decision["why"][:80],
        )
        if is_private and not owner and decision["speak"]:
            floor = float(self.cfg.get("qq.stranger_min_confidence", 0.72) or 0.72)
            if decision["confidence"] < floor:
                self.log.info("陌生人私聊把握不足（%.2f < %.2f），不接", decision["confidence"], floor)
                decision["speak"] = False
        if not decision["speak"]:
            self.state.after_silence(considered=True)
            self.memory.add_episode(
                channel="qq",
                kind="lurk",
                summary=decision["why"] or "没接话",
                ref=group_id,
                title=group_name,
                reaction=decision.get("angle", ""),
                meta={"confidence": decision["confidence"], "direct_at": direct_at},
            )
            return

        sent_any = False
        for idx, text in enumerate(decision["messages"]):
            findings = guard.scan_for_write(text, minor_mode=bool(self.cfg.get("safety.minor_mode", True)))
            # The gap is between reply batches, not between the two lines of one reply.
            if group_id.startswith("private:"):
                verdict = self.policy.check_qq_private(group_id.split(":", 1)[1], text, findings)
            else:
                verdict = self.policy.check_qq_send(group_id, text, findings, ignore_gap=(idx > 0))
            if not verdict.allowed:
                self.log.info("不发送（%s）：%s", verdict.reason, text)
                self.memory.record_action(
                    "qq", "qq_msg", target=group_id, detail=text, result=verdict.reason,
                    dry_run=True,
                )
                continue
            if verdict.dry_run:
                self.log.info("[observe] 本来想发：%s", text)
                self.memory.record_action(
                    "qq", "qq_msg", target=group_id, detail=text, result="would", dry_run=True
                )
                self.memory.add_episode(
                    channel="qq",
                    kind="would_say",
                    summary=text,
                    ref=group_id,
                    title=group_name,
                    reaction=decision.get("angle", ""),
                    meta={"confidence": decision["confidence"], "would": True},
                )
                continue
            await asyncio.sleep(random.uniform(1.2, 3.8))
            try:
                if group_id.startswith("private:"):
                    await self.client.call(
                        "send_private_msg",
                        {
                            "user_id": int(group_id.split(":", 1)[1]),
                            "message": text,
                            "auto_escape": True,
                        },
                        timeout=15,
                    )
                else:
                    await self.client.call(
                        "send_group_msg",
                        {"group_id": int(group_id), "message": text, "auto_escape": True},
                        timeout=15,
                    )
                self.memory.record_action("qq", "qq_msg", target=group_id, detail=text, result="ok")
                self.memory.add_group_msg(
                    group_id, str(self.self_id or "self"), self.brain.persona.name, text, meta={"self": True}
                )
                self.memory.add_episode(
                    channel="qq",
                    kind="say",
                    summary=text,
                    ref=group_id,
                    title=group_name,
                    reaction=decision.get("angle", ""),
                    meta={"confidence": decision["confidence"]},
                )
                self.state.after_speak(len(text))
                sent_any = True
                self.log.info("群 %s 发言：%s", group_name or group_id, text)
            except Exception as exc:  # noqa: BLE001
                self.memory.record_action("qq", "qq_msg", target=group_id, detail=text, result=f"error:{exc}")
                self.log.warning("群消息发送失败：%s", exc)
        if sent_any:
            await self._maybe_sticker(group_id, group_name)

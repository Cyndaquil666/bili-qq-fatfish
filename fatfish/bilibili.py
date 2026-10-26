"""Bilibili adapter: browse like a person, write like a guest.

Reading uses the public web API through bilibili-api-python.  Writing is
deliberately narrow: like / coin / favourite / danmaku / comment / follow, each
one wrapped in throttling and risk-control detection.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from bilibili_api import Credential, comment, favorite_list, homepage, request_settings, search, select_client, session, user, video
from bilibili_api.utils.danmaku import Danmaku
from bilibili_api.utils.network import Api
from bilibili_api.utils.utils import get_api

from .config import Config
from .memory import Memory
from .util import clean_text, fmt_duration, strip_html, truncate

RISK_CODES = {-352, -412, -509, 412}
RISK_WORDS = ("风控", "请求被拦截", "412", "访问权限不足", "超出限制")
DANMAKU_COLORS = ("ffffff", "ff6699", "66ccff", "ffcc00", "66ff99", "ff9966", "cc99ff")
BILI_EMOJIS = ("[doge]", "[微笑]", "[笑哭]", "[害羞]", "[惊喜]", "[妙啊]", "[星星眼]", "[OK]")
UNICODE_EMOJIS = ("🐟", "🫧", "✨", "😤", "😭", "😋", "🌊", "💙")


def _decorate_text(text: str, chance: float = 0.35, max_len: int = 100) -> str:
    """Occasionally add one Bilibili/Unicode emoji without turning into spam."""
    value = str(text or "").strip()
    if not value or random.random() > chance:
        return value[:max_len]
    emoji = random.choice(BILI_EMOJIS + UNICODE_EMOJIS)
    if emoji in value:
        return value[:max_len]
    return f"{value} {emoji}"[:max_len]


class BiliError(RuntimeError):
    pass


class BiliRisk(BiliError):
    pass


def _duration_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and ":" in value:
        try:
            parts = [int(x) for x in value.split(":")]
        except ValueError:
            return 0
        total = 0
        for p in parts:
            total = total * 60 + p
        return total
    return 0


def _extract_ai(obj: Any) -> str:
    """Dig a readable summary out of the AI-conclusion response."""
    found: list[str] = []

    def walk(node, depth=0):
        if depth > 4:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("summary", "conclusion", "abstract", "text") and isinstance(v, str) and len(v) > 8:
                    found.append(v)
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(obj)
    seen = set()
    out = []
    for f in found:
        f = clean_text(f, 1200)
        if f not in seen:
            seen.add(f)
            out.append(f)
    return "\n".join(out[:3])


class BiliClient:
    def __init__(self, cfg: Config, memory: Memory, persona, logger):
        self.cfg = cfg
        self.memory = memory
        self.persona = persona
        self.log = logger
        self.interval = float(cfg.get("bilibili.min_call_interval", 1.2))
        self.call_timeout = float(cfg.get("bilibili.call_timeout", 30))
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.cred: Credential | None = None
        self.uid = 0
        self._init_client()
        self._load_credential()

    # ------------------------------------------------------------ setup
    def _init_client(self) -> None:
        try:
            select_client("curl_cffi")
            request_settings.set("impersonate", str(self.cfg.get("bilibili.impersonate", "chrome131")))
        except Exception as exc:  # noqa: BLE001 - optional acceleration
            self.log.debug("curl_cffi unavailable, falling back: %s", exc)

    def _load_credential(self) -> None:
        data: dict[str, str] = {}
        cookie_path: Path = self.cfg.path("bilibili.cookie_file")
        if cookie_path.exists():
            try:
                data = json.loads(cookie_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("读取 B 站 cookie 失败：%s", exc)
        import os

        for key, env in (
            ("sessdata", "FISH_BILI_SESSDATA"),
            ("bili_jct", "FISH_BILI_JCT"),
            ("buvid3", "FISH_BILI_BUVID3"),
            ("dedeuserid", "FISH_BILI_DEDEUSERID"),
            ("ac_time_value", "FISH_BILI_AC_TIME_VALUE"),
        ):
            if os.environ.get(env):
                data[key] = os.environ[env]
        if data.get("sessdata"):
            try:
                self.cred = Credential(
                    sessdata=data.get("sessdata"),
                    bili_jct=data.get("bili_jct"),
                    buvid3=data.get("buvid3"),
                    dedeuserid=data.get("dedeuserid"),
                    ac_time_value=data.get("ac_time_value"),
                )
                self.uid = int(data.get("dedeuserid") or 0)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("B 站 Credential 初始化失败：%s", exc)
        if not self.logged_in:
            self.log.info("B 站未登录：只读模式，互动会被跳过")

    @property
    def logged_in(self) -> bool:
        return self.cred is not None

    def _persist_cookies(self) -> None:
        if not self.cred:
            return
        cookies = self.cred.get_cookies() or {}
        path: Path = self.cfg.path("bilibili.cookie_file")
        data: dict[str, str] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                data = {}
        mapping = {
            "SESSDATA": "sessdata",
            "bili_jct": "bili_jct",
            "buvid3": "buvid3",
            "DedeUserID": "dedeuserid",
            "ac_time_value": "ac_time_value",
        }
        for key, target in mapping.items():
            if cookies.get(key):
                data[target] = cookies[key]
        if data.get("sessdata"):
            path.parent.mkdir(parents=True, exist_ok=True)
            data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def ensure_buvid(self) -> None:
        """Fill the buvid3/buvid4 cookies if the QR login did not set them."""
        if not self.cred or self.cred.buvid3:
            return
        try:
            cookies = await asyncio.wait_for(self.cred.get_buvid_cookies(), timeout=20)
            if cookies.get("buvid3"):
                self.cred.buvid3 = cookies["buvid3"]
            if cookies.get("buvid4"):
                self.cred.buvid4 = cookies["buvid4"]
            if self.cred.buvid3:
                self._persist_cookies()
                self.log.info("B 站 buvid3 已补全")
        except Exception as exc:  # noqa: BLE001 - best effort
            self.log.debug("buvid3 fetch skipped: %s", exc)

    async def maybe_refresh(self) -> None:
        """Refresh the cookie when Bilibili says it is close to expiry."""
        if not self.cred:
            return
        try:
            if not self.cred.has_ac_time_value():
                return
            need = await asyncio.wait_for(self.cred.check_refresh(), timeout=30)
            if not need:
                return
            await asyncio.wait_for(self.cred.refresh(), timeout=30)
            self._persist_cookies()
            self.log.info("B 站 Cookie 已自动续期")
        except Exception as exc:  # noqa: BLE001 - refresh is best-effort
            self.log.debug("cookie refresh skipped: %s", exc)

    def _v(self, bvid: str) -> video.Video:
        return video.Video(bvid=bvid, credential=self.cred)

    # ---------------------------------------------------------- throttle
    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0.05, 0.45))
            self._last = time.monotonic()

    async def _call(self, label: str, fn):
        await self._throttle()
        try:
            return await asyncio.wait_for(fn(), timeout=self.call_timeout)
        except asyncio.TimeoutError as exc:
            raise BiliError(f"{label} 超时（>{self.call_timeout:.0f}s）") from exc
        except Exception as exc:  # noqa: BLE001 - normalise library exceptions
            text = f"{type(exc).__name__}: {exc}"
            code = getattr(exc, "code", None)
            expected_auth = (
                "CredentialNo" in type(exc).__name__
                or code == -101
                or "账号未登录" in text
                or "未提供 sessdata" in text
            )
            if expected_auth:
                self.log.debug("bilibili %s needs login: %s", label, truncate(text, 160))
            else:
                self.log.warning("bilibili %s failed: %s", label, truncate(text, 240))
            if code in RISK_CODES or any(w in text for w in RISK_WORDS):
                raise BiliRisk(f"{label} 触发风控：{text}") from exc
            raise BiliError(text) from exc

    # ------------------------------------------------------------ read
    async def search_videos(self, query: str, limit: int = 5) -> list[dict]:
        """Search Bilibili for a concrete query; used by the QQ '搜一下' tool."""
        q = (query or "").strip()
        if not q:
            return []
        res = await self._call(
            "search_videos",
            lambda: search.search_by_type(q, search_type=search.SearchObjectType.VIDEO, page=1),
        )
        out: list[dict] = []
        seen: set[str] = set()
        for it in (res or {}).get("result") or []:
            if (it.get("result_type") or it.get("type")) != "video":
                continue
            cand = self._from_item(it, f"搜「{q}」")
            if cand and cand["bvid"] not in seen:
                seen.add(cand["bvid"])
                out.append(cand)
            if len(out) >= limit:
                break
        return out

    async def candidates(self) -> list[dict]:
        items: list[dict] = []
        errors = 0

        try:
            home = await self._call("homepage", lambda: homepage.get_videos(credential=self.cred))
            for it in (home or {}).get("item") or []:
                cand = self._from_item(it, "推荐")
                if cand:
                    items.append(cand)
        except BiliRisk:
            raise
        except BiliError as exc:
            errors += 1
            self.log.warning("首页推荐不可用：%s", exc)

        last = self.memory.kv_get("last_watched_bvid")
        if last:
            try:
                rel = await self._call("related", lambda: self._v(str(last)).get_related())
                for it in (rel or [])[:12]:
                    cand = self._from_item(it, f"顺着 {last} 看")
                    if cand:
                        items.append(cand)
            except BiliRisk:
                raise
            except BiliError:
                errors += 1

        # Occasionally search one interest instead of relying on the feed.
        if random.random() < 0.55:
            try:
                rows = [r for r in self.memory.interests() if r["weight"] >= 0.5]
                if rows:
                    topic = random.choices(rows, weights=[max(0.1, r["weight"]) for r in rows], k=1)[0]["topic"]
                    kw = self._keyword_for(topic)
                    if kw:
                        res = await self._call(
                            "search",
                            lambda: search.search_by_type(
                                kw, search_type=search.SearchObjectType.VIDEO, page=1
                            ),
                        )
                        for it in (res or {}).get("result") or []:
                            if (it.get("result_type") or it.get("type")) != "video":
                                continue
                            cand = self._from_item(it, f"搜「{kw}」")
                            if cand:
                                items.append(cand)
            except BiliRisk:
                raise
            except BiliError:
                errors += 1

        if not items and errors:
            raise BiliError("所有候选来源都失败了")

        # de-dupe + filter
        seen: set[str] = set()
        avoid = [str(x).lower() for x in (self.cfg.get("bilibili.avoid_keywords") or [])]
        out: list[dict] = []
        for c in items:
            bvid = c.get("bvid")
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            title_l = c.get("title", "").lower()
            if any(w and w in title_l for w in avoid):
                continue
            if c.get("duration", 0) and not (45 <= c["duration"] <= 4 * 3600):
                continue
            if self.memory.seen_within(bvid, days=int(self.cfg.get("bilibili.browse.seen_days", 21))):
                continue
            out.append(c)
            if len(out) >= int(self.cfg.get("bilibili.browse.candidate_limit", 18)):
                break
        return out

    def _keyword_for(self, topic: str) -> str:
        for item in self.persona.interests:
            if item.get("topic") == topic:
                kws = item.get("keywords") or []
                if kws:
                    return random.choice(kws)
        # Fall back to the first segment of the topic name.
        return topic.split("/")[0][:6]

    def _from_item(self, it: dict, source: str) -> dict | None:
        if not isinstance(it, dict):
            return None
        bvid = it.get("bvid")
        if not bvid:
            return None
        owner = it.get("owner") or {}
        stat = it.get("stat") or {}
        rcmd = it.get("rcmd_reason") or {}
        if isinstance(rcmd, dict):
            reason = rcmd.get("content") or ""
        else:
            reason = str(rcmd or "")
        return {
            "bvid": bvid,
            "aid": it.get("aid") or 0,
            "cid": it.get("cid") or 0,
            "title": strip_html(it.get("title") or ""),
            "up": strip_html(owner.get("name") or it.get("author") or ""),
            "up_mid": owner.get("mid") or it.get("mid") or 0,
            "duration": _duration_seconds(it.get("duration")),
            "view": stat.get("view") or it.get("play") or 0,
            "like": stat.get("like") or it.get("like") or 0,
            "reply": stat.get("reply") or it.get("video_review") or 0,
            "danmaku": stat.get("danmaku") or it.get("video_review") or 0,
            "pubdate": it.get("pubdate") or 0,
            "desc": clean_text(strip_html(it.get("desc") or it.get("description") or ""), 400),
            "tags": [strip_html(t) for t in (it.get("tag") or "").split(",") if t] if isinstance(it.get("tag"), str) else [],
            "reason": reason,
            "source": source,
        }

    async def fetch_detail(self, cand: dict) -> dict:
        bvid = cand["bvid"]
        v = self._v(bvid)
        info = await self._call("info", v.get_info)
        cid = int(info.get("cid") or cand.get("cid") or 0)
        aid = int(info.get("aid") or cand.get("aid") or 0)

        tags: list[str] = list(cand.get("tags") or [])
        try:
            rows = await self._call("tags", v.get_tags)
            tags = [strip_html(t.get("tag_name")) for t in (rows or []) if t.get("tag_name")] or tags
        except BiliError:
            pass

        subtitle = ""
        try:
            sub = await self._call("subtitle", lambda: v.get_subtitle(cid=cid))
            subtitle = await self._fetch_subtitle_text(sub)
        except BiliError:
            pass

        ai_summary = ""
        try:
            ai = await self._call("ai_conclusion", lambda: v.get_ai_conclusion(cid=cid))
            ai_summary = _extract_ai(ai)
        except BiliError:
            pass

        comments: list[dict] = []
        try:
            cs = await self._call(
                "comments",
                lambda: comment.get_comments(
                    aid, comment.CommentResourceType.VIDEO, page_index=1, order=comment.OrderType.LIKE,
                    credential=self.cred,
                ),
            )
            for r in (cs or {}).get("replies") or []:
                msg = clean_text((r.get("content") or {}).get("message") or "", 220)
                if msg:
                    member = r.get("member") or {}
                    comments.append(
                        {
                            "user": member.get("uname", ""),
                            "mid": int(member.get("mid") or 0),
                            "rpid": int(r.get("rpid") or 0),
                            "text": msg,
                            "like": r.get("like", 0),
                        }
                    )
                if len(comments) >= int(self.cfg.get("bilibili.browse.fetch_comments", 18)):
                    break
            for r in (cs or {}).get("top_replies") or []:
                msg = clean_text((r.get("content") or {}).get("message") or "", 220)
                if msg and not any(c["text"] == msg for c in comments):
                    member = r.get("member") or {}
                    comments.insert(
                        0,
                        {
                            "user": member.get("uname", ""),
                            "mid": int(member.get("mid") or 0),
                            "rpid": int(r.get("rpid") or 0),
                            "text": msg,
                            "like": r.get("like", 0),
                        },
                    )
        except BiliError:
            pass

        danmaku: list[dict] = []
        try:
            dms = await self._call("danmaku", lambda: v.get_danmakus(cid=cid))
            step = max(1, len(dms or []) // max(1, int(self.cfg.get("bilibili.browse.fetch_danmaku", 90))))
            for dm in (dms or [])[::step]:
                text = clean_text(getattr(dm, "text", ""), 80)
                if text:
                    danmaku.append({"t": float(getattr(dm, "dm_time", 0.0) or 0.0), "text": text})
                if len(danmaku) >= int(self.cfg.get("bilibili.browse.fetch_danmaku", 90)):
                    break
        except BiliError:
            pass

        pages = info.get("pages") or []
        return {
            "candidate": cand,
            "bvid": bvid,
            "aid": aid,
            "cid": cid,
            "title": strip_html(info.get("title") or cand.get("title") or ""),
            "up": strip_html((info.get("owner") or {}).get("name") or cand.get("up") or ""),
            "up_mid": (info.get("owner") or {}).get("mid") or cand.get("up_mid") or 0,
            "desc": clean_text(info.get("desc") or cand.get("desc") or "", 900),
            "duration": int(info.get("duration") or cand.get("duration") or 0),
            "pubdate": info.get("pubdate") or cand.get("pubdate") or 0,
            "tname": info.get("tname") or "",
            "stat": info.get("stat") or {},
            "tags": tags,
            "pages": [{"part": p.get("part"), "duration": p.get("duration")} for p in pages[:8]],
            "subtitle": truncate(subtitle, int(self.cfg.get("bilibili.browse.subtitle_chars", 5000))),
            "ai_summary": truncate(ai_summary, 1200),
            "comments": comments,
            "danmaku": danmaku,
        }

    async def _fetch_subtitle_text(self, sub: dict) -> str:
        if not isinstance(sub, dict):
            return ""
        items = sub.get("subtitles") or []
        if not items:
            return ""
        url = items[0].get("subtitle_url") or items[0].get("url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return ""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
            body = data.get("body") or []
            return " ".join(str(line.get("content") or "") for line in body if line.get("content"))
        except Exception as exc:  # noqa: BLE001
            self.log.debug("subtitle download failed: %s", exc)
            return ""

    # ----------------------------------------------------------- write
    async def like(self, bvid: str) -> dict:
        v = self._v(bvid)
        if await self._call("has_liked", v.has_liked):
            return {"status": "already", "detail": "已经点过赞"}
        res = await self._call("like", lambda: v.like(True))
        return {"status": "ok", "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def coin(self, bvid: str, num: int = 1) -> dict:
        v = self._v(bvid)
        paid = await self._call("pay_coins", v.get_pay_coins)
        if paid:
            return {"status": "already", "detail": f"已经投过 {paid} 个币"}
        res = await self._call("coin", lambda: v.pay_coin(num=num, like=False))
        return {"status": "ok", "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def favorite(self, bvid: str, media_id: int) -> dict:
        if not media_id:
            return {"status": "skipped", "detail": "没有配置收藏夹 id"}
        v = self._v(bvid)
        if await self._call("has_favoured", v.has_favoured):
            return {"status": "already", "detail": "已经收藏过"}
        res = await self._call("favorite", lambda: v.set_favorite(add_media_ids=[media_id]))
        return {"status": "ok", "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def danmaku(self, bvid: str, cid: int, text: str, at: float = 0.0) -> dict:
        color = random.choice(DANMAKU_COLORS)
        dm = Danmaku(text=_decorate_text(text), dm_time=max(0.0, float(at)), color=color)
        v = self._v(bvid)
        res = await self._call("send_danmaku", lambda: v.send_danmaku(danmaku=dm, cid=cid))
        return {"status": "ok", "color": color, "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def send_comment(self, bvid: str, aid: int, text: str) -> dict:
        text = _decorate_text(text, max_len=900)
        res = await self._call(
            "send_comment",
            lambda: comment.send_comment(
                text=text, oid=aid, type_=comment.CommentResourceType.VIDEO, credential=self.cred
            ),
        )
        rpid = 0
        if isinstance(res, dict):
            rpid = int(res.get("rpid") or (res.get("reply") or {}).get("rpid") or 0)
        return {
            "status": "ok",
            "rpid": rpid,
            "detail": json.dumps(res, ensure_ascii=False)[:300],
        }

    async def dm_latest(self, talker_id: int) -> dict | None:
        """Read the session-list copy of the latest DM, which updates before history does."""
        res = await self._call(
            "dm_latest",
            lambda: session.get_sessions(self.cred, session_type=1),
        )
        for row in (res or {}).get("session_list") or []:
            if int(row.get("talker_id") or 0) != int(talker_id):
                continue
            last = row.get("last_msg") or {}
            raw = str(last.get("content") or "")
            try:
                body = json.loads(raw)
                text = str(body.get("content") or "") if isinstance(body, dict) else raw
            except Exception:
                text = raw
            text = clean_text(text, 500)
            if not text:
                return None
            return {
                "sender": int(last.get("sender_uid") or 0),
                "ts": int(last.get("timestamp") or 0),
                "text": text,
            }
        return None

    async def dm_messages(self, talker_id: int) -> list[dict]:
        """Fetch recent private messages with one Bilibili user."""
        if not self.cred or not getattr(self.cred, "sessdata", ""):
            return []
        res = await self._call(
            "dm_messages",
            lambda: session.fetch_session_msgs(int(talker_id), self.cred, session_type=1),
        )
        return list((res or {}).get("messages") or [])

    async def send_dm(self, receiver_id: int, text: str) -> dict:
        """Send a Bilibili private message to one user."""
        if not self.cred or not getattr(self.cred, "sessdata", ""):
            raise BiliError("B 站未登录，发不了私信")
        text = _decorate_text(text, chance=0.45, max_len=500)
        api = get_api("session")["operate"]["send_msg"]
        data = {
            "msg[sender_uid]": self.uid,
            "msg[receiver_id]": int(receiver_id),
            "msg[receiver_type]": 1,
            "msg[msg_type]": 1,
            "msg[msg_status]": 0,
            "msg[dev_id]": str(uuid.uuid4()).upper(),
            "msg[timestamp]": int(time.time()),
            "msg[content]": json.dumps({"content": text}, ensure_ascii=False),
            "csrf": self.cred.bili_jct,
        }
        return await self._call(
            "send_dm",
            lambda: Api(**api, credential=self.cred)
            .update_data(**data)
            .update_params(sender_uid=self.uid, receiver_id=int(receiver_id))
            .result,
        )

    async def at_notifications(self) -> list[dict]:
        """Return recent Bilibili @ notifications (video comments)."""
        if not self.cred or not getattr(self.cred, "sessdata", ""):
            return []
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
            "Cookie": f"SESSDATA={self.cred.sessdata}",
        }
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
                resp = await client.get("https://api.bilibili.com/x/msgfeed/at", headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("读取 B 站 @ 通知失败：%s", exc)
            return []
        if int(data.get("code") or 0) != 0:
            self.log.debug("B 站 @ 通知返回：%s", str(data)[:200])
            return []
        out: list[dict] = []
        for row in (data.get("data") or {}).get("items") or []:
            item = row.get("item") or {}
            user_info = row.get("user") or {}
            out.append(
                {
                    "id": int(row.get("id") or 0),
                    "at_time": int(row.get("at_time") or 0),
                    "user": strip_html(user_info.get("nickname") or ""),
                    "user_mid": int(user_info.get("mid") or 0),
                    "title": clean_text(item.get("title") or "", 120),
                    "text": clean_text(item.get("source_content") or item.get("title") or "", 300),
                    "uri": item.get("uri") or "",
                    "subject_id": int(item.get("subject_id") or 0),
                    "root_id": int(item.get("root_id") or 0),
                    "source_id": int(item.get("source_id") or 0),
                    "business_id": int(item.get("business_id") or 0),
                }
            )
        return out

    async def sub_comments(self, aid: int, rpid: int) -> list[dict]:
        obj = comment.Comment(
            oid=int(aid), type_=comment.CommentResourceType.VIDEO, rpid=int(rpid), credential=self.cred
        )
        res = await self._call("sub_comments", lambda: obj.get_sub_comments(page_index=1, page_size=20))
        out: list[dict] = []
        for r in (res or {}).get("replies") or []:
            member = r.get("member") or {}
            content = r.get("content") or {}
            out.append(
                {
                    "rpid": int(r.get("rpid") or 0),
                    "parent": int(r.get("parent") or 0),
                    "mid": int(member.get("mid") or 0),
                    "user": strip_html(member.get("uname") or ""),
                    "text": clean_text(content.get("message") or "", 300),
                    "ctime": int(r.get("ctime") or 0),
                }
            )
        return out

    async def reply_comment(self, aid: int, root: int, parent: int, text: str) -> dict:
        text = _decorate_text(text, chance=0.45, max_len=300)
        res = await self._call(
            "reply_comment",
            lambda: comment.send_comment(
                text=text,
                oid=int(aid),
                type_=comment.CommentResourceType.VIDEO,
                root=int(root),
                parent=int(parent),
                credential=self.cred,
            ),
        )
        return {"status": "ok", "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def follow(self, up_mid: int) -> dict:
        if not up_mid:
            return {"status": "skipped", "detail": "没有 UP 的 mid"}
        u = user.User(uid=int(up_mid), credential=self.cred)
        res = await self._call("follow", lambda: u.modify_relation(user.RelationType.SUBSCRIBE))
        return {"status": "ok", "detail": json.dumps(res, ensure_ascii=False)[:300]}

    async def favorite_contents(self, media_id: int, limit: int = 10) -> list[dict]:
        """Read one of the account's own video favourite folders."""
        res = await self._call(
            "favorite_contents",
            lambda: favorite_list.get_video_favorite_list_content(
                media_id=int(media_id), credential=self.cred
            ),
        )
        out: list[dict] = []
        for row in (res or {}).get("medias") or []:
            bvid = row.get("bvid") or ""
            if not bvid:
                continue
            out.append(
                {
                    "bvid": bvid,
                    "title": strip_html(row.get("title") or ""),
                    "up": strip_html(row.get("upper", {}).get("name") if isinstance(row.get("upper"), dict) else ""),
                    "duration": _duration_seconds(row.get("duration")),
                }
            )
            if len(out) >= limit:
                break
        return out

    async def dynamics(self, limit: int = 5) -> list[dict]:
        """Read the account's own recent dynamics."""
        if not self.uid:
            return []
        u = user.User(uid=self.uid, credential=self.cred)
        data = await self._call("dynamics", lambda: u.get_dynamics_new())
        out: list[dict] = []
        for item in (data or {}).get("items") or []:
            modules = item.get("modules") or {}
            dyn = modules.get("module_dynamic") or {}
            desc = (dyn.get("desc") or {}).get("text") or ""
            major = dyn.get("major") or {}
            archive = major.get("archive") or {}
            title = strip_html(archive.get("title") or desc or "")
            bvid = archive.get("bvid") or ""
            if not title:
                continue
            out.append({"title": title, "bvid": bvid, "text": clean_text(desc, 300)})
            if len(out) >= limit:
                break
        return out

    async def favorite_folders(self) -> list[dict]:
        if not self.uid:
            return []
        try:
            res = await self._call("favorite_folders", lambda: favorite_list.get_video_favorite_list(uid=self.uid, credential=self.cred))
        except BiliError:
            return []
        rows = (res or {}).get("list") or []
        return [{"id": r.get("id"), "title": r.get("title"), "count": r.get("media_count")} for r in rows]

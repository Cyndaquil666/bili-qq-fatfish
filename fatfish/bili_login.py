"""Bilibili web QR login with direct cookie capture.

bilibili-api-python's QrCodeLogin parsed an empty cookie URL in this
environment (login reported success, but SESSDATA/bili_jct/DedeUserID came out
empty).  This implementation talks to the passport endpoints directly and
captures cookies from both the response cookie jar and the returned cross-domain
URL, so it is not dependent on that parser.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import qrcode

GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class BiliLoginError(RuntimeError):
    pass


def _pick(query: dict, name: str) -> str:
    for key in (name, name.lower(), name.upper()):
        values = query.get(key)
        if values:
            return str(values[0])
    return ""


async def login_qr(qr_path: Path, timeout: float = 300.0, logger=None, poll_interval: float = 2.0) -> dict:
    qr_path.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        resp = await client.get(GENERATE)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        url = data.get("url") or ""
        key = data.get("qrcode_key") or ""
        if not url or not key:
            raise BiliLoginError("B 站没有返回二维码，可能被风控或接口变更")

        img = qrcode.make(url)
        img.save(str(qr_path))
        if logger:
            logger.info("二维码已生成：%s", qr_path)

        deadline = asyncio.get_running_loop().time() + timeout
        last_state = None
        while asyncio.get_running_loop().time() < deadline:
            poll = await client.get(POLL, params={"qrcode_key": key})
            poll.raise_for_status()
            body = poll.json()
            payload = body.get("data") or {}
            code = payload.get("code")
            state = {86101: "waiting", 86090: "scanned", 86038: "expired", 0: "done"}.get(code, f"code:{code}")
            if state != last_state:
                if logger:
                    logger.info("扫码状态：%s", state)
                last_state = state

            if code == 86038:
                raise BiliLoginError("二维码已过期，请重新运行登录")
            if code != 0:
                await asyncio.sleep(poll_interval)
                continue

            # Cookies are set by the poll response; the cross-domain URL also
            # carries them.  Use both, in case the cookie jar misses one.
            jar = {k: v for k, v in client.cookies.items()}
            query = parse_qs(urlparse(payload.get("url") or "").query)
            sessdata = jar.get("SESSDATA") or _pick(query, "SESSDATA")
            bili_jct = jar.get("bili_jct") or _pick(query, "bili_jct")
            dedeuserid = jar.get("DedeUserID") or _pick(query, "DedeUserID")
            if not sessdata or not bili_jct:
                raise BiliLoginError(
                    "登录接口返回成功，但没有拿到 SESSDATA/bili_jct；"
                    "可能是 B 站改了返回格式，请把这条错误发给我。"
                )
            return {
                "sessdata": sessdata,
                "bili_jct": bili_jct,
                "buvid3": jar.get("buvid3") or _pick(query, "buvid3"),
                "dedeuserid": dedeuserid,
                "ac_time_value": payload.get("refresh_token") or "",
            }
        raise BiliLoginError("等待扫码超时")

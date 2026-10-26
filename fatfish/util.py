"""Small shared helpers: time, text, logging, files."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent


def now() -> datetime:
    return datetime.now(TZ)


def ts() -> int:
    return int(time.time())


def day_str(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m-%d")


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_hhmm(value: str) -> dtime:
    hh, mm = value.split(":")
    return dtime(int(hh), int(mm))


def minutes_since(dt: datetime) -> float:
    return (now() - dt).total_seconds() / 60


def in_window(dt: datetime, start: str, end: str) -> bool:
    """True when dt's clock time is inside [start, end), handling midnight wrap."""
    s, e = parse_hhmm(start), parse_hhmm(end)
    t = dt.timetz().replace(tzinfo=None)
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def in_quiet_hours(dt: datetime, windows: list[list[str]] | None) -> bool:
    for w in windows or []:
        if len(w) == 2 and in_window(dt, w[0], w[1]):
            return True
    return False


def jitter(lo: float, hi: float) -> float:
    return random.uniform(lo, hi)


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_ts(value: int | float | None) -> str:
    if not value:
        return "?"
    return datetime.fromtimestamp(value, TZ).strftime("%H:%M")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


def clean_text(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(1, limit - len(suffix))] + suffix


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fatfish")
    if logger.handlers:
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    rotating = logging.handlers.RotatingFileHandler(
        log_dir / "fish.log", maxBytes=4 * 1024 * 1024, backupCount=7, encoding="utf-8"
    )
    rotating.setFormatter(fmt)
    logger.addHandler(rotating)

    # Keep third-party noise out of our log file.
    for noisy in ("httpx", "httpcore", "websockets", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


def mask(value: str | None, keep: int = 4) -> str:
    if not value:
        return "<empty>"
    value = str(value)
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "..." + value[-keep:]


def human_ago(epoch: int | float | None) -> str:
    if not epoch:
        return "从未"
    delta = time.time() - float(epoch)
    if delta < 60:
        return f"{int(delta)}秒前"
    if delta < 3600:
        return f"{int(delta / 60)}分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)}小时前"
    return f"{int(delta / 86400)}天前"


def split_messages(text: str, max_chars: int, max_parts: int = 2) -> list[str]:
    """Split a chat reply on natural punctuation, keeping each part short."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。！？!?～~\n" and len(buf) >= max_chars * 0.5:
            parts.append(buf.strip())
            buf = ""
        if len(buf) >= max_chars:
            parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    parts = [p for p in parts if p][:max_parts]
    return [truncate(p, max_chars) for p in parts]


def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def pid_alive(pid: int) -> bool:
    """Cross-platform-ish check used to stop a second fish instance."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def load_env_file(path: Path) -> None:
    """Tiny .env loader: KEY=VALUE lines, no overriding real environment."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value

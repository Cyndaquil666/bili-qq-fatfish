"""Configuration loading with defaults and secret indirection.

Secrets never need to live in config.yaml.  Any string value can be written as
``env:NAME`` or ``file:path/to/secret`` and is resolved lazily.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from .util import ROOT, load_env_file

DEFAULTS: dict[str, Any] = {
    "mode": "observe",  # observe | live
    "logging": {"level": "INFO"},
    "safety": {
        "live_ack": False,
        "minor_mode": True,
        "quiet_hours": [["00:30", "08:30"]],
        "risk_pause_hours": 12,
        "max_daily_llm_tokens": 1500000,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "vision_model": "deepseek-v4-flash-vision-exp",
        "vision_enabled": True,
        "vision_max_tokens": 220,
        "api_key": "env:FISH_LLM_API_KEY",
        "temperature": 0.9,
        "max_tokens": 900,
        "timeout": 90,
        "retries": 2,
        "json_mode": True,
    },
    "bilibili": {
        "enabled": True,
        "cookie_file": "secrets/bilibili.json",
        "impersonate": "chrome131",
        "min_call_interval": 1.2,
        "call_timeout": 30,
        "favorite_media_id": 0,
        "avoid_keywords": [],
        "browse": {
            "active_hours": ["09:30", "23:30"],
            "min_gap_minutes": 60,
            "max_gap_minutes": 60,
            "batch_size": 2,
            "max_videos_per_day": 60,
            "max_comment_worthy_per_day": 30,
            "candidate_limit": 18,
            "min_pick_score": 56,
            "exploration_rate": 0.12,
            "seen_days": 21,
            "fetch_comments": 18,
            "fetch_danmaku": 90,
            "subtitle_chars": 5000,
        },
        "other_fish": {
            "enabled": True,
            "names": ["大肥鱼", "肥鱼", "鱼鱼"],
            "min_video_score": 70,
            "max_per_day": 8,
            "gap_minutes": 20,
        },
        "join_comments": {
            "enabled": True,
            "min_video_score": 65,
            "max_per_day": 6,
            "gap_minutes": 20,
        },
        "write": {
            "enabled_actions": ["like", "coin", "favorite", "danmaku"],
            "daily_caps": {
                "like": 3,
                "coin": 1,
                "favorite": 1,
                "danmaku": 1,
                "comment": 1,
                "follow": 1,
            },
            "min_score": {
                "like": 76,
                "coin": 88,
                "favorite": 82,
                "danmaku": 84,
                "comment": 91,
                "follow": 92,
            },
            "weekly_caps": {"comment": 1, "follow": 1},
            "min_gap_minutes": {"like": 45, "coin": 240, "favorite": 240, "danmaku": 180, "comment": 2880, "follow": 4320},
        },
        "comments": {
            "enabled": True,
            "check_minutes": 60,
            "max_threads": 5,
            "reply_gap_minutes": 60,
            "max_replies_per_day": 6,
            "reply_to_outsiders": True,
            "user_name": "主人B站",
        },
        "notifications": {
            "enabled": True,
            "check_minutes": 1,
            "reply_to_user": True,
        },
        "dm": {
            "enabled": True,
            "user_mid": 0,
            "check_seconds": 15,
        },
        "proactive": {
            "enabled": True,
            "first_delay_minutes": 10,
            "min_gap_minutes": 45,
            "max_gap_minutes": 150,
            "max_per_day": 4,
            "min_idle_minutes": 60,
            "active_hours": ["09:00", "23:30"],
        },
    },
    "qq": {
        "enabled": True,
        "onebot": {
            "ws_url": "ws://127.0.0.1:3001",
            "access_token": "env:FISH_ONEBOT_TOKEN",
            "self_id": 0,
            "reconnect_max_seconds": 60,
        },
        "groups": [],
        "private_users": [],
        "private_allow_strangers": True,
        "stranger_max_msgs_per_day": 30,
        "stranger_min_gap_seconds": 30,
        "stranger_min_confidence": 0.72,
        "private_debounce_seconds": 0,
        "private_max_msgs_per_day": 0,
        "private_min_gap_seconds": 0,
        "proactive": {
            "enabled": True,
            "first_delay_minutes": 5,
            "min_gap_minutes": 120,
            "max_gap_minutes": 240,
            "max_per_day": 4,
            "min_idle_minutes": 90,
            "active_hours": ["09:30", "23:30"],
        },
        "active_hours": ["08:30", "00:30"],
        "debounce_seconds": 5,
        "context_messages": 24,
        "context_minutes": 20,
        "max_context_chars": 3000,
        "min_gap_seconds": 0,
        "max_msgs_per_day_per_group": 200,
        "max_msgs_per_day_total": 200,
        "max_msgs_per_window": 20,
        "window_seconds": 600,
        "max_chars_per_msg": 58,
        "max_parts": 2,
        "min_confidence": 0.68,
        "min_confidence_at": 0.52,
        "reply_cooldown_seconds": 5,
        "stickers": {
            "enabled": True,
            "dir": "assets/stickers",
            "chance": 0.15,
            "max_per_day": 3,
        },
        "reply_to_at": True,
        "ignore_keywords": ["/", "#"],
        "blocked_users": [],
    },
    "identity": {
        "persona_file": "persona.yaml",
        "display_name": "大肥鱼",
    },
    "paths": {
        "data_dir": "var",
        "log_dir": "var/logs",
        "journal_dir": "var/journal",
        "report_dir": "var/reports",
        "db_file": "var/fish.db",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class ConfigError(RuntimeError):
    pass


class Config:
    def __init__(self, raw: dict, path: Path | None = None):
        self.raw = _deep_merge(DEFAULTS, raw or {})
        self.config_path = path or (ROOT / "config.yaml")
        self.root = self.config_path.resolve().parent

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        load_env_file(ROOT / ".env")
        p = Path(path) if path else ROOT / "config.yaml"
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        if not p.exists():
            example = p.parent / "config.example.yaml"
            if example.exists():
                raise ConfigError(f"没有找到 {p}。先复制配置：copy config.example.yaml config.yaml")
            raise ConfigError(f"没有找到配置文件 {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError("config.yaml 顶层必须是映射")
        return cls(raw, p)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def secret(self, dotted: str, default: str = "") -> str:
        """Resolve env:/file: indirection. Returns '' when unset."""
        value = self.get(dotted)
        if value is None:
            return default
        value = str(value)
        if value.startswith("env:"):
            return os.environ.get(value[4:], default)
        if value.startswith("file:"):
            p = Path(value[5:])
            if not p.is_absolute():
                p = self.root / p
            try:
                return p.read_text(encoding="utf-8").strip()
            except OSError:
                return default
        return value

    def path(self, dotted: str, default: str = "") -> Path:
        value = self.get(dotted, default)
        p = Path(value)
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def mode(self) -> str:
        return str(self.get("mode", "observe")).lower()

    @property
    def live(self) -> bool:
        return self.mode == "live" and bool(self.get("safety.live_ack"))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode not in ("observe", "live"):
            problems.append(f"mode 只能是 observe 或 live，当前是 {self.mode!r}")
        if self.mode == "live" and not self.get("safety.live_ack"):
            problems.append("mode=live 但没有 safety.live_ack=true，出于安全会按 observe 运行")
        if self.live and not self.secret("llm.api_key"):
            problems.append("live 模式需要 LLM API key（llm.api_key 或环境变量 FISH_LLM_API_KEY）")
        if self.get("bilibili.enabled"):
            import os

            cookie_path = self.path("bilibili.cookie_file")
            if not cookie_path.exists() and not os.environ.get("FISH_BILI_SESSDATA"):
                problems.append("B 站未登录（secrets/bilibili.json 不存在），只能匿名只读，互动会被跳过")
        groups = self.get("qq.groups") or []
        if self.get("qq.enabled") and not groups:
            problems.append("qq.groups 为空：QQ 只监听不发言（这是安全的默认值）")
        return problems

    def dump_safe(self) -> dict:
        """Config copy with secret-looking values masked, for logs/status."""
        def scrub(obj, key=""):
            if isinstance(obj, dict):
                return {k: scrub(v, k) for k, v in obj.items()}
            if isinstance(obj, list):
                return [scrub(v, key) for v in obj]
            if any(s in key.lower() for s in ("key", "token", "sessdata", "jct", "secret", "cookie")):
                return "***" if obj else ""
            return obj

        return scrub(self.raw)

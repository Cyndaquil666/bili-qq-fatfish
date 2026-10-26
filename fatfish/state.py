"""Slow-moving internal state: mood, energy, social battery, curiosity.

This is what keeps the fish from behaving like a fixed script: thresholds
drift a little with how it feels, and it can be "not in the mood" without any
special-case code.
"""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field

from .memory import Memory
from .util import now

MOODS = ["元气", "困", "炸毛", "社恐", "上头", "有点丧", "走神", "平静", "有点懒"]


@dataclass
class MindState:
    mood: str = "平静"
    energy: float = 62.0
    sociability: float = 46.0
    curiosity: float = 58.0
    updated_at: int = field(default_factory=lambda: int(time.time()))
    last_watch_at: int = 0
    last_speak_at: int = 0
    watches_today: int = 0
    speaks_today: int = 0
    day: str = ""

    def clamp(self) -> None:
        self.energy = max(8.0, min(100.0, self.energy))
        self.sociability = max(0.0, min(100.0, self.sociability))
        self.curiosity = max(5.0, min(100.0, self.curiosity))

    def mood_hint(self) -> str:
        return (
            f"心情：{self.mood}；精力：{self.energy:.0f}/100；"
            f"社交电量：{self.sociability:.0f}/100；好奇：{self.curiosity:.0f}/100"
        )

    def bili_bias(self) -> float:
        """Added to the pick threshold: low energy makes it pickier."""
        bias = 0.0
        if self.mood in ("有点懒", "困"):
            bias += 5
        elif self.mood == "上头":
            bias -= 5
        elif self.mood == "有点丧":
            bias += 7
        elif self.mood in ("炸毛", "社恐"):
            bias += 2
        if self.energy < 30:
            bias += 5
        if self.sociability < 20:
            bias += 2
        return bias

    def qq_bias(self) -> float:
        """Added to the confidence threshold: tired fish talks less."""
        bias = 0.0
        if self.energy < 30:
            bias += 0.12
        if self.sociability < 25:
            bias += 0.10
        elif self.sociability > 75:
            bias -= 0.05
        if self.mood == "有点丧":
            bias += 0.08
        elif self.mood == "困":
            bias += 0.08
        elif self.mood == "社恐":
            bias += 0.10
        elif self.mood == "上头":
            bias -= 0.08
        elif self.mood == "炸毛":
            bias -= 0.05
        return bias


class StateStore:
    KEY = "mind_state"

    def __init__(self, memory: Memory):
        self.memory = memory
        self.state = self._load()

    def _load(self) -> MindState:
        raw = self.memory.kv_get(self.KEY)
        if not isinstance(raw, dict):
            st = MindState()
            st.day = now().strftime("%Y-%m-%d")
            return st
        allowed = {f: raw[f] for f in MindState.__dataclass_fields__ if f in raw}
        try:
            st = MindState(**allowed)
        except Exception:
            st = MindState()
        self._roll_day(st)
        return st

    def _roll_day(self, st: MindState) -> None:
        today = now().strftime("%Y-%m-%d")
        if st.day != today:
            st.day = today
            st.watches_today = 0
            st.speaks_today = 0

    def save(self) -> None:
        self._roll_day(self.state)
        self.state.clamp()
        self.state.updated_at = int(time.time())
        self.memory.kv_set(self.KEY, asdict(self.state))

    def decay(self) -> None:
        st = self.state
        elapsed_min = max(0.0, (time.time() - st.updated_at) / 60)
        if elapsed_min <= 0:
            return
        # Social battery refills while alone; energy drifts back slowly.
        st.sociability += min(12.0, elapsed_min / 12.0)
        st.energy += min(10.0, elapsed_min / 30.0)
        if st.sociability > 70:
            st.sociability = 70
        st.clamp()
        self.save()

    def after_watch(self, score: float, interacted: bool) -> None:
        st = self.state
        st.last_watch_at = int(time.time())
        st.watches_today += 1
        st.energy -= 2.5 if interacted else 1.2
        st.curiosity += (score - 50.0) / 100.0
        if score >= 85:
            st.mood = "上头"
            st.curiosity += 2
        elif score >= 65:
            st.mood = "元气"
        elif score < 40:
            st.mood = "困"
            st.curiosity -= 1.5
        elif st.mood not in ("有点丧",):
            st.mood = "元气"
        if st.energy < 25 and st.mood == "上头":
            st.mood = "困"
        self.save()

    def after_speak(self, chars: int) -> None:
        st = self.state
        st.last_speak_at = int(time.time())
        st.speaks_today += 1
        st.sociability -= 4.0 + min(6.0, chars / 30.0)
        st.energy -= 1.0
        if st.sociability < 15:
            st.mood = "社恐"
        self.save()

    def after_silence(self, considered: bool = False) -> None:
        st = self.state
        st.sociability += 0.8 if considered else 0.3
        self.save()

    def maybe_shift_mood(self, rng: random.Random | None = None) -> None:
        """Occasional, small, explainable mood drift instead of random chaos."""
        rng = rng or random
        if rng.random() < 0.12:
            if self.state.energy < 30:
                self.state.mood = rng.choice(["困", "有点懒", "走神"])
            elif self.state.sociability > 70:
                self.state.mood = rng.choice(["上头", "元气", "炸毛"])
            else:
                self.state.mood = rng.choice(["元气", "平静", "社恐", "炸毛"])
            self.save()

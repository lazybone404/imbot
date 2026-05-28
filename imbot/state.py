import json
import os
import time
from pathlib import Path

MOODS = ("安静", "低落", "烦躁", "好奇", "开心", "疲惫")
STAGES = ("初识", "熟识", "亲近", "深层", "羁绊")
THRESHOLDS = (0, 5, 20, 50, 100)


class RuntimeState:
    def __init__(self, path: str, owner_id: str, owner_baseline_stage: str = "熟识"):
        self._path = path
        self._owner_id = owner_id
        self._owner_baseline = owner_baseline_stage
        self.mood = "安静"
        self.mood_intensity = 0.5
        self.energy = 1.0
        self._previous_mood = "安静"
        self._familiarity: dict[str, int] = {}
        self._silence_count: dict[str, int] = {}
        self._last_interaction: dict[str, float] = {}
        self._last_proactive_sent: float = 0.0

    # ── 说话者分类 ──
    def classify_speaker(self, user_id: str) -> str:
        if not user_id:
            return "stranger"
        if user_id == self._owner_id:
            return "owner"
        count = self._familiarity.get(user_id, 0)
        return "known" if count >= 5 else "stranger"

    # ── 熟悉度 ──
    def get_stage(self, user_id: str) -> str:
        count = self._familiarity.get(user_id, 0)
        for i in range(len(THRESHOLDS) - 1, -1, -1):
            if count >= THRESHOLDS[i]:
                return STAGES[i]
        return STAGES[0]

    @property
    def familiar_count(self) -> dict[str, int]:
        return self._familiarity

    @property
    def last_interaction(self) -> dict[str, float]:
        return self._last_interaction

    def get_interaction_count(self, user_id: str) -> int:
        return self._familiarity.get(user_id, 0)

    def get_last_interaction_gap(self, user_id: str) -> float:
        t = self._last_interaction.get(user_id)
        return time.time() - t if t else 0.0

    def record_interaction(self, user_id: str):
        if not user_id:
            return
        if user_id == self._owner_id and user_id not in self._familiarity:
            try:
                idx = STAGES.index(self._owner_baseline)
                self._familiarity[user_id] = THRESHOLDS[idx]
            except ValueError:
                self._familiarity[user_id] = THRESHOLDS[1]  # 默认熟识
        else:
            self._familiarity[user_id] = self._familiarity.get(user_id, 0) + 1
        self._silence_count[user_id] = 0
        self._last_interaction[user_id] = time.time()

    # ── 沉默计数 ──
    def record_silence(self, user_id: str):
        self._silence_count[user_id] = self._silence_count.get(user_id, 0) + 1

    def should_force_respond(self, user_id: str) -> bool:
        if user_id == self._owner_id:
            return self._silence_count.get(user_id, 0) >= 5
        return self._silence_count.get(user_id, 0) >= 20

    # ── 情绪 ──
    def update_mood(self, mood: str, intensity: float = None):
        if mood not in MOODS:
            return
        self._previous_mood = self.mood
        # 情绪惯性：强度>0.6 时保留旧情绪，抵抗变化
        if self.mood_intensity > 0.6:
            self.mood = self._previous_mood
        else:
            self.mood = mood
        if intensity is not None:
            self.mood_intensity = max(0.1, min(1.0, intensity))

    def decay(self):
        elapsed_ms = 0
        last_time = max(self._last_interaction.values()) if self._last_interaction else None
        if last_time:
            elapsed_ms = (time.time() - last_time) * 1000
        # 每 5 分钟衰减 5%
        steps = max(0, elapsed_ms / (5 * 60 * 1000))
        self.mood_intensity = max(0.1, self.mood_intensity - steps * 0.05)
        self.energy = max(0.1, self.energy - steps * 0.02)

    # ── 持久化 ──
    def to_dict(self) -> dict:
        return {
            "mood": self.mood,
            "mood_intensity": self.mood_intensity,
            "energy": self.energy,
            "previous_mood": self._previous_mood,
            "familiarity": self._familiarity,
            "silence_count": self._silence_count,
            "last_interaction": self._last_interaction,
            "last_proactive_sent": self._last_proactive_sent,
        }

    @classmethod
    def load(cls, path: str, owner_id: str, owner_baseline_stage: str = "熟识") -> "RuntimeState":
        state = cls(path, owner_id, owner_baseline_stage)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            state.mood = data.get("mood", "安静")
            state.mood_intensity = data.get("mood_intensity", 0.5)
            state.energy = data.get("energy", 1.0)
            state._previous_mood = data.get("previous_mood", "安静")
            state._familiarity = data.get("familiarity", {})
            state._silence_count = data.get("silence_count", {})
            state._last_interaction = data.get("last_interaction", {})
            state._last_proactive_sent = data.get("last_proactive_sent", 0.0)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError:
            backup = path + ".corrupted"
            try:
                os.rename(path, backup)
            except OSError:
                pass
        return state

    def save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def reset(self):
        self.mood = "安静"
        self.mood_intensity = 0.5
        self.energy = 1.0
        self._familiarity.clear()
        self._silence_count.clear()
        self._last_interaction.clear()

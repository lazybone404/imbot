"""
连续熬夜追踪。定时循环调用 tick()，streak 持久化到 state.json。
"""
import json
import os
import time
from datetime import datetime


class LateNightTracker:
    def __init__(self, state_path: str):
        self.streak = 0
        self._last_late_date = ""  # 上次深夜活跃的日期
        self._path = state_path
        self._load()

    def tick(self, hour: int, is_active: bool):
        """定时循环调用"""
        if not (0 <= hour < 6 and is_active):
            return  # 非深夜或非活跃，不计数

        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._last_late_date:
            return  # 同一天不重复计数

        yesterday = (datetime.now().timestamp() - 86400)
        yesterday_str = datetime.fromtimestamp(yesterday).strftime("%Y-%m-%d")

        if self._last_late_date == yesterday_str:
            self.streak += 1  # 连续
        else:
            self.streak = 1   # 中断

        self._last_late_date = today
        self._save()

    def get_context(self) -> str:
        if self.streak >= 3:
            return f"连续第{self.streak}天熬夜了"
        if self.streak >= 1:
            return "又在熬夜"
        return ""

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.streak = data.get("late_night_streak", 0)
            self._last_late_date = data.get("last_late_night_date", "")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            data = {}
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            data["late_night_streak"] = self.streak
            data["last_late_night_date"] = self._last_late_date
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

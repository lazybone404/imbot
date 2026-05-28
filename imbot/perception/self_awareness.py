"""
自身状态感知。运行时长、今日话量、唤醒间隔、对话轮次。
输出 dict 给 aggregator，不耦合 RuntimeState。
"""
import time
from datetime import datetime


class SelfAwareness:
    def __init__(self):
        self._start_time = time.time()
        self._today_messages = 0
        self._last_wake_time = 0.0
        self._conv_round = 0
        self._date = datetime.now().strftime("%Y-%m-%d")

    @property
    def daily_messages(self) -> int:
        return self._today_messages

    @property
    def uptime_hours(self) -> float:
        return round((time.time() - self._start_time) / 3600, 1)

    def tick(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            self._today_messages = 0
            self._date = today

    def on_message(self):
        self._today_messages += 1
        self._conv_round += 1

    def on_wake(self):
        self._last_wake_time = time.time()

    def snapshot(self) -> dict:
        now = time.time()
        uptime_hours = (now - self._start_time) / 3600
        silence = (now - self._last_wake_time) / 60 if self._last_wake_time else 999
        return {
            "uptime_hours": round(uptime_hours, 1),
            "today_messages": self._today_messages,
            "silence_minutes": round(silence, 1),
            "conv_round": self._conv_round,
        }

"""
会话时长感知。追踪今日累计活跃时长 + 与昨日对比。
独立文件 session.json，不与 state.json 冲突。
"""
import json
import os
import time
from datetime import datetime


class SessionDurationTracker:
    def __init__(self, data_dir: str, long_threshold: int = 4):
        self._path = os.path.join(data_dir, "session.json")
        self._long_threshold = long_threshold
        self._session_start = time.time()
        self._today_minutes = 0
        self._yesterday_minutes = 0
        self._date = datetime.now().strftime("%Y-%m-%d")
        self._load()

    @property
    def today_minutes(self) -> int:
        return self._today_minutes

    def tick(self, is_active: bool):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            self._yesterday_minutes = self._today_minutes
            self._today_minutes = 0
            self._date = today
        if is_active:
            self._today_minutes += 1  # 每 tick ~60s
        self._save()

    def context_text(self) -> str:
        hours = self._today_minutes / 60
        if hours < 1:
            return ""
        if hours < self._long_threshold:
            return "你待了好一阵了"
        return "你待了好久"

    def regularity_text(self) -> str:
        if not self._yesterday_minutes:
            return ""
        today_hours = self._today_minutes / 60
        yesterday_hours = self._yesterday_minutes / 60
        diff = today_hours - yesterday_hours
        if abs(diff) < 2:
            return ""
        if diff > 0:
            return "今天上来得挺早"
        return "今天上来得挺晚"

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._today_minutes = data.get("today", 0)
            self._yesterday_minutes = data.get("yesterday", 0)
            self._date = data.get("date", self._date)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({
                    "today": self._today_minutes,
                    "yesterday": self._yesterday_minutes,
                    "date": self._date,
                }, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

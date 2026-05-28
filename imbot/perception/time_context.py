"""
时间语境感知。从 utils.py 迁入，扩展节假日检测。
"""
from datetime import datetime


# 2026 年中国法定假日（仅日期变更的假日，不含调休）
HOLIDAYS_2026 = {
    "01-01": "元旦",
    "02-17": "除夕",
    "02-18": "春节",
    "02-19": "春节",
    "02-20": "春节",
    "02-21": "春节",
    "02-22": "春节",
    "02-23": "春节",
    "04-05": "清明节",
    "05-01": "劳动节",
    "05-02": "劳动节",
    "05-03": "劳动节",
    "05-04": "劳动节",
    "05-05": "劳动节",
    "06-19": "端午节",
    "09-25": "中秋节",
    "10-01": "国庆节",
    "10-02": "国庆节",
    "10-03": "国庆节",
    "10-04": "国庆节",
    "10-05": "国庆节",
    "10-06": "国庆节",
    "10-07": "国庆节",
}

PERIOD_NAMES = {
    (0, 6): "凌晨", (6, 9): "清晨", (9, 12): "上午",
    (12, 14): "中午", (14, 18): "下午", (18, 22): "傍晚",
    (22, 24): "深夜",
}

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


class TimeContext:
    def __init__(self):
        self.date = ""
        self.time = ""
        self.hour = 0
        self.weekday = ""
        self.period = ""
        self.time_feel = ""
        self.holiday = ""
        self._update()

    def _update(self):
        now = datetime.now()
        self.date = now.strftime("%Y年%m月%d日")
        self.time = now.strftime("%H:%M")
        self.hour = now.hour
        self.weekday = WEEKDAYS[now.weekday()]

        for (start, end), name in PERIOD_NAMES.items():
            if start <= now.hour < end:
                self.period = name
                break

        self.holiday = HOLIDAYS_2026.get(now.strftime("%m-%d"), "")
        self.time_feel = self._make_time_feel()

    def snapshot(self) -> dict:
        self._update()
        return {
            "date": self.date, "time": self.time, "hour": self.hour,
            "weekday": self.weekday, "period": self.period,
            "time_feel": self.time_feel, "holiday": self.holiday,
        }

    def _make_time_feel(self) -> str:
        if self.holiday:
            return f"{self.holiday}，{self.time}"
        if self.period == "深夜":
            return "夜深了"
        if self.period == "凌晨":
            return "天还没亮"
        if self.period == "清晨":
            return "一大早"
        return f"{self.period}{self.time}"

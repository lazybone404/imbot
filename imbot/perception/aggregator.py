"""
感知聚合器。将各感知器原始数据合并为自然语言语境摘要。
"""
from imbot.perception.idle_monitor import IdleMonitor
from imbot.perception.time_context import TimeContext
from imbot.perception.late_night import LateNightTracker
from imbot.perception.process_monitor import ProcessMonitor
from imbot.perception.media_monitor import MediaMonitor


class PerceptionAggregator:
    def __init__(self, idle: IdleMonitor, time_ctx: TimeContext,
                 late_night: LateNightTracker,
                 process: ProcessMonitor = None,
                 media: MediaMonitor = None,
                 session=None,
                 self_aware=None,
                 steam=None):
        self.idle = idle
        self.time = time_ctx
        self.late_night = late_night
        self.process = process
        self.media = media
        self.session = session
        self.self_aware = self_aware
        self.steam = steam

    def snapshot(self) -> dict:
        t = self.time.snapshot()
        late_ctx = self.late_night.get_context()

        # 进程/媒体语境
        process_text = self.process.context_text() if self.process else ""
        media_text = self.media.context_text() if self.media else ""

        # 主要行为
        primary = process_text or ""
        if not primary and media_text:
            primary = media_text
        idle_text = self.idle.context_text()
        if not primary:
            if idle_text == "人不在":
                primary = "人不在电脑前"
            elif idle_text == "刚回来":
                primary = "刚刚回到电脑前"

        # 注意力点
        attention_parts = []
        if late_ctx:
            attention_parts.append(late_ctx)
        if idle_text:
            attention_parts.append(idle_text)
        if process_text:
            attention_parts.append(process_text)
        if media_text and media_text != process_text:
            attention_parts.append(media_text)
        if t["holiday"]:
            attention_parts.insert(0, f"今天是{t['holiday']}")

        # 会话语境
        if self.session:
            ctx = self.session.context_text()
            if ctx:
                attention_parts.append(ctx)
            reg = self.session.regularity_text()
            if reg:
                attention_parts.append(reg)
        # Steam 语境（替换 process 的模糊判断）
        if self.steam and self.steam.is_playing():
            steam_text = self.steam.context_text()
            if steam_text:
                primary = steam_text  # 以 Steam 为准，覆盖进程检测
                if steam_text not in attention_parts:
                    attention_parts.insert(0, steam_text)

        # 自身状态
        self_aware_data = {}
        if self.self_aware:
            self_aware_data = self.self_aware.snapshot()

        return {
            "primary_context": primary,
            "time_context": t["time_feel"],
            "user_state": idle_text or "在线",
            "jun_attention": "。".join(attention_parts) if attention_parts else t["time_feel"],
            "period": t["period"],
            "holiday": t["holiday"],
            "self_aware": self_aware_data,
        }

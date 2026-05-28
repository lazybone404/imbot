"""
感知层统一入口。协调 9 个感知器 + 后台循环。
"""
import asyncio
from astrbot.api import logger

from imbot.perception.idle_monitor import IdleMonitor
from imbot.perception.time_context import TimeContext
from imbot.perception.late_night import LateNightTracker
from imbot.perception.process_monitor import ProcessMonitor
from imbot.perception.media_monitor import MediaMonitor
from imbot.perception.session_duration import SessionDurationTracker
from imbot.perception.self_awareness import SelfAwareness
from imbot.perception.steam_monitor import SteamMonitor
from imbot.perception.aggregator import PerceptionAggregator


class PerceptionManager:
    def __init__(self, config, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self.enabled = config.perception.enabled
        self._running = False
        pc = config.perception

        self.idle = IdleMonitor()
        self.time = TimeContext()
        self.late_night = None
        self.process = ProcessMonitor() if pc.process else None
        self.media = MediaMonitor() if pc.media else None
        self.session = SessionDurationTracker(data_dir, pc.session_duration.long_threshold) if pc.session_duration.enabled else None
        self.self_aware = SelfAwareness() if pc.self_awareness.enabled else None
        self.steam = None
        if pc.steam.enabled and pc.steam.steam_id:
            api_key = pc.steam.api_key
            if api_key:
                self.steam = SteamMonitor(api_key, pc.steam.steam_id,
                                          pc.steam.expose, pc.steam.poll_interval,
                                          proxy=getattr(pc.steam, "proxy", ""),
                                          timeout=getattr(pc.steam, "timeout", 15))
                # 启动独立轮询循环（key 直接捕获，避免依赖私有属性）
                _captured_key = api_key
                async def _steam_key_getter():
                    return _captured_key
                self._steam_task = asyncio.create_task(self.steam.loop(_steam_key_getter))
                logger.info(f"Steam 感知轮询启动 (间隔{pc.steam.poll_interval}s, SteamID={pc.steam.steam_id})")
        self.aggregator = None

    def init_late_night(self, state_path: str):
        self.late_night = LateNightTracker(state_path)
        self.aggregator = PerceptionAggregator(
            self.idle, self.time, self.late_night,
            self.process, self.media,
            self.session, self.self_aware,
            steam=self.steam,
        )

    async def loop(self, engine):
        if not self.enabled or not self.aggregator:
            return
        self._running = True
        interval = max(60, self.config.perception.interval)  # 最少 60 秒，防死循环
        active = [n for n, m in (
            ("idle",True),("late_night",True),("process",self.process),
            ("media",self.media),("session",self.session),
            ("self_aware",self.self_aware),
        ) if m]
        logger.info(f"感知循环启动 (间隔{interval}s, 模块: {', '.join(active)})")
        while self._running:
            try:
                self.idle._update()
                if self.process:       self.process._update()
                if self.media:         self.media._update()
                if self.session:       self.session.tick(self.idle.state == "active")
                if self.self_aware:    self.self_aware.tick()
                self.late_night.tick(self.time.hour, self.idle.state == "active")
                snap = self.aggregator.snapshot()
                if snap != engine._current_perception:
                    attn = snap.get("jun_attention", "") or snap.get("primary_context", "")
                    if attn:
                        logger.info(f"感知: {attn}")
                engine._current_perception = snap
            except Exception:
                logger.error("感知循环异常", exc_info=True)
            await asyncio.sleep(interval)
        logger.info("感知循环停止")

    def on_message(self):
        if self.self_aware:
            self.self_aware.on_message()

    def on_wake(self):
        if self.self_aware:
            self.self_aware.on_wake()
        if self.session:
            self.session.tick(True)

    def stop(self):
        self._running = False
        if hasattr(self, "_steam_task") and not self._steam_task.done():
            self._steam_task.cancel()

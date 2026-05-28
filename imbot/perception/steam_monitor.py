"""
Steam API 感知。通过 Steam Web API 获取游戏状态、时长。
与 process_monitor 互补——Steam 知道确切游戏名，process 知道实时进程。
"""
import asyncio
import time

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False

STEAM_API = "https://api.steampowered.com"
MIN_POLL_INTERVAL = 60  # Steam API 最低轮询间隔（防呆）


class SteamMonitor:
    def __init__(self, api_key: str, steam_id: str, expose_cfg, poll_interval: int = 300,
                 proxy: str = "", timeout: int = 15):
        self._key = api_key
        self._steam_id = steam_id
        self._expose = expose_cfg
        self._interval = max(MIN_POLL_INTERVAL, poll_interval)
        self._proxy = proxy
        self._timeout = timeout
        self._last_poll = 0
        self._backoff = 1.0

        # 当前快照
        self.current_game: str = ""
        self.current_game_id: int = 0
        self.game_extra: str = ""       # Rich Presence 文本（如有）
        self.session_start: float = 0   # 本次游戏开始时间戳
        self.playtime_2weeks: int = 0
        self.playtime_forever: int = 0
        self.recently_played: list[dict] = []  # [{name, appid, playtime_2weeks}]
        self._prev_game_id: int = 0     # 上一次的游戏 ID，用于检测切换

        # 一次性事件标志（供 proactive 层消费后重置）
        self.game_just_ended: bool = False
        self.last_game_name: str = ""
        self.new_achievements: list[str] = []   # 本轮新解锁的成就名列表
        self._achievement_snapshot: dict = {}     # {appid: set(achieved_apinames)}

    # ── 主循环 ──
    async def loop(self, api_key_getter):
        """
        独立异步循环。api_key_getter 是闭包，返回最新的 API Key。
        """
        while True:
            try:
                key = api_key_getter()
                if key and self._steam_id:
                    await self._poll(key)
            except Exception:
                from astrbot.api import logger
                logger.error("Steam 轮询循环异常", exc_info=True)
            await asyncio.sleep(self._interval)

    async def _poll(self, key: str):
        if not _HAS_AIOHTTP:
            if not getattr(self, "_aiohttp_warned", False):
                self._aiohttp_warned = True
                from astrbot.api import logger
                logger.warning("Steam 感知: aiohttp 未安装，无法轮询")
            return
        now = time.time()
        if now - self._last_poll < self._interval * self._backoff:
            return
        self._last_poll = now

        # 重置一次性标志（保留上一轮游戏名用于结束检测）
        self.game_just_ended = False
        self.new_achievements.clear()
        self._prev_game_name = self.current_game  # 保存，供游戏结束时使用

        try:
            session_kwargs = {}
            if self._proxy:
                session_kwargs["proxy"] = self._proxy
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(**session_kwargs) as sess:
                # GetPlayerSummaries
                url = f"{STEAM_API}/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={self._steam_id}"
                async with sess.get(url, timeout=timeout) as resp:
                    if resp.status == 429:
                        self._backoff = min(8.0, self._backoff * 2)
                        return
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    players = data.get("response", {}).get("players", [])
                    if players:
                        p = players[0]
                        self.current_game = p.get("gameextrainfo", "")
                        self.current_game_id = int(p.get("gameid", 0))
                        self.game_extra = ""  # Rich Presence 需额外 API，暂不采集

                # GetRecentlyPlayedGames
                url2 = f"{STEAM_API}/IPlayerService/GetRecentlyPlayedGames/v1/?key={key}&steamid={self._steam_id}&count=5"
                async with sess.get(url2, timeout=timeout) as resp2:
                    if resp2.status == 200:
                        data2 = await resp2.json()
                        games = data2.get("response", {}).get("games", [])
                        self.recently_played = games
                        if games and self.current_game:
                            for g in games:
                                if g["name"] == self.current_game:
                                    self.playtime_2weeks = g.get("playtime_2weeks", 0)
                                    self.playtime_forever = g.get("playtime_forever", 0)
                                    break

                # 游戏结束检测：上次在游戏中，现在不在
                if self._prev_game_id and not self.current_game_id:
                    self.game_just_ended = True
                    self.last_game_name = self._prev_game_name or self.current_game

                # 游戏切换检测
                if self.current_game_id and self.current_game_id != self._prev_game_id:
                    if self._prev_game_id:
                        self._on_game_switch()
                    else:
                        self.session_start = now
                elif not self.current_game_id:
                    if self._prev_game_id and self.current_game:
                        self.last_game_name = self.current_game
                    self.session_start = 0

                self._prev_game_id = self.current_game_id

                # 成就检测
                if self._expose.achievements and self.current_game_id:
                    await self._poll_achievements(sess, key, self.current_game_id)

                self._backoff = 1.0  # 成功后重置 backoff

                # 首次成功轮询 / 每 10 次定期状态
                count = getattr(self, "_poll_count", 0) + 1
                self._poll_count = count
                from astrbot.api import logger
                if count == 1:
                    game_info = f"在打{self.current_game}" if self.current_game else "未在游戏中"
                    logger.info(f"Steam API 连接成功: {game_info}")
                elif count % 10 == 0:
                    games = ", ".join(g.get("name", "?") for g in self.recently_played[:3])
                    game_info = f"在打{self.current_game}" if self.current_game else "未在游戏中"
                    logger.info(f"Steam 状态: {game_info} | 最近: {games or '无'}")

        except Exception:
            from astrbot.api import logger
            logger.error("Steam API 轮询失败", exc_info=True)

    def _on_game_switch(self):
        """用户换了游戏——重置会话计时"""
        self.session_start = time.time()

    async def _poll_achievements(self, sess, key: str, appid: int):
        """获取成就列表，对比变化检测新解锁"""
        try:
            url = f"{STEAM_API}/ISteamUserStats/GetPlayerAchievements/v1/?key={key}&steamid={self._steam_id}&appid={appid}"
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=self._timeout)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                achievements = data.get("playerstats", {}).get("achievements", [])
                if not achievements:
                    return
                prev = self._achievement_snapshot.get(appid, set())
                current = set()
                for ach in achievements:
                    name = ach.get("apiname", "")
                    if not name:
                        continue
                    current.add(name)
                    if ach.get("achieved", 0) and name not in prev:
                        self.new_achievements.append(ach.get("name", name))
                self._achievement_snapshot[appid] = current
        except (KeyError, TypeError, ValueError):
            pass

    # ── 语境输出 ──
    def context_text(self) -> str:
        if not self.current_game:
            return ""
        parts = []
        if self._expose.current_game:
            parts.append(f"在打{self.current_game}")
        if self._expose.playtime_session and self.session_start:
            mins = (time.time() - self.session_start) / 60
            if mins > 10:
                parts.append(f"{int(mins / 60)}小时{int(mins % 60)}分钟了" if mins > 60 else f"{int(mins)}分钟了")
        if self._expose.rich_presence and self.game_extra:
            parts.append(self.game_extra)
        return "，".join(parts) if parts else ""

    def game_duration_hours(self) -> float:
        if not self.session_start:
            return 0
        return (time.time() - self.session_start) / 3600

    def is_playing(self) -> bool:
        return bool(self.current_game)

"""
兴趣池 — imbot 的好奇心引擎。
独立于感知/记忆系统，管理兴趣的捕获、衰减、探索和分享。
"""
import json
import os
import random
import time


class InterestPool:
    def __init__(self, config, data_dir: str) -> None:
        self.cfg = config
        self._data_dir: str = data_dir
        self._path: str = os.path.join(data_dir, "interests.json")
        self.interests: list[dict] = []
        self._media_counts: dict[str, int] = {}       # {player_name: today_count}
        self._game_streaks: dict[str, int] = {}        # {game_name: consecutive_days}
        self._game_last_date: dict[str, str] = {}
        self._topic_mentions: dict[str, int] = {}       # {topic: count}
        self._last_explore_time: float = 0.0
        self._today_shares: int = 0
        self._today_date: str = ""
        self._llm_call = None  # 由 engine 注入: async fn(prompt) -> str

    def bind_llm(self, fn) -> None:
        """注入 LLM 调用函数: async fn(prompt) -> str"""
        self._llm_call = fn

    # ── 持久化 ──
    def load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.interests = data.get("interests", [])
        except FileNotFoundError:
            self.interests = []
        except json.JSONDecodeError:
            backup = self._path + ".corrupted"
            try:
                os.rename(self._path, backup)
            except OSError:
                pass
            self.interests = []

    def apply_seed_keywords(self) -> None:
        """将用户配置的初始关键词注入兴趣池（已存在则跳过）"""
        for kw in self.cfg.seed_keywords:
            kw = kw.strip()
            if kw and not any(i["keyword"] == kw for i in self.interests):
                self._add_interest(kw, "user_seed", intensity=3)
        if self.cfg.seed_keywords:
            self.save()

    def save(self) -> None:
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump({"interests": self.interests}, f, ensure_ascii=False, indent=2)
        except Exception:
            from astrbot.api import logger
            logger.error("兴趣池写入失败", exc_info=True)

    # ── 每日重置 ──
    def _check_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if self._today_date != today:
            self._today_date = today
            self._media_counts.clear()
            self._today_shares = 0

    # ── tick: 每 30s 调用 ──
    def tick(self, perception: dict) -> None:
        """驱动媒体/游戏计数累积和衰减"""
        if not self.cfg.enabled:
            return
        self._check_day()
        now = time.time()

        # 媒体计数
        if self.cfg.auto_observe_media and perception:
            media_type = perception.get("media_type", "")
            player = perception.get("player_name", "")
            is_playing = perception.get("is_playing", False)
            if is_playing and player:
                self._media_counts[player] = self._media_counts.get(player, 0) + 1

        # 游戏计数（每天 +1）
        if self.cfg.auto_observe_games and perception:
            game_name = perception.get("game_name", "")
            if game_name:
                today = self._today_date
                last = self._game_last_date.get(game_name, "")
                if last != today:
                    self._game_streaks[game_name] = self._game_streaks.get(game_name, 0) + 1
                    self._game_last_date[game_name] = today

        # 衰减：7 天未探索 → intensity -1
        for it in self.interests:
            if it.get("status") != "active":
                continue
            last = it.get("last_explored", it.get("created_at", 0))
            if last > 0 and now - last > 7 * 86400:
                it["intensity"] = max(0, it.get("intensity", 1) - 1)
                if it["intensity"] <= 0:
                    it["status"] = "dormant"

    # ── 兴趣捕获 ──
    async def maybe_capture(self) -> list[str]:
        """检查是否达到捕获阈值，返回新捕获的关键词列表"""
        if not self.cfg.enabled:
            return []
        captured = []

        # 媒体：同一播放器今天 ≥ 10 次计数 → LLM 语义提取
        for player, count in list(self._media_counts.items()):
            if count >= 10:
                # 优先用 LLM 语义提取
                keywords = await self.extract_media_interest(player_name=player)
                if not keywords:
                    # 降级：静态映射
                    kw = self._player_to_keyword(player)
                    keywords = [kw] if kw else []
                for kw in keywords:
                    if self._add_interest(kw, "media_observe", intensity=3):
                        captured.append(kw)
                self._media_counts[player] = 0

        # 游戏：连续 ≥ 3 天 → LLM 语义提取
        for game, streak in list(self._game_streaks.items()):
            if streak >= 3:
                keywords = await self.extract_media_interest(game_name=game)
                if not keywords:
                    keywords = [game]
                for kw in keywords:
                    if self._add_interest(kw, "game_observe", intensity=4):
                        captured.append(kw)
                self._game_streaks[game] = 0

        # 对话主题：≥ 3 次提及
        for topic, count in list(self._topic_mentions.items()):
            if count >= 3:
                if self._add_interest(topic, "user_mention", intensity=2):
                    captured.append(topic)
                    self._topic_mentions[topic] = 0

        return captured

    def capture_topic(self, text: str) -> None:
        """从对话消息中提取话题关键词。用标点/空格切分，取 2-8 字片段去重计数。"""
        if not self.cfg.auto_observe_topics:
            return
        if not text.strip():
            return
        import re
        # 按标点、空格、换行切分
        chunks = re.split(r'[，。！？、；：""''（）\s]+', text)
        seen = set()
        for chunk in chunks:
            chunk = chunk.strip()
            if 2 <= len(chunk) <= 8 and not chunk.isdigit():
                if chunk not in seen:
                    seen.add(chunk)
                    self._topic_mentions[chunk] = self._topic_mentions.get(chunk, 0) + 1

    @staticmethod
    def _player_to_keyword(player: str) -> str:
        mapping = {
            "网易云音乐": "网易云音乐", "QQ音乐": "QQ音乐", "Spotify": "Spotify",
            "B站": "B站", "斗鱼": "斗鱼", "虎牙": "虎牙",
        }
        return mapping.get(player, "")

    # ── 兴趣 CRUD ──
    def _add_interest(self, keyword: str, source: str, intensity: int = 2) -> bool:
        """添加兴趣，返回 True 表示新增，False 表示合并/拒绝"""
        keyword = keyword.strip()
        if not keyword:
            return False

        # 去重：合并
        for it in self.interests:
            if it["keyword"] == keyword:
                it["intensity"] = max(it["intensity"], intensity) + 1
                if it["status"] == "dormant":
                    it["status"] = "active"
                return False

        # 容量检查
        if len(self.interests) >= self.cfg.max_interests:
            self._evict_one()

        self.interests.append({
            "keyword": keyword,
            "source": source,
            "intensity": intensity,
            "explore_count": 0,
            "last_explored": 0.0,
            "share_count": 0,
            "last_shared": 0.0,
            "status": "active",
            "created_at": time.time(),
        })
        self.save()
        return True

    def _evict_one(self) -> None:
        """淘汰一个兴趣：优先 dormant，其次最低 intensity"""
        dormant = [i for i in self.interests if i.get("status") == "dormant"]
        if dormant:
            target = min(dormant, key=lambda i: i.get("intensity", 0))
        else:
            target = min(self.interests, key=lambda i: i.get("intensity", 0))
        self.interests.remove(target)

    def user_add(self, keyword: str) -> bool:
        """用户手动添加"""
        added = self._add_interest(keyword, "user_suggest", intensity=3)
        self.save()
        return added

    def forget(self, keyword: str) -> bool:
        for it in self.interests:
            if it["keyword"] == keyword:
                it["status"] = "archived"
                self.save()
                return True
        return False

    def pause(self, keyword: str) -> bool:
        for it in self.interests:
            if it["keyword"] == keyword:
                it["status"] = "dormant"
                self.save()
                return True
        return False

    def resume(self, keyword: str) -> bool:
        for it in self.interests:
            if it["keyword"] == keyword:
                it["status"] = "active"
                self.save()
                return True
        return False

    def list_active(self) -> list[dict]:
        return [i for i in self.interests if i.get("status") == "active"]

    # ── 探索 ──
    def pick_for_exploration(self) -> dict | None:
        """按 intensity 加权随机选一个活跃兴趣（受 min_re_explore 冷却限制）"""
        active = self.list_active()
        if not active:
            return None
        now = time.time()
        min_gap = max(3600, min(604800, self.cfg.min_re_explore))  # 防呆: 1h-7天
        eligible = [
            i for i in active
            if now - i.get("last_explored", 0) >= min_gap
        ]
        if not eligible:
            return None
        return self._weighted_pick(eligible)

    def pick_for_heartbeat(self) -> dict | None:
        """心跳保底选择：跳过 min_re_explore 冷却，只要能说的都行"""
        active = self.list_active()
        if not active:
            return None
        # 优先选今天还没探索过的
        now = time.time()
        fresh = [i for i in active if i.get("last_explored", 0) == 0
                 or now - i.get("last_explored", 0) >= 3600]
        pool = fresh or active
        return self._weighted_pick(pool)

    @staticmethod
    def _weighted_pick(items: list[dict]) -> dict | None:
        weights = [i.get("intensity", 1) for i in items]
        total = sum(weights)
        r = random.random() * total
        acc = 0
        for i, w in zip(items, weights):
            acc += w
            if r <= acc:
                return i
        return items[-1]

    def record_exploration(self, keyword: str, interesting: bool, summary: str = "") -> None:
        now = time.time()
        for it in self.interests:
            if it["keyword"] == keyword:
                it["explore_count"] = it.get("explore_count", 0) + 1
                it["last_explored"] = now
                if interesting:
                    it["intensity"] = min(5, it.get("intensity", 1) + 1)
                self.save()
                return

    def record_share(self, keyword: str) -> None:
        now = time.time()
        for it in self.interests:
            if it["keyword"] == keyword:
                it["share_count"] = it.get("share_count", 0) + 1
                it["last_shared"] = now
                self._today_shares += 1
                self.save()
                return

    def record_user_reaction(self, keyword: str, positive: bool) -> None:
        for it in self.interests:
            if it["keyword"] == keyword:
                if positive:
                    it["intensity"] = min(5, it.get("intensity", 1) + 1)
                else:
                    it["intensity"] = max(0, it.get("intensity", 1) - 1)
                self.save()
                return

    # ── LLM 语义提取 ──
    async def extract_media_interest(self, player_name: str, game_name: str = "") -> list[str]:
        """用轻量 LLM 从媒体/游戏上下文提取兴趣关键词"""
        if not self._llm_call:
            return []

        context_parts = []
        if player_name:
            context_parts.append(f"用户最近频繁使用 {player_name}")
        if game_name:
            context_parts.append(f"用户最近在玩 {game_name}")
        if not context_parts:
            return []

        prompt = (
            "用户最近频繁接触以下内容：\n" + "\n".join(context_parts) +
            "\n\n从中提取 1-2 个兴趣关键词，每个 2-8 字。只输出关键词，用换行分隔。不要输出其他内容。"
        )

        try:
            result = await asyncio.wait_for(self._llm_call(prompt), timeout=5.0)
            if not result:
                return []
            keywords = [k.strip() for k in result.strip().split("\n") if k.strip()]
            return keywords[:2]
        except asyncio.TimeoutError:
            return []
        except Exception:
            return []

    # ── 心跳 ──
    @property
    def today_share_count(self) -> int:
        self._check_day()
        return self._today_shares

    @property
    def last_explore_time(self) -> float:
        return self._last_explore_time

    @last_explore_time.setter
    def last_explore_time(self, val: float) -> None:
        self._last_explore_time = val

    def heartbeat_due(self) -> bool:
        """检查是否在心跳窗口内且今天分享不足"""
        if not self.cfg.heartbeat.enabled:
            return False
        self._check_day()
        if self._today_shares >= self.cfg.heartbeat.daily_min_share:
            return False

        start = self._parse_time(self.cfg.heartbeat.active_window_start)
        end_raw = self._parse_time(self.cfg.heartbeat.active_window_end)
        now_h, now_m = time.localtime().tm_hour, time.localtime().tm_min
        now_minutes = now_h * 60 + now_m

        if start == -1 or end_raw == -1:
            return False  # 配置解析失败，安全跳过

        end = 1440 if end_raw == 0 else end_raw
        if start > end:
            # 跨日窗口 (如 21:00-02:00)：now >= start 或 now < end
            return now_minutes >= start or now_minutes < end
        return start <= now_minutes < end

    def heartbeat_in_range(self, minutes: int) -> bool:
        """检查给定分钟数是否在窗口内（用于跨日场景）"""
        start = self._parse_time(self.cfg.heartbeat.active_window_start)
        end_raw = self._parse_time(self.cfg.heartbeat.active_window_end)
        if start == -1 or end_raw == -1:
            return False
        end = 1440 if end_raw == 0 else end_raw
        if start > end:
            end += 1440
        return start <= minutes < end

    @staticmethod
    def _parse_time(s: str) -> int:
        """解析 HH:MM 为分钟数 (0-1439)，失败返回 -1"""
        try:
            parts = s.strip().split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
            return -1
        except (ValueError, IndexError):
            return -1

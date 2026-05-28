import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from imbot.config import ImbotConfig


class ProactiveManager:
    def __init__(self, config: ImbotConfig, engine) -> None:
        self.cfg = config.proactive
        self.engine = engine
        self._last_proactive_time: float = 0.0
        self._proactive_count: int = 0
        self._proactive_window_start: float = 0.0
        self._ignore_streak: int = 0
        self._quiet_until: float = 0.0
        self._last_trigger_type: tuple | None = None
        self._last_anchor_check: float = 0.0

    # ── 触发检查 ──
    def check_triggers(self) -> dict | None:
        now = time.time()

        # 每日重置
        today = time.strftime("%Y-%m-%d")
        if getattr(self, "_today_date", "") != today:
            self._today_date = today
            self._long_session_today = False

        # 冷却检查
        if now - self._last_proactive_time < self.cfg.min_interval * 60:
            return None
        # 3h 滑动窗口
        if now - self._proactive_window_start > self.cfg.window_hours * 3600:
            self._proactive_count = 0
            self._proactive_window_start = now
        if self._proactive_count >= self.cfg.max_per_3h:
            return None
        # 静音
        if now < self._quiet_until:
            return None
        # 连续忽略降频
        if self._ignore_streak >= self.cfg.silence_after_ignored:
            extra = self._ignore_streak - self.cfg.silence_after_ignored + 1
            effective = self.cfg.min_interval * 60 * (1.5 ** extra)
            if now - self._last_proactive_time < effective:
                return None

        # 动机评估
        time_ctx = self.engine._last_time_ctx or {}
        mot = self.engine.motivation.evaluate(
            self.engine.state, time_ctx, speaker_type="owner", is_group=False
        )
        tone = mot.get("tone", "安静")
        if not mot.get("should_respond", True):
            return None

        # 逐个检查触发源
        triggers = []
        oid = self.engine.config.owner.qq_id
        perc = self.engine._current_perception or {}
        period = perc.get("period", "")
        sw = getattr(self.engine, "social_world", None)

        # ① 长时间沉默
        if self.cfg.triggers.long_silence_hours > 0:
            owner_id = self.engine.config.owner.qq_id
            gap = self.engine.state.get_last_interaction_gap(owner_id)
            threshold = self.cfg.triggers.long_silence_hours * 3600
            if gap > threshold:
                triggers.append({"type": "long_silence", "gap_hours": round(gap / 3600, 1), "category": "care"})

        # ② 用户归来
        if self.cfg.triggers.user_returned:
            if self.engine.perception and self.engine.perception.idle:
                if self.engine.perception.idle.state == "just_returned":
                    triggers.append({"type": "user_returned", "category": "curiosity"})

        # ③ 深夜陪伴
        if self.cfg.triggers.late_night:
            hour = getattr(self.engine.perception.time, "hour", 0) if self.engine.perception else 0
            start = self.cfg.triggers.late_night_start
            end = self.cfg.triggers.late_night_end
            is_late = (hour >= start or hour < end) if start > end else (start <= hour < end)
            if is_late:
                if self.engine.perception and self.engine.perception.late_night:
                    streak = self.engine.perception.late_night.streak
                    if streak >= 3:
                        triggers.append({"type": "late_night", "streak": streak, "category": "care"})

        # ── P2 触发源 ──

        # ④ 游戏结束
        if self.cfg.triggers.game_ended:
            pm = self.engine.perception.process if self.engine.perception else None
            if pm and pm.game_just_ended:
                triggers.append({"type": "game_ended", "duration_minutes": pm.game_duration_minutes, "category": "curiosity"})

        # ⑤ 深夜 + 音乐
        if self.cfg.triggers.late_night_music:
            hour = getattr(self.engine.perception.time, "hour", 0) if self.engine.perception else 0
            start = max(0, min(5, self.cfg.triggers.late_night_start))
            end = max(0, min(8, self.cfg.triggers.late_night_end))
            is_late = hour >= start if start > end else (start <= hour < end)
            if is_late:
                mm = self.engine.perception.media if self.engine.perception else None
                if mm and mm.media_type == "music" and mm.is_playing:
                    triggers.append({"type": "late_night_music", "category": "sharing"})

        # ⑧ 长会话（每天最多触发一次，避免频繁打扰）
        if self.cfg.triggers.long_session and not getattr(self, "_long_session_today", False):
            st = self.engine.perception.session if self.engine.perception else None
            threshold = max(1, min(24, self.cfg.triggers.long_session_hours)) * 60
            if st and st.today_minutes > threshold:
                self._long_session_today = True
                triggers.append({
                    "type": "long_session",
                    "hours": round(st.today_minutes / 60, 1),
                    "category": "sharing",
                    "period": period,
                    "media": perc.get("media_type", ""),
                    "process": perc.get("primary_context", ""),
                })

        # ⑨ 孤独感：今天话少 + 运行久
        if self.cfg.triggers.self_lonely:
            sa = self.engine.perception.self_aware if self.engine.perception else None
            msg_max = max(1, min(100, self.cfg.triggers.self_lonely_msg_max))
            uptime_min = max(1, min(48, self.cfg.triggers.self_lonely_uptime_min))
            if sa and sa.daily_messages < msg_max and sa.uptime_hours > uptime_min:
                triggers.append({"type": "self_lonely", "daily_messages": sa.daily_messages, "uptime_hours": sa.uptime_hours, "category": "care"})

        # ⑩ 自身情绪波动
        if self.cfg.triggers.self_mood_swing:
            st = self.engine.state
            if st._previous_mood and st._previous_mood != st.mood and st.mood_intensity > 0.5:
                triggers.append({"type": "self_mood_swing", "from_mood": st._previous_mood, "to_mood": st.mood, "category": "sharing"})

        # ⑪ Steam 游戏结束
        if self.cfg.triggers.steam_game_ended:
            steam = self.engine.perception.steam if self.engine.perception else None
            if steam and getattr(steam, "game_just_ended", False):
                triggers.append({"type": "steam_game_ended", "game_name": getattr(steam, "last_game_name", ""), "category": "curiosity"})

        # ⑫ Steam 成就
        if self.cfg.triggers.steam_achievement:
            steam = self.engine.perception.steam if self.engine.perception else None
            if steam and getattr(steam, "new_achievements", []):
                for name in list(steam.new_achievements):
                    triggers.append({"type": "steam_achievement", "achievement_name": name, "category": "curiosity"})

        # ⑬ 关系跃迁
        if sw and self.cfg.triggers.relationship_milestone:
            for uid in list(sw.people.keys())[:20]:
                milestone = sw.check_relationship_milestone(uid) if hasattr(sw, "check_relationship_milestone") else None
                if milestone:
                    triggers.append({"type": "relationship_milestone", **milestone, "target_user_id": uid, "category": "sharing"})

        # ⑭ 记忆锚点（每 5 分钟检查一次，避免频繁遍历）
        if self.cfg.triggers.memory_anchor and self.engine.memory and period and now - self._last_anchor_check > 300:
            self._last_anchor_check = now
            mem = self.engine.memory.find_anchor_match(period, min_weight=2.0)
            if mem:
                uid = mem.get("user_id", oid)
                triggers.append({
                    "type": "memory_anchor",
                    "seed": mem.get("seed", "")[:40],
                    "summary": mem.get("summary", ""),
                    "time_feel": mem.get("time_feel", ""),
                    "target_user_id": uid,
                    "category": "sharing",
                })

        # 静音刚到期（用户设的静音，不是普通冷却）
        if getattr(self, "_quiet_was_user_set", False):
            triggers.append({"type": "quiet_lifted", "category": "sharing"})

        # ── 设置默认 target_user_id（owner），记忆锚点和关系跃迁会覆盖 ──
        for t in triggers:
            t.setdefault("target_user_id", oid)

        # 情绪门禁（trigger 收集之后再做，避免过度拦截）
        trigger_types_set = {t["type"] for t in triggers}
        if tone in ("冷淡", "烦躁"):
            # 保护：≥3 触发源且含 self_lonely → 放行，"我本来不想说但还是说了"
            if not (len(triggers) >= 3 and "self_lonely" in trigger_types_set):
                return None

        if not triggers:
            from astrbot.api import logger
            reasons = []
            if hasattr(self, "_last_skip_gap") and now - self._last_skip_gap > 600:
                self._last_skip_gap = now
                if now - self._last_proactive_time < self.cfg.min_interval * 60:
                    reasons.append("冷却中")
                if self._proactive_count >= self.cfg.max_per_3h:
                    reasons.append("达到时段上限")
                if now < self._quiet_until:
                    reasons.append("静音期")
                if reasons:
                    logger.debug(f"主动跳过: {', '.join(reasons)}")
            return None

        # 去抖：同一触发组合不连续重复
        trigger_types = tuple(sorted(t["type"] for t in triggers))
        if trigger_types == self._last_trigger_type:
            return None

        # 概率采样
        intensity = min(3, len(triggers))
        base_prob = max(0.05, min(1.0, mot.get("initiative", self.cfg.base_probability)))
        if random.random() > base_prob * intensity:
            return None

        # 消费一次性标志（所有门禁已通过，确保不会丢失事件）
        for t in triggers:
            if t["type"] == "game_ended":
                if self.engine.perception and self.engine.perception.process:
                    self.engine.perception.process.game_just_ended = False
            elif t["type"] == "steam_game_ended":
                steam = self.engine.perception.steam if self.engine.perception else None
                if steam:
                    steam.game_just_ended = False
            elif t["type"] == "steam_achievement":
                steam = self.engine.perception.steam if self.engine.perception else None
                if steam:
                    steam.new_achievements.clear()
            elif t["type"] == "quiet_lifted":
                self._quiet_was_user_set = False

        return {"triggers": triggers, "intensity": intensity, "tone": tone}

    # ── 执行记录 ──
    def execute(self, trigger_ctx: dict) -> None:
        self._last_proactive_time = time.time()
        self._proactive_count += 1
        self._last_trigger_type = tuple(sorted(
            t["type"] for t in trigger_ctx["triggers"]
        ))

    # ── 用户交互钩子 ──
    def on_user_interaction(self) -> None:
        self._proactive_count = 0
        self._proactive_window_start = time.time()
        self._ignore_streak = 0

    def on_proactive_ignored(self) -> None:
        self._ignore_streak += 1

    def set_quiet(self, minutes: int) -> None:
        actual = min(minutes, 120)  # 最长 2 小时
        self._quiet_until = time.time() + actual * 60
        self._quiet_was_user_set = True

    # ── 熟人主动 ──
    def check_known_targets(self) -> dict | None:
        """检查是否应对亲近的人主动搭话（极克制）"""
        if not self.cfg.allow_known_targets:
            return None
        sw = getattr(self.engine, "social_world", None)
        if not sw:
            return None
        now = time.time()
        oid = self.engine.config.owner.qq_id
        cooldowns = getattr(self, "_known_cooldowns", {})
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        day_key = f"_{today}_known_count"
        day_count = getattr(self, day_key, 0)
        if day_count >= self.cfg.known_max_per_day:
            return None

        for uid, person in list(sw.people.items())[:30]:
            if uid == oid:
                continue  # 跳过 owner（已有独立逻辑）
            fam = self.engine.state.get_interaction_count(uid)
            if fam < 20:
                continue  # 仅亲近及以上
            # 该人冷却检查
            last = cooldowns.get(uid, 0)
            if now - last < self.cfg.known_min_interval * 60:
                continue

            triggers = []
            # 长时间沉默
            gap = self.engine.state.get_last_interaction_gap(uid)
            if gap > self.cfg.triggers.known_long_silence_hours * 3600:
                triggers.append({"type": "long_silence", "gap_hours": round(gap / 3600, 1),
                                 "target_user_id": uid, "category": "care"})

            if not triggers:
                continue

            # 动机评估（用 known 身份）
            time_ctx = self.engine._last_time_ctx or {}
            mot = self.engine.motivation.evaluate(
                self.engine.state, time_ctx, speaker_type="known", is_group=False
            )
            tone = mot.get("tone", "安静")
            if tone in ("冷淡", "烦躁"):
                continue
            if not mot.get("should_respond", True):
                continue

            # 低概率（15%）
            if random.random() > 0.15:
                continue

            cooldowns[uid] = now
            self._known_cooldowns = cooldowns
            setattr(self, day_key, day_count + 1)
            return {"triggers": triggers, "intensity": 1, "tone": tone}
        return None

    # ── 兴趣探索 ──
    def check_interest_triggers(self) -> dict | None:
        """兴趣探索 + 心跳保底，合并为一个入口供 engine 调用"""
        pool = getattr(self.engine, "interests", None)
        if not pool or not self.cfg.interests.enabled:
            return None

        now = time.time()

        # 硬保护：1min 内只能触发一次（防跑飞）
        last_interest = getattr(self, "_last_interest_trigger", 0)
        if now - last_interest < 60:
            return None

        interest = None
        is_heartbeat = False

        # 先检查心跳
        if pool.heartbeat_due():
            interest = pool.pick_for_heartbeat()  # 绕过 min_re_explore
            if interest:
                is_heartbeat = True
                # 心跳绕过冷却闸，但不绕过静音和忽略降频
                if now < self._quiet_until:
                    return None
                if self._ignore_streak >= self.cfg.silence_after_ignored:
                    return None

        # 不是心跳 → 常规探索
        if not interest:
            min_gap = max(1800, min(86400, self.cfg.interests.min_explore_interval))  # 防呆: 30min-24h
            if now - pool.last_explore_time < min_gap:
                return None
            interest = pool.pick_for_exploration()
            if not interest:
                return None
            # 常规探索需要过概率和动机
            prob = max(0.05, min(0.8, self.cfg.interests.discovery_probability))  # 防呆: 5%-80%
            if random.random() > prob:
                return None
            # 动机评估
            time_ctx = self.engine._last_time_ctx or {}
            mot = self.engine.motivation.evaluate(
                self.engine.state, time_ctx, speaker_type="owner", is_group=False
            )
            tone = mot.get("tone", "安静")
            if tone in ("冷淡", "烦躁"):
                return None
            if not mot.get("should_respond", True):
                return None

        self._last_interest_trigger = now
        pool.last_explore_time = now

        oid = self.engine.config.owner.qq_id
        return {
            "triggers": [{
                "type": "interest_discovery",
                "keyword": interest["keyword"],
                "category": "sharing",
                "target_user_id": oid,
            }],
            "intensity": 1,
            "tone": "好奇",
            "keyword": interest["keyword"],
            "heartbeat": is_heartbeat,
        }

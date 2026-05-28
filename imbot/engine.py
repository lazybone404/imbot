import asyncio
import os
import random
import time

from imbot.utils import get_current_time_context, parse_llm_json, call_lightweight_llm, parse_time_window
from imbot.state import RuntimeState
from imbot.memory import MemoryManager
from imbot.group_memory import GroupMemoryManager
from imbot.group_perception import GroupPerception
from imbot.motivation import MotivationEngine
from imbot.rules import MetaRules
from imbot.prompt import PromptBuilder
from imbot.social_world import SocialWorld
from imbot.social_observer import SocialObserver
from imbot.segmentation import SegmentationEngine
from imbot.perception import PerceptionManager
from imbot.capability import CapabilityManager
from imbot.interests import InterestPool
from imbot.proactive import ProactiveManager
from imbot.secretary import Secretary
from imbot.prompt_orch import PromptOrchestrator, FALLBACK_TIME_CTX
from imbot.proactive_orch import ProactiveOrchestrator


class CoreEngine:
    def __init__(self, config, plugin_dir: str, plugin_context=None):
        self.config = config
        self.plugin_dir = plugin_dir
        self.plugin_context = plugin_context
        # 优先使用 AstrBot 推荐数据路径（防止更新/重装时数据丢失）
        try:
            from pathlib import Path
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            self.data_dir = str(Path(get_astrbot_data_path()) / "plugin_data" / "imbot")
        except ImportError:
            self.data_dir = os.path.join(plugin_dir, "data")
        self._last_time_ctx = None
        self._last_should_silence = False
        self._last_motivation_result = None
        self._is_proactive = False
        self._last_was_proactive = False
        self._tasks: list = []
        self._platform_id = "aiocqhttp"  # 从事件中动态更新

        self.state = None
        self.memory = None
        self.group_memory = None
        self.group_perception = None
        self.motivation = None
        self.rules = None
        self.prompt = None
        self.social_world = None
        self.social_observer = None
        self.segmentation = None
        self.perception = None
        self._current_perception = {}

    async def initialize(self):
        from astrbot.api import logger

        os.makedirs(self.data_dir, exist_ok=True)

        # 数据迁移：旧路径 → 新路径（仅首次）
        old_data = os.path.join(self.plugin_dir, "data")
        if os.path.isdir(old_data) and old_data != self.data_dir:
            if not os.path.isfile(os.path.join(self.data_dir, "state.json")):
                try:
                    import shutil
                    for item in os.listdir(old_data):
                        src = os.path.join(old_data, item)
                        dst = os.path.join(self.data_dir, item)
                        if os.path.isdir(src) and not os.path.exists(dst):
                            shutil.copytree(src, dst)
                        elif os.path.isfile(src) and not os.path.exists(dst):
                            shutil.copy2(src, dst)
                    logger.info(f"数据已从 {old_data} 迁移到 {self.data_dir}")
                except Exception:
                    logger.warning("数据迁移失败，将使用新路径", exc_info=True)

        state_path = os.path.join(self.data_dir, "state.json")
        owner_id = self.config.owner.qq_id
        self.state = RuntimeState.load(state_path, owner_id, self.config.familiarity.owner_baseline)

        mem_path = os.path.join(self.data_dir, "memory.json")
        mc = self.config.memory
        mem_cfg = {
            "deep_pool_size": mc.deep_pool_size, "core_limit": mc.core_limit,
            "decay_rates": mc.decay_rates, "intensity_decay_mod": mc.intensity_decay_mod,
            "negative_emotion_mod": mc.negative_emotion_mod, "consolidation": mc.consolidation,
            "mood_congruence_bonus": mc.mood_congruence_bonus, "output_diversity": mc.output_diversity,
            "tot_enabled": mc.tot_enabled, "emotion_residue": mc.emotion_residue,
            "owner_decay_mult": mc.owner_decay_mult, "owner_retrieval_bonus": mc.owner_retrieval_bonus,
            "owner_core_limit": mc.owner_core_limit,
        }
        self._mem_cfg = mem_cfg
        if self.config.core.use_own_memory:
            self.memory = MemoryManager(mem_path, mc.max_entries, mem_cfg,
                                         plugin_context=self.plugin_context)
            self.memory._embedding_provider = self.config.models.embedding
            if mc.consolidation:
                self.memory.consolidate()

            gm_dir = os.path.join(self.data_dir, "group_memory")
            self.group_memory = GroupMemoryManager(gm_dir, self.config.group_memory.max_entries,
                                                   self.config.group_memory.base_decay_rate)

            sw_path = os.path.join(self.data_dir, "social_world.json")
            self.social_world = SocialWorld.load(sw_path, owner_id)
            self.social_observer = SocialObserver(self.social_world)
        else:
            self.memory = None
            self.group_memory = None
            self.social_world = None
            self.social_observer = None

        self.group_perception = GroupPerception(self.state)

        self.motivation = MotivationEngine()
        self.rules = MetaRules()

        # 从已注册平台获取真实 ID
        self._resolve_platform_id()

        template_dir = os.path.join(self.plugin_dir, "templates")
        self.prompt = PromptBuilder(template_dir)

        self.segmentation = SegmentationEngine(self.config)

        # ── 编排器 ──
        self.prompt_orch = PromptOrchestrator(self)
        self.proactive_orch = ProactiveOrchestrator(self)

        # 兴趣池
        self.interests = InterestPool(self.config.interests, self.data_dir) if self.config.interests.enabled else None
        if self.interests:
            self.interests.load()
            self.interests.apply_seed_keywords()
            self.interests.bind_llm(self.proactive_orch._llm_generate_lightweight)

        # 感知层
        self.perception = PerceptionManager(self.config, self.data_dir)
        state_path = os.path.join(self.data_dir, "state.json")
        self.perception.init_late_night(state_path)
        self._tasks.append(asyncio.create_task(self.perception.loop(self)))

        # 能力层
        self.capability = CapabilityManager(self.config)

        # 秘书模型
        self.secretary = Secretary(self.config.secretary) if self.config.group_chat.enabled else None

        # 主动层
        self.proactive = ProactiveManager(self.config, self) if self.config.proactive.enabled else None
        if self.proactive:
            self._tasks.append(asyncio.create_task(self.proactive_orch._proactive_loop()))

        # 启动摘要
        owner = self.config.owner.qq_id or "(未配置)"
        mem = f"{self.config.memory.max_entries}主+{self.config.memory.deep_pool_size}深"
        perc_mods = [n for n, m in (
            ("idle", True), ("时间", True), ("熬夜", True),
            ("进程", self.config.perception.process), ("媒体", self.config.perception.media),
            ("会话", self.config.perception.session_duration.enabled),
            ("自身", self.config.perception.self_awareness.enabled),
        ) if m]
        steam_info = ""
        if self.perception and self.perception.steam:
            s = self.config.perception.steam
            steam_info = f"\n  ├─ Steam: 已启用 (SteamID={s.steam_id}, 间隔{s.poll_interval}s)"
        proactive_info = ""
        if self.config.proactive.enabled:
            p = self.config.proactive
            proactive_info = f"\n  ├─ 主动层: 私聊 {p.min_interval}min/{p.max_per_3h}次 per 3h"
        sw_count = len(self.social_world.people) if self.social_world else 0
        logger.info(
            f"imbot 引擎初始化完成\n"
            f"  ├─ 身份: owner={owner}\n"
            f"  ├─ 记忆: {mem} | 衰减率 {self.config.memory.base_decay_rate}\n"
            f"  ├─ 感知: {', '.join(perc_mods)}"
            f"{steam_info}"
            f"{proactive_info}\n"
            f"  ├─ 兴趣池: {len(self.interests.list_active()) if self.interests else 0} 个活跃兴趣\n"
            f"  └─ 社交世界: {sw_count} 人"
        )

    # ── 构建系统 Prompt ──
    async def build_system_prompt(self, event) -> dict | None:
        """返回 {"static": str, "dynamic": str} | None。
        static → req.system_prompt（可缓存），dynamic → req.extra_user_content_parts（每轮变化）。
        """
        from astrbot.api import logger

        # 从事件中提取真实的平台 ID（用户可能在 AstrBot 中自定义了名称）
        try:
            umo = getattr(event, "unified_msg_origin", "")
            if umo and ":" in umo:
                self._platform_id = umo.split(":", 1)[0]
        except Exception:
            pass

        # 时间语境
        try:
            time_ctx = get_current_time_context()
        except Exception:
            time_ctx = FALLBACK_TIME_CTX
            logger.error("时间上下文获取失败", exc_info=True)
        self._last_time_ctx = time_ctx

        # 说话者
        try:
            speaker_id = event.get_sender_id()
        except Exception:
            speaker_id = ""
        speaker_type = self.state.classify_speaker(speaker_id)

        # 场景
        try:
            is_group = bool(event.get_group_id())
        except Exception:
            is_group = False

        logger.info(f"LLM请求: speaker={speaker_type}, id={speaker_id}, owner配置={self.config.owner.qq_id}, group={is_group}, mood={self.state.mood}/{self.state.mood_intensity:.1f}")

        # 自身感知：记录唤醒
        if self.perception:
            self.perception.on_wake()
        # 状态衰减
        try:
            self.state.decay()
            self.memory.apply_decay() if self.memory else None
        except Exception:
            from astrbot.api import logger
            logger.error("状态或记忆衰减失败", exc_info=True)

        # 群聊路径
        if is_group and self.config.group_chat.enabled:
            return await self.prompt_orch._build_group_prompt(event, speaker_id, speaker_type, time_ctx)

        # 私聊路径
        return await self.prompt_orch._build_private_prompt(event, speaker_id, speaker_type, time_ctx)

    # ── 处理回复 ──
    async def process_response(self, event, response_text: str):
        from astrbot.api import logger
        if not response_text or not response_text.strip():
            return

        try:
            speaker_id = event.get_sender_id()
        except Exception:
            speaker_id = ""
        is_group = bool(event.get_group_id()) if event.get_group_id else False

        # 提取情绪
        mood = self.prompt_orch._extract_mood(response_text)

        if is_group:
            group_id = event.get_group_id()
            activity = self.group_perception.assess_activity(event)
            topic = self.group_perception.extract_topic(event)
            if self.group_memory:
                self.group_memory.add(group_id, response_text[:100], "event",
                                      int(self.state.mood_intensity * 5),
                                      {"time_period": self._last_time_ctx["period"] if self._last_time_ctx else "未知",
                                       "she_was": self.state.mood,
                                       "group_activity": activity},
                                      seed="",
                                      group_topic=topic)
                self.group_memory.save(group_id)
        else:
            self.state.record_interaction(speaker_id)

            user_msg = getattr(event, "message_str", "") or ""

            # 兴趣池：从对话中捕获话题
            if self.interests and len(user_msg) > 4:
                self.interests.capture_topic(user_msg)

            # 写入门控
            time_period = self._last_time_ctx["period"] if self._last_time_ctx else "未知"
            intensity = int(self.state.mood_intensity * 5)
            env = {"time_period": time_period, "she_was": self.state.mood}
            is_proactive = getattr(self, "_is_proactive", False)

            is_owner = bool(speaker_id) and (speaker_id == self.config.owner.qq_id)
            # 事件重要性：高强度+owner 偏好 → 可触发 core 永久记忆
            event_importance = 0.5
            if intensity >= 4:
                event_importance = 1.0
            if intensity >= 5:
                event_importance = 1.5
            if is_owner and intensity >= 4:
                event_importance += 0.5
            if self.memory and self.memory.should_write(user_msg, response_text, intensity, is_proactive, time_period, is_owner=is_owner):
                self.memory.add(response_text[:100], "event", intensity, env,
                                user_id=speaker_id, is_owner=is_owner,
                                event_importance=event_importance)
                self.memory.save()
                logger.info(f"记忆写入: intensity={intensity}, owner={is_owner}")
            elif self.memory:
                logger.debug(f"记忆跳过: gate=False")

        self.state.update_mood(mood)
        self.state.save()

        # 自身感知：记录消息和唤醒
        if self.perception:
            self.perception.on_message()

        # 社交观察
        if self.social_observer and self.config.social_world.observer_enabled:
            observations = self.social_observer.observe(event, response_text, is_group)
            observations.append({
                "type": "self_signal",
                "mood": mood,
                "target_id": speaker_id,
            })
            for obs in observations:
                self.social_world.apply_observation(obs)
            self.social_world.apply_decay(
                self.config.social_world.decay_days,
                self.config.social_world.fuzzy_days,
            )
            self.social_world.save()

    # ── 状态查询 ──
    async def get_status(self) -> str:
        sw = self.social_world
        lines = [
            f"imbot 状态",
            f"情绪: {self.state.mood} (强度: {self.state.mood_intensity:.1f})",
            f"精力: {self.state.energy:.1f}",
            f"所有者: {self.config.owner.qq_id or '未配置'}",
            f"私聊记忆: {len(self.memory.entries) if self.memory else 0} 条",
            f"熟悉度记录: {len(self.state.familiar_count)} 人",
            f"社交世界: {len(sw.people)} 人",
            f"记忆召回: {self.memory._recall_hits}/{self.memory._recall_total}",
            f"感知: {self._current_perception.get('jun_attention', '无')}",
            f"熬夜: 连续{self.perception.late_night.streak if self.perception and self.perception.late_night else 0}天",
        ]
        if sw.people:
            lines.append("认识的人:")
            for pid, p in list(sw.people.items())[:10]:
                rel = p.relationship_to_user
                lines.append(f"  {p.current_display_name or pid}: {rel.primary}({rel.confidence:.0f})")
        return "\n".join(lines)

    async def reset(self):
        self.state.reset()
        self.state.save()
        if self.memory:
            self.memory.reset()
            self.memory.save()
        if self.social_world:
            self.social_world = SocialWorld(self.config.owner.qq_id)
            self.social_world._path = os.path.join(self.data_dir, "social_world.json")
            self.social_world.save()
        if self.interests:
            from imbot.interests import InterestPool
            self.interests = InterestPool(self.config.interests, self.data_dir)
            self.interests.load()

    def reload_data(self):
        """导入数据后重新加载 MemoryManager 和 SocialWorld"""
        if self.memory:
            self.memory = MemoryManager(self.memory._path, self.config.memory.max_entries, self._mem_cfg)
        if self.social_world:
            self.social_world = SocialWorld.load(self.social_world._path, self.config.owner.qq_id)

    async def shutdown(self):
        # 停止感知层和主动层后台循环
        if self.perception:
            self.perception.stop()
        for task in self._tasks:
            if not task.done():
                task.cancel()
        # 等待任务完成取消
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self.state:
            self.state.save()
        if self.memory:
            self.memory.save()
        if self.social_world:
            self.social_world.save()
        if self.interests:
            self.interests.save()

    def _resolve_platform_id(self):
        """从已注册平台获取真实 ID"""
        try:
            for p in self.plugin_context.platform_manager.platform_insts:
                pid = p.meta().id
                cls_name = str(p.__class__.__name__).lower()
                if "aiocqhttp" in cls_name or "onebot" in cls_name:
                    self._platform_id = pid
                    return
            insts = self.plugin_context.platform_manager.platform_insts
            if insts:
                self._platform_id = insts[0].meta().id
        except Exception:
            from astrbot.api import logger
            logger.error("平台ID解析失败", exc_info=True)

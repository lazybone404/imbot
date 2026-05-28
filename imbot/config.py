from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class OwnerConfig:
    qq_id: str = ""




@dataclass(frozen=True)
class FamiliarityConfig:
    stages: tuple = ("初识", "熟识", "亲近", "深层", "羁绊")
    thresholds: tuple = (0, 5, 20, 50, 100)
    owner_baseline: str = "熟识"


@dataclass(frozen=True)
class MemoryConfig:
    max_entries: int = 150
    base_decay_rate: float = 0.02
    deep_pool_size: int = 500
    core_limit: int = 5
    decay_rates: dict = field(default_factory=lambda: {"event": 0.02, "impression": 0.01, "emotion": 0.025})
    intensity_decay_mod: float = 0.5
    negative_emotion_mod: float = 0.7
    consolidation: bool = True
    mood_congruence_bonus: float = 1.5
    output_diversity: bool = True
    tot_enabled: bool = True
    emotion_residue: bool = True
    allow_user_correction: bool = True
    owner_decay_mult: float = 1.5
    owner_retrieval_bonus: float = 1.0
    owner_core_limit: int = 3

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryConfig":
        kwargs = {"max_entries": data.get("max_entries", 80), "base_decay_rate": data.get("base_decay_rate", 0.02)}
        for k in ("deep_pool_size", "core_limit", "intensity_decay_mod", "negative_emotion_mod",
                   "mood_congruence_bonus", "consolidation", "output_diversity",
                   "tot_enabled", "emotion_residue", "allow_user_correction",
                   "owner_decay_mult", "owner_retrieval_bonus", "owner_core_limit"):
            if k in data:
                kwargs[k] = data[k]
        if "decay_rates" in data:
            kwargs["decay_rates"] = data["decay_rates"]
        return cls(**kwargs)


@dataclass(frozen=True)
class GroupMemoryConfig:
    max_entries: int = 30
    base_decay_rate: float = 0.03


@dataclass(frozen=True)
class SessionDurationConfig:
    enabled: bool = True
    long_threshold: int = 4


@dataclass(frozen=True)
class SelfAwarenessConfig:
    enabled: bool = True


@dataclass(frozen=True)
class SteamExposeConfig:
    current_game: bool = True
    rich_presence: bool = False
    playtime_session: bool = True
    recently_played: bool = True
    achievements: bool = False


@dataclass(frozen=True)
class SteamPerceptionConfig:
    enabled: bool = False
    api_key: str = ""          # "$SECRET:steam.api_key"
    steam_id: str = ""
    poll_interval: int = 300
    proxy: str = ""            # 代理地址，如 http://127.0.0.1:7890
    timeout: int = 15          # API 超时秒数
    expose: SteamExposeConfig = field(default_factory=SteamExposeConfig)


@dataclass(frozen=True)
class PerceptionConfig:
    enabled: bool = True
    interval: int = 300  # 感知刷新间隔（秒），默认 5 分钟
    idle: bool = True
    late_night_tracking: bool = True
    process: bool = True
    media: bool = True
    session_duration: SessionDurationConfig = field(default_factory=SessionDurationConfig)
    self_awareness: SelfAwarenessConfig = field(default_factory=SelfAwarenessConfig)
    steam: SteamPerceptionConfig = field(default_factory=SteamPerceptionConfig)


@dataclass(frozen=True)
class GroupChatConfig:
    enabled: bool = True
    proactive_in_group: bool = False
    proactive_level: str = "克制"  # 群聊主动回复程度: 克制/适度/活跃


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    provider: str = "openai"
    api_base: str = ""
    api_key: str = ""
    model: str = "text-embedding-3-small"


@dataclass(frozen=True)
class ModelsConfig:
    summarization: str = ""
    embedding: str = ""


@dataclass(frozen=True)
class SocialWorldConfig:
    max_people: int = 200
    decay_days: int = 30
    fuzzy_days: int = 60
    observer_enabled: bool = True


@dataclass(frozen=True)
class SceneOverridesConfig:
    group_chat_enabled: bool = False


@dataclass(frozen=True)
class SegmentationConfig:
    enabled: bool = True
    preset: str = "natural"
    min_text_length: int = 50
    method: str = "auto"
    split_strong: tuple = ("。", "？", "！", "…", "\n")
    split_medium: tuple = ("，", "；")
    min_segment_chars: int = 5
    max_segments: int = 3
    semantic_guard: bool = True
    delay_enabled: bool = True
    delay_base_min: float = 0.3
    delay_base_max: float = 1.0
    add_fillers: str = "contextual"
    rate_limit_max_per_min: int = 10
    rate_limit_burst: int = 3
    scene_overrides: SceneOverridesConfig = field(default_factory=SceneOverridesConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "SegmentationConfig":
        overrides = SceneOverridesConfig(**data.get("scene_overrides", {}))
        kwargs = {k: v for k, v in data.items() if k != "scene_overrides"}
        if "split_strong" in kwargs and isinstance(kwargs["split_strong"], list):
            kwargs["split_strong"] = tuple(kwargs["split_strong"])
        if "split_medium" in kwargs and isinstance(kwargs["split_medium"], list):
            kwargs["split_medium"] = tuple(kwargs["split_medium"])
        return cls(scene_overrides=overrides, **kwargs)


@dataclass(frozen=True)
class SpeakingStyleConfig:
    enabled: bool = True
    verbosity: str = "normal"       # terse | normal | verbose
    formality: str = "casual"       # casual | normal | formal
    emoji_usage: str = "never"      # never | rarely | sometimes
    sentence_style: str = "natural" # natural | short | long


@dataclass(frozen=True)
class ProactiveHeartbeatConfig:
    enabled: bool = True
    daily_min: int = 1        # 0-5
    window_start: str = "20:00"
    window_end: str = "23:00"


@dataclass(frozen=True)
class ProactiveTriggersConfig:
    long_silence_hours: int = 6
    late_night: bool = True
    late_night_start: int = 0     # 0-5
    late_night_end: int = 5       # 0-8
    user_returned: bool = True
    game_ended: bool = True
    late_night_music: bool = True
    long_session: bool = True
    long_session_hours: int = 4   # 1-24
    self_lonely: bool = True
    self_lonely_msg_max: int = 5   # 1-100
    self_lonely_uptime_min: int = 8  # 1-48
    self_mood_swing: bool = True
    steam_game_ended: bool = True
    steam_achievement: bool = True
    memory_anchor: bool = False
    relationship_milestone: bool = True
    known_long_silence_hours: int = 24


@dataclass(frozen=True)
class InterestHeartbeatConfig:
    enabled: bool = True
    daily_min_share: int = 1
    active_window_start: str = "12:00"
    active_window_end: str = "00:00"


@dataclass(frozen=True)
class InterestConfig:
    enabled: bool = True
    min_explore_interval: int = 14400
    min_re_explore: int = 86400
    discovery_probability: float = 0.2
    share_cooldown_per_interest: int = 86400
    auto_observe_media: bool = True
    auto_observe_games: bool = True
    auto_observe_topics: bool = True
    max_interests: int = 20
    seed_keywords: tuple = ()  # 用户手动添加的初始兴趣关键词
    heartbeat: InterestHeartbeatConfig = field(default_factory=InterestHeartbeatConfig)


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = False
    min_interval: int = 15
    max_per_3h: int = 3
    window_hours: int = 3  # 滑动窗口小时数
    base_probability: float = 0.3   # 0.05-1.0
    silence_after_ignored: int = 3
    quiet_mode_duration: int = 30
    known_min_interval: int = 120
    known_max_per_day: int = 1
    allow_known_targets: bool = False
    tendency: str = "natural"       # 主动互动语气倾向
    custom_hint: str = ""           # 主动互动自定义提示词（追加到 prompt 末尾）
    triggers: ProactiveTriggersConfig = field(default_factory=ProactiveTriggersConfig)
    heartbeat: ProactiveHeartbeatConfig = field(default_factory=ProactiveHeartbeatConfig)



@dataclass(frozen=True)
class PromptsConfig:
    max_memories_in_prompt: int = 3


@dataclass(frozen=True)
class SecretaryConfig:
    enabled: bool = True
    model_provider: str = ""
    message_window: int = 20
    min_activity: str = "活跃"
    allow_motivation_override: bool = True
    rules_apply_at: str = "both"      # "secretary" | "motivation" | "both"
    rules: str = ""
    silence_rules: str = ""           # 什么时候必须闭嘴


@dataclass(frozen=True)
class CoreConfig:
    inject_rules: bool = True
    use_own_memory: bool = True


@dataclass(frozen=True)
class ImbotConfig:
    owner: OwnerConfig = field(default_factory=OwnerConfig)
    familiarity: FamiliarityConfig = field(default_factory=FamiliarityConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    group_memory: GroupMemoryConfig = field(default_factory=GroupMemoryConfig)
    group_chat: GroupChatConfig = field(default_factory=GroupChatConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    perception: PerceptionConfig = field(default_factory=PerceptionConfig)
    social_world: SocialWorldConfig = field(default_factory=SocialWorldConfig)
    output_segmentation: SegmentationConfig = field(default_factory=SegmentationConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    speaking_style: SpeakingStyleConfig = field(default_factory=SpeakingStyleConfig)
    custom_rules: tuple = ()
    prompts: PromptsConfig = field(default_factory=PromptsConfig)
    interests: InterestConfig = field(default_factory=InterestConfig)
    core: CoreConfig = field(default_factory=CoreConfig)
    secretary: SecretaryConfig = field(default_factory=SecretaryConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "ImbotConfig":
        return cls(
            owner=OwnerConfig(**data.get("owner", {})),
            familiarity=FamiliarityConfig(
                **{k: (tuple(v) if k in ("stages", "thresholds") else v)
                   for k, v in data.get("familiarity", {}).items()}
            ),
            memory=MemoryConfig.from_dict(data.get("memory", {})),
            group_memory=GroupMemoryConfig(**data.get("group_memory", {})),
            group_chat=GroupChatConfig(**data.get("group_chat", {})),
            embedding=EmbeddingConfig(**data.get("embedding", {})),
            models=ModelsConfig(**data.get("models", {})),
            perception=PerceptionConfig(
                **{k: v for k, v in data.get("perception", {}).items()
                   if k not in ("session_duration", "self_awareness", "steam")},
                session_duration=SessionDurationConfig(**data.get("perception", {}).get("session_duration", {})),
                self_awareness=SelfAwarenessConfig(**data.get("perception", {}).get("self_awareness", {})),
                steam=SteamPerceptionConfig(
                    **{k: v for k, v in data.get("perception", {}).get("steam", {}).items() if k != "expose"},
                    expose=SteamExposeConfig(**data.get("perception", {}).get("steam", {}).get("expose", {})),
                ),
            ),
            social_world=SocialWorldConfig(**data.get("social_world", {})),
            output_segmentation=SegmentationConfig.from_dict(data.get("output_segmentation", {})),
            proactive=ProactiveConfig(
                **{k: v for k, v in data.get("proactive", {}).items()
                   if k not in ("triggers", "heartbeat")},
                triggers=ProactiveTriggersConfig(**data.get("proactive", {}).get("triggers", {})),
                heartbeat=ProactiveHeartbeatConfig(**data.get("proactive", {}).get("heartbeat", {})),
            ),
            speaking_style=SpeakingStyleConfig(**data.get("speaking_style", {})),
            custom_rules=tuple(data.get("custom_rules", []) if isinstance(data.get("custom_rules"), list) else []),
            prompts=PromptsConfig(**data.get("prompts", {})),
            interests=InterestConfig(
                **{k: v for k, v in data.get("interests", {}).items() if k != "heartbeat"},
                heartbeat=InterestHeartbeatConfig(**data.get("interests", {}).get("heartbeat", {})),
            ),
            core=CoreConfig(**data.get("core", {})),
            secretary=SecretaryConfig(**data.get("secretary", {})),
        )


class ConfigLoader:
    def __init__(self, default_config_path: str):
        self.default_path = default_config_path

    def load(self, user_config_path: str = None, webui_config: dict = None) -> ImbotConfig:
        default = self._load_yaml(self.default_path)

        if webui_config:
            merged = self._deep_merge(default, webui_config)
        else:
            merged = default

        if user_config_path and Path(user_config_path).exists():
            try:
                user = self._load_yaml(user_config_path)
                merged = self._deep_merge(merged, user)
            except yaml.YAMLError as e:
                from astrbot.api import logger
                logger.warning(f"用户配置语法错误，已忽略: {e}")

        return ImbotConfig.from_dict(merged)

    @staticmethod
    def _load_yaml(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise
        except yaml.YAMLError:
            raise

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigLoader._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

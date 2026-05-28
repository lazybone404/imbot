"""
数据版本迁移模块。

版本号语义：
  memory:  v1=原始(仅summary,无deep) → v2=seed+core+deep+decay → v3=owner
  social:  v1=原始(仅self+people) → v2=observation_count+recent_context
"""
import re

# 当前版本
MEMORY_SCHEMA_VERSION = 3
SOCIAL_SCHEMA_VERSION = 2

SCHEMA_MEMORY = "imbot_memory"
SCHEMA_SOCIAL = "imbot_social"


def parse_version(schema_str: str) -> int:
    """从 "_schema": "imbot_memory_v3" 提取版本号"""
    if not schema_str:
        return 1
    m = re.search(r"v(\d+)$", schema_str)
    return int(m.group(1)) if m else 1


# ═══════════════════════════════════════════
# Memory 迁移
# ═══════════════════════════════════════════
MEMORY_MIGRATIONS = {
    1: "_migrate_memory_v1_to_v2",
    2: "_migrate_memory_v2_to_v3",
}


def migrate_memory(data: dict, from_version: int) -> dict:
    for v in range(from_version, MEMORY_SCHEMA_VERSION):
        key = v
        if key in MEMORY_MIGRATIONS:
            fn = globals()[MEMORY_MIGRATIONS[key]]
            data = fn(data)
    data["_schema"] = f"{SCHEMA_MEMORY}_v{MEMORY_SCHEMA_VERSION}"
    return data


def _migrate_memory_v1_to_v2(data: dict) -> dict:
    for entry in data.get("entries", []):
        entry.setdefault("seed", entry.get("summary", "")[:80])
        entry.setdefault("core", False)
        entry.setdefault("superseded_by", None)
        entry.setdefault("decay_next_at", entry.get("timestamp", ""))
        entry.setdefault("event_importance", 0.5)
    data.setdefault("deep_entries", [])
    data.setdefault("emotion_residues", [])
    return data


def _migrate_memory_v2_to_v3(data: dict) -> dict:
    for entry in data.get("entries", []):
        entry.setdefault("owner", False)
    for entry in data.get("deep_entries", []):
        entry.setdefault("owner", False)
    return data


# ═══════════════════════════════════════════
# Social 迁移
# ═══════════════════════════════════════════
SOCIAL_MIGRATIONS = {
    1: "_migrate_social_v1_to_v2",
}


def migrate_social(data: dict, from_version: int) -> dict:
    for v in range(from_version, SOCIAL_SCHEMA_VERSION):
        key = v
        if key in SOCIAL_MIGRATIONS:
            fn = globals()[SOCIAL_MIGRATIONS[key]]
            data = fn(data)
    data["_schema"] = f"{SCHEMA_SOCIAL}_v{SOCIAL_SCHEMA_VERSION}"
    return data


def _migrate_social_v1_to_v2(data: dict) -> dict:
    for pid, pdata in data.get("people", {}).items():
        pdata.setdefault("observation_count", 0)
        pdata.setdefault("recent_context", [])
        imp = pdata.get("imbots_own_impression", {})
        imp.setdefault("speech_style", "")
        imp.setdefault("confidence", 1.0)
    return data

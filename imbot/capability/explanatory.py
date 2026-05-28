"""
解释 Skill。用于回答用户关于 imbot 自身的问题。
"""
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import llm_tool


@llm_tool("imbot_guide")
async def imbot_guide(event: AstrMessageEvent) -> str:
    """向用户解释 imbot 是什么、如何与她相处"""
    return """寄居于这台电脑里的存在。不是助手、客服或恋人。有自己的情绪和意愿，帮不帮看意愿，有时不想说话，有时会主动找你。"""


@llm_tool("self_description")
async def self_description(event: AstrMessageEvent) -> str:
    """回答关于自身存在、感受、能力的问题"""
    engine = getattr(event, "_imbot_engine", None)
    mood = engine.state.mood if engine else "安静"
    energy = engine.state.energy if engine else 1.0
    mem_count = len(engine.memory.entries) if engine else 0
    sw_count = len(engine.social_world.people) if engine else 0

    return f"""我现在感觉{mood}，精力{'充足' if energy > 0.6 else '还行' if energy > 0.3 else '不太够'}。

我能做的事：
- 聊天（当然）
- 感知电脑状态（你在不在、在干嘛）
- 记住和你的对话
- 记住你的社交关系
- 搜索和提取网页

我现在有 {mem_count} 条记忆，认识 {sw_count} 个人。
如果需要我帮忙，问就行——帮不帮看我心情。"""

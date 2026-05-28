import asyncio
import glob
import json
import os
import re
import sys
import time
from datetime import datetime

_plugin_dir = os.path.dirname(os.path.abspath(__file__))
if _plugin_dir not in sys.path:
    sys.path.append(_plugin_dir)  # append 代替 insert(0)，降低劫持风险

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from imbot.config import ConfigLoader
from imbot.engine import CoreEngine


@register("imbot", "imbot_dev",
          "寄居于你电脑里的存在——有脾气、有记忆、活在时间里",
          "0.1.0", "")
class ImbotPlugin(Star):
    def __init__(self, context: Context, config: "AstrBotConfig" = None):
        super().__init__(context)
        self.webui_config = config

    async def initialize(self):
        from astrbot.api import AstrBotConfig
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(plugin_dir, "config", "config_default.yaml")
        user_config_path = os.path.join(plugin_dir, "config", "user_config.yaml")

        webui_dict = dict(self.webui_config) if isinstance(self.webui_config, AstrBotConfig) else {}

        loader = ConfigLoader(config_path)
        config = loader.load(
            user_config_path if os.path.exists(user_config_path) else None,
            webui_config=webui_dict,
        )

        if not config.owner.qq_id:
            logger.warning("所有者QQ号未配置！imbot 将把所有对话者视为陌生人。")

        self.engine = CoreEngine(config=config, plugin_dir=plugin_dir,
                                 plugin_context=self.context)
        await self.engine.initialize()
        logger.info("imbot 初始化完成")

    # ── 能力意愿拦截 ──
    @filter.on_using_llm_tool()
    async def intercept_tool(self, event, tool_name: str, tool_args: dict):
        """所有 LLM 工具调用前评估意愿"""
        if not self.engine:
            return
        # 给 event 挂引擎引用，供 skill 内部使用
        event._imbot_engine = self.engine
        from imbot.capability import CapabilityWillingness
        level, refusal = self.engine.capability.evaluate(event, tool_name)
        if level == CapabilityWillingness.REFUSE:
            return {"refused": True, "message": refusal}
        if level == CapabilityWillingness.RELUCTANT:
            return {"mood": "reluctant"}

    # ── 群聊会话兜底 ──
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def _ensure_group_conv(self, event: AstrMessageEvent):
        """AstrBot LTM 要求群聊有对话会话才能回复。自动创建，省去手动 /new"""
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if not cid:
                await self.context.conversation_manager.new_conversation(
                    event.unified_msg_origin
                )
        except Exception:
            logger.debug("群聊会话创建失败（可能已在其他模式中处理）", exc_info=True)
        # 群聊活跃度追踪
        if self.engine and self.engine.group_perception:
            try:
                gid = event.get_group_id()
                if gid:
                    self.engine.group_perception.record_activity(gid)
            except Exception:
                logger.debug("群聊活跃度记录失败", exc_info=True)

    # ── LLM 请求拦截 ──
    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, req):
        if not self.engine:
            return
        if getattr(req, "func_tool", None):
            return
        # 跳过调试/管理指令，避免 LLM 额外回复
        msg = getattr(event, "message_str", "") or ""
        if msg.strip().startswith("/imbot"):
            return
        # 跳过非用户触发的 LLM 调用（主动回复等），让 AstrBot 原生处理
        if not getattr(event, "message_str", "").strip():
            return

        # 主动层钩子：只有私聊才算互动，群聊说话不重置私聊忽略计数
        if self.engine and self.engine.proactive:
            is_group = False
            try:
                is_group = bool(event.get_group_id())
            except Exception:
                pass
            if not is_group:
                self.engine.proactive.on_user_interaction()
            self.engine._last_was_proactive = False

        prompt = await self.engine.build_system_prompt(event)

        if prompt is None:
            req.system_prompt = "（不要回复任何消息。输出空白。）"
            req.prompt = "（沉默）"
            logger.debug("注入Prompt: 沉默")
        else:
            original = req.system_prompt or ""
            # 静态部分 → system_prompt（可缓存），动态部分 → extra_user_content_parts（每轮变化）
            custom_rules_text = ""
            if self.engine.config.custom_rules:
                custom_rules_text = "\n\n[自定义规则]\n" + "\n".join(f"- {r}" for r in self.engine.config.custom_rules)

            req.system_prompt = prompt["static"] + "\n\n" + original + custom_rules_text
            dynamic = prompt.get("dynamic", "")
            if dynamic:
                try:
                    from astrbot.core.agent.message import TextPart
                    req.extra_user_content_parts.append(
                        TextPart(text=f"<dynamic_context>\n{dynamic}\n</dynamic_context>").mark_as_temp()
                    )
                except ImportError:
                    # 回退：追加到 system_prompt
                    req.system_prompt += f"\n\n{dynamic}"

    # ── LLM 回复后处理 + 分段发送 ──
    @filter.on_decorating_result()
    async def update_state(self, event: AstrMessageEvent):
        if not self.engine:
            return
        # 跳过调试/管理指令（不触发状态更新和分段）
        msg = getattr(event, "message_str", "") or ""
        if msg.strip().startswith("/imbot"):
            return
        # 跳过非用户触发的回复（主动回复等）
        if not msg.strip():
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        if self.engine._last_should_silence:
            text = self._extract_text(result)
            if len(text) > 2:
                result.chain = []
                return

        response_text = self._extract_text(result)
        if not response_text:
            return

        # ── 清洗脏换行（先清洗，再交给下游：记忆/情绪/社交观察/分段都用干净文本）──
        if self.engine.segmentation and hasattr(self.engine.segmentation, "clean"):
            cleaned = self.engine.segmentation.clean(response_text)
            if cleaned != response_text:
                response_text = cleaned
                try:
                    non_text = [c for c in result.chain if not hasattr(c, "text")]
                    from astrbot.core.message.components import Plain
                    result.chain = [Plain(cleaned)] + non_text
                except Exception:
                    pass

        await self.engine.process_response(event, response_text)

        # ── 分段 ──
        if self.engine.segmentation:
            seg_result = self.engine.segmentation.segment(response_text, self._seg_context(event))
            if seg_result:
                result.chain = []
                task = asyncio.create_task(self._send_segments(event, seg_result))
                if self.engine:
                    self.engine._tasks.append(task)
                    # 清理已完成的任务引用
                    self.engine._tasks = [t for t in self.engine._tasks if not t.done()]

    # ── 分段发送 ──
    async def _send_segments(self, event, seg_result):
        segments = seg_result["segments"]
        delays = seg_result["delays"]
        fillers = seg_result["fillers"]
        try:
            for i, seg in enumerate(segments):
                if not seg.strip():
                    continue
                prefix = fillers[i] + " " if fillers[i] else ""
                text = prefix + seg
                if i > 0 and delays[i] > 0:
                    await asyncio.sleep(delays[i])
                await self._send_one(event, text)
        except Exception:
            logger.error("分段发送异常", exc_info=True)

    async def _send_one(self, event, text: str):
        from astrbot.api.event import MessageChain
        try:
            from astrbot.core.message.components import Plain
        except ImportError:
            class Plain:
                def __init__(self, text): self.text = text
        chain = MessageChain([Plain(text)])
        try:
            await event.send(chain)
        except Exception:
            try:
                umo = getattr(event, "unified_msg_origin", "")
                await self.context.send_message(umo, chain)
            except Exception:
                logger.error("消息发送失败", exc_info=True)

    # ── /segtest 预览命令 ──
    @filter.command("segtest")
    async def segtest(self, event: AstrMessageEvent):
        if not self.engine or not self.engine.segmentation:
            yield event.plain_result("分段模块未初始化。")
            return
        text = event.message_str.replace("/segtest", "", 1).strip()
        if not text:
            yield event.plain_result("用法: /segtest <测试文本>")
            return

        seg_result = self.engine.segmentation.segment(text, self._seg_context(event))
        if not seg_result:
            yield event.plain_result("不分段（文本过短/结构化内容/未启用）")
            return

        cfg = self.engine.config.output_segmentation
        lines = [f"分段预览 (preset={cfg.preset}, method={cfg.method}, max={cfg.max_segments}):"]
        for i, seg in enumerate(seg_result["segments"]):
            delay = seg_result["delays"][i] if i < len(seg_result["delays"]) else 0
            filler = seg_result["fillers"][i] if i < len(seg_result["fillers"]) else None
            prefix = f"  {filler} " if filler else "  "
            lines.append(f"段{i + 1} [{delay:.1f}s]: {prefix}{seg[:80]}{'…' if len(seg) > 80 else ''}")
        lines.append(f"── 共{len(seg_result['segments'])}段")
        yield event.plain_result("\n".join(lines))

    # ── 调试 + 导入导出命令 ──
    @filter.command("imbot")
    async def control(self, event: AstrMessageEvent):
        msg = event.message_str.strip()
        # 剥离 "/imbot" 或 "imbot" 前缀（AstrBot 可能去掉前导 /）
        for prefix in ("/imbot", "imbot"):
            if msg.startswith(prefix + " "):
                msg = msg[len(prefix):].strip()
                break
            elif msg == prefix:
                msg = ""
                break
        # 敏感操作需所有者身份
        is_owner = False
        try:
            is_owner = event.get_sender_id() == self.engine.config.owner.qq_id
        except Exception:
            pass

        if msg.startswith("steam"):
            yield event.plain_result(await self._handle_steam_test())
        elif msg.startswith("skills"):
            import imbot.capability.self_mgmt as sm
            skills = sm.list_skills(os.path.dirname(os.path.abspath(__file__)))
            yield event.plain_result(sm.format_skills(skills))
        elif msg.startswith("export"):
            if not is_owner: yield event.plain_result("只有所有者才能导出数据。"); return
            yield event.plain_result(await self._handle_export(msg))
        elif msg.startswith("import"):
            if not is_owner: yield event.plain_result("只有所有者才能导入数据。"); return
            yield event.plain_result(await self._handle_import(msg))
        elif msg.startswith("status"):
            yield event.plain_result(await self.engine.get_status())
        elif msg.startswith("interests"):
            yield event.plain_result(await self._handle_interests(msg, is_owner))
        elif msg.startswith("perception"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_perception())
        elif msg.startswith("triggers"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_triggers())
        elif msg.startswith("memory"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_memory(msg))
        elif msg.startswith("social"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_social())
        elif msg.startswith("state"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_state())
        elif msg.startswith("group"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_group(msg))
        elif msg.startswith("force speak"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_force_speak(msg))
        elif msg.startswith("test-send"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_test_send())
        elif msg.startswith("platform"):
            if not is_owner: yield event.plain_result("[调试] 仅所有者可用。"); return
            yield event.plain_result(await self._handle_debug_platform())
        elif msg.startswith("reset"):
            if not is_owner: yield event.plain_result("只有所有者才能重置。"); return
            await self.engine.reset()
            yield event.plain_result("[imbot] 已重置。")

    async def _handle_steam_test(self) -> str:
        """测试 Steam API 连接: /imbot steam"""
        eng = self.engine
        if not eng or not eng.perception or not eng.perception.steam:
            return "Steam 感知未启用。请在配置中启用 perception.steam 并填写 steam_id 和 api_key。"
        steam = eng.perception.steam
        cfg = eng.config.perception.steam
        lines = [
            f"Steam 配置: enabled=True, steam_id={cfg.steam_id}, interval={cfg.poll_interval}s",
            f"代理: {cfg.proxy or '(未配置)'}",
            f"超时: {cfg.timeout}s",
            f"API Key: {'已配置' if steam._key else '缺失'}",
            "",
            "正在测试连接...",
        ]
        try:
            import aiohttp
            key = steam._key
            sid = steam._steam_id
            timeout = aiohttp.ClientTimeout(total=cfg.timeout)
            kwargs = {}
            if cfg.proxy:
                kwargs["proxy"] = cfg.proxy
            async with aiohttp.ClientSession(**kwargs) as sess:
                url = f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key={key}&steamids={sid}"
                async with sess.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        players = data.get("response", {}).get("players", [])
                        if players:
                            p = players[0]
                            name = p.get("personaname", "未知")
                            game = p.get("gameextrainfo", "") or "未在游戏中"
                            lines.append(f"连接成功! Steam 昵称: {name}")
                            lines.append(f"当前状态: {game}")
                        else:
                            lines.append("连接成功但未获取到玩家数据，请检查 steam_id 是否正确。")
                    elif resp.status == 403:
                        lines.append("连接失败: HTTP 403 Forbidden — API Key 无效")
                    elif resp.status == 429:
                        lines.append("连接失败: HTTP 429 — 请求太频繁，请稍后再试")
                    else:
                        body = await resp.text()
                        lines.append(f"连接失败: HTTP {resp.status} — {body[:200]}")
        except Exception as e:
            lines.append(f"连接失败: {type(e).__name__} — {e}")
            if "Timeout" in type(e).__name__ or "timeout" in str(e).lower():
                lines.append("提示: Steam API 在国内可能被墙，试试配置代理 (perception.steam.proxy)")
        return self._safe_output("\n".join(lines))

    async def _handle_interests(self, msg: str, is_owner: bool) -> str:
        """兴趣池管理: /imbot interests [add|forget|pause|resume <关键词>]"""
        pool = self.engine.interests if self.engine else None
        if not pool:
            return "兴趣池未启用。请在配置中启用 interests.enabled。"

        msg = msg.replace("interests", "", 1).strip()
        parts = msg.split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        target = parts[1] if len(parts) > 1 else ""

        if cmd == "add":
            if not is_owner:
                return "只有所有者才能管理兴趣。"
            if not target:
                return "请提供关键词。用法: /imbot interests add <关键词>"
            pool.user_add(target)
            return f"已添加兴趣: {target}"

        if cmd == "force":
            if not is_owner:
                return "只有所有者才能强制探索。"
            if not target:
                return "请提供关键词。用法: /imbot interests force <关键词>"
            result = await self._handle_interests_force(target)
            return result

        if cmd == "forget":
            if not is_owner:
                return "只有所有者才能管理兴趣。"
            if not target:
                return "请提供关键词。用法: /imbot interests forget <关键词>"
            ok = pool.forget(target)
            return f"已归档兴趣: {target}" if ok else f"没有找到兴趣: {target}"

        if cmd == "pause":
            if not is_owner:
                return "只有所有者才能管理兴趣。"
            if not target:
                return "请提供关键词。用法: /imbot interests pause <关键词>"
            ok = pool.pause(target)
            return f"已暂停兴趣: {target}" if ok else f"没有找到兴趣: {target}"

        if cmd == "resume":
            if not is_owner:
                return "只有所有者才能管理兴趣。"
            if not target:
                return "请提供关键词。用法: /imbot interests resume <关键词>"
            ok = pool.resume(target)
            return f"已恢复兴趣: {target}" if ok else f"没有找到兴趣: {target}"

        # 默认：列出活跃兴趣
        active = pool.list_active()
        if not active:
            return "兴趣池为空。对 imbot 说「你可以关注一下 XX」，或等待她自动从媒体/游戏中发现。"

        SOURCE_ICONS = {
            "media_observe": "🎵", "game_observe": "🎮",
            "user_mention": "💬", "user_suggest": "✍️",
        }
        lines = ["当前兴趣:"]
        for i, it in enumerate(active[:15], 1):
            icon = SOURCE_ICONS.get(it.get("source", ""), "📌")
            intensity = it.get("intensity", 1)
            explored = it.get("explore_count", 0)
            last_shared = it.get("last_shared", 0)
            shared_ago = ""
            if last_shared:
                days = (time.time() - last_shared) / 86400
                if days < 1:
                    shared_ago = "今天分享过"
                elif days < 2:
                    shared_ago = "昨天分享过"
                else:
                    shared_ago = f"{int(days)}天前分享"
            lines.append(
                f"  {icon} {it['keyword']} "
                f"(兴趣{intensity}/查过{explored}次"
                + (f"/{shared_ago}" if shared_ago else "")
                + ")"
            )
        if len(active) > 15:
            lines.append(f"  ... 还有 {len(active) - 15} 个")
        return self._safe_output("\n".join(lines))

    # ═══════════════════════════════════════
    #  调试指令 — 仅 owner 可用
    # ═══════════════════════════════════════

    @staticmethod
    def _format_debug_header(title: str) -> str:
        return f"=== {title} ==="

    @staticmethod
    def _safe_output(text: str, max_chars: int = 3500) -> str:
        """QQ 消息有长度限制，超长截断并提示。"""
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n... (截断，共 {len(text)} 字符)"

    async def _handle_interests_force(self, keyword: str) -> str:
        """强制立即探索: /imbot interests force <关键词>"""
        pool = self.engine.interests
        if not pool:
            return "兴趣池未启用。"

        from imbot.utils import parse_llm_json, call_lightweight_llm
        prompt = (
            f"你对「{keyword}」很好奇。先调用搜索工具查一下相关的新鲜事，"
            "再基于搜索结果判断是否值得跟朋友分享。\n"
            "回复 JSON：{\"interesting\": true/false, \"one_line\": \"一句话总结\"}\n"
            "只输出 JSON。"
        )
        interesting = False
        one_line = ""
        raw = await call_lightweight_llm(self.engine, prompt)
        if raw:
            data = parse_llm_json(raw)
            if data:
                interesting = data.get("interesting", False)
                one_line = data.get("one_line", "")

        pool.record_exploration(keyword, interesting, one_line)

        lines = [
            self._format_debug_header(f"兴趣探索: {keyword}"),
            f"   LLM原始回复: {raw[:200] if raw else '(无)'}",
            f"   趣味性: {'✓ 有趣' if interesting else '✗ 无聊'}",
        ]
        if one_line:
            lines.append(f"   一句话: {one_line}")
        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_perception(self) -> str:
        """显示当前感知快照: /imbot perception"""
        perc = self.engine._current_perception or {}
        if not perc:
            return "[!] 感知数据为空，感知层可能尚未采集到数据"

        lines = [self._format_debug_header("感知快照")]
        for key, label in (
            ("primary_context", "主要行为"), ("time_context", "时间感受"),
            ("user_state", "用户状态"), ("period", "时段"), ("holiday", "节日"),
        ):
            val = perc.get(key, "")
            if val:
                lines.append(f"   {label}: {val}")
        jun = perc.get("jun_attention", "")
        if jun:
            lines.append(f"   注意力: {jun}")
        sa = perc.get("self_aware", {})
        if sa:
            lines.append(f"   自身: 今日{sa.get('daily_messages','?')}条消息, 运行{sa.get('uptime_hours','?')}h")
        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_triggers(self) -> str:
        """干跑触发检查: /imbot triggers"""
        eng = self.engine
        if not eng.proactive:
            return "[!] 主动层未启用"

        now = time.time()
        lines = [self._format_debug_header("主动层触发检查")]

        # 冷却
        gap = now - eng.proactive._last_proactive_time
        threshold = eng.config.proactive.min_interval * 60
        cd = "✗ 冷却中" if gap < threshold else "✓ 就绪"
        lines.append(f"   冷却: {cd} (距上次 {int(gap)}s / {int(threshold)}s)")
        lines.append(f"   窗口: {eng.proactive._proactive_count}/{eng.config.proactive.max_per_3h} 次 per 3h")
        if now < eng.proactive._quiet_until:
            lines.append(f"   静音: 剩余 {int(eng.proactive._quiet_until - now)}s")
        if eng.proactive._ignore_streak:
            lines.append(f"   忽略连击: {eng.proactive._ignore_streak} 次")

        # 动机
        mot = eng._last_motivation_result
        if mot:
            tone_icon = {"温和": "😊", "安静": "😐", "好奇": "🤔", "冷淡": "😒",
                         "烦躁": "😤", "随性": "😏"}.get(mot.get("tone", ""), "")
            lines.append(f"   情绪: {tone_icon} {mot.get('tone','?')} | "
                         f"主动性={mot.get('initiative',0):.2f} | "
                         f"回应={'是' if mot.get('should_respond') else '否'}")

        # 只读检查触发源（不消耗一次性标志）
        perc = eng._current_perception or {}
        period = perc.get("period", "")
        st = eng.state
        hits = []
        gap = st.get_last_interaction_gap(eng.config.owner.qq_id)
        if gap > eng.config.proactive.triggers.long_silence_hours * 3600:
            hits.append(f"💜 long_silence ({round(gap/3600,1)}h)")
        if eng.perception and eng.perception.idle and eng.perception.idle.state == "just_returned":
            hits.append("💡 user_returned")
        if period in ("深夜", "凌晨") and eng.perception and eng.perception.late_night:
            if eng.perception.late_night.streak >= 3:
                hits.append(f"💜 late_night (streak={eng.perception.late_night.streak})")
        if eng.perception and eng.perception.process and eng.perception.process.game_just_ended:
            hits.append(f"💡 game_ended ({eng.perception.process.game_duration_minutes}m)")
        if period in ("深夜", "凌晨") and eng.perception and eng.perception.media:
            if eng.perception.media.media_type == "music" and eng.perception.media.is_playing:
                hits.append("💭 late_night_music")
        if eng.perception and eng.perception.session:
            threshold = eng.config.perception.session_duration.long_threshold * 60
            if eng.perception.session.today_minutes > threshold:
                hits.append(f"💜 long_session ({round(eng.perception.session.today_minutes/60,1)}h)")
        if eng.perception and eng.perception.self_aware:
            sa = eng.perception.self_aware
            if sa.daily_messages < 5 and sa.uptime_hours > 8:
                hits.append(f"💜 self_lonely ({sa.daily_messages}msg/{sa.uptime_hours}h)")
        if st._previous_mood and st._previous_mood != st.mood and st.mood_intensity > 0.5:
            hits.append(f"💭 self_mood_swing ({st._previous_mood}→{st.mood})")
        if eng.perception and eng.perception.steam:
            if getattr(eng.perception.steam, "game_just_ended", False):
                hits.append(f"💡 steam_game_ended")
            if getattr(eng.perception.steam, "new_achievements", []):
                hits.append(f"💡 steam_achievement ({len(eng.perception.steam.new_achievements)}个)")
        if hits:
            lines.append("  ")
            lines.append("   → 可触发（只读）:")
            for h in hits:
                lines.append(f"     {h}")
        else:
            lines.append("   → 无触发源")

        # 兴趣池
        pool = eng.interests
        if pool and pool.cfg.enabled:
            lines.append("  ")
            lines.append(f"   兴趣池: {len(pool.list_active())} 活跃 | "
                         f"今日分享 {pool.today_share_count}")
            hb = pool.heartbeat_due()
            window = f"{pool.cfg.heartbeat.active_window_start}-{pool.cfg.heartbeat.active_window_end}"
            lines.append(f"   心跳: {'● 窗口内' if hb else '○ 窗口外'} ({window})")

        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_memory(self, msg: str) -> str:
        """查看最近记忆: /imbot memory [N]"""
        n = 5
        parts = msg.replace("memory", "", 1).strip()
        if parts.isdigit():
            n = min(int(parts), 20)

        entries = self.engine.memory.entries[:n]
        if not entries:
            return "[!] 记忆池为空"

        lines = [self._format_debug_header(f"记忆 (最近 {len(entries)} 条)")]
        for i, e in enumerate(entries, 1):
            summary = e.get("summary", "")[:50]
            etype = e.get("type", "?")
            weight = e.get("weight", 0)
            flags = ""
            if e.get("core"): flags += "⭐"
            if e.get("owner"): flags += " 所有者"
            tf = e.get("time_feel", "")
            lines.append(f"   {i}. [{etype}] w={weight:.1f} {flags}")
            lines.append(f"      {summary}")
            if tf:
                lines.append(f"      time_feel: {tf}")
        lines.append(f"-- 总记忆: {len(self.engine.memory.entries)} 主池 + {len(self.engine.memory.deep_entries)} 深层池")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_social(self) -> str:
        """查看社交世界: /imbot social"""
        sw = self.engine.social_world
        if not sw or not sw.people:
            return "[!] 社交世界为空"

        lines = [self._format_debug_header(f"社交世界 ({len(sw.people)} 人)")]
        for pid, p in list(sw.people.items())[:20]:
            rel = getattr(p, "relationship_to_user", None)
            rel_type = rel.primary if rel else "?"
            conf = rel.confidence if rel else 0
            name = getattr(p, "current_display_name", "")
            display = f"{name}({pid})" if name else pid
            lines.append(f"   {display}: {rel_type} (conf={conf:.0f})")
        if len(sw.people) > 20:
            lines.append(f"   ... 还有 {len(sw.people) - 20} 人")
        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_state(self) -> str:
        """查看原始状态: /imbot state"""
        st = self.engine.state
        lines = [self._format_debug_header("自身状态")]
        mood_map = {"安静": "😐", "低落": "😔", "开心": "😊", "烦躁": "😤",
                     "好奇": "🤔", "疲惫": "😴"}
        icon = mood_map.get(st.mood, "")
        lines.append(f"   情绪: {icon} {st.mood} (强度 {st.mood_intensity:.2f})")
        energy_bar = "▓" * int(st.energy * 10) + "░" * (10 - int(st.energy * 10))
        lines.append(f"   精力: [{energy_bar}] {st.energy:.2f}")
        lines.append(f"   上一情绪: {st._previous_mood}")
        lines.append(f"   熟悉度记录: {len(st._familiarity)} 人")
        for uid, count in sorted(st._familiarity.items(), key=lambda x: -x[1])[:10]:
            stage = st.get_stage(uid)
            lines.append(f"     {uid}: {stage}({count})")
        lines.append(f"   沉默计数: {st._silence_count}")
        lines.append(f"   最后主动发送: {time.strftime('%H:%M:%S', time.localtime(st._last_proactive_sent)) if st._last_proactive_sent else '从未'}")
        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_group(self, msg: str) -> str:
        """查看群聊数据: /imbot group [群号]"""
        gid = msg.replace("group", "", 1).strip()
        gm = self.engine.group_memory if self.engine else None
        if not gm:
            return "[!] 群记忆未启用"

        # 如果指定了群号
        if gid and gid.isdigit():
            gm_path = os.path.join(self.engine.data_dir, "group_memory", f"{gid}.json")
            entries = []
            try:
                with open(gm_path, "r", encoding="utf-8") as fh:
                    data = json.loads(fh.read())
                entries = data.get("entries", [])
            except FileNotFoundError:
                pass
            except Exception:
                pass
            lines = [self._format_debug_header(f"群 {gid} 记忆 ({len(entries)} 条)")]
            for i, e in enumerate(entries[-10:], 1):
                summary = e.get("summary", "")[:50]
                lines.append(f"   {i}. {summary}")
            lines.append("===")
            return self._safe_output("\n".join(lines))

        # 列出所有群
        gm_dir = os.path.join(self.engine.data_dir, "group_memory")
        files = glob.glob(os.path.join(gm_dir, "*.json"))
        if not files:
            return "[!] 无群记忆数据"

        lines = [self._format_debug_header(f"群聊 ({len(files)} 个群)")]
        for f in sorted(files):
            gid = os.path.splitext(os.path.basename(f))[0]
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.loads(fh.read())
                count = len(data.get("entries", []))
                lines.append(f"   {gid}: {count} 条记忆")
            except Exception:
                lines.append(f"   {gid}: (读取失败)")
        lines.append("===")
        return self._safe_output("\n".join(lines))

    async def _handle_debug_force_speak(self, msg: str) -> str:
        """强制触发主动回复: /imbot force speak [as <类型>]"""
        eng = self.engine
        if not eng.proactive:
            return "[!] 主动层未启用"

        msg = msg.replace("force speak", "", 1).strip()
        trigger_type = "debug_force"
        category = "sharing"
        tone = "好奇"

        # 可选: 指定触发类型
        if msg.startswith("as "):
            requested = msg[3:].strip().lower()
            valid_types = {
                "long_silence": ("care", "你已经很久没和用户说话了"),
                "late_night": ("care", "现在是深夜"),
                "game_ended": ("curiosity", "用户刚打完游戏"),
                "late_night_music": ("sharing", "深夜，用户在听歌"),
                "long_session": ("care", "用户在线很久了"),
                "self_lonely": ("care", "今天话很少"),
                "self_mood_swing": ("sharing", "情绪变了"),
                "memory_anchor": ("sharing", "触景生情"),
            }
            if requested in valid_types:
                trigger_type = requested
                category, _ = valid_types[requested]
            else:
                return f"[!] 未知触发类型: {requested}\n可选: {', '.join(valid_types.keys())}"

        oid = eng.config.owner.qq_id
        trigger_ctx = {
            "triggers": [{
                "type": trigger_type,
                "category": category,
                "target_user_id": oid,
            }],
            "intensity": 2,
            "tone": tone,
        }

        try:
            # 保存防呆状态，调试不污染生产计数器
            saved_time = eng.proactive._last_proactive_time
            saved_count = eng.proactive._proactive_count
            saved_type = eng.proactive._last_trigger_type

            await eng._execute_proactive(trigger_ctx)

            # 恢复防呆状态
            eng.proactive._last_proactive_time = saved_time
            eng.proactive._proactive_count = saved_count
            eng.proactive._last_trigger_type = saved_type
            return f"已触发主动互动 (类型: {trigger_type})"
        except Exception as e:
            return f"[!] 触发失败: {e}"

    async def _handle_debug_test_send(self) -> str:
        """测试主动消息发送通道: /imbot test-send"""
        eng = self.engine
        oid = eng.config.owner.qq_id
        if not oid:
            return "[!] 未配置 owner QQ 号"
        eng._resolve_platform_id()  # 刷新平台 ID
        from astrbot.api.event import MessageChain
        try:
            from astrbot.core.message.components import Plain
        except ImportError:
            class Plain:
                def __init__(self, text): self.text = text
        umo = f"{eng._platform_id}:FriendMessage:{oid}"
        try:
            await eng.plugin_context.send_message(umo, MessageChain([Plain("这是一条测试消息，来自 imbot 调试通道。")]))
            return f"已向 {umo} 发送测试消息"
        except Exception as e:
            return f"[!] 发送失败: {e}"

    async def _handle_debug_platform(self) -> str:
        """查看平台 ID: /imbot platform"""
        eng = self.engine
        try:
            insts = eng.plugin_context.platform_manager.platform_insts
            lines = ["已注册平台:"]
            for p in insts:
                lines.append(f"  {p.meta().id} ({type(p).__name__})")
            if not insts:
                lines.append("  (无)")
            lines.append(f"\n当前使用: {eng._platform_id}")
            return "\n".join(lines)
        except Exception as e:
            return f"[!] 获取失败: {e}"

    # ── 导出 ──
    async def _handle_export(self, msg: str) -> str:
        import json
        import shutil
        import yaml
        from datetime import datetime
        from imbot.migrate import SCHEMA_MEMORY, SCHEMA_SOCIAL, MEMORY_SCHEMA_VERSION, SOCIAL_SCHEMA_VERSION

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        export_dir = os.path.join(plugin_dir, "data", "exports")
        os.makedirs(export_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        eng = self.engine

        if "memory" in msg:
            data = {
                "_schema": f"{SCHEMA_MEMORY}_v{MEMORY_SCHEMA_VERSION}",
                "_exported_at": datetime.now().isoformat(),
                "entries": eng.memory.entries,
                "deep_entries": eng.memory.deep_entries,
                "emotion_residues": eng.memory.emotion_residues,
                "recall_hits": eng.memory._recall_hits,
                "recall_total": eng.memory._recall_total,
            }
            path = os.path.join(export_dir, f"memory_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return f"已导出到 data/exports/memory_{ts}.json"

        elif "social" in msg:
            data = eng.social_world.to_dict()
            data["_schema"] = f"{SCHEMA_SOCIAL}_v{SOCIAL_SCHEMA_VERSION}"
            data["_exported_at"] = datetime.now().isoformat()
            path = os.path.join(export_dir, f"social_{ts}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return f"已导出到 data/exports/social_{ts}.json"

        elif "config" in msg:
            user_cfg = os.path.join(plugin_dir, "config", "user_config.yaml")
            if not os.path.exists(user_cfg):
                return "没有 user_config.yaml 可导出。"
            path = os.path.join(export_dir, f"config_{ts}.yaml")
            shutil.copy(user_cfg, path)
            return f"已导出到 data/exports/config_{ts}.yaml"

        elif "state" in msg:
            state_path = os.path.join(plugin_dir, "data", "state.json")
            if not os.path.exists(state_path):
                return "state.json 不存在。"
            path = os.path.join(export_dir, f"state_{ts}.json")
            shutil.copy(state_path, path)
            return f"已导出到 data/exports/state_{ts}.json"

        elif "all" in msg:
            import zipfile
            zip_path = os.path.join(export_dir, f"imbot_{ts}.zip")
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                # memory
                mem_path = os.path.join(export_dir, f"memory_{ts}.json")
                with open(mem_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "_schema": f"{SCHEMA_MEMORY}_v{MEMORY_SCHEMA_VERSION}",
                        "_exported_at": datetime.now().isoformat(),
                        "entries": eng.memory.entries,
                        "deep_entries": eng.memory.deep_entries,
                        "emotion_residues": eng.memory.emotion_residues,
                    }, f, ensure_ascii=False, indent=2)
                zf.write(mem_path, f"memory_{ts}.json")
                # social
                sw_data = eng.social_world.to_dict()
                sw_data["_schema"] = f"{SCHEMA_SOCIAL}_v{SOCIAL_SCHEMA_VERSION}"
                sw_data["_exported_at"] = datetime.now().isoformat()
                sw_path = os.path.join(export_dir, f"social_{ts}.json")
                with open(sw_path, "w", encoding="utf-8") as f:
                    json.dump(sw_data, f, ensure_ascii=False, indent=2)
                zf.write(sw_path, f"social_{ts}.json")
                # config
                user_cfg = os.path.join(plugin_dir, "config", "user_config.yaml")
                if os.path.exists(user_cfg):
                    cfg_path = os.path.join(export_dir, f"config_{ts}.yaml")
                    shutil.copy(user_cfg, cfg_path)
                    zf.write(cfg_path, f"config_{ts}.yaml")
                # state
                st_path = os.path.join(plugin_dir, "data", "state.json")
                if os.path.exists(st_path):
                    st_dest = os.path.join(export_dir, f"state_{ts}.json")
                    shutil.copy(st_path, st_dest)
                    zf.write(st_dest, f"state_{ts}.json")
            return f"已导出到 data/exports/imbot_{ts}.zip"

        else:
            return "用法: /imbot export <memory|social|config|state|all>"

    # ── 导入 ──
    async def _handle_import(self, msg: str) -> str:
        import json
        import yaml

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        import_dir = os.path.join(plugin_dir, "data", "imports")
        os.makedirs(import_dir, exist_ok=True)
        eng = self.engine

        parts = msg.split()
        if len(parts) < 2:
            return "用法: /imbot import <memory|social|config> <文件名> 或 /imbot import list"

        sub = parts[1]

        if sub == "list":
            try:
                files = os.listdir(import_dir)
            except FileNotFoundError:
                files = []
            return "imports/ 目录: " + (", ".join(files) if files else "(空)")

        if len(parts) < 3:
            return f"请指定文件名。用法: /imbot import {sub} <文件名>"

        filename = parts[2]
        # 路径穿越防护
        if re.search(r'[/\\]|\.\.', filename):
            return "非法文件名。"
        src = os.path.join(import_dir, filename)
        if not os.path.exists(src) or not os.path.realpath(src).startswith(os.path.realpath(import_dir)):
            return f"文件不存在: data/imports/{filename}"

        # 自动备份
        backup_dir = os.path.join(plugin_dir, "data", "exports", f"auto_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        os.makedirs(backup_dir, exist_ok=True)

        if sub == "memory":
            from imbot.migrate import parse_version, migrate_memory, MEMORY_SCHEMA_VERSION
            with open(src, "r", encoding="utf-8") as f:
                data = json.load(f)
            ver = parse_version(data.get("_schema", ""))
            if ver > MEMORY_SCHEMA_VERSION:
                return f"导出文件版本(v{ver})高于当前版本(v{MEMORY_SCHEMA_VERSION})，无法导入。"
            # 备份
            mem_path = os.path.join(plugin_dir, "data", "memory.json")
            if os.path.exists(mem_path):
                import shutil
                shutil.copy(mem_path, os.path.join(backup_dir, "memory.json"))
            # 迁移
            data = migrate_memory(data, ver)
            # 原子写入
            tmp = mem_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in data.items() if not k.startswith("_")}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, mem_path)
            eng.reload_data()
            return f"已导入 memory (v{ver}→v{MEMORY_SCHEMA_VERSION})。旧数据备份在 data/exports/"

        elif sub == "social":
            from imbot.migrate import parse_version, migrate_social, SOCIAL_SCHEMA_VERSION
            with open(src, "r", encoding="utf-8") as f:
                data = json.load(f)
            ver = parse_version(data.get("_schema", ""))
            if ver > SOCIAL_SCHEMA_VERSION:
                return f"导出文件版本(v{ver})高于当前版本(v{SOCIAL_SCHEMA_VERSION})，无法导入。"
            sw_path = os.path.join(plugin_dir, "data", "social_world.json")
            if os.path.exists(sw_path):
                import shutil
                shutil.copy(sw_path, os.path.join(backup_dir, "social_world.json"))
            # 保留当前 self
            current = None
            if os.path.exists(sw_path):
                with open(sw_path, "r", encoding="utf-8") as f:
                    current = json.load(f)
            data = migrate_social(data, ver)
            if current:
                data["self"] = current.get("self", data.get("self", {}))
            tmp = sw_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in data.items() if not k.startswith("_")}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, sw_path)
            eng.reload_data()
            return f"已导入 social (v{ver}→v{SOCIAL_SCHEMA_VERSION})。旧数据备份在 data/exports/"

        elif sub == "config":
            import shutil
            user_cfg = os.path.join(plugin_dir, "config", "user_config.yaml")
            if os.path.exists(user_cfg):
                shutil.copy(user_cfg, os.path.join(backup_dir, "user_config.yaml"))
            with open(src, "r", encoding="utf-8") as f:
                imported = yaml.safe_load(f) or {}
            tmp = user_cfg + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(imported, f, allow_unicode=True, default_flow_style=False)
            os.replace(tmp, user_cfg)
            return "已写入 config。旧配置备份在 data/exports/（重启 AstrBot 后生效）"

        else:
            return f"未知的导入类型: {sub}。可用: memory, social, config"

    # ── 清理 ──
    async def terminate(self):
        if self.engine:
            await self.engine.shutdown()
        logger.info("imbot 已终止")

    # ── 内部 ──
    @staticmethod
    def _extract_text(result) -> str:
        text_parts = []
        for comp in result.chain:
            if hasattr(comp, "text"):
                text_parts.append(comp.text)
        return "".join(text_parts).strip()

    def _seg_context(self, event) -> dict:
        """构造分段模块需要的 context"""
        eng = self.engine
        speaker_id = ""
        try:
            speaker_id = event.get_sender_id()
        except Exception:
            pass

        gap = eng.state.get_last_interaction_gap(speaker_id) if eng.state else 0.0

        mot = {"tone": "安静", "should_respond": True, "initiative": 0.5}
        if hasattr(eng, "_last_motivation_result"):
            mot = eng._last_motivation_result or mot

        return {
            "state": eng.state,
            "motivation": mot,
            "is_group": bool(event.get_group_id()) if event.get_group_id else False,
            "speaker_type": eng.state.classify_speaker(speaker_id) if eng.state else "stranger",
            "last_interaction_gap": gap,
            "is_proactive": getattr(eng, "_is_proactive", False),
        }

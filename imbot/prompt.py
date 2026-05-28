"""Prompt 构建器。静态模板（场景骨架 + MetaRules）与动态上下文分离。"""
import os

from jinja2 import Environment, FileSystemLoader, Template


class PromptBuilder:
    def __init__(self, template_dir: str):
        self._env = Environment(loader=FileSystemLoader(template_dir), autoescape=False)
        try:
            self._template: Template = self._env.get_template("system_base.j2")
        except Exception:
            self._template = None

    def build(self, **kwargs) -> str:
        kwargs.setdefault("rules", "")
        kwargs.setdefault("dynamic_context", "")
        for key in kwargs:
            if kwargs[key] is None:
                kwargs[key] = ""
        if self._template is None:
            return f"[场景]\n这是你的用户，你寄居在他电脑里。\n\n你的身份、性格和规则已在系统提示词中完整定义。\n\n{{dynamic_context}}"
        return self._template.render(**kwargs)

    @staticmethod
    def build_dynamic(**kwargs) -> str:
        """构建每轮变化的动态上下文（不参与 system_prompt 缓存）。"""
        parts = []

        speaking_style = kwargs.get("speaking_style", "")
        if speaking_style:
            parts.append(f"[你的说话方式]\n{speaking_style}")

        time_text = kwargs.get("time_context", "")
        if time_text:
            parts.append(f"[现在的时间]\n{time_text}")

        state_text = kwargs.get("self_state", "")
        if state_text:
            parts.append(f"[你现在的状态]\n{state_text}")

        tone = kwargs.get("tone", "")
        if tone:
            parts.append(f"[你此刻的语气]\n{tone}")

        context = kwargs.get("context", "")
        if context:
            parts.append(context)

        return "\n\n".join(parts)

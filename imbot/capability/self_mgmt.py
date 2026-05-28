"""
自我管理 Skill。管理 imbot 的扩展能力。
"""
import os


def list_skills(plugin_dir: str) -> list[dict]:
    """列出所有已注册的能力 Skill"""
    skills_dir = os.path.join(plugin_dir, "skills")
    result = []
    try:
        for name in os.listdir(skills_dir):
            skill_path = os.path.join(skills_dir, name)
            if os.path.isdir(skill_path):
                md_path = os.path.join(skill_path, "SKILL.md")
                desc = ""
                if os.path.exists(md_path):
                    with open(md_path, "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("description:"):
                                desc = line.split(":", 1)[1].strip()
                                break
                result.append({"name": name, "description": desc})
    except FileNotFoundError:
        pass
    return result


def format_skills(skills: list[dict]) -> str:
    if not skills:
        return "暂无已注册的能力 Skill。"
    lines = ["当前加载的能力 Skill:"]
    for s in skills:
        lines.append(f"  {s['name']} — {s['description']}")
    return "\n".join(lines)

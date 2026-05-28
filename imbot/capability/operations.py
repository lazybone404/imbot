"""
操作 Skill 实现。引擎直接调度，不暴露给 LLM。
"""
import ctypes
import os
import platform
from ctypes import wintypes


# ── 白名单程序 ──
PROGRAM_WHITELIST = {
    "notepad": "notepad.exe",
    "记事本": "notepad.exe",
    "calc": "calc.exe",
    "计算器": "calc.exe",
    "explorer": "explorer.exe",
    "资源管理器": "explorer.exe",
    "mspaint": "mspaint.exe",
    "画图": "mspaint.exe",
    "cmd": "cmd.exe",
}

# ── 打开程序 ──
def open_program(name: str) -> str:
    """打开白名单程序"""
    exe = PROGRAM_WHITELIST.get(name.lower())
    if not exe:
        return f"'{name}'不在白名单中。可用: {', '.join(PROGRAM_WHITELIST.keys())}"
    try:
        os.startfile(exe)
        return f"已打开 {name}。"
    except Exception as e:
        return f"打开失败: {e}"


# ── 创建便签 ──
def create_note(content: str, data_dir: str = "", filename: str = "") -> str:
    """创建文本便签到 data/notes/"""
    import datetime
    if not data_dir:
        return "便签目录未配置。"
    notes_dir = os.path.join(data_dir, "notes")
    os.makedirs(notes_dir, exist_ok=True)
    if not filename:
        filename = datetime.datetime.now().strftime("note_%Y%m%d_%H%M%S.txt")
    filepath = os.path.join(notes_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已创建便签 data/notes/{filename}"
    except Exception as e:
        return f"便签创建失败: {e}"

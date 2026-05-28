"""
能力 Skill 注册。通过 AstrBot @llm_tool 暴露给 LLM。
经过 @on_using_llm_tool 意愿拦截后才执行。
"""
import os

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import llm_tool


@llm_tool("text_extract")
async def text_extract(event: AstrMessageEvent, url: str) -> str:
    """提取网页文本内容并摘要"""
    import re
    from urllib.parse import urlparse
    try:
        import ipaddress
        PRIVATE_RANGES = [
            ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("0.0.0.0/8"),
        ]
    except ImportError:
        PRIVATE_RANGES = []

    # SSRF 防护
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "仅支持 http/https 链接。"
    hostname = parsed.hostname or ""
    if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"):
        return "不允许访问内部地址。"
    if PRIVATE_RANGES:
        try:
            addr = ipaddress.ip_address(hostname)
            if any(addr in r for r in PRIVATE_RANGES):
                return "不允许访问内部地址。"
        except ValueError:
            pass  # 非 IP 地址 hostname，放过

    try:
        import aiohttp
        MAX_SIZE = 5 * 1024 * 1024  # 5MB
        async with aiohttp.ClientSession() as sess:
            headers = {"User-Agent": "imbot/1.0"}
            async with sess.get(url, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return f"无法访问该链接: HTTP {resp.status}"
                length = resp.headers.get("Content-Length")
                if length and int(length) > MAX_SIZE:
                    return f"网页过大（>{MAX_SIZE // 1024 // 1024}MB），拒绝提取。"
                html = await resp.text()
                if len(html) > MAX_SIZE:
                    return f"网页过大（>{MAX_SIZE // 1024 // 1024}MB），拒绝提取。"
                text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S)
                text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:2000] if len(text) > 2000 else text
    except ImportError:
        return "aiohttp 未安装，无法提取网页。"
    except Exception as e:
        return f"提取失败: {e}"

"""分享消息的精简文本：不依赖 AstrBot，也不修改解析结果或下载地址。"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

# 只删除已知分享追踪参数；未知参数、分 P / 定位参数、访问凭证必须保留。
_TRACKING_KEYS = {
    "spm", "spm_id_from", "from_spmid", "vd_source", "unique_k",
    "share_source", "share_medium", "share_plat", "share_session_id",
    "share_tag", "share_app_id", "share_link_id", "share_from_user_hidden",
    "share_iid", "share_id", "shareuid", "share_at",
    "is_from_webapp", "sender_device", "web_id",
}
# 平台分享参数只在已知平台清理；自定义解析器的同名参数可能有业务含义。
_SHARE_DOMAINS = (
    "bilibili.com", "b23.tv", "douyin.com", "iesdouyin.com", "kuaishou.com",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn", "weibo.com", "weibo.cn",
    "acfun.cn", "twitter.com", "x.com",
)
# timestamp / ts 在其它网站可能是签名或播放定位，只在 B 站按分享追踪处理。
_BILI_TRACKING_KEYS = {"from", "timestamp", "bbid", "ts", "share_times"}
_SOURCE_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::[0-9]+)?"
    r"(?:[/?#][^\s<>\"'，。！？；（）【】]*)?"
)
_PLAY_COUNT_RE = re.compile(r"(?:👀|播放(?:量)?[：:]?)\s*([0-9][0-9.,]*\s*[万亿kKmMwW]?)")


def source_url(searched: re.Match[str]) -> str | None:
    """缺少规范地址时，找回与解析器匹配片段重叠的原链接（不是消息中别的链接）。

    重定向解析会先在内层拿到落地页链接；已有规范地址的结果不调用此函数。
    """
    for match in _SOURCE_URL_RE.finditer(searched.string):
        if match.start() < searched.end() and match.end() > searched.start():
            url = match.group().rstrip(".,!?;:)]}")
            return url if url.startswith(("http://", "https://")) else "https://" + url
    return None


def clean_share_url(raw: Any) -> str:
    """仅清理展示用 URL；不重编码剩余参数，避免破坏访问令牌 / 签名。"""
    value = str(raw or "").strip()
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        if parts.username or parts.password:
            return ""
    except ValueError:
        return ""
    host = parts.hostname.lower().rstrip(".")
    is_share_host = any(host == domain or host.endswith("." + domain) for domain in _SHARE_DOMAINS)
    is_bili = host == "bilibili.com" or host.endswith(".bilibili.com")
    is_twitter = host in ("twitter.com", "www.twitter.com", "x.com", "www.x.com", "mobile.twitter.com")
    query = []
    for param in parts.query.split("&"):
        key = unquote_plus(param.partition("=")[0]).casefold()
        if not param or key.startswith("utm_") or (is_share_host and key in _TRACKING_KEYS):
            continue
        if (is_bili and key in _BILI_TRACKING_KEYS) or (is_twitter and key in ("s", "t")):
            continue
        query.append(param)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(query), parts.fragment))


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _duration(result: Any) -> str:
    if value := result.extra.get("duration"):
        if isinstance(value, str):
            return _one_line(value, 24)
    else:
        media = result.video_contents or result.audio_contents
        value = media[0].duration if media else None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(seconds) or seconds <= 0:
        return ""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


_PLACEHOLDER_RE = re.compile(r"\{(title|url|info|warnings|summary|cover|play_url|platform|author)\}")
# 地址为空的 [文字]() / ![图]()：模板里某个平台没有该字段时整段去掉
_EMPTY_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(\s*\)[ \t]*")
# 只认 http(s)：模板里的 file:// 等地址不能变成读本地文件的图片
_MD_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\((https?://[^\s)]+)\)")
_MD_LINK_RE = re.compile(r"[ \t]*\[([^\]\n]*)\]\((https?://[^\s)]+)\)[ \t]*")


def _http_url(value: Any) -> str:
    text = str(value or "")
    return text if text.startswith(("http://", "https://")) else ""


def _fields(result: Any) -> dict[str, str]:
    """模板可用字段；值都是单行（warnings 除外，每条限制一行）。"""
    title = _one_line(result.title, 100) or _one_line(result.text, 100)
    if not title:
        title = f"{result.platform.display_name} · {result.content_type}"
    info = []
    if duration := _duration(result):
        info.append(f"⏱ {duration}")
    if match := _PLAY_COUNT_RE.search(str(result.extra.get("stats_line") or "")):
        info.append(f"播放 {''.join(match.group(1).split())}")
    warnings = result.extra.get("limit_warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    warnings = list(dict.fromkeys(_one_line(w, 100) for w in warnings if w))
    shown = warnings[:2] + ([f"另有 {len(warnings) - 2} 项限制"] if len(warnings) > 2 else [])
    author = getattr(result, "author", None)
    return {
        "title": title,
        "url": clean_share_url(result.url),
        "info": " · ".join(info),
        "warnings": "\n".join(shown),
        "summary": _one_line(result.extra.get("summary"), 500),
        "cover": _http_url(result.extra.get("cover")),
        "play_url": _http_url(result.extra.get("play_url")),
        "platform": result.platform.display_name,
        "author": _one_line(getattr(author, "name", ""), 50),
    }


def render_template(template: str, fields: dict[str, str]) -> str:
    """替换占位符；所含占位符全为空的行整行删掉、空地址链接去掉，不留「AI 总结：」这种空壳。"""
    lines = []
    for line in template.splitlines():
        keys = _PLACEHOLDER_RE.findall(line)
        if keys and not any(fields[key] for key in keys):
            continue
        line = _EMPTY_LINK_RE.sub("", _PLACEHOLDER_RE.sub(lambda m: fields[m.group(1)], line))
        if keys and not line.strip():
            continue
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_markdown(text: str) -> list[tuple[str, str]]:
    """给不渲染 Markdown 的平台（OneBot / NapCat）：拆成 ``("image", 地址)`` / ``("text", 文本)``。

    链接改成「文字：地址」单独一行，去掉 ** 加粗并合并空行。
    """
    parts: list[tuple[str, str]] = []
    for index, piece in enumerate(_MD_IMAGE_RE.split(text)):
        if index % 2:
            parts.append(("image", piece))
            continue
        piece = _MD_LINK_RE.sub(
            lambda m: f"\n{m.group(1)}：{m.group(2)}\n" if m.group(1).strip() else f"\n{m.group(2)}\n",
            piece,
        )
        piece = re.sub(r"\n\s*\n", "\n", piece.replace("**", "")).strip()
        if piece:
            parts.append(("text", piece))
    return parts


def build_share_text(result: Any, template: str = "") -> str:
    """默认：标题、干净地址、时长 / 播放量各一行，每条限制提示单独一行。

    简介只在没有标题时截取作标题；默认不发正文、AI 总结、作者头、在线人数
    或整排互动统计。填了模板（如 Markdown）则按模板输出，仍是同一条消息。
    """
    fields = _fields(result)
    if template.strip():
        return render_template(template, fields)
    return "\n".join(fields[key] for key in ("title", "url", "info", "warnings") if fields[key])

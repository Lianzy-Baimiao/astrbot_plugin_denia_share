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


def build_share_text(result: Any) -> str:
    """通常三行：标题、干净地址、时长 / 播放量；缺料说明合并进最后一行。

    简介只在没有标题时截取作标题，不再发送正文、AI 总结、作者头、在线人数
    或整排互动统计。保留下载限制 / 缺料提示，但不再单独发一条消息。
    """
    title = _one_line(result.title, 100) or _one_line(result.text, 100)
    if not title:
        title = f"{result.platform.display_name} · {result.content_type}"
    lines = [title]
    if url := clean_share_url(result.url):
        lines.append(url)
    info = []
    if duration := _duration(result):
        info.append(f"⏱ {duration}")
    if match := _PLAY_COUNT_RE.search(str(result.extra.get("stats_line") or "")):
        count = "".join(match.group(1).split())
        info.append(f"播放 {count}")
    warnings = result.extra.get("limit_warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    warnings = list(dict.fromkeys(_one_line(w, 100) for w in warnings if w))
    info.extend(warnings[:2])
    if len(warnings) > 2:
        info.append(f"另有 {len(warnings) - 2} 项限制")
    if info:
        lines.append(" · ".join(info))
    return "\n".join(lines)

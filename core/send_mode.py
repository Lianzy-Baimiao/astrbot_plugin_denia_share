"""发送模式的纯逻辑：模式 → 三轴开关，节点 → 文字 / 媒体切分与选取。

**为什么单独一个模块**：这段是「发送什么」的判定核心，但不依赖 AstrBot ——
放这里就能直接写断言跑（``main.py`` 牵进 Star / 适配器一整条链，本地跑不动），
``config.py`` 与 ``main.py`` 只负责调用。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from astrbot.api import logger

# 发送模式 →（发卡片, 发文字, 发原始媒体）。形状取自 yaya 的 OUTPUT_MODE_FLAGS
# （那边是「文本 / 富媒体」两档），这里多切一刀「卡片」——因为卡片是**渲染出来的
# 一张图**，用户想「只发视频、别刷卡片」时它必须能单独关掉。
#
#   卡片 = 渲染出的分享卡片；文字 = header 与纯文本节点；
#   原始媒体 = 图集 / 图文原图节点 + 视频 + 音频。
#
# 「全部发送」与只有两档时的行为一致（卡片+文字+图片节点+视频），零回归。
_SEND_MODES: tuple[str, ...] = ("全部发送", "仅文本", "仅富媒体")
_SEND_MODE_FLAGS: dict[str, tuple[bool, bool, bool]] = {
    "全部发送": (True, True, True),
    "仅文本": (True, True, False),
    "仅富媒体": (False, False, True),
}

# 覆盖表的条目分隔符：与 permissions 的名单共用同一套（逗号 / 顿号 / 空白）
_OVERRIDE_SPLIT_RE = re.compile(r"[,\s、，]+")


def norm_mode_key(raw: Any) -> str:
    """覆盖表键归一化：去空白 + 转小写（群号数字不受影响，openid 大小写不敏感）。"""
    return str(raw or "").strip().casefold()


def is_send_mode(value: Any) -> bool:
    """是否是合法的发送模式取值。"""
    return str(value or "").strip() in _SEND_MODES


def mode_flags(mode: Any) -> tuple[bool, bool, bool]:
    """模式 → ``(发卡片, 发文字, 发原始媒体)``；未知值按「全部发送」。"""
    val = str(mode or "").strip()
    return _SEND_MODE_FLAGS.get(val, _SEND_MODE_FLAGS["全部发送"])


def parse_mode_overrides(raw: Any) -> dict[str, str]:
    """解析「按会话覆盖发送模式」的配置串。

    格式：``键=模式``，多条用逗号 / 顿号 / 空白分隔。键可以是

    * 完整 umo（``aiocqhttp:GroupMessage:972781741``）—— 最精确；
    * 群号 / 群 openid（``972781741``、``21E86DB8...``）；
    * 平台名（``qq_official``）—— 该平台下所有会话。

    由精确到宽泛取第一个命中。格式或模式值不合法的条目**逐条忽略**（各记一条
    warning），不会让整张表失效 —— 一条写错不该把其它群的配置一起废掉。
    """
    overrides: dict[str, str] = {}
    for part in _OVERRIDE_SPLIT_RE.split(str(raw or "")):
        part = part.strip()
        if not part:
            continue
        key, sep, mode = part.partition("=")
        key = norm_mode_key(key)
        mode = mode.strip()
        if not sep or not key:
            logger.warning(
                f"[denia_share] 发送模式覆盖表条目格式应为「键=模式」，已忽略: {part}"
            )
            continue
        if not is_send_mode(mode):
            logger.warning(
                f"[denia_share] 发送模式覆盖表里的模式无效，已忽略: {part}"
                f"（可选 {' / '.join(_SEND_MODES)}）"
            )
            continue
        if key in overrides and overrides[key] != mode:
            logger.warning(f"[denia_share] 发送模式覆盖表键重复，后一条生效: {key}")
        overrides[key] = mode
    return overrides


def resolve_mode_flags(
    raw_overrides: Any, global_mode: Any, *keys: Any
) -> tuple[bool, bool, bool]:
    """按会话解析生效的开关。

    Args:
        raw_overrides: 覆盖表原文（未解析）。
        global_mode: 全局默认模式。
        keys: 由精确到宽泛排列的候选键 —— 典型是
            ``(unified_msg_origin, 群号, 平台名)``，空值会被跳过。

    Returns:
        命中的第一个覆盖条目的开关；都不命中时用 ``global_mode``。
    """
    overrides = parse_mode_overrides(raw_overrides)
    if overrides:
        for key in keys:
            mode = overrides.get(norm_mode_key(key))
            if mode:
                return mode_flags(mode)
    return mode_flags(global_mode)


def split_nodes(
    nodes_content: Iterable[list], is_media: Callable[[list], bool]
) -> tuple[list[list], list[list]]:
    """把节点条目切成 ``(文字条目, 媒体条目)``。

    ``is_media`` 判断单个条目是否算媒体（由调用方给，避免这里依赖消息组件类型）。
    """
    text_items: list[list] = []
    media_items: list[list] = []
    for item in nodes_content:
        (media_items if is_media(item) else text_items).append(item)
    return text_items, media_items


def pick_items(
    nodes_content: list[list],
    text_items: list[list],
    media_items: list[list],
    *,
    keep_text: bool,
    keep_media: bool,
) -> list[list]:
    """两种条目都留时**按原顺序**整串发，否则只发留下的那一类。

    这条规则是「全部发送」零回归的关键：切分再拼接会把 graphics 里的纯文本条目
    挪到图片之前，直接返回原列表则与改动前逐条一致。
    """
    if keep_text and keep_media:
        return list(nodes_content)
    if keep_text:
        return text_items
    if keep_media:
        return media_items
    return []

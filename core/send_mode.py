"""发送模式的纯逻辑：模式 → 三轴开关，节点 → 文字 / 媒体切分与选取。

**为什么单独一个模块**：这段是「发送什么」的判定核心，但不依赖 AstrBot ——
放这里就能直接写断言跑（``main.py`` 牵进 Star / 适配器一整条链，本地跑不动），
``config.py`` 与 ``main.py`` 只负责调用。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from astrbot.api import logger

# 三项可独立勾选；富媒体指原始图片 / 视频 / 音频，不包含渲染卡片。
SEND_OPTIONS: tuple[str, ...] = ("文本", "卡片", "富媒体")
_LEGACY_SELECTIONS: dict[str, tuple[str, ...]] = {
    "全部发送": SEND_OPTIONS,
    "文本+富媒体": ("文本", "富媒体"),
    "仅文本": ("文本",),
    "仅富媒体": ("富媒体",),
    "不发送": (),
}

# 覆盖表的条目分隔符：与 permissions 的名单共用同一套（逗号 / 顿号 / 空白）
_OVERRIDE_SPLIT_RE = re.compile(r"[,\s、，]+")


def norm_mode_key(raw: Any) -> str:
    """覆盖表键归一化：去空白 + 转小写（群号数字不受影响，openid 大小写不敏感）。"""
    return str(raw or "").strip().casefold()


def _mode_selection(value: Any) -> list[str] | None:
    """解析多选列表或旧版 / 覆盖表写法。None 表示无效，[] 表示明确不发送。"""
    if isinstance(value, str):
        text = value.strip()
        if text in _LEGACY_SELECTIONS:
            return list(_LEGACY_SELECTIONS[text])
        value = [part.strip() for part in text.split("+")]
    if not isinstance(value, (list, tuple)):
        return None
    if any(not isinstance(part, str) or part not in SEND_OPTIONS for part in value):
        return None
    return [option for option in SEND_OPTIONS if option in value]


def is_send_mode(value: Any) -> bool:
    """是否是合法的发送选项列表或兼容写法（空列表也合法）。"""
    return _mode_selection(value) is not None


def normalize_mode(value: Any) -> list[str]:
    """统一为按界面顺序排列的独立列表；未知值保持旧版「全部发送」回退。"""
    selection = _mode_selection(value)
    return list(SEND_OPTIONS) if selection is None else selection


def mode_flags(mode: Any) -> tuple[bool, bool, bool]:
    """选项 → ``(发卡片, 发文字, 发原始媒体)``，空选项不发送任何产物。"""
    selected = normalize_mode(mode)
    return "卡片" in selected, "文本" in selected, "富媒体" in selected


def parse_mode_overrides(raw: Any) -> dict[str, str]:
    """解析「按会话覆盖发送模式」的配置串。

    格式：``键=文本+卡片``（兼容旧模式名），``键=不发送`` 可全部关闭，多条用逗号 / 顿号 / 空白分隔。键可以是

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
                "（可用 文本 / 卡片 / 富媒体，以 + 组合；不发送=全部关闭；兼容旧模式名）"
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

"""访问控制：白名单 / 黑名单。

判定顺序照搬 yaya（作者另一个插件）的模型：

    管理员 > 个人白名单 > 个人黑名单 > 群组白名单 > 群组黑名单

五种情况都不命中时：**白名单启用 → 拒绝**（白名单的语义是「只放行名单里的」），
否则放行。所以「启用白名单但名单为空」= 除管理员外谁都不能用。

管理员由 AstrBot 判定（``event.is_admin()``），这里只收结果 —— 与
``/denia_status`` 等命令的 ADMIN 门保持一致，不再单独配一个管理员 ID。
AstrBot 的「管理员」= 全局 ``admins_id`` 名单，与平台无关：NapCat 那边填 QQ 号、
QQ 官方 Bot 那边填**用户 openid**，填了在两边都是管理员。

``BLACKLIST_ADMIN_EXEMPT``（默认开）单独控制管理员**是否豁免黑名单**：关掉后
黑名单对管理员同样生效（白名单的「只放行名单内」仍然不适用于管理员 —— 那一条
管的是「谁能用」，而管理员一直能用）。

名单在配置里是**逗号分隔的字符串**（不是列表）：本插件的配置是扁平键值，
``_conf_schema.json`` 只支持 bool / int / select / string / text，
``DISABLED_PLATFORMS`` 已经是这个套路。

**ID 一律按大小写不敏感比对**（见 :func:`_norm_id`）：NapCat 的群号是纯数字，
而 QQ 官方 Bot 的群 / 用户 ID 是 32 位**大写**十六进制串（如
``21E86DB833C12870E3287DF3C5704B5F``）—— 从日志里复制时很容易带上小写，
写死大小写会让名单「填了却不生效」且完全看不出来。
"""

from __future__ import annotations

import re
from typing import Any

# 分隔符同时收中文逗号与顿号：从聊天里复制群号时经常带上
_ID_SPLIT_RE = re.compile(r"[,\s、，]+")


def _norm_id(raw: Any) -> str:
    """ID 归一化：去空白 + 转大写（大小写不敏感比对用）。

    QQ 官方 Bot 的 openid 是大写十六进制，NapCat 是纯数字 —— 两者统一转大写
    都能正确比对；数字串不受影响。
    """
    return str(raw or "").strip().upper()


def parse_id_list(raw: Any) -> frozenset[str]:
    """把逗号 / 空白分隔的 ID 串解析成集合（已归一化）。"""
    return frozenset(
        token for part in _ID_SPLIT_RE.split(str(raw or "")) if (token := _norm_id(part))
    )


def is_allowed(
    cfg: Any,
    *,
    is_admin: bool,
    is_private: bool,
    sender_id: Any,
    group_id: Any,
) -> bool:
    """这次请求该不该放行。

    Args:
        cfg: 配置对象，读 ``WHITELIST_*`` / ``BLACKLIST_*`` 与
            ``BLACKLIST_ADMIN_EXEMPT``。
        is_admin: 是否管理员。默认无条件放行；``BLACKLIST_ADMIN_EXEMPT`` 关掉后
            仍受**黑名单**约束。
        is_private: 是否私聊。私聊没有群号，**群组名单对它不生效** ——
            所以只配了群组白名单时，私聊会被拒。
        sender_id: 发送者 ID。
        group_id: 群号（私聊时会被忽略）。

    Returns:
        True 放行，False 拦下。
    """
    whitelist_on = bool(cfg.WHITELIST_ENABLE)
    blacklist_on = bool(cfg.BLACKLIST_ENABLE)
    # 老配置里没有这一项时按「豁免」处理，等于保持旧行为
    admin_exempt = bool(getattr(cfg, "BLACKLIST_ADMIN_EXEMPT", True))

    if is_admin and not (blacklist_on and not admin_exempt):
        return True

    sender = _norm_id(sender_id)
    group = "" if is_private else _norm_id(group_id)

    # 两个开关都关着是常态：直接放行，不做任何解析
    if not whitelist_on and not blacklist_on:
        return True

    if whitelist_on and sender in parse_id_list(cfg.WHITELIST_USER):
        return True
    if blacklist_on and sender in parse_id_list(cfg.BLACKLIST_USER):
        return False
    if whitelist_on and group and group in parse_id_list(cfg.WHITELIST_GROUP):
        return True
    if blacklist_on and group and group in parse_id_list(cfg.BLACKLIST_GROUP):
        return False

    # 都没命中：白名单启用时默认拒绝（它是「只放行名单里的」），否则放行。
    # 管理员不受这条约束：走到这里说明他被放行过黑名单，而白名单管的是
    # 「谁能用」，管理员一直能用。
    return is_admin or not whitelist_on


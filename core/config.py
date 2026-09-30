"""配置管理模块。

配置项的**唯一来源**是 :data:`CONFIG_META`（分组 + 键 + 类型 + 默认值 + 文案）。
插件的 WebUI 页面直接从它渲染表单；``_conf_schema.json`` 是同一次定义的**存储契约**
（AstrBot 加载插件配置时会剔除 schema 之外的键，两者必须保持一致，
由 :func:`verify_schema_alignment` 在启动时自检并告警）。

相比 rika 原版移除了 Cloudflare 截图 Fallback（20+ 配置项）与 B站 Cookie 监控轮询。
相比娅娅版移除了 LLM 文本翻译（10 语言 × 10 厂商接口）与「视频仅发送封面」。

明确**不包含**的能力（有意不做，勿再引入）：
- LLM 文本翻译
- 视频仅发送封面 / 跳过视频本体
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .send_mode import (
    is_send_mode,
    mode_flags,
    parse_mode_overrides,
    resolve_mode_flags,
)

_config = None

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 发送模式的三轴开关与「按会话覆盖」的解析都在 core/send_mode.py（纯逻辑、可单测），
# 这里只负责把它们接到配置上。


# 配置分组：展示顺序即此处的顺序，常用在前、折腾在后
CONFIG_GROUPS: tuple[tuple[str, str], ...] = (
    ("解析设置", "日常最常用到的几项"),
    ("权限控制", "谁能触发解析：白名单 / 黑名单"),
    ("B站设置", "B站 Cookie 与下载清晰度"),
    ("Steam 设置", "价格地区与史低数据源"),
    ("网页截图", "把任意网页截成图片发送"),
    ("卡片外观", "把解析结果渲染成分享卡片图片"),
    ("媒体发送", "视频怎么送到消息平台：共享目录或中转链接"),
    ("高级设置", "一般用不到，按需开启"),
    ("维护", "缓存与调试，改动后立即生效"),
)

# 大类（一级分组）：把 CONFIG_GROUPS 按使用场景再归并，WebUI 配置页按
# 「大类 → 分组 → （可选）子组 → 配置项」三级展示。仅影响展示层，不参与存储契约
# （_conf_schema.json 仍按「分组」生成，键与默认值都不变，无需重新生成）。
CONFIG_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "key": "basic",
        "label": "基础",
        "description": "解析行为、发送策略与访问权限，日常最常改",
        "groups": ("解析设置", "权限控制"),
    },
    {
        "key": "platform",
        "label": "平台接入",
        "description": "各平台的凭据、清晰度与第三方数据源",
        "groups": ("B站设置", "Steam 设置", "网页截图"),
    },
    {
        "key": "output",
        "label": "输出与投递",
        "description": "卡片怎么渲染、视频怎么送到消息平台",
        "groups": ("卡片外观", "媒体发送"),
    },
    {
        "key": "advanced",
        "label": "高级与维护",
        "description": "凭据、网络与缓存调试，一般用不到",
        "groups": ("高级设置", "维护"),
    },
)

# 配置项元数据。字段说明：
#   key/group/label/type/default/hint 必填；type 取值 string | text | int | bool | select
#   secret=True 表示前端以密码框展示（仅遮罩显示，配置文件里仍是明文，与原生面板一致）
#   options/labels 仅 select 使用；min/max/unit 仅 int 使用；placeholder 仅输入框使用
#   list=True 表示「逗号分隔字符串」类型的列表项，配置页渲染成标签编辑器
#   而不是单行输入框（存储格式不变，纯展示层增强）；
#   list_source="platforms" 时进一步渲染成平台勾选网格（选项取自 config 接口
#   返回的 platforms 列表），目前仅 DISABLED_PLATFORMS 使用
#   subgroup 可选：组内第三级小标题（配置项多的分组按它再分段，如「卡片外观」
#   按 基础/内容元素/配色 拆分）；不带 subgroup 的项排在同组最前
CONFIG_META: tuple[dict[str, Any], ...] = (
    # ---------------- 解析设置 ---------------- #
    {
        "key": "DISABLED_PLATFORMS",
        "group": "解析设置",
        "label": "禁用的平台",
        "type": "string",
        "default": "",
        "list": True,
        "list_source": "platforms",
        "hint": "勾选=禁用该平台，都不勾=全部启用。「总览」页的平台开关改的是同一份（那边点一下立即生效）",
    },
    {
        "key": "VIDEO_DURATION_MAXIMUM",
        "group": "解析设置",
        "label": "视频最大时长",
        "type": "int",
        "default": 480,
        "min": 0,
        "max": 86400,
        "unit": "秒",
        "hint": "超过此时长不下载视频，但仍会返回标题、作者和封面",
    },
    {
        "key": "VIDEO_SIZE_MAXIMUM_MB",
        "group": "解析设置",
        "label": "单个视频体积上限",
        "type": "int",
        "default": 100,
        "min": 1,
        "max": 4096,
        "unit": "MB",
        "hint": "下载前按 Content-Length 判断，边下边判。QQ 大文件上传容易失败，建议不超过 60",
    },
    {
        "key": "SEND_ERROR_MESSAGES",
        "group": "解析设置",
        "label": "解析失败时发送错误提示",
        "type": "bool",
        "default": False,
        "hint": "关闭则只写日志、不在群里刷报错。排查问题时可临时打开",
    },
    # ---------------- 权限控制 ---------------- #
    # 名单是**逗号分隔的字符串**，不是列表：本插件的配置是扁平键值，
    # _conf_schema.json 只支持 bool / int / select / string / text，
    # DISABLED_PLATFORMS 已经是这个套路。判定顺序见 core/permissions.py。
    # list=True 只影响 WebUI 的呈现（标签编辑器，回车添加、点 × 删除），
    # 存储格式不变。
    {
        "key": "WHITELIST_ENABLE",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "启用白名单",
        "type": "bool",
        "default": False,
        "hint": "开启后**只有**名单里的用户 / 群能触发解析，管理员始终豁免。名单全空 = 除管理员外谁都不能用",
    },
    {
        "key": "WHITELIST_USER",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "白名单用户",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "用户 ID，回车添加",
        "hint": "留空 = 不按用户放行（私聊也会因此被拒）。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "WHITELIST_GROUP",
        "group": "权限控制",
        "subgroup": "白名单",
        "label": "白名单群组",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "群号，回车添加",
        "hint": "私聊没有群号，不受这一项影响。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "BLACKLIST_ENABLE",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "启用黑名单",
        "type": "bool",
        "default": False,
        "hint": "开启后名单里的用户 / 群不能触发解析，管理员默认仍豁免（见下一项）。与白名单同时开启时先看白名单、再看黑名单",
    },
    {
        "key": "BLACKLIST_ADMIN_EXEMPT",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "管理员豁免黑名单",
        "type": "bool",
        "default": True,
        "hint": (
            "开启（默认）时管理员不受黑名单影响；**关掉后黑名单对管理员同样生效** —— "
            "想让某个群连管理员都不响应，就把群号填进「黑名单群组」并把这里关掉。"
            "这一项只影响黑名单：管理员一直不受白名单限制"
        ),
    },
    {
        "key": "BLACKLIST_USER",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "黑名单用户",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "用户 ID，回车添加",
        "hint": "名单里的用户不能触发解析。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    {
        "key": "BLACKLIST_GROUP",
        "group": "权限控制",
        "subgroup": "黑名单",
        "label": "黑名单群组",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "群号，回车添加",
        "hint": "私聊没有群号，不受这一项影响。支持整串粘贴，逗号 / 空格都能自动拆开",
    },
    # ---------------- B站设置 ---------------- #
    {
        "key": "BILI_CK",
        "group": "B站设置",
        "label": "哔哩哔哩 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "推荐用「总览」页的扫码登录自动写入；配了才能下 1080P 及以上",
    },
    {
        "key": "BILI_QUALITY",
        "group": "B站设置",
        "label": "B站下载清晰度",
        "type": "select",
        "default": "1080P",
        "options": ["360P", "480P", "720P", "1080P", "1080P+", "4K", "8K"],
        "hint": "高画质需要对应账号权限；未登录时实际只能拿到 720P",
    },
    {
        "key": "BILI_CODEC",
        "group": "B站设置",
        "label": "B站视频编码",
        "type": "select",
        "default": "H264兼容",
        "options": ["H264兼容", "全部编码"],
        "hint": (
            "H264兼容：只取 H.264 流，电脑版 QQ 才能正常播放（默认）。"
            "全部编码：额外允许 HEVC/AV1，可以拿到 4K/8K，但电脑版 QQ 会花屏或放不出来"
        ),
    },
    # ---------------- Steam 设置 ---------------- #
    {
        "key": "STEAM_REGION",
        "group": "Steam 设置",
        "label": "价格地区代码",
        "type": "string",
        "default": "cn",
        "placeholder": "cn",
        "hint": "决定官方价格的货币（cn/us/jp 等），同时用作 ITAD 的 country 参数",
    },
    {
        "key": "ITAD_API_KEY",
        "group": "Steam 设置",
        "label": "IsThereAnyDeal API Key",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "只有想看「历史最低价」才需要，免费申请 https://isthereanydeal.com/apps 。留空时 Steam 解析照常工作，只是不显示史低",
    },
    # ---------------- 网页截图 ---------------- #
    {
        "key": "SCREENSHOT_BACKEND",
        "group": "网页截图",
        "subgroup": "通用",
        "label": "截图后端",
        "type": "select",
        "default": "thum",
        "options": ["thum", "cloudflare"],
        "labels": ["thum（免 key）", "cloudflare（需账号）"],
        "hint": "thum=image.thum.io，900×900 视窗图开箱即用；cloudflare 可截长图/等待 JS，但必须有账号",
    },
    {
        "key": "SCREENSHOT_FALLBACK",
        "group": "网页截图",
        "subgroup": "通用",
        "label": "匹配不到平台的链接自动截图",
        "type": "bool",
        "default": False,
        "hint": "默认关闭，避免群里刷图。开启后任何解析不了的 http 链接都会尝试截图",
    },
    {
        "key": "CF_ACCOUNT_ID",
        "group": "网页截图",
        "subgroup": "Cloudflare 后端",
        "label": "Cloudflare Account ID",
        "type": "string",
        "default": "",
        "hint": "仅 cloudflare 后端需要，在 CF 控制台右侧栏可复制",
    },
    {
        "key": "CF_API_TOKEN",
        "group": "网页截图",
        "subgroup": "Cloudflare 后端",
        "label": "Cloudflare API Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "仅 cloudflare 后端需要，需带 Browser Rendering 写权限",
    },
    # ---------------- 卡片外观 ---------------- #
    {
        "key": "RENDER_ENABLED",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "启用卡片渲染",
        "type": "bool",
        "default": True,
        "hint": "关闭则回退为纯文本输出",
    },
    {
        "key": "RENDER_THEME",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片主题",
        "type": "select",
        "default": "dark",
        "options": ["dark", "light"],
        "labels": ["深色", "浅色"],
        "hint": "只影响发送出去的卡片图片，与网页界面主题无关",
    },
    {
        "key": "RENDER_LAYOUT",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片布局",
        "type": "select",
        "default": "standard",
        "options": ["standard", "magazine", "immersive", "feed"],
        "labels": ["standard 标准", "magazine 杂志", "immersive 沉浸", "feed 信息流"],
        "hint": "切换后可在「解析」页立刻预览效果",
    },
    {
        "key": "RENDER_WIDTH",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "卡片宽度",
        "type": "int",
        "default": 800,
        "min": 520,
        "max": 1080,
        "unit": "px",
        "hint": "520~1080，越宽信息越舒展，但在聊天软件里显示会更小",
    },
    {
        "key": "RENDER_COVER_FULL_SIZE",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "封面按原始尺寸展示",
        "type": "bool",
        "default": False,
        "hint": "开启后不裁切封面，长图会完整铺在卡片顶部",
    },
    {
        "key": "RENDER_FONT_PATH",
        "group": "卡片外观",
        "subgroup": "基础",
        "label": "自定义字体文件",
        "type": "string",
        "default": "",
        "hint": "留空自动探测系统字体；卡片出现方块字时才需要指定绝对路径",
    },
    {
        "key": "RENDER_ACCENT_COLOR",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "自定义强调色",
        "type": "string",
        "default": "",
        "placeholder": "#FB7299",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=每个平台用自己的品牌色。填 #RRGGBB 后所有卡片统一用这个强调色（徽章/光晕/水印圆点）",
    },
    {
        "key": "RENDER_WATERMARK",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "卡片水印文字",
        "type": "string",
        "default": "希望解析",
        "max_length": 12,
        "hint": "卡片右下角的水印文字，留空则不显示水印，最长 12 个字符",
    },
    {
        "key": "RENDER_DESC_MAX_LINES",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "正文最大行数",
        "type": "int",
        "default": 0,
        "min": 0,
        "max": 12,
        "unit": "行",
        "hint": "0=按布局默认（4~6 行）。简介超长时按此行数截断，卡片更紧凑",
    },
    {
        "key": "RENDER_SHOW_AVATAR",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "显示作者头像",
        "type": "bool",
        "default": True,
        "hint": "关闭后作者行只保留昵称与签名，不绘制头像圆（头像也不会再被加载）",
    },
    {
        "key": "RENDER_SHOW_PLAY_BUTTON",
        "group": "卡片外观",
        "subgroup": "内容元素",
        "label": "封面播放按钮",
        "type": "bool",
        "default": True,
        "hint": "视频封面中央的毛玻璃播放按钮。关闭后封面更干净（yaya 那边默认是关的，这里默认开以保持现有观感）",
    },
    {
        "key": "RENDER_GRADIENT_TOP",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "背景渐变·顶部色",
        "type": "string",
        "default": "",
        "placeholder": "#242B3F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的顶部颜色",
    },
    {
        "key": "RENDER_GRADIENT_BOTTOM",
        "group": "卡片外观",
        "subgroup": "配色",
        "label": "背景渐变·底部色",
        "type": "string",
        "default": "",
        "placeholder": "#12161F",
        "pattern": "^#[0-9a-fA-F]{6}$",
        "hint": "留空=跟随卡片主题。填 #RRGGBB 覆盖卡片背景渐变的底部颜色",
    },
    # ---------------- 媒体发送 ---------------- #
    {
        "key": "SEND_MODE",
        "group": "媒体发送",
        "label": "发送模式",
        "type": "select",
        "default": "全部发送",
        "options": ["全部发送", "仅文本", "仅富媒体"],
        "hint": (
            "全部发送=卡片 + 文字 + 图片/视频都发；仅文本=卡片 + 文字，不发图片/视频；"
            "仅富媒体=只发图片/视频原图，不发卡片也不发文字。"
            "媒体在解析阶段就已下载，这两档省的是「发送」而不是带宽。"
            "想只对某个群 / 某个平台生效，用下面的「按会话覆盖发送模式」"
        ),
    },
    {
        "key": "SEND_MODE_OVERRIDES",
        "group": "媒体发送",
        "label": "按会话覆盖发送模式",
        "type": "string",
        "default": "",
        "list": True,
        "placeholder": "群号=仅富媒体，回车添加",
        "hint": (
            "给单个群 / 平台指定发送模式，形如 `972781741=仅富媒体`、"
            "`21E86DB833C12870E3287DF3C5704B5F=仅文本`、`qq_official=仅文本`。"
            "键可以填群号、官方 Bot 群 openid、平台名（aiocqhttp / qq_official）或完整 umo；"
            "优先级 umo > 群号 > 平台名 > 上面的全局默认。留空 = 全部走全局默认"
        ),
    },
    {
        "key": "CACHE_DIR",
        "group": "媒体发送",
        "subgroup": "方式一 · 本地共享目录",
        "label": "共享缓存目录",
        "type": "string",
        "default": "",
        "placeholder": "/app/sharedFolder/denia_share/cache",
        "hint": (
            "① 本地文件方式：填一个「所有容器都把宿主机同一目录挂到它上面」的容器内路径，"
            "协议端（NapCat 等）就能直接读走视频。留空=用插件数据目录 —— 分容器部署时协议端读不到，"
            "视频会发不出去（图片语音不受影响，它们会转成 base64）。"
            "安全限制：清理缓存会递归删除该目录，所以根目录、把插件数据目录包在里面的上级目录，"
            "以及「已有内容且没有 .denia_share_cache 哨兵文件」的目录都会被拒绝并回退到插件数据目录"
        ),
    },
    {
        "key": "MEDIA_RELAY_ENABLED",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "启用媒体中转",
        "type": "bool",
        "default": False,
        "hint": (
            "② 链接方式：把已下载的视频注册成 AstrBot 的临时 HTTP 链接（/api/file/<token>）再发送。"
            "协议端只要能访问到下面的回调地址即可，不需要共享挂载；注册失败会自动回退本地文件"
        ),
    },
    {
        "key": "MEDIA_RELAY_CALLBACK_URL",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "AstrBot 回调地址",
        "type": "string",
        "default": "",
        "placeholder": "http://astrbot:6185",
        "hint": (
            "留空时回退 AstrBot 全局 callback_api_base。必须填「消息平台所在容器能访问到」的地址，"
            "同一 Docker 网络内可直接用容器名，如 http://astrbot:6185。"
            "QQ 官方 Bot 场景下该地址必须**公网可达**（由 QQ 服务器来拉取），"
            "且官方 Bot 默认不用中转，见下面的「官方 Bot 也走中转链接」"
        ),
    },
    {
        "key": "MEDIA_RELAY_TTL",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "中转链接有效期",
        "type": "int",
        "default": 300,
        "min": 30,
        "max": 86400,
        "unit": "秒",
        "hint": "到期后链接失效。视频越大、链路越慢越要留足时间，建议不低于 120 秒",
    },
    {
        "key": "OFFICIAL_RELAY_FIRST",
        "group": "媒体发送",
        "subgroup": "方式二 · 中转链接",
        "label": "官方 Bot 视频优先用中转链接（不推荐）",
        "type": "bool",
        "default": False,
        "hint": (
            "默认走**本地上传**（>10MB 或补位后走分片上传，单请求 300 秒）。"
            "打开这一项会让官方 Bot 的视频先注册成链接、由 QQ 服务器来拉 —— 但实测走不通："
            "AstrBot 的文件 Token 是**一次性**的（file_token_service 取一次就 pop），"
            "QQ 那边只要有一次没拉完（或先发了 HEAD），框架的后续重试全部 404，"
            "整条链接路径必然失败。仅当你的机器上行极快、文件很小，才值得一试"
        ),
    },
    {
        "key": "OFFICIAL_CHUNK_PAD_MIN_MB",
        "group": "媒体发送",
        "label": "官方 Bot 分片上传起点（MB）",
        "type": "int",
        "default": 4,
        "min": 0,
        "max": 200,
        "unit": "MB",
        "hint": (
            "默认 4：官方 Bot 上 4MB 以上（且不超过 10MB）的视频会被补到略超 10MB，"
            "逼框架走**分片上传**（每片 5MB、4 路并发、单请求 300 秒）—— 因为不超过 10MB 是"
            "单次 base64、客户端超时只有 20 秒，上行稍慢就「上传文件API返回None」重试 5 次后失败。"
            "填 0 = 关闭（上行很快、想省流量时）；也可以填 6～8 只对更大的视频补位。"
            "另外：一旦本机被观察到单发失败一次，之后 ≤10MB 的视频会自动走分片，无需再设这一项"
        ),
    },
    # ---------------- 高级设置 ---------------- #
    {
        "key": "XHS_CK",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "小红书 Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "部分笔记需要登录态才能完整解析，从浏览器复制 xiaohongshu.com 的 cookie",
    },
    {
        "key": "PIXIV_CK",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "Pixiv Cookie",
        "type": "text",
        "default": "",
        "secret": True,
        "hint": "留空也能搜索。填了收录更全，但搜索结果会出现 R18 —— 插件对非全年龄内容有硬性过滤，不会放宽",
    },
    {
        "key": "GITHUB_TOKEN",
        "group": "高级设置",
        "subgroup": "平台凭据",
        "label": "GitHub Token",
        "type": "string",
        "default": "",
        "secret": True,
        "hint": "强烈建议填。免 token 限额 60 次/小时且按出口 IP 计算，共享 IP 下极易被限流；填后 5000 次/小时",
    },
    {
        "key": "PROXY",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "全局代理",
        "type": "string",
        "default": "",
        "placeholder": "http://127.0.0.1:7897",
        "hint": "留空不使用。作用于下载与自建请求（GitHub/Pixiv/截图）。填运行 AstrBot 那台机器上的地址",
    },
    {
        "key": "HTTP_VERIFY_SSL",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "校验 HTTPS 证书",
        "type": "bool",
        "default": True,
        "hint": (
            "默认开启。下载与自建请求都会校验服务端证书，防止中间人截获带 Cookie 的请求。"
            "只有遇到证书链有问题的站点（自建反代、内网镜像）导致解析失败时，才临时关掉"
        ),
    },
    {
        "key": "TWITTER_MEDIA_PROXY_BASE",
        "group": "高级设置",
        "subgroup": "网络",
        "label": "Twitter 图片/视频反代根地址",
        "type": "string",
        "default": "",
        "placeholder": "https://your-proxy.example",
        "hint": "留空=不启用。服务器连不上 twimg 时填写",
    },
    # ---------------- 维护 ---------------- #
    {
        "key": "CACHE_TTL_HOURS",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "缓存保留时长",
        "type": "int",
        "default": 24,
        "min": 0,
        "max": 8760,
        "unit": "小时",
        "hint": "超过此时间未使用的缓存会被自动清理，设为 0 禁用自动清理",
    },
    {
        "key": "CACHE_CLEANUP_INTERVAL_MINUTES",
        "group": "维护",
        "subgroup": "缓存清理",
        "label": "缓存清理间隔",
        "type": "int",
        "default": 60,
        "min": 1,
        "max": 1440,
        "unit": "分钟",
        "hint": "后台清理任务多久跑一次，改动需重载插件后才生效",
    },
    {
        "key": "DEBUG_LOG_ENABLED",
        "group": "维护",
        "subgroup": "日志",
        "label": "输出调试日志",
        "type": "bool",
        "default": True,
        "hint": "关闭后只保留警告与错误日志，排查问题时可打开",
    },
)

_ITEM_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in CONFIG_META}

CONFIG_GROUP_KEYS: dict[str, tuple[str, ...]] = {
    group: tuple(item["key"] for item in CONFIG_META if item["group"] == group)
    for group, _ in CONFIG_GROUPS
}

_KEY_GROUP_MAP: dict[str, str] = {
    key: group for group, keys in CONFIG_GROUP_KEYS.items() for key in keys
}

_DEFAULTS: dict[str, Any] = {item["key"]: item["default"] for item in CONFIG_META}


def config_meta_payload() -> dict[str, Any]:
    """返回供 WebUI 渲染的配置元数据（大类 + 分组 + 全部配置项定义）。

    ``groups`` 保持扁平结构不变（向后兼容，旧前端/脚本直接读它）；
    ``sections`` 是大类视角的导航：配置页按「大类 → 分组 → 子组 → 配置项」
    渲染，子组来自配置项的可选 ``subgroup`` 字段。
    """
    return {
        "sections": [
            {
                "key": section["key"],
                "label": section["label"],
                "description": section["description"],
                # 只带真实存在的分组：CONFIG_SECTIONS 写错组名时这里静默收窄，
                # 由 section_coverage_problems() 在自检里把问题报出来
                "groups": [
                    name for name in section["groups"] if name in CONFIG_GROUP_KEYS
                ],
            }
            for section in CONFIG_SECTIONS
        ],
        "groups": [
            {
                "name": name,
                "description": description,
                "keys": list(CONFIG_GROUP_KEYS.get(name, ())),
            }
            for name, description in CONFIG_GROUPS
        ],
        "items": [dict(item) for item in CONFIG_META],
    }


def section_coverage_problems() -> list[str]:
    """校验 CONFIG_SECTIONS 对 CONFIG_GROUPS 的覆盖，返回问题列表（空 = 正常）。

    每个分组必须恰好归属一个大类；引用不存在的分组、一个分组进两个大类、
    分组没有归属，都会报出来。不在 import 时断言 —— 配置模块在 stub 环境里
    也要能 import，覆盖问题由自检（test/webui/selfcheck.py）钉住。
    """
    problems: list[str] = []
    known = {name for name, _ in CONFIG_GROUPS}
    seen: dict[str, str] = {}
    for section in CONFIG_SECTIONS:
        for group in section["groups"]:
            if group not in known:
                problems.append(
                    f"大类「{section['label']}」引用了不存在的分组「{group}」"
                )
            elif group in seen:
                problems.append(
                    f"分组「{group}」同时属于「{seen[group]}」和「{section['label']}」"
                )
            else:
                seen[group] = section["label"]
    for name, _ in CONFIG_GROUPS:
        if name not in seen:
            problems.append(f"分组「{name}」没有归属任何大类")
    return problems


def coerce_value(item: dict[str, Any], raw: Any) -> tuple[Any, str | None]:
    """把前端传来的值转成配置要求的类型。

    Returns:
        (转换后的值, 错误描述)。错误描述非 None 时调用方应丢弃该值。
    """
    key = item["key"]
    label = item.get("label", key)
    kind = item["type"]

    if kind == "bool":
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True, None
            if lowered in {"false", "0", "no", "off", ""}:
                return False, None
        return None, f"{label}：需要是开关值"

    if kind == "int":
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None, f"{label}：需要是整数"
        low, high = item.get("min"), item.get("max")
        if low is not None and value < low:
            return None, f"{label}：不能小于 {low}"
        if high is not None and value > high:
            return None, f"{label}：不能大于 {high}"
        return value, None

    text = "" if raw is None else str(raw).strip()
    if kind == "select":
        options = item.get("options") or []
        if text and text not in options:
            return None, f"{label}：只能是 {' / '.join(options)} 之一"
    if kind == "text" and len(text) > 8000:
        return None, f"{label}：内容过长（超过 8000 字）"
    max_length = item.get("max_length")
    if max_length is not None and len(text) > int(max_length):
        return None, f"{label}：不能超过 {max_length} 个字符"
    pattern = item.get("pattern")
    if pattern and text:
        import re

        if not re.match(pattern, text):
            return None, f"{label}：格式不正确（需要 {pattern}）"
    return text, None


def verify_schema_alignment(schema: dict[str, Any]) -> list[str]:
    """自检 ``_conf_schema.json`` 与 :data:`CONFIG_META` 的键集合是否一致。

    AstrBot 加载插件配置时会剔除 schema 中不存在的键（``check_config_integrity``），
    两者一旦不一致，用户保存的值会在下次重载时静默丢失，因此必须显式比对。

    Returns:
        描述差异的字符串列表，空列表表示一致。
    """
    schema_keys: set[str] = set()
    schema_group_of: dict[str, str] = {}
    problems: list[str] = []

    for group, node in schema.items():
        if not isinstance(node, dict):
            problems.append(f"schema 分组 {group} 不是对象")
            continue
        items = node.get("items")
        if not isinstance(items, dict):
            problems.append(f"schema 分组 {group} 缺少 items")
            continue
        for key in items:
            schema_keys.add(key)
            schema_group_of[key] = group

    meta_keys = set(_ITEM_BY_KEY)
    for key in sorted(schema_keys - meta_keys):
        problems.append(f"schema 有而 CONFIG_META 缺失的键：{key}")
    for key in sorted(meta_keys - schema_keys):
        problems.append(f"CONFIG_META 有而 schema 缺失的键：{key}")
    for key in sorted(schema_keys & meta_keys):
        if schema_group_of[key] != _KEY_GROUP_MAP.get(key):
            problems.append(
                f"{key} 在 schema 里属于 {schema_group_of[key]} 组，"
                f"但 CONFIG_META 归到 {_KEY_GROUP_MAP.get(key)} 组"
            )
    for group in schema:
        if group not in CONFIG_GROUP_KEYS:
            problems.append(f"schema 里存在 CONFIG_GROUPS 未声明的分组：{group}")
    return problems


def migrate_grouped_config(config: Any) -> bool:
    """旧版扁平配置 → 分组配置，幂等。"""
    changed = False
    for group, keys in CONFIG_GROUP_KEYS.items():
        group_cfg = config.get(group)
        if not isinstance(group_cfg, dict):
            group_cfg = {}
            config[group] = group_cfg
        for key in keys:
            default = _DEFAULTS.get(key)
            flat_val = config.get(key, default)
            nested_val = group_cfg.get(key, default)
            if nested_val == default and flat_val != default:
                group_cfg[key] = flat_val
                config[key] = default
                changed = True
    return changed


class ParserConfig:
    """解析器配置 - 由 main.py 初始化"""

    def __init__(self, astrbot_config: Any, cache_dir: Path, config_dir: Path):
        self._cfg = astrbot_config
        self.cache_dir = cache_dir
        self.config_dir = config_dir

    def _cfg_get(self, key: str, default: Any = None) -> Any:
        """优先读分组配置，回退到扁平旧配置。"""
        group = _KEY_GROUP_MAP.get(key)
        if group is not None:
            group_cfg = self._cfg.get(group)
            if isinstance(group_cfg, dict) and key in group_cfg:
                default_val = _DEFAULTS.get(key, default)
                flat_val = self._cfg.get(key, default_val)
                nested_val = group_cfg[key]
                if nested_val == default_val and flat_val != default_val:
                    return flat_val
                return nested_val
        return self._cfg.get(key, default)

    # ---------------- WebUI 读写 ---------------- #

    def current_values(self) -> dict[str, Any]:
        """返回所有配置项的当前生效值（键 → 值）。"""
        return {
            item["key"]: self._cfg_get(item["key"], item["default"])
            for item in CONFIG_META
        }

    def apply_updates(self, payload: Any) -> tuple[list[str], list[str]]:
        """按白名单把 WebUI 提交的值写回配置。

        只接受 :data:`CONFIG_META` 里声明的键；写入分组位置的同时把扁平旧键重置为
        默认值，避免遗留的旧值把新值盖掉（与 :func:`migrate_grouped_config` 同源）。

        Args:
            payload: 前端提交的 ``{配置键: 新值}``。

        Returns:
            (实际变更的键列表, 错误信息列表)。
        """
        if not isinstance(payload, dict):
            return [], ["提交内容不是对象"]

        changed: list[str] = []
        errors: list[str] = []

        for key, raw in payload.items():
            item = _ITEM_BY_KEY.get(key)
            if item is None:
                errors.append(f"未知配置项：{key}")
                continue
            value, error = coerce_value(item, raw)
            if error is not None:
                errors.append(error)
                continue
            if self._cfg_get(key, item["default"]) == value:
                continue
            group_cfg = self._cfg.get(_KEY_GROUP_MAP[key])
            if not isinstance(group_cfg, dict):
                group_cfg = {}
                self._cfg[_KEY_GROUP_MAP[key]] = group_cfg
            group_cfg[key] = value
            self._cfg[key] = item["default"]
            changed.append(key)

        if changed:
            if not self.save():
                errors.append(
                    "配置已生效但没能写入磁盘，重启插件后会回滚；"
                    "请检查配置目录是否可写、磁盘是否已满"
                )
        return changed, errors

    def reset_to_defaults(self) -> list[str]:
        """把所有配置项恢复为默认值，返回被改动的键列表。"""
        changed, _ = self.apply_updates(
            {item["key"]: item["default"] for item in CONFIG_META}
        )
        return changed

    def save(self) -> bool:
        """持久化配置（AstrBotConfig 提供 save_config，缺失时静默跳过）。

        Returns:
            是否真的写盘成功。失败必须让调用方看见 —— 只写内存的修改在
            重启后会无声回滚，前端却已经提示「已保存」。
        """
        from astrbot.api import logger

        save = getattr(self._cfg, "save_config", None)
        if not callable(save):
            logger.warning(
                "[denia_share] 配置对象没有 save_config，本次修改只存在于内存，"
                "重启后会回滚"
            )
            return False
        try:
            save()
            return True
        except Exception:
            logger.exception(
                "[denia_share] 配置写入磁盘失败，本次修改只存在于内存，重启后会回滚"
            )
            return False

    # ---------------- 平台 ---------------- #

    @property
    def DISABLED_PLATFORMS(self) -> list[str]:
        raw = self._cfg_get("DISABLED_PLATFORMS", "")
        if not raw:
            return []
        return [p.strip().lower() for p in str(raw).split(",") if p.strip()]

    @property
    def VIDEO_DURATION_MAXIMUM(self) -> int:
        return int(self._cfg_get("VIDEO_DURATION_MAXIMUM", 480))

    @property
    def VIDEO_SIZE_MAXIMUM_MB(self) -> int:
        """单个视频体积上限（MB），超限不下载。"""
        return max(1, int(self._cfg_get("VIDEO_SIZE_MAXIMUM_MB", 100)))

    @property
    def PROXY(self) -> str:
        """全局代理地址，形如 http://127.0.0.1:7890，留空不使用。"""
        return str(self._cfg_get("PROXY", "") or "").strip()

    @property
    def HTTP_VERIFY_SSL(self) -> bool:
        """是否校验 HTTPS 证书（默认开启；关掉会让带 Cookie 的请求可被中间人截获）。"""
        return bool(self._cfg_get("HTTP_VERIFY_SSL", True))

    @property
    def XHS_CK(self) -> str | None:
        return self._cfg_get("XHS_CK", None)

    # ---------------- Pixiv / GitHub ---------------- #

    @property
    def PIXIV_CK(self) -> str:
        """Pixiv Cookie（可选）。

        配了 Cookie 搜索收录会更全，但也会让搜索结果开始出现 R18 作品。
        解析器侧对 xRestrict != 0 有硬性过滤，Cookie 不会放宽这条限制。
        """
        return str(self._cfg_get("PIXIV_CK", "") or "").strip()

    @property
    def GITHUB_TOKEN(self) -> str:
        """GitHub Personal Access Token（可选，但强烈建议）。

        免 token 时限额 60 次/小时且按**出口 IP**计，共享 IP 下几乎必被限流。
        填了 token 提升到 5000 次/小时（按账号计）。
        """
        return str(self._cfg_get("GITHUB_TOKEN", "") or "").strip()

    # ---------------- 网页截图 ---------------- #

    @property
    def SCREENSHOT_BACKEND(self) -> str:
        val = str(self._cfg_get("SCREENSHOT_BACKEND", "thum")).strip().lower()
        return val if val in {"thum", "cloudflare"} else "thum"

    @property
    def SCREENSHOT_FALLBACK(self) -> bool:
        """链接匹配不到任何平台时，是否自动截图（默认关，避免群里刷图）。"""
        return bool(self._cfg_get("SCREENSHOT_FALLBACK", False))

    @property
    def CF_ACCOUNT_ID(self) -> str:
        return str(self._cfg_get("CF_ACCOUNT_ID", "") or "").strip()

    @property
    def CF_API_TOKEN(self) -> str:
        return str(self._cfg_get("CF_API_TOKEN", "") or "").strip()

    # ---------------- B站 ---------------- #

    @property
    def BILI_CK(self) -> str | None:
        return self._cfg_get("BILI_CK", None)

    @property
    def BILI_QUALITY(self) -> str:
        return str(self._cfg_get("BILI_QUALITY", "1080P"))

    @property
    def BILI_CODEC(self) -> str:
        """B站视频流编码策略：``H264兼容``（默认）或 ``全部编码``。

        只取 H.264 是**兼容性**取舍：B站高清晰度档位的 DASH 流只有 HEVC/AV1，
        而插件是零转码直接装进 .mp4 的，电脑版 QQ 解不了 HEVC → 花屏 / 放不出来
        （手机端大多能放，所以症状是「只有电脑 QQ 坏」）。1080P 及以下 AVC 都有，
        代价只是拿不到 4K/8K。识别不了的值一律按 H264兼容 处理。
        """
        val = str(self._cfg_get("BILI_CODEC", "H264兼容") or "").strip()
        return "全部编码" if val == "全部编码" else "H264兼容"

    # ---------------- Steam ---------------- #

    @property
    def STEAM_REGION(self) -> str:
        """Steam 价格地区代码（同时用于 ITAD 的 country 参数）。"""
        return str(self._cfg_get("STEAM_REGION", "cn") or "cn").strip().lower()

    @property
    def ITAD_API_KEY(self) -> str:
        """IsThereAnyDeal API key（可选）。

        只有想要「历史最低价」才需要。免费申请：https://isthereanydeal.com/apps
        留空时 Steam 解析照常工作，只是不显示史低。
        """
        return str(self._cfg_get("ITAD_API_KEY", "") or "").strip()

    # ---------------- 缓存 ---------------- #

    @property
    def CACHE_TTL_HOURS(self) -> int:
        return int(self._cfg_get("CACHE_TTL_HOURS", 24))

    @property
    def CACHE_CLEANUP_INTERVAL_MINUTES(self) -> int:
        return int(self._cfg_get("CACHE_CLEANUP_INTERVAL_MINUTES", 60))

    # ---------------- 渲染 ---------------- #

    @property
    def RENDER_ENABLED(self) -> bool:
        return bool(self._cfg_get("RENDER_ENABLED", True))

    @property
    def RENDER_THEME(self) -> str:
        val = str(self._cfg_get("RENDER_THEME", "dark")).strip().lower()
        return val if val in {"dark", "light"} else "dark"

    @property
    def RENDER_LAYOUT(self) -> str:
        val = str(self._cfg_get("RENDER_LAYOUT", "standard")).strip().lower()
        return val if val in {"standard", "magazine", "immersive", "feed"} else "standard"

    @property
    def RENDER_WIDTH(self) -> int:
        return max(520, min(1080, int(self._cfg_get("RENDER_WIDTH", 800))))

    @property
    def RENDER_FONT_PATH(self) -> str:
        return str(self._cfg_get("RENDER_FONT_PATH", "") or "").strip()

    @property
    def RENDER_COVER_FULL_SIZE(self) -> bool:
        return bool(self._cfg_get("RENDER_COVER_FULL_SIZE", False))

    @property
    def RENDER_ACCENT_COLOR(self) -> str:
        """自定义强调色（#RRGGBB），留空表示按平台品牌色。"""
        value = str(self._cfg_get("RENDER_ACCENT_COLOR", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_WATERMARK(self) -> str:
        """卡片右下角水印文字，留空表示不显示。"""
        return str(self._cfg_get("RENDER_WATERMARK", "希望解析") or "").strip()[:12]

    @property
    def RENDER_DESC_MAX_LINES(self) -> int:
        """正文最大行数，0 表示按布局默认。"""
        return max(0, min(12, int(self._cfg_get("RENDER_DESC_MAX_LINES", 0))))

    @property
    def RENDER_SHOW_AVATAR(self) -> bool:
        return bool(self._cfg_get("RENDER_SHOW_AVATAR", True))

    @property
    def RENDER_SHOW_PLAY_BUTTON(self) -> bool:
        return bool(self._cfg_get("RENDER_SHOW_PLAY_BUTTON", True))

    @property
    def RENDER_GRADIENT_TOP(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_TOP", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    @property
    def RENDER_GRADIENT_BOTTOM(self) -> str:
        value = str(self._cfg_get("RENDER_GRADIENT_BOTTOM", "") or "").strip()
        return value if _HEX_COLOR_RE.match(value) else ""

    def renderer_options(self) -> dict[str, Any]:
        """返回构造 ShareCardRenderer 的关键字参数（配置的唯一装配点）。

        main.py 的常驻渲染器与 webui.py 的临时预览渲染器都用它，
        新增外观项只需要在这里和 ``_build_renderer`` 的覆盖层各加一行。
        """
        return {
            "enabled": self.RENDER_ENABLED,
            "width": self.RENDER_WIDTH,
            "theme": self.RENDER_THEME,
            "font_path": self.RENDER_FONT_PATH or None,
            "layout": self.RENDER_LAYOUT,
            "cover_full_size": self.RENDER_COVER_FULL_SIZE,
            "accent_color": self.RENDER_ACCENT_COLOR or None,
            "watermark": self.RENDER_WATERMARK,
            "desc_max_lines": self.RENDER_DESC_MAX_LINES,
            "show_avatar": self.RENDER_SHOW_AVATAR,
            "show_play_button": self.RENDER_SHOW_PLAY_BUTTON,
            "gradient_top": self.RENDER_GRADIENT_TOP or None,
            "gradient_bottom": self.RENDER_GRADIENT_BOTTOM or None,
        }

    # ---------------- 媒体发送 ---------------- #

    @property
    def CACHE_DIR(self) -> str:
        """Docker 共享缓存目录，留空表示用插件数据目录（默认行为）。

        与 yaya 的差别：那边在容器里会把默认值切成自己约定的
        ``/app/sharedFolder/...``；这里保守一些 —— 留空始终用插件数据目录，
        免得用户没挂载却被切到一个不可写路径上，把下载整个搞坏。
        """
        return str(self._cfg_get("CACHE_DIR", "") or "").strip()

    @property
    def MEDIA_RELAY_ENABLED(self) -> bool:
        """是否把已下载的视频注册成 AstrBot 的临时 HTTP 链接再发送。"""
        return bool(self._cfg_get("MEDIA_RELAY_ENABLED", False))

    @property
    def MEDIA_RELAY_CALLBACK_URL(self) -> str:
        """中转链接用的回调地址；留空时回退 AstrBot 全局 callback_api_base。"""
        return str(self._cfg_get("MEDIA_RELAY_CALLBACK_URL", "") or "").strip().rstrip("/")

    @property
    def MEDIA_RELAY_TTL(self) -> int:
        """中转链接有效期（秒），最小 30。"""
        return max(30, int(self._cfg_get("MEDIA_RELAY_TTL", 300)))

    @property
    def OFFICIAL_RELAY_FIRST(self) -> bool:
        """QQ 官方 Bot 的视频是否**优先**用中转链接（默认否）。

        默认走本地上传，因为链接路径实测不可靠：AstrBot 的 ``file_token_service``
        是**一次性**的（``handle_file`` 取一次就 ``pop``），而 QQ 拉取一旦没一次拉完
        （或先发了 HEAD），框架的后续重试全部 404 —— 整条路必然失败。
        见 README「官方 Bot 视频发送」。
        """
        return bool(self._cfg_get("OFFICIAL_RELAY_FIRST", False))

    @property
    def OFFICIAL_CHUNK_PAD_MIN_MB(self) -> int:
        """官方 Bot 上「补位到分片上传」的体积起点（MB，0 = 关闭，默认 4）。

        详见 ``core.media_utils.pad_to_chunked_upload``：框架 ≤10MB 走单次 base64
        （客户端 20s 超时），补到 10MB 以上就走分片上传（单请求 300s）。
        """
        try:
            value = int(self._cfg_get("OFFICIAL_CHUNK_PAD_MIN_MB", 4) or 0)
        except (TypeError, ValueError):
            return 4
        return max(0, value)

    @property
    def SEND_MODE(self) -> str:
        """发送模式：``全部发送`` / ``仅文本`` / ``仅富媒体``（未知值按全部发送）。"""
        val = str(self._cfg_get("SEND_MODE", "全部发送") or "").strip()
        return val if is_send_mode(val) else "全部发送"

    @property
    def SEND_MODE_OVERRIDES(self) -> dict[str, str]:
        """按会话覆盖发送模式的映射（键已归一化，见 ``core.send_mode``）。"""
        return parse_mode_overrides(self._cfg_get("SEND_MODE_OVERRIDES", ""))

    @property
    def send_mode_flags(self) -> tuple[bool, bool, bool]:
        """把 :attr:`SEND_MODE` 映射成 ``(发卡片, 发文字, 发原始媒体)``。

        「卡片」是渲染出来的那一张图，和「文字 / 原始媒体」分开 —— 所以
        「仅富媒体」可以做到**不发卡片**只发图片与视频。
        """
        return mode_flags(self.SEND_MODE)

    def send_mode_flags_for(self, *keys: Any) -> tuple[bool, bool, bool]:
        """按会话解析生效的发送模式开关。

        Args:
            keys: 由精确到宽泛排列的候选键 —— 典型是
                ``(unified_msg_origin, 群号, 平台名)``。空值会被跳过。

        Returns:
            命中的第一个覆盖条目的开关；都不命中时回落到 :attr:`send_mode_flags`。
        """
        return resolve_mode_flags(
            self._cfg_get("SEND_MODE_OVERRIDES", ""), self.SEND_MODE, *keys
        )

    # ---------------- 行为与调试 ---------------- #

    @property
    def SEND_ERROR_MESSAGES(self) -> bool:
        """解析失败时是否向对话发送错误提示（关闭则只记日志，群里更安静）。"""
        return bool(self._cfg_get("SEND_ERROR_MESSAGES", False))

    @property
    def TWITTER_MEDIA_PROXY_BASE(self) -> str:
        """Twitter/X 媒体反代根地址（结尾斜杠会被去掉），留空表示不启用。"""
        return str(self._cfg_get("TWITTER_MEDIA_PROXY_BASE", "") or "").strip().rstrip("/")

    @property
    def DEBUG_LOG_ENABLED(self) -> bool:
        return bool(self._cfg_get("DEBUG_LOG_ENABLED", True))

    # ---------------- 权限控制 ----------------
    # 名单返回**原始字符串**（逗号分隔），解析交给 core/permissions.py ——
    # 那里同时收中文逗号与顿号，且在两个开关都关时根本不解析。

    @property
    def WHITELIST_ENABLE(self) -> bool:
        return bool(self._cfg_get("WHITELIST_ENABLE", False))

    @property
    def WHITELIST_USER(self) -> str:
        return str(self._cfg_get("WHITELIST_USER", "") or "")

    @property
    def WHITELIST_GROUP(self) -> str:
        return str(self._cfg_get("WHITELIST_GROUP", "") or "")

    @property
    def BLACKLIST_ENABLE(self) -> bool:
        return bool(self._cfg_get("BLACKLIST_ENABLE", False))

    @property
    def BLACKLIST_USER(self) -> str:
        return str(self._cfg_get("BLACKLIST_USER", "") or "")

    @property
    def BLACKLIST_GROUP(self) -> str:
        return str(self._cfg_get("BLACKLIST_GROUP", "") or "")

    @property
    def BLACKLIST_ADMIN_EXEMPT(self) -> bool:
        """管理员是否豁免黑名单（默认 True = 旧行为）。

        关掉之后黑名单对管理员同样生效 —— 用来做「这个群连我自己的号都不响应」。
        只影响黑名单：白名单一直是「管理员不受限」。
        """
        return bool(self._cfg_get("BLACKLIST_ADMIN_EXEMPT", True))


def init_config(astrbot_config: Any, cache_dir: Path, config_dir: Path) -> ParserConfig:
    global _config
    _config = ParserConfig(astrbot_config, cache_dir, config_dir)
    return _config


def get_config() -> ParserConfig:
    global _config
    if _config is None:
        raise RuntimeError("ParserConfig not initialized yet")
    return _config

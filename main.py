"""
希望解析器 - 链接分享自动解析插件

支持 B站 | 抖音 | 快手 | 微博 | 小红书 | Twitter | AcFun | NGA | GitHub | Pixiv | Steam
另有网页截图（thum.io / Cloudflare 双后端）与 Pixiv 关键词搜索。
Steam 历史最低价需配置 ITAD_API_KEY（免费申请），未配置时只显示当前价与折扣。

代码来源（涉及第三方许可的都标在各自源文件里）：
- astrbot_plugin_rika_share（MIT）：解析框架 / 卡片渲染 / B站 / 微博 / 小红书 / AcFun / NGA
- Johnserf-Seed/f2（Apache-2.0）：抖音 a_bogus 签名，见 core/parsers/douyin/sign.py
- 作者自己的 astrbot_plugin_media_parser（娅娅版）：抖音签名 Web API 路径、
  快手新版页面兼容、Twitter GraphQL 兜底、媒体发送机制

有意不实现的能力：LLM 文本翻译、视频仅发送封面。
"""

import os
import re
import io
import json
import asyncio
import hashlib
from pathlib import Path
from typing import Any, AsyncGenerator, Dict

import aiohttp
import qrcode
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register, StarTools

from .core.media_utils import (
    CACHE_MARKER_NAME,
    QQ_CHUNKED_UPLOAD_THRESHOLD_BYTES,
    cleanup_cache_dir,
    ensure_cache_marker,
    is_docker_environment,
    pad_to_chunked_upload,
    should_pad_for_chunked,
)
from .core.config import init_config, get_config, verify_schema_alignment
from .core.download import StreamDownloader
from .core.data import (
    ParseResult, ImageContent, VideoContent, AudioContent, Author, platform_of,
)
from .core.history import HistoryStore, ParseRecord
from .core.permissions import is_allowed
from .core.send_mode import pick_items, split_nodes
from .core.relay import register_file, resolve_callback_base
from .core.constants import (
    PLATFORM_DISPLAY_NAMES, PlatformEnum, is_custom_platform, platform_meta,
)
# 版本号只认 __init__.py 那一处：register 装饰器直接读它，
# 不用再在装饰器里手写一遍版本字符串（以前改版本要同时改三处，漏一处就版本不一致）
from . import __version__
from .core.exception import (
    ParseException, IgnoreException, SilentException,
)
from .core.card_renderer import ShareCardRenderer
from .core.screenshot import ScreenshotService, is_probably_screenshotable
from .core.parsers import (
    BilibiliParser, DouyinParser, KuaiShouParser, WeiBoParser,
    XiaoHongShuParser, TwitterParser, NGAParser, AcfunParser,
    GitHubParser, PixivParser, SteamParser,
)
from .core.custom_parsers import (
    SUBDIR_NAME as CUSTOM_PARSERS_SUBDIR,
    CustomParserLoader,
    LoadResult as CustomParserLoadResult,
)

PLUGIN_NAME = "astrbot_plugin_denia_share"

# ========== B站扫码登录 API ==========
BILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
BILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_CODE_SCANNED = 86090
QR_CODE_EXPIRED = 86038
QR_CODE_SUCCESS = 0
QR_CODE_EXPIRE_TIME = 180
POLL_INTERVAL = 5

# 登录状态在任务结束后还要留一会儿：WebUI 是**轮询**读取的，任务一结束就删，
# 最后一轮会拿到「登录任务不存在」而不是「登录成功」。
LOGIN_STATE_TTL = 300
# 状态字典的条数上限。WebUI 每点一次「获取二维码」都是一个随机 task_id，
# 原先只从 _bili_login_tasks 里 pop，状态永远留着 —— 点得越多字典越长。
LOGIN_STATE_MAX = 32

# 解析结果内存缓存的条数上限。`_result_cache` 原先只在「清理循环 / 切换缓存目录 /
# 手动清空缓存」三处 clear，而清理循环在 CACHE_TTL_HOURS=0 时根本不启动 ——
# 也就是说那种部署下它永不回收。加个条数上限兜底，与 TTL 无关。
MAX_RESULT_CACHE_ENTRIES = 128


def _get_plugin_data_dir() -> Path:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME


# ========== URL 匹配模式 ==========
BILIBILI_PATTERN = re.compile(r"(bilibili\.com|b23\.tv|bili2233\.cn|BV[1-9a-zA-Z]{10}|av\d{6,})")
DOUYIN_PATTERN = re.compile(r"(v\.douyin\.com|douyin\.com|iesdouyin\.com|m\.douyin\.com|jx\.douyin\.com|jingxuan\.douyin\.com)")
KUAISHOU_PATTERN = re.compile(r"(v\.kuaishou\.com|kuaishou\.com|chenzhongtech\.com)")
WEIBO_PATTERN = re.compile(r"(weibo\.com|weibo\.cn|m\.weibo\.cn|video\.weibo\.com|mapp\.api\.weibo\.cn)")
XHS_PATTERN = re.compile(r"(xhslink\.com|xhslink\.cn|xiaohongshu\.com)")
TWITTER_PATTERN = re.compile(r"(?:^|[\s./@])x\.com(?:[/:\s]|$)|twitter\.com")
NGA_PATTERN = re.compile(r"(nga\.178\.com|ngabbs\.com|bbs\.nga\.cn)")
ACFUN_PATTERN = re.compile(r"acfun\.cn")
GITHUB_PATTERN = re.compile(r"github\.com/[\w.\-]+/[\w.\-]+")
PIXIV_PATTERN = re.compile(r"pixiv\.net/(?:artworks|i/|member_illust)")
STEAM_PATTERN = re.compile(r"store\.steampowered\.com")

URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


class _EventUrlWrapper:
    """把任意 URL 伪装成一个只含该 URL 的事件，供解析流程复用。"""

    def __init__(self, event: AstrMessageEvent, url: str):
        self._event = event
        self.message_str = url

    def __getattr__(self, name):
        return getattr(self._event, name)


@register(
    "希望解析器",
    "xiaoxi2760",
    # 平台列表也从 PLATFORMS 派生：这是第 5 处会写出平台名的地方，
    # 手写一份就等于又埋一个「改了这里忘了那里」
    "链接分享自动解析，支持 " + "|".join(PLATFORM_DISPLAY_NAMES.values()),
    __version__,
)
class DeniaSharePlugin(Star):

    @staticmethod
    def _is_onebot(event: AstrMessageEvent) -> bool:
        """合并转发（Comp.Nodes）是 OneBot v11 独有特性。"""
        try:
            return "aiocqhttp" in event.get_platform_name().lower()
        except Exception:
            return False

    @staticmethod
    def _is_qq_official(event: AstrMessageEvent) -> bool:
        """是否 QQ 官方 Bot（``qq_official`` / ``qqofficial_webhook`` 两种注册名）。

        官方适配器会**丢弃** ``Comp.Nodes``（合并转发），所以文本只能走普通消息；
        富媒体则相反 —— 它支持本地文件上传视频，但不能像 aiocqhttp 那样读容器路径。
        """
        try:
            return "qqofficial" in event.get_platform_name().lower().replace("_", "")
        except Exception:
            return False

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        """平台类型名（``aiocqhttp`` / ``qq_official``），取不到返回空串。

        AstrBot 的 ``get_platform_name()`` 返回的是**适配器类型**而不是实例名
        （实例名在 ``get_platform_id()``），所以它可以直接当发送模式覆盖表的键。
        """
        try:
            return str(event.get_platform_name() or "")
        except Exception:
            return ""

    @staticmethod
    def _is_media_item(item: list) -> bool:
        """该节点条目是不是媒体（图片）—— 供 ``split_nodes`` 切分文字 / 媒体。"""
        return any(isinstance(comp, Comp.Image) for comp in item)

    @staticmethod
    def _fallback_cache_dir(data_dir: Path) -> tuple[Path, str]:
        """回退到插件数据目录，并保证它带缓存哨兵。"""
        default_dir = data_dir / "cache"
        default_dir.mkdir(parents=True, exist_ok=True)
        ensure_cache_marker(default_dir, allow_nonempty=True)
        return default_dir, "fallback"

    @staticmethod
    def _resolve_cache_dir(data_dir: Path, configured: str) -> tuple[Path, str]:
        """决定媒体缓存目录。

        留空用插件数据目录；填了「共享缓存目录」就用它，让协议端容器也能按
        同一个容器内路径读到下载下来的视频。

        与 yaya 的差别：那边在容器里会把默认值切成自己约定的
        ``/app/sharedFolder/...``；这里在目录不可用时**回退**插件数据目录而不是
        拒绝下载 —— 用户没挂载却被切到不可写路径时，至少功能还是通的。

        **安全约束**：缓存清理是递归删除（``clear_cache_dir`` / ``cleanup_cache_dir``），
        所以这里必须先把危险路径挡掉，而不是等清理时再补救。三类被拒：
        文件系统根目录、把插件数据目录包在里面的祖先目录、
        已有内容却没有缓存哨兵的目录。三者在**建目录之前**判定。

        Returns:
            (缓存目录, 来源说明)，来源取 ``default`` / ``configured`` / ``fallback``。
        """
        if not configured:
            default_dir = data_dir / "cache"
            default_dir.mkdir(parents=True, exist_ok=True)
            ensure_cache_marker(default_dir, allow_nonempty=True)
            return default_dir, "default"

        candidate = Path(configured).expanduser()
        # resolve() 不要求路径存在（strict=False），所以危险路径可以在建目录**之前**
        # 就判掉。之前是先 mkdir(parents=True) 再校验，虽然删除链已经被哨兵挡住，
        # 但「按一个自由文本配置项在任意路径建出目录树」这个原语还在 —— 顺序调过来
        # 就顺手消掉了，代价为零。
        resolved = candidate.resolve()

        if resolved == Path(resolved.anchor):
            logger.warning(
                f"[denia_share] 拒绝把文件系统根目录 {resolved} 当作缓存目录"
                f"（清理会递归删掉整块盘），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            data_dir.resolve().relative_to(resolved)
        except ValueError:
            pass
        else:
            logger.warning(
                f"[denia_share] 缓存目录 {resolved} 把插件数据目录包在里面，"
                f"清理时会连配置与解析记录一起删掉，已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                f"[denia_share] 共享缓存目录不可用（{configured}）：{exc}；"
                f"已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not os.access(resolved, os.W_OK):
            logger.warning(
                f"[denia_share] 共享缓存目录不可写（{resolved}），已回退到插件数据目录"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        if not ensure_cache_marker(resolved, allow_nonempty=False):
            logger.warning(
                f"[denia_share] 共享缓存目录 {resolved} 里已有内容且没有 "
                f"{CACHE_MARKER_NAME} 哨兵，无法确认它就该被当作缓存目录，"
                f"已回退到插件数据目录。确认要用它，请先在该目录下建一个空的 "
                f"{CACHE_MARKER_NAME} 文件"
            )
            return DeniaSharePlugin._fallback_cache_dir(data_dir)

        return resolved, "configured"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        data_dir = _get_plugin_data_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self.config_dir = data_dir / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)

        pconfig = init_config(config, data_dir / "cache", self.config_dir)

        # 缓存目录可被「共享缓存目录」配置项接管：Docker 分容器部署时，
        # 协议端要按同一个容器内路径才能读到下载下来的视频
        self.cache_dir, self.cache_dir_source = self._resolve_cache_dir(
            data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        pconfig.cache_dir = self.cache_dir

        try:
            from .core.config import migrate_grouped_config

            if migrate_grouped_config(config):
                save = getattr(config, "save_config", None)
                if callable(save):
                    save()
        except Exception:
            logger.warning("旧版配置迁移失败，将使用兼容回退读取", exc_info=True)

        self.downloader = StreamDownloader(
            self.cache_dir,
            proxies=pconfig.PROXY or None,
            max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
        )
        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        self.parsers: dict[str, Any] = {}
        # 用户自定义解析器的加载器。延迟到 _init_parsers 里建（那时 downloader 已就绪）
        self._custom_loader: CustomParserLoader | None = None
        self._custom_result: CustomParserLoadResult | None = None
        # 上一轮装进 self.parsers 的自定义平台键。重扫时靠它把**已经不存在**的
        # 平台（文件被删/改名，或新版本加载失败）摘掉 —— 见 _init_custom_parsers。
        self._custom_parser_keys: set[str] = set()
        self._init_parsers()
        self._result_cache: dict[str, ParseResult] = {}
        self._render_cache: dict[str, Path] = {}
        self._cache_cleanup_task: asyncio.Task | None = None

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())

        # ========== B站 Cookie ==========
        self._bili_cookie: str = ""
        # 上一次**生效过**的配置项 BILI_CK。用来判断「配置项到底变没变」：
        # apply_runtime_config 是「保存配置 / 恢复默认 / 保存外观」三个接口都会走的，
        # 无条件重新应用 cookie 会误伤（详见那里的注释）。
        self._bili_ck_applied: str = ""
        self._bili_http_session: aiohttp.ClientSession | None = None
        self._bili_login_tasks: Dict[str, asyncio.Task] = {}
        self._bili_login_states: Dict[str, dict[str, Any]] = {}
        # 二维码临时文件的延迟清理任务，terminate() 时一并取消
        self._delayed_cleanup_tasks: list[asyncio.Task] = []
        self._bili_data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._bili_data_dir.mkdir(parents=True, exist_ok=True)
        self._bili_cookie_file = self._bili_data_dir / "bili_cookie.json"
        # 「本机单次 base64 上传不可靠」的记忆（QQ 官方 Bot 专用，见 _send_official_video）
        self._official_upload_file = self._bili_data_dir / "official_upload.json"
        self._official_single_shot_broken = self._load_official_single_shot_broken()

        # ========== 解析记录（WebUI「缓存」页的数据源） ==========
        self.history = HistoryStore(data_dir / "history.jsonl")

        self._check_schema_alignment()
        self._register_webui()

        logger.info(
            f"[denia_share] 已启用平台: {', '.join(self.parsers.keys()) or '无'}；"
            f"卡片渲染: {'开' if self._renderer.enabled else '关'}；"
            f"媒体缓存: {self.cache_dir}"
            f"{'（共享目录）' if self.cache_dir_source == 'configured' else ''}"
            f"{'（配置的共享目录不可用，已回退）' if self.cache_dir_source == 'fallback' else ''}；"
            f"媒体中转: {'开' if pconfig.MEDIA_RELAY_ENABLED else '关'}"
            f"{'（容器内）' if is_docker_environment() else ''}"
        )

    def _check_schema_alignment(self):
        """自检 _conf_schema.json 与 CONFIG_META 是否一致（不一致会导致配置静默丢失）。"""
        try:
            schema_path = Path(__file__).with_name("_conf_schema.json")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[denia_share] 无法读取 _conf_schema.json，跳过一致性自检", exc_info=True)
            return
        problems = verify_schema_alignment(schema)
        for problem in problems:
            logger.warning(f"[denia_share] 配置 schema 不一致：{problem}")

    def _register_webui(self):
        """注册插件 WebUI 的后端接口。

        AstrBot < 4.24.2 没有 register_web_api，此时静默跳过——
        聊天命令与解析功能都不受影响，只是没有网页界面。
        """
        if not hasattr(self.context, "register_web_api"):
            logger.info("[denia_share] 当前 AstrBot 不支持插件页面，跳过 WebUI 注册")
            self._webui_ready = False
            return
        try:
            from .core.webui import WebUIApi

            self._webui = WebUIApi(self)
            self._webui.register()
            self._webui_ready = True
            logger.info("[denia_share] WebUI 接口已注册（插件页面 pages/denia）")
        except Exception:
            self._webui_ready = False
            logger.warning("[denia_share] WebUI 接口注册失败", exc_info=True)

    def _init_parsers(self):
        pconfig = get_config()
        disabled = self.disabled_platforms
        if "bilibili" not in disabled:
            self.parsers["bilibili"] = BilibiliParser(
                self.downloader, bili_ck=pconfig.BILI_CK, config_dir=self.config_dir
            )
        if "douyin" not in disabled:
            self.parsers["douyin"] = DouyinParser(self.downloader)
        if "kuaishou" not in disabled:
            self.parsers["kuaishou"] = KuaiShouParser(self.downloader)
        if "weibo" not in disabled:
            self.parsers["weibo"] = WeiBoParser(self.downloader)
        if "xiaohongshu" not in disabled:
            self.parsers["xiaohongshu"] = XiaoHongShuParser(self.downloader, xhs_ck=pconfig.XHS_CK)
        if "twitter" not in disabled:
            self.parsers["twitter"] = TwitterParser(self.downloader)
        if "nga" not in disabled:
            self.parsers["nga"] = NGAParser(self.downloader)
        if "acfun" not in disabled:
            self.parsers["acfun"] = AcfunParser(self.downloader)
        if "github" not in disabled:
            self.parsers["github"] = GitHubParser(self.downloader, token=pconfig.GITHUB_TOKEN)
        if "pixiv" not in disabled:
            self.parsers["pixiv"] = PixivParser(self.downloader, cookie=pconfig.PIXIV_CK)
        if "steam" not in disabled:
            self.parsers["steam"] = SteamParser(
                self.downloader,
                itad_key=pconfig.ITAD_API_KEY,
                region=pconfig.STEAM_REGION,
            )

        self._init_custom_parsers()
        self._build_screenshot_service()

    def _init_custom_parsers(self) -> None:
        """加载用户自定义解析器（目录扫描 + 失败隔离）。

        每次都会重新扫描目录：这个方法在插件初始化与 ``apply_runtime_config``
        里都会被调到，所以「保存配置 / 切换平台 / 点重新加载」都能让新写的文件
        生效，不必重载插件。加载器自身保证反复调用是幂等的（先反注册再注册）。
        """
        if self._custom_loader is None:
            self._custom_loader = CustomParserLoader(
                self._data_dir / CUSTOM_PARSERS_SUBDIR
            )
        result = self._custom_loader.reload(
            self.downloader, skip_keys=set(self.disabled_platforms)
        )
        self._custom_result = result
        # 先把上一轮装进去的自定义平台摘掉，再装这一轮的。
        #
        # **不能只 update**：dict.update 只覆盖同名键，删掉（或改了平台键）的文件
        # 会把自己的旧实例留在 self.parsers 里 —— 平台列表里已经没有它了
        # （加载器的 _purge 反注册了展示名），但实例还在：
        #   · WebUI 手动解析照旧用**旧代码**解析（_match_parser 找得到它）
        #   · toggle_platform 会对它回「未知平台」（展示名已不在 PLATFORM_DISPLAY_NAMES）
        # 也就是「看起来删了、其实还在跑」，本仓最贵的那类 bug。
        # 用自己记的键来摘，而不是 is_custom_platform(key)：后者查的是 PLATFORMS，
        # 而 _purge 已经把旧键从那里删掉了，问它只会得到「这不是自定义平台」。
        for stale_key in self._custom_parser_keys:
            self.parsers.pop(stale_key, None)
        self._custom_parser_keys = set(result.parsers)
        self.parsers.update(result.parsers)

    def reload_custom_parsers(self) -> CustomParserLoadResult:
        """供 WebUI 的「重新加载自定义解析器」按钮调用：重扫 + 立刻生效。"""
        self._init_custom_parsers()
        return self._custom_result or CustomParserLoadResult()

    def _build_screenshot_service(self) -> None:
        """按当前配置（重）建截图服务。

        抽成独立方法是因为热更新也要调它：截图后端的代理与证书校验都是构造时
        读死的，只在初始化时建一次的话，网页上改这两项要重载插件才生效。
        """
        pconfig = get_config()
        self.screenshot = ScreenshotService(
            self.cache_dir,
            backend=pconfig.SCREENSHOT_BACKEND,
            cf_account_id=pconfig.CF_ACCOUNT_ID,
            cf_api_token=pconfig.CF_API_TOKEN,
            proxy=pconfig.PROXY,
            verify_ssl=pconfig.HTTP_VERIFY_SSL,
        )
        self._screenshot_fallback = pconfig.SCREENSHOT_FALLBACK

    async def _cache_cleanup_loop(self):
        """缓存清理循环。

        **每一轮重新读配置**，而不是把 TTL / 间隔烤进闭包 —— 配置页的「维护」
        分组写着「改动后立即生效」，而 apply_runtime_config 并不重建这个任务，
        烤进闭包就等于「网页上改了 CACHE_TTL_HOURS 也不会生效」。
        """
        while True:
            pconfig = get_config()
            ttl = pconfig.CACHE_TTL_HOURS
            interval = max(pconfig.CACHE_CLEANUP_INTERVAL_MINUTES, 1) * 60
            try:
                if ttl > 0:
                    await cleanup_cache_dir(self.cache_dir, ttl_hours=ttl)
                    self._result_cache.clear()
                    self._render_cache.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 一次清理失败（如磁盘满/文件占用）不应杀死整个循环
                logger.warning("[denia_share] 缓存清理失败，下轮重试", exc_info=True)
            await asyncio.sleep(interval)

    async def initialize(self):
        # 循环常驻：TTL=0 时它自己跳过清理，这样「把 TTL 从 0 改成非 0」也能生效
        self._cache_cleanup_task = asyncio.create_task(self._cache_cleanup_loop())

        await self._bili_load_cookie()
        self._bili_http_session = aiohttp.ClientSession()
        if self._bili_cookie:
            self._bili_apply_cookie_to_parser(self._bili_cookie)

        self._log_official_media_plan()

    def _log_official_media_plan(self) -> None:
        """启动时把 QQ 官方 Bot 的媒体发送方案打一条日志。

        这一条是给「官方 Bot 视频发不出去」排障用的：走本地还是走链接、补位起点、
        回调地址解析成了什么，都在这里。少了它，用户只能等发送失败后翻日志猜 ——
        而升级插件不会覆盖已保存的配置值（AstrBot 只补缺失的键），
        「默认值改了但我的值还是旧的」这种情况光看代码是看不出来的。
        """
        try:
            pconfig = get_config()
            relay_first = pconfig.OFFICIAL_RELAY_FIRST
            pad_min = pconfig.OFFICIAL_CHUNK_PAD_MIN_MB
            base = resolve_callback_base(pconfig.MEDIA_RELAY_CALLBACK_URL)
        except Exception:
            logger.warning("[denia_share] 读取官方 Bot 媒体配置失败", exc_info=True)
            return

        order = "先试中转链接，失败再走本地" if relay_first else "本地上传（分片）优先，链接仅兜底"
        logger.info(
            f"[denia_share] 官方 Bot 视频发送：{order}；"
            f"≥{pad_min}MB（且 ≤10MB）的视频补位到分片上传；回调地址 "
            f"{base or '（未配置 → 链接兜底不可用）'}"
        )
        if pad_min == 0 and not self._official_single_shot_broken:
            logger.warning(
                "[denia_share] 补位已关闭且尚未观测到单发失败：≤10MB 的视频会走单次 base64"
                "（客户端 20 秒超时），上行稍慢就会「上传文件API返回None」重试 5 次；"
                "出问题就把「官方 Bot 分片上传起点」设成 4～6"
            )

    # ==================== 平台处理器 ====================

    def _access_denied(self, event: AstrMessageEvent) -> bool:
        """白名单 / 黑名单判定：True 表示这次请求该被**静默**拦下。

        管理员由 AstrBot 判定（``event.is_admin()``），与 ``/denia_status`` 等命令的
        ADMIN 门一致，不单独配管理员 ID。判定顺序见 ``core/permissions.py``。

        拦下时只写一条 debug 日志：黑名单群里不该因为有人发链接就刷出提示。

        判定本身抛异常时**按放行处理**并留一条 warning —— 名单配错最多是「没拦住」，
        而在这里静默拦下全部请求会让插件看起来彻底坏了（用户只会看到「零反应」）。
        """
        try:
            allowed = is_allowed(
                get_config(),
                is_admin=event.is_admin(),
                is_private=event.is_private_chat(),
                sender_id=event.get_sender_id(),
                group_id=event.get_group_id(),
            )
        except Exception:
            logger.warning("[denia_share] 权限判定失败，本次按放行处理", exc_info=True)
            return False
        if not allowed:
            # 带上平台与管理员标记：这两种 ID 混着用时，光看 user/group 判断不出
            # 「是不是官方 Bot 的 openid」「管理员豁免有没有生效」
            logger.debug(
                "[denia_share] 被权限名单拦下：platform=%s admin=%s user=%s group=%s private=%s",
                self._platform_name(event),
                event.is_admin(),
                event.get_sender_id(),
                event.get_group_id(),
                event.is_private_chat(),
            )
        return not allowed

    async def _dispatch(self, event: AstrMessageEvent, name: str):
        if self._has_json_component(event):
            return
        parser = self.parsers.get(name)
        if not parser:
            return
        async for r in self._process_url(event, parser):
            yield r

    # 内置平台的链接处理器。每条 handler 的 docstring 会**原样显示在插件详情页的
    # 「指令」列表里**（AstrBot 取 handler.__doc__ 作为描述），所以一句话说清解析什么。
    @filter.regex(BILIBILI_PATTERN)
    async def bilibili_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """B站链接解析（视频 / 动态 / 直播 / 专栏 / 收藏夹）"""
        async for r in self._dispatch(event, "bilibili"):
            yield r

    @filter.regex(DOUYIN_PATTERN)
    async def douyin_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """抖音链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "douyin"):
            yield r

    @filter.regex(KUAISHOU_PATTERN)
    async def kuaishou_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """快手链接解析（视频 / 图集）"""
        async for r in self._dispatch(event, "kuaishou"):
            yield r

    @filter.regex(WEIBO_PATTERN)
    async def weibo_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """微博链接解析（正文 / 视频 / 长文）"""
        async for r in self._dispatch(event, "weibo"):
            yield r

    @filter.regex(XHS_PATTERN)
    async def xiaohongshu_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """小红书链接解析（图文 / 视频，图片改写为无水印原图）"""
        async for r in self._dispatch(event, "xiaohongshu"):
            yield r

    @filter.regex(TWITTER_PATTERN)
    async def twitter_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Twitter / X 链接解析（图片 / 视频）"""
        async for r in self._dispatch(event, "twitter"):
            yield r

    @filter.regex(NGA_PATTERN)
    async def nga_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """NGA 帖子解析"""
        async for r in self._dispatch(event, "nga"):
            yield r

    @filter.regex(ACFUN_PATTERN)
    async def acfun_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """AcFun 视频解析"""
        async for r in self._dispatch(event, "acfun"):
            yield r

    @filter.regex(GITHUB_PATTERN)
    async def github_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """GitHub 仓库卡片（星标 / 分支 / issue / 主语言 / 最近提交）"""
        async for r in self._dispatch(event, "github"):
            yield r

    @filter.regex(PIXIV_PATTERN)
    async def pixiv_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Pixiv 作品解析（非全年龄内容一律过滤）"""
        async for r in self._dispatch(event, "pixiv"):
            yield r

    @filter.regex(STEAM_PATTERN)
    async def steam_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """Steam 商店页卡片（价格 / 折扣 / 类型，可含历史最低价）"""
        async for r in self._dispatch(event, "steam"):
            yield r

    # 自定义解析器的聊天入口。内置平台各有自己的 ``@filter.regex(<域名>)`` 处理器，
    # 而自定义平台是**运行时**才知道的，没法为它生成静态过滤器 —— 所以用这个通用 URL
    # 处理器兜住它们。少了这一条，自定义解析器只能在网页上手动解析：群里发链接**零反应**
    # （加载成功、平台开关里能看到它、自检也过，但聊天链路根本没有入口）。
    #
    # 两条让路规则：
    # - 消息带 JSON 组件（QQ 小程序卡片）时直接返回：``json_card_handler`` 已经会遍历
    #   全部解析器（含自定义），不返回就会把同一链接解析两遍。
    # - URL 归一个**已启用**的内置平台时跳过：那是内置处理器的活，重复处理会发两条。
    #   内置平台被禁用时不算命中，此时自定义解析器可以接过去。
    #
    # 上面这段原本是 handler 的 docstring，会整段显示在插件详情页里，太长，改成注释。
    @filter.regex(URL_PATTERN)
    async def custom_parser_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """自定义解析器的聊天入口（通用 URL 匹配）"""
        if self._has_json_component(event):
            return
        for link in dict.fromkeys(URL_PATTERN.findall(event.message_str or "")):
            if self._match_builtin_parser(link) is not None:
                continue
            parser = self._match_custom_parser(link)
            if parser is None:
                continue
            async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                yield r
            return

    # ==================== 网页截图 ====================

    @filter.command("shot")
    async def shot_command(self, event: AstrMessageEvent):
        """对链接截图 /shot <网址>"""
        url = self._first_url(event)
        if not url:
            yield event.plain_result("用法: /shot <网址>")
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.regex(URL_PATTERN)
    async def screenshot_fallback_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """无可解析平台时按需截图兜底（默认关闭）。"""
        if not self._screenshot_fallback or self._has_json_component(event):
            return
        url = self._first_url(event)
        if not url or self._match_parser(url) is not None:
            return
        async for r in self._do_screenshot(event, url):
            yield r

    @filter.command("pixiv")
    async def pixiv_search_command(self, event: AstrMessageEvent):
        """Pixiv 关键词搜索 /pixiv <关键词>"""
        if self._access_denied(event):
            return
        keyword = " ".join((event.message_str or "").split()[1:]).strip()
        if not keyword:
            yield event.plain_result("用法: /pixiv <关键词>\n结果强制过滤非全年龄内容")
            return

        parser = self.parsers.get("pixiv")
        if parser is None:
            yield event.plain_result("Pixiv 平台已被禁用")
            return

        try:
            cache_key = f"pixiv:{keyword}"
            result = self._result_cache.get(cache_key)
            if result is None:
                result = await parser.search(keyword)
                self._remember_result(cache_key, result)
            async for r in self._deliver(event, result, cache_key):
                yield r
            await self._record_history(
                result, self._render_cache.get(cache_key), via="command"
            )
        except IgnoreException as e:
            logger.warning(f"[denia_share] Pixiv 搜索被忽略: {e.message}")
            yield event.plain_result(f"ℹ️ {e.message}")
        except ParseException as e:
            logger.warning(f"[denia_share] Pixiv 搜索失败: {e.message}")
            yield event.plain_result(f"❌ 搜索失败: {e.message}")
        except Exception as e:
            logger.exception("Pixiv 搜索异常")
            # 同样不回显异常原文，避免把 URL / 容器内路径带进群里
            yield event.plain_result(
                f"❌ 搜索出错（{type(e).__name__}），详情见 AstrBot 日志"
            )

    # ==================== JSON 卡片（QQ 小程序分享） ====================

    @filter.regex(r".*")
    async def json_card_handler(self, event: AstrMessageEvent, matched: re.Match | None = None):
        """QQ 小程序分享卡片（Json 消息段）里的链接解析"""
        if not self._has_json_component(event):
            return
        links = self._extract_links_from_event(event)
        if not links:
            return
        for link in dict.fromkeys(links):
            for parser in self.parsers.values():
                try:
                    parser.search_url(link)
                    async for r in self._process_url(_EventUrlWrapper(event, link), parser):
                        yield r
                    return
                except (SilentException, ParseException):
                    continue

    # ==================== 核心流程 ====================

    @staticmethod
    def result_cache_key(url: str) -> str:
        """解析结果缓存键：全量 URL 的短哈希。

        直接截断 URL 前 64 字符会让不同链接撞成同一个 key（小红书 / 微博的
        分享链常带长参数），撞上就会把 A 链接的解析结果当成 B 链接的返回。
        WebUI 的手动解析也必须用同一个键，否则同一链接在两条链路上各缓存一份。
        """
        return hashlib.md5(url.encode()).hexdigest()[:16]

    async def _process_url(
        self, event: AstrMessageEvent, parser: Any
    ) -> AsyncGenerator[MessageEventResult, None]:
        # 白名单 / 黑名单的唯一收口点：内置平台处理器、自定义解析器、
        # JSON 卡片三条路径最后都走这里（截图与 /pixiv 各自另有入口）。
        if self._access_denied(event):
            return
        url = event.message_str.strip()
        try:
            cache_key = self.result_cache_key(url)
            result = self._result_cache.get(cache_key)
            if result is None:
                keyword, searched = parser.search_url(url)
                result = await parser.parse(keyword, searched)
                self._remember_result(cache_key, result)

            async for r in self._deliver(event, result, cache_key):
                yield r

            # 交付完成后再记录：此时媒体都已落盘，记录里能带上真实文件名
            await self._record_history(result, self._render_cache.get(cache_key))

        except SilentException as e:
            # 「这条链接不归我管」是每条普通消息都会走到的正常流量，只在调试级留痕
            logger.debug(f"[denia_share] 静默跳过: {e.message}")
        except ParseException as e:
            # 解析失败必须无条件留痕：Cookie 全失效、接口改版、被风控都走这条，
            # 而 SEND_ERROR_MESSAGES 默认是关的 —— 只由它决定「发不发群」，
            # 不能连日志一起决定，否则群里不回复、日志也一片空白，用户和运维同时失明。
            logger.warning(f"[denia_share] 解析未完成: {e.message}")
            if e.notify_prefix and self._send_errors:
                yield event.plain_result(f"{e.notify_prefix} {e.message}")
        except Exception as e:
            logger.exception("解析异常")
            if self._send_errors:
                # 不回显异常原文：里面常带完整 URL 与容器内本地路径
                yield event.plain_result(
                    f"❌ 处理出错（{type(e).__name__}），详情见 AstrBot 日志"
                )

    async def _deliver(
        self, event: AstrMessageEvent, result: ParseResult, cache_key: str
    ) -> AsyncGenerator[MessageEventResult, None]:
        """把已得到的 ParseResult 渲染并发送出去。

        链接解析与 /pixiv 搜索共用这条输出链路。
        """
        # 先结算媒体下载再构建产物：下载失败的条目会在这里被记进
        # result.extra["limit_warnings"]，卡片与聊天消息都会如实带上
        # 「少了几张图」的说明 —— 否则图少了 3 张的消息和图一张不缺的消息
        # 长得一模一样，用户只能靠肉眼数。
        try:
            missing = await result.audit_missing_media()
        except Exception:
            missing = {}
            logger.warning("[denia_share] 媒体下载结算失败", exc_info=True)
        if missing:
            logger.warning(
                f"[denia_share] 本次解析有媒体未落盘: "
                f"{'、'.join(f'{k}×{v}' for k, v in missing.items())}"
            )

        header, nodes_content = await self._build_output(result)

        warnings = result.extra.get("limit_warnings") or []

        # 发送模式：卡片 / 文字 / 原始媒体三轴独立，映射与按会话覆盖见 ParserConfig。
        # 覆盖顺序由精确到宽泛：完整 umo > 群号（含官方 Bot 群 openid）> 平台名。
        send_card, send_text, send_media = get_config().send_mode_flags_for(
            event.unified_msg_origin,
            event.get_group_id(),
            self._platform_name(event),
        )

        render_path: Path | None = None
        # 卡片不发送时连渲染都不做 —— 渲染要么起浏览器要么画图，白做一次很贵
        if self._renderer.enabled and send_card:
            render_path = await self._renderer.render(
                result, cache_key=cache_key, existing=self._render_cache.get(cache_key),
            )
            if render_path is not None:
                self._render_cache[cache_key] = render_path

        # 节点按「文字 / 媒体」切开：仅富媒体要丢掉前者、仅文本要丢掉后者
        text_items, media_items = split_nodes(nodes_content, self._is_media_item)
        header_text = header if send_text else ""
        if not send_text:
            text_items = []
        if not send_media:
            media_items = []

        if render_path is not None:
            await self._send_image(event, render_path)
            # 卡片已承载标题/作者/统计/时长与图片，视频场景不再重复发节点
            if result.video_contents:
                header_text = ""
                text_items = []
                media_items = []
        elif text_items:
            # 卡片不在场时「少了几张图」只能靠这条说明带出来（卡片在时由卡片承载）
            text_items += [[Comp.Plain(w)] for w in warnings]

        # 两种都发时**按原顺序**整串发（与改动前逐字节一致），否则只发留下的那一类
        items = pick_items(
            nodes_content,
            text_items,
            media_items,
            keep_text=bool(text_items),
            keep_media=bool(media_items),
        )

        if items:
            # 只有文字才值得合并转发；纯媒体（仅富媒体）直发图片本体 ——
            # 合并转发在群里长得又是一张「聊天记录卡片」，那正是用户要躲开的东西
            if self._is_onebot(event) and text_items:
                yield await self._build_nodes_result(event, header_text, items)
            else:
                async for r in self._send_plain_output(event, header_text, items):
                    yield r

        if not send_media:
            logger.debug("[denia_share] 发送模式不含视频 / 音频，跳过媒体发送")
            return

        async for r in self._try_send_media(event, result):
            yield r

    def _remember_result(self, cache_key: str, result: ParseResult) -> None:
        """写入解析结果内存缓存，并维持条数上限（见 MAX_RESULT_CACHE_ENTRIES）。"""
        cache = self._result_cache
        cache[cache_key] = result
        # dict 保持插入序：超限就丢最早写入的（FIFO 够用，这里只是防无界增长）
        while len(cache) > MAX_RESULT_CACHE_ENTRIES:
            cache.pop(next(iter(cache)), None)

    # ==================== 解析记录 ====================

    async def _record_history(
        self,
        result: ParseResult,
        render_path: Path | None,
        *,
        via: str = "auto",
        elapsed_ms: int | None = None,
    ) -> ParseRecord | None:
        """把一次成功的解析写入历史，供 WebUI 查看与管理。

        这是纯旁路：任何异常都只记日志，绝不影响正常的解析与发送。
        """
        try:
            media_files: list[str] = []
            for cont in result.contents:
                path = cont.path_task.resolved
                if path is not None:
                    media_files.append(path.name)

            record = ParseRecord.create(
                url=result.url or "",
                platform=result.platform.name,
                # 记录页是管理视图，用**正式名**：页面上的平台筛选与统计都是正式名，
                # 行内再显示「猴山」这种卡片叫法就会出现同一平台两个名字并排
                platform_display=platform_meta(result.platform.name).display_name,
                content_type=result.content_type,
                title=result.title or "",
                author=result.author.name if result.author else "",
                via=via,
                card_file=render_path.name if render_path else None,
                media_files=media_files,
                detail=self._history_detail(result),
                elapsed_ms=elapsed_ms,
            )
            if not await asyncio.to_thread(self.history.add, record):
                # 写盘失败（Windows 上 os.replace 会撞开着的文件）：记录没能落盘，
                # 不能假装成功 —— 至少要在日志里说清楚，而不是静默少一条。
                logger.warning(
                    "[denia_share] 解析记录未能落盘（记录页可能正被占用），本条记录已丢失"
                )
            return record
        except Exception:
            logger.warning("[denia_share] 写入解析记录失败", exc_info=True)
            return None

    @staticmethod
    def _history_detail(result: ParseResult) -> dict[str, Any]:
        """挑出适合展示在记录详情里的字段（不含媒体本体）。"""
        extra = result.extra or {}
        return {
            "stats_line": extra.get("stats_line") or "",
            "duration": extra.get("duration") or "",
            "online": extra.get("online") or "",
            "info": extra.get("info") or "",
            "text": (result.text or "")[:300],
            "image_count": len(result.img_contents),
            "video_count": len(result.video_contents),
            "audio_count": len(result.audio_contents),
            "warnings": list(extra.get("limit_warnings") or []),
        }

    # ==================== 配置热生效 ====================

    async def apply_runtime_config(self) -> dict[str, Any]:
        """按最新配置重建运行时组件。

        ``ParserConfig`` 直接读取 AstrBotConfig，所以配置值本身是实时的；
        这里负责重建那些「构造时把配置读死」的对象（解析器表、渲染器、截图服务、
        下载器参数），让网页端保存后不需要重载插件。
        """
        pconfig = get_config()
        summary: dict[str, Any] = {"rebuilt": False}

        self.disabled_platforms = pconfig.DISABLED_PLATFORMS
        self._send_errors = pconfig.SEND_ERROR_MESSAGES

        # 共享缓存目录可能被改了：换目录后旧的内存缓存全部失效
        new_cache_dir, source = self._resolve_cache_dir(
            self._data_dir, pconfig.CACHE_DIR
        )
        self.cache_dir_source = source
        if new_cache_dir != self.cache_dir:
            self.cache_dir = new_cache_dir
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            pconfig.cache_dir = self.cache_dir
            self._result_cache.clear()
            self._render_cache.clear()
            logger.info(f"[denia_share] 媒体缓存目录已切换到 {self.cache_dir}")
        summary["cache_dir"] = str(self.cache_dir)
        summary["cache_dir_source"] = self.cache_dir_source

        try:
            await self.downloader.reconfigure(
                proxies=pconfig.PROXY or None,
                max_size_mb=pconfig.VIDEO_SIZE_MAXIMUM_MB,
                cache_dir=self.cache_dir,
                verify_ssl=pconfig.HTTP_VERIFY_SSL,
            )
        except Exception:
            logger.warning("[denia_share] 更新下载器参数失败", exc_info=True)

        self.parsers = {}
        self._init_parsers()

        # B站 Cookie **只在配置项真的变了的时候**才动。
        #
        # 为什么不能无条件重新应用：这个方法是「保存配置 / 恢复默认 / 保存外观」
        # 三个接口都会走的（webui.py:293/316/352）。无条件应用会有两种误伤 ——
        #   · 原先「登录态优先」：改 BILI_CK 不生效，接口却回 changed + runtime_ok
        #   · 改成「配置项优先」后：配置项非空时，改任何别的配置（代理/外观）都会
        #     把配置项里的 cookie 再应用一次，覆盖掉期间的扫码登录结果
        # 两种都是「用户没动 cookie，cookie 却被改了」。所以按「变化」触发。
        current_ck = str(pconfig.BILI_CK or "")
        if current_ck != self._bili_ck_applied:
            self._bili_ck_applied = current_ck
            if current_ck:
                self._bili_cookie = current_ck
                self._bili_apply_cookie_to_parser(current_ck)
            else:
                # 配置项被清空 = 用户明确要清掉（「恢复默认」的文案承诺了「包括 Cookie」）。
                # 走 bili_logout 而不是只清配置项：它会把主模块内存、解析器的持久化文件
                # 与 bili_cookie.json 一起清掉，否则「看起来清了、实际还在用」。
                await self.bili_logout()

        self._renderer = ShareCardRenderer(self.cache_dir, **pconfig.renderer_options())
        # 渲染参数进了产物文件名，配置变了就得让旧缓存失效
        self._render_cache.clear()

        # 截图后端的代理 / 证书校验同样是构造时读死的，热更新要一起重建
        self._build_screenshot_service()

        summary["rebuilt"] = True
        summary["platforms"] = sorted(self.parsers)
        summary["render_enabled"] = self._renderer.enabled
        return summary

    # ==================== 网页截图 ====================

    @staticmethod
    def _first_url(event: AstrMessageEvent) -> str | None:
        urls = URL_PATTERN.findall(event.message_str or "")
        return urls[0] if urls else None

    def _match_parser(self, url: str) -> str | None:
        """找出能处理该 URL 的解析器名，没有则返回 None。"""
        for name, parser in self.parsers.items():
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_builtin_parser(self, url: str) -> str | None:
        """该 URL 是否归一个**当前启用**的内置平台；是则返回平台键。

        只在内置平台启用时才算命中：内置平台被禁用后它的处理器会直接 return
        （``_dispatch`` 里 ``self.parsers.get(name)`` 拿不到），这时应当让自定义
        解析器接过去 —— README 承诺过自定义解析器可以「改掉某个内置平台的行为」。
        """
        for name, parser in self.parsers.items():
            if is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return name
            except Exception:
                continue
        return None

    def _match_custom_parser(self, url: str):
        """找出能处理该 URL 的自定义解析器实例；没有则返回 None。"""
        for name, parser in self.parsers.items():
            if not is_custom_platform(name):
                continue
            try:
                parser.search_url(url)
                return parser
            except Exception:
                continue
        return None

    # ==================== 示例卡片（外观页离线预览） ==================== #

    def _sample_image(self, kind: str) -> Path:
        """合成/复用一张离线示例图（渐变 + 几何图形），不联网。"""
        from PIL import Image as _Image, ImageDraw as _Draw

        path = self.cache_dir / f"_sample_{kind}.png"
        if path.is_file():
            return path
        if kind == "hero":
            w, h = 1280, 720
            img = _Image.new("RGB", (w, h))
            top, bottom = (58, 76, 128), (16, 20, 32)
            for yy in range(h):
                t = yy / max(h - 1, 1)
                img.paste(tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                          (0, yy, w, yy + 1))
            dr = _Draw.Draw(img, "RGBA")
            dr.ellipse((int(w * 0.62), -h // 3, int(w * 1.25), h // 2), fill=(139, 124, 246, 90))
            dr.ellipse((-w // 5, int(h * 0.55), w // 3, int(h * 1.4)), fill=(46, 196, 182, 70))
            dr.rounded_rectangle((int(w * 0.08), int(h * 0.6), int(w * 0.5), int(h * 0.78)),
                                 radius=28, fill=(255, 255, 255, 26))
        else:  # avatar
            size = 256
            img = _Image.new("RGB", (size, size), (36, 43, 63))
            dr = _Draw.Draw(img, "RGBA")
            # 椭圆要的是 (x0, y0, x1, y1) 四个坐标，少一个会被 Pillow 判为参数错误
            dr.ellipse((28, 28, size - 28, size - 28), fill=(251, 114, 153, 255))
            dr.ellipse((88, 96, 168, 176), fill=(255, 255, 255, 235))
            dr.arc((64, 150, 192, 236), start=15, end=165, fill=(255, 255, 255, 235), width=14)
        try:
            img.save(path, "PNG")
        except OSError:
            logger.warning("[denia_share] 示例图写入失败", exc_info=True)
        return path

    def build_sample_result(self) -> ParseResult:
        """构造离线示例解析结果（视频卡片：封面 + 头像 + 统计），供外观页实时预览。"""
        from .core.task import PathTask

        async def _static(path: Path) -> Path:
            return path

        hero_path = self._sample_image("hero")
        avatar_path = self._sample_image("avatar")
        video = VideoContent(
            path_task=PathTask(_static(hero_path)),
            cover=PathTask(_static(hero_path)),
            duration=309,
        )
        return ParseResult(
            platform=platform_of(PlatformEnum.BILIBILI),
            author=Author(
                name="希望示例频道",
                avatar=PathTask(_static(avatar_path)),
                description="本卡片为离线示例，用于预览外观效果",
            ),
            title="示例：全新外观设计器效果预览",
            text="这是一条用于预览的示例简介。调整左侧的主题、布局、强调色、水印等参数，"
                 "卡片会立刻按新外观重新渲染，无需发送任何真实链接。",
            timestamp=1760000000,
            url="https://www.bilibili.com/video/BV1uth56uEz3",
            contents=[video],
            extra={
                "content_type": "视频",
                "stats_line": "👍 12.3万 🪙 3.4万 ⭐ 5.6万 👀 210.5万",
                "duration": "05:09",
            },
        )

    async def _do_screenshot(self, event: AstrMessageEvent, url: str):
        # /shot 与「匹配不到平台就截图」两个入口都汇到这里
        if self._access_denied(event):
            return
        if not is_probably_screenshotable(url):
            yield event.plain_result("这个地址不支持截图")
            return
        if not self.screenshot.is_configured:
            yield event.plain_result(
                "截图后端未配置：使用 Cloudflare 时请填写 Account ID 与 API Token"
            )
            return

        path = await self.screenshot.capture(url)
        if path is None:
            detail = self.screenshot.last_error or "未知原因"
            yield event.plain_result(f"截图失败：{detail}\n{url}")
            return

        await self._send_image(event, path)
        yield event.plain_result(f"截图完成 {url}")

    async def _send_image(self, event: AstrMessageEvent, path: Path):
        """发送渲染好的卡片图片。

        OneBot 那边走主动发送是为了绕开事件回复管线（避免被附加「引用回复 / @」）；
        但 QQ 官方 Bot **必须**走 ``event.send``：官方适配器的 ``send_by_session``
        一律不带 msg_id（见 :meth:`_post_official_media` 的说明），会被 QQ 判成
        「主动消息」而回报无权限。
        """
        chain = MessageChain().file_image(str(path))
        try:
            if self._is_qq_official(event):
                await event.send(chain)
                return
            sent = await self.context.send_message(event.unified_msg_origin, chain)
            if not sent:
                logger.warning(f"解析卡片主动发送未找到匹配平台会话: {path.name}")
        except Exception as e:
            logger.warning(f"解析卡片主动发送异常: {e}")

    @staticmethod
    async def _build_nodes_result(event: AstrMessageEvent, header: str, items: list[list]):
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()
        nodes = Comp.Nodes([])

        def _node(content):
            # AstrBot 4.x 的 Node.__init__(content, **_) 会把 uin/name 吞进 **_ 丢弃，
            # 必须构造后赋值属性，否则合并转发里昵称与 QQ 号全空
            node = Comp.Node(content=content)
            node.uin = sender_id
            node.name = sender_name
            return node

        if header:
            nodes.nodes.append(_node([Comp.Plain(header)]))
        for item in items:
            nodes.nodes.append(_node(item))
        return event.chain_result([nodes])

    async def _build_output(self, result: ParseResult) -> tuple[str, list[list]]:
        """统一构建 (标题头, 内容节点列表)。"""
        header = f"希望解析器 | {result.platform.display_name} - {result.content_type}"
        if result.author:
            header += f" @{result.author.name}"

        nodes: list[list] = []
        if result.title:
            nodes.append([Comp.Plain(result.title)])
        if result.text:
            nodes.append([Comp.Plain(result.text[:300])])

        for key, prefix in (
            ("stats_line", ""),
            ("duration", "⏱ 时长："),
            ("online", ""),
            ("info", "📝 "),
        ):
            if value := result.extra.get(key):
                nodes.append([Comp.Plain(f"{prefix}{value}")])

        for image in result.img_contents:
            path = await image.path_task.safe_get()
            if path:
                nodes.append([Comp.Image.fromFileSystem(str(path))])
        for graphic in result.graphics:
            if isinstance(graphic, ImageContent):
                path = await graphic.path_task.safe_get()
                if path:
                    nodes.append([Comp.Image.fromFileSystem(str(path))])
            elif isinstance(graphic, str):
                nodes.append([Comp.Plain(graphic)])

        return header, nodes

    async def _send_plain_output(self, event: AstrMessageEvent, header: str, nodes_content: list[list]):
        """非 OneBot 平台：合并成一条消息链。"""
        parts: list = []
        if header:
            parts.append(Comp.Plain(header + "\n"))

        text_lines: list[str] = []
        for node_content in nodes_content:
            for comp in node_content:
                if isinstance(comp, Comp.Plain):
                    text_lines.append(comp.text)
                elif isinstance(comp, Comp.Image):
                    parts.append(comp)
        if text_lines:
            parts.append(Comp.Plain("\n".join(text_lines)))

        if parts:
            yield event.chain_result(parts)

    async def _try_send_media(self, event: AstrMessageEvent, result: ParseResult):
        """单独发送视频 / 音频。图片已在文本节点中，不重复发送。

        **视频为什么需要特殊处理**：AstrBot 把 ``Video`` 段的 ``file:///...``
        原样交给协议端（图片 / 语音会先转成 base64，所以没这个问题），
        而那个路径是 astrbot 容器内的路径。协议端在另一个容器里读不到，
        视频就发不出去。

        所以开了「媒体中转」就先把文件注册成 ``{回调地址}/api/file/<token>``
        用 URL 发送；没开、注册失败、或地址不合法时，回退成原来的本地文件发送。

        **QQ 官方 Bot 是另一套语义**：它不走协议端读文件，而是由框架把本地文件
        转 base64 / 分片上传给 QQ 服务器，所以本地上传才是默认路径；中转 URL 会
        被 QQ 服务器直接拉取，要求回调地址**公网可达** —— 只有用户显式打开
        「官方 Bot 也走中转链接」时才那么发。
        """
        pconfig = get_config()
        # OneBot：共享目录与「媒体中转」二选一，由 MEDIA_RELAY_ENABLED 决定（默认共享目录）。
        # 官方 Bot 不走这里 —— 它的媒体一律由 _send_official_video 主动发送，
        # 好让失败可见并逐级回退（链接 → 本地 → 文字）。
        relay_enabled = pconfig.MEDIA_RELAY_ENABLED
        callback_base = pconfig.MEDIA_RELAY_CALLBACK_URL
        ttl = pconfig.MEDIA_RELAY_TTL
        is_official = self._is_qq_official(event)

        for cont in result.contents:
            if not isinstance(cont, (VideoContent, AudioContent)):
                continue
            path = await cont.path_task.safe_get()
            if path is None:
                continue

            if isinstance(cont, VideoContent):
                if is_official:
                    # 官方 Bot 一律走这条：链接优先 → 本地（按需分片）→ 文字兜底，
                    # 每一步失败都能被插件看见（见 _send_official_video）
                    async for r in self._send_official_video(
                        event, path, result.url, pconfig
                    ):
                        yield r
                    continue
                url = None
                if relay_enabled:
                    url = await register_file(path, callback_base, ttl)
                    if url is None:
                        logger.info(
                            f"[denia_share] 视频中转不可用，回退本地文件发送: {path.name}"
                        )
                if url:
                    yield event.chain_result([Comp.Video.fromURL(url)])
                else:
                    yield event.chain_result([Comp.Video.fromFileSystem(str(path))])
            else:
                # 语音走 base64，不依赖文件系统，无需中转
                yield event.chain_result([Comp.Record(file=str(path))])

    async def _send_official_video(
        self, event: AstrMessageEvent, path: Path, link: str | None, pconfig: Any
    ) -> AsyncGenerator[MessageEventResult, None]:
        """QQ 官方 Bot 的视频发送：本地（分片）优先 → 链接 → 文字，逐级回退。

        为什么不像 OneBot 那样直接 yield 一个 ``Comp.Video`` 就走人：官方适配器对
        本地文件是框架自己上传，≤10MB 走单次 base64、客户端超时只有 20 秒
        （``BotHttp(timeout=20)``），上行稍慢就「上传文件API返回None」重试 5 次后失败
        —— 而这个失败发生在框架的发送阶段，插件原本看不见、也补救不了。
        改用主动发送（``send_message``）后异常会回到这里，于是能补救。

        **为什么默认是本地分片而不是链接**（实测结论，见 README）：
        AstrBot 的 ``file_token_service`` 是**一次性**的 —— ``handle_file`` 取一次
        就把 token ``pop`` 掉。QQ 拉取只要有一次没拉完（访问日志里那次是 8.89MB 拉到
        4.68MB 就断），框架的后续重试全部 404，整条链接路径必然失败；
        而分片上传是把文件直接推进 QQ 的预签名 COS 地址，没有一次性状态，
        单请求预算还有 300 秒、4 路并发。所以：

        1. **本地优先**：≤10MB 先补位到略超 10MB 走分片（``pad_to_chunked_upload``），
           单发失败就补位重试一次并把「本机单发不可靠」记下来；
        2. 本地两次都不行 → 链接兜底（若解析得到回调地址）；
        3. 仍然失败 → 发一条带原链接的文字，别让用户什么都收不到。
        """

        async def _try_local() -> bool:
            send_path = await self._prepare_official_video(path, pconfig)
            if await self._post_official_media(
                event, Comp.Video.fromFileSystem(str(send_path))
            ):
                return True
            # 单次上传失败 → 补位到分片再来一次，并把「本机单发不可靠」记住
            if send_path == path:
                retry_path = await pad_to_chunked_upload(path)
                if retry_path != path:
                    self._remember_official_single_shot_broken()
                    if await self._post_official_media(
                        event, Comp.Video.fromFileSystem(str(retry_path))
                    ):
                        return True
            return False

        async def _try_relay() -> bool:
            base = resolve_callback_base(pconfig.MEDIA_RELAY_CALLBACK_URL)
            if not base:
                logger.info(
                    f"[denia_share] 官方 Bot 无可用回调地址（想试链接请填「AstrBot 回调地址」）: {path.name}"
                )
                return False
            url = await register_file(path, base, pconfig.MEDIA_RELAY_TTL)
            if not url:
                logger.info(f"[denia_share] 官方 Bot 注册中转链接失败: {path.name}")
                return False
            ok = await self._post_official_media(event, Comp.Video.fromURL(url))
            if ok:
                logger.info(
                    f"[denia_share] 官方 Bot 视频走中转链接（QQ 服务器拉取）: {path.name}"
                )
            else:
                logger.warning(
                    f"[denia_share] 官方 Bot 中转链接不可用（{base}）或 QQ 拉取失败: {path.name}"
                    "（注意 AstrBot 的文件 Token 是一次性的，框架重试必然 404）"
                )
            return ok

        if pconfig.OFFICIAL_RELAY_FIRST:
            logger.info(
                f"[denia_share] 官方 Bot 视频优先试中转链接（配置项「官方 Bot 视频优先用中转链接」已开）: {path.name}"
            )
            ok = await _try_relay() or await _try_local()
        else:
            ok = await _try_local() or await _try_relay()
        if ok:
            return

        if link:
            yield event.plain_result(
                f"⚠️ 视频发送失败（QQ 官方 Bot 上传链路不通），原链接：{link}"
            )
        else:
            yield event.plain_result("⚠️ 视频发送失败（QQ 官方 Bot 上传链路不通）")

    async def _post_official_media(self, event: AstrMessageEvent, comp: Any) -> bool:
        """把单个媒体组件发出去；失败吞掉并留痕，返回是否成功。

        **必须用 ``event.send`` 而不是 ``context.send_message``**：
        官方适配器把 ``_allow_group_proactive_send`` 硬编码成 True
        （``qqofficial_platform_adapter.py:324``），于是 ``send_by_session`` 里
        ``if msg_id and not allow_group_proactive_send`` 永远不成立 —— payload 里
        **从不带 msg_id**（:410），等于一律按「主动消息」发送；而多数机器人没有
        主动消息权限，QQ 直接回 ``ServerError: 主动消息失败, 无权限``
        （实测：分片上传 33 秒成功，最后死在这一步）。

        走 ``event.send`` 才会带上**入站消息的 msg_id** 作为被动回复
        （``qqofficial_message_event.py:215 → _post_send_one``），既合法，
        又能让上传/发送的异常抛回这里由上层补救。
        """
        try:
            await event.send(MessageChain(chain=[comp]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[denia_share] 官方 Bot 媒体发送异常（上传超时 / QQ 侧拒绝都会走这里）",
                exc_info=True,
            )
            return False
        return True

    def _load_official_single_shot_broken(self) -> bool:
        """读取「本机单次 base64 上传不可靠」的记忆；没记录过就按可靠处理。"""
        try:
            raw = json.loads(self._official_upload_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return False
        except Exception:
            logger.warning(
                "[denia_share] 读取官方 Bot 上传状态失败，本次按未失败处理", exc_info=True
            )
            return False
        return bool(raw.get("single_shot_broken"))

    def _remember_official_single_shot_broken(self) -> None:
        """记下「单次上传不可靠」，之后直接走分片（写入数据目录，重启仍生效）。"""
        if self._official_single_shot_broken:
            return
        self._official_single_shot_broken = True
        logger.warning(
            "[denia_share] 观测到官方 Bot 单次 base64 上传失败，"
            "之后的小视频将直接走分片上传（可用「官方 Bot 分片上传起点」手动调）"
        )
        try:
            self._official_upload_file.write_text(
                json.dumps({"single_shot_broken": True}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[denia_share] 保存官方 Bot 上传状态失败", exc_info=True)

    async def _prepare_official_video(self, path: Path, pconfig: Any) -> Path:
        """QQ 官方 Bot 本地视频的发送前处理：先诊断，再按需补位到分片上传。

        框架走哪条上传路径**只由文件体积决定**，而两条路的时间预算差 15 倍
        （单次 base64 客户端 20s / 分片单请求 300s），所以这里把体积与预测路径
        写进日志：下次出现「上传文件API返回None」时，一眼就知道落在哪条路上。
        详见 ``core.media_utils.pad_to_chunked_upload``。
        """
        try:
            size = path.stat().st_size
        except OSError:
            return path

        size_mb = size / 1024 / 1024
        threshold = QQ_CHUNKED_UPLOAD_THRESHOLD_BYTES
        pad_min_mb = pconfig.OFFICIAL_CHUNK_PAD_MIN_MB

        if size > threshold:
            logger.info(
                f"[denia_share] 官方 Bot 视频 {size_mb:.1f}MB → 框架走分片上传"
                f"（5MB/片、4 路并发、单请求 300 秒）: {path.name}"
            )
            return path

        if should_pad_for_chunked(
            size,
            pad_min_mb=pad_min_mb,
            single_shot_broken=self._official_single_shot_broken,
            threshold=threshold,
        ):
            return await pad_to_chunked_upload(path, threshold=threshold)

        # 单次 base64 的 body ≈ 体积 × 4/3；框架客户端超时 20 秒 → 反推所需上行
        need_mbps = size * 4 / 3 * 8 / 20 / 1024 / 1024
        # 日志用 %s 拼消息体而不是 f-string 里的长表达式，保持与仓库其它日志一致
        logger.info(
            "[denia_share] 官方 Bot 视频 %.1fMB → 框架走单次 base64 上传"
            "（客户端超时 20 秒，需要约 %.1f Mbps 上行；超时会重试 5 次后失败）: %s",
            size_mb, need_mbps, path.name,
        )
        if size_mb >= 5:
            logger.warning(
                "[denia_share] 该视频已接近单次上传的 20 秒超时边界：若日志出现"
                "「上传文件API返回None」，把「官方 Bot 分片上传起点」设成不大于 %.0f"
                " 即可改走分片上传（一次失败后本插件也会自动切到分片）",
                size_mb,
            )
        return path

    # ==================== B站扫码登录 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_qr_login(self, event: AstrMessageEvent):
        """B站扫码登录，Cookie 会持久化保存"""
        sender_id = event.get_sender_id()
        if sender_id in self._bili_login_tasks and not self._bili_login_tasks[sender_id].done():
            yield event.plain_result("⏳ 你有一个正在进行的扫码登录，请先完成或等待超时")
            return

        try:
            qrcode_url, qrcode_key = await self.bili_qr_create()

            try:
                qr_image = qrcode.make(qrcode_url)
            except Exception:
                yield event.plain_result("❌ 二维码生成失败，请检查是否已安装 qrcode 库")
                return

            qr_path = self._bili_data_dir / f"qrcode_{sender_id}.png"
            qr_image.save(str(qr_path), "PNG")
            yield event.image_result(str(qr_path))
            yield event.plain_result(
                "📱 请使用 **B站App** 扫描上方二维码\n⏱️ 有效期约3分钟\n📋 扫码后请在手机上点击「确认登录」"
            )

            self.bili_login_start(
                sender_id, qrcode_key, notify_umo=event.unified_msg_origin, qr_path=qr_path
            )
        except ParseException as e:
            yield event.plain_result(f"❌ {e.message}")
        except Exception as e:
            logger.exception("扫码登录出错")
            yield event.plain_result(f"❌ 生成二维码失败: {e}")

    @filter.command("bili_check")
    async def bili_check_cookie(self, event: AstrMessageEvent):
        """检测B站Cookie是否有效"""
        if not self._bili_cookie:
            yield event.plain_result("⚠️ 尚未配置B站Cookie，请使用 /bili_login 扫码登录")
            return
        result = await self._bili_check_cookie_valid()
        if result["valid"]:
            yield event.plain_result(
                f"✅ B站Cookie有效\n用户: {result.get('username', '未知')}\nUID: {result.get('uid', 0)}"
            )
        else:
            yield event.plain_result(f"❌ B站Cookie失效\n错误: {result.get('error', '未知错误')}")

    # ---------- 扫码登录：WebUI 与聊天命令共用的实现 ----------

    async def bili_qr_create(self) -> tuple[str, str]:
        """向 B站申请扫码登录二维码。

        Returns:
            (扫码 URL, qrcode_key)

        Raises:
            ParseException: 申请失败或返回内容不完整。
        """
        if not self._bili_http_session or self._bili_http_session.closed:
            self._bili_http_session = aiohttp.ClientSession()

        async with self._bili_http_session.get(
            BILI_QR_GENERATE_URL, headers=self._bili_headers(),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json()

        if data.get("code") != 0:
            raise ParseException(f"获取二维码失败: {data.get('message', '未知错误')}")

        payload = data.get("data") or {}
        qrcode_url = payload.get("url")
        qrcode_key = payload.get("qrcode_key")
        if not qrcode_url or not qrcode_key:
            raise ParseException("B站未返回二维码地址或 qrcode_key")
        return str(qrcode_url), str(qrcode_key)

    @staticmethod
    def render_qr_png(data: str) -> bytes:
        """把文本渲染成 PNG 字节，供 WebUI 拼成 data URL 展示。"""
        buffer = io.BytesIO()
        qrcode.make(data).save(buffer, "PNG")
        return buffer.getvalue()

    def bili_login_state(self, task_id: str) -> dict[str, Any]:
        """读取扫码登录任务的当前状态（WebUI 轮询用）。"""
        state = self._bili_login_states.get(task_id)
        if state is None:
            return {"state": "unknown", "message": "登录任务不存在或已结束"}
        return dict(state)

    def bili_login_start(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ) -> None:
        """启动后台轮询任务。

        Args:
            task_id: 任务标识，聊天命令用发送者 ID，WebUI 用随机串。
            qrcode_key: ``bili_qr_create`` 返回的轮询凭据。
            notify_umo: 需要把登录结果发到聊天时填会话标识，WebUI 场景传 None。
            qr_path: 临时二维码文件，任务结束时删除（WebUI 不落盘，传 None）。
        """
        existing = self._bili_login_tasks.get(task_id)
        if existing is not None and not existing.done():
            return

        import time as _time

        self._bili_login_states[task_id] = {
            "state": "waiting",
            "message": "等待扫码",
            "username": "",
            "uid": 0,
            "started_at": _time.time(),
            "expires_in": QR_CODE_EXPIRE_TIME,
        }
        self._bili_login_tasks[task_id] = asyncio.create_task(
            self._bili_poll_qr_login(
                task_id, qrcode_key, notify_umo=notify_umo, qr_path=qr_path
            )
        )

    async def bili_cookie_status(self) -> dict[str, Any]:
        """检查当前 Cookie 是否仍有效。

        会真实请求 B站接口，仅在用户主动点「检测」时调用，不要放进总览的自动刷新里。
        """
        if not self._bili_cookie:
            return {"configured": False, "valid": False, "error": "尚未配置 Cookie"}
        result = await self._bili_check_cookie_valid()
        return {
            "configured": True,
            "valid": bool(result.get("valid")),
            "username": result.get("username", ""),
            "uid": result.get("uid", 0),
            "error": result.get("error", ""),
        }

    async def bili_logout(self) -> bool:
        """清除本地保存的 B站 Cookie（不会撤销 B站侧的登录态）。

        要清三处，少一处都会「看起来清了、实际还在用」：
        主模块的 ``bili_cookie.json``、解析器自己持久化的 ``bilibili_cookies.json``、
        以及配置项 ``BILI_CK``（否则重建解析器时又被读回来）。
        """
        self._bili_cookie = ""
        # 同步「已生效的配置项值」：下面会把配置项也清空，不清这里的话
        # 下一次热更新会以为配置项「没变」而跳过（其实该走的分支已经走完了）
        self._bili_ck_applied = ""

        parser = self.parsers.get("bilibili")
        if parser is not None and hasattr(parser, "clear_cookie"):
            parser.clear_cookie()

        pconfig = get_config()
        try:
            if str(pconfig.BILI_CK or "").strip():
                pconfig.apply_updates({"BILI_CK": ""})
        except Exception:
            logger.warning("[denia_share] 清除配置里的 B站 Cookie 失败", exc_info=True)

        try:
            self._bili_cookie_file.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning("[denia_share] 删除 bili_cookie.json 失败", exc_info=True)
            return False

    # ---------- 内部方法 ----------

    @staticmethod
    def _bili_headers() -> dict:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }

    async def _bili_poll_qr_login(
        self,
        task_id: str,
        qrcode_key: str,
        *,
        notify_umo: str | None = None,
        qr_path: Path | None = None,
    ):
        """轮询扫码结果，成功后写入并持久化 Cookie。

        状态写进 ``self._bili_login_states``，WebUI 通过 ``bili_login_state`` 读取；
        聊天命令则靠 ``notify_umo`` 收结果。两条路径共用这一份实现。
        """
        import time as _time

        state = self._bili_login_states.get(task_id)
        if state is None:
            state = {"state": "waiting", "message": "等待扫码", "username": "", "uid": 0}
            self._bili_login_states[task_id] = state

        start = _time.time()
        try:
            while _time.time() - start < QR_CODE_EXPIRE_TIME:
                if not self._bili_http_session or self._bili_http_session.closed:
                    # 这里 break 会跳过 while 的 else（只有「时间耗尽」才走 else），
                    # 不显式置状态的话 UI 会一直显示「等待扫码」直到 300 秒回收。
                    state["state"] = "failed"
                    state["message"] = "登录会话已关闭，请重新获取二维码"
                    break
                try:
                    async with self._bili_http_session.get(
                        BILI_QR_POLL_URL, params={"qrcode_key": qrcode_key},
                        headers=self._bili_headers(), timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        poll_data = await resp.json()
                        set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                code = poll_data.get("data", {}).get("code", -1)
                if code == QR_CODE_EXPIRED:
                    state["state"] = "expired"
                    state["message"] = "二维码已过期，请重新获取"
                    break
                if code == QR_CODE_SCANNED:
                    state["state"] = "scanned"
                    state["message"] = "已扫码，请在手机上确认登录"
                elif code == QR_CODE_SUCCESS:
                    cookie_dict: dict[str, str] = {}
                    for header in set_cookie_headers:
                        part = header.split(";")[0].strip()
                        if "=" in part:
                            k, v = part.split("=", 1)
                            cookie_dict[k.strip()] = v.strip()
                    if not cookie_dict:
                        state["state"] = "failed"
                        state["message"] = "B站未返回 Cookie，请重试"
                        break
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
                    self._bili_cookie = cookie_str
                    await self._bili_save_cookie(cookie_str)
                    self._bili_apply_cookie_to_parser(cookie_str)
                    result = await self._bili_check_cookie_valid()
                    if result["valid"]:
                        state["state"] = "success"
                        state["username"] = result.get("username", "")
                        state["uid"] = result.get("uid", 0)
                        state["message"] = "登录成功"
                        if notify_umo:
                            await self._bili_notify(
                                notify_umo,
                                f"🎉 登录成功！\n👤 用户: {result.get('username')}\n🆔 UID: {result.get('uid', 0)}",
                            )
                    else:
                        state["state"] = "failed"
                        state["message"] = f"Cookie 已保存但校验失败：{result.get('error', '未知错误')}"
                    break
                await asyncio.sleep(POLL_INTERVAL)
            else:
                state["state"] = "expired"
                state["message"] = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            state["state"] = "cancelled"
            state["message"] = "登录任务已取消"
            raise
        except Exception as e:
            logger.exception("扫码轮询出错")
            state["state"] = "failed"
            state["message"] = f"轮询出错：{str(e)[:120]}"
        finally:
            self._bili_login_tasks.pop(task_id, None)
            # 状态不能跟着一起 pop：UI 还要读最终结果，见 _schedule_login_state_cleanup
            self._schedule_login_state_cleanup(task_id)
            if qr_path is not None:
                self._schedule_qr_cleanup(qr_path)

    def _schedule_login_state_cleanup(self, task_id: str) -> None:
        """登录任务结束后延迟回收状态，并把总量夹在上限内。

        两个理由：一是 WebUI 轮询读取，任务刚结束就删会让最后一轮拿到
        「登录任务不存在」而不是「登录成功」；二是 task_id 在 WebUI 侧是随机串，
        不清就会随点击次数无限增长（原先只 pop 了 ``_bili_login_tasks``，
        状态字典是只进不出的）。
        """
        if len(self._bili_login_states) > LOGIN_STATE_MAX:
            ordered = sorted(
                self._bili_login_states.items(),
                key=lambda kv: kv[1].get("started_at") or 0.0,
            )
            for old_id, _ in ordered[: len(ordered) - LOGIN_STATE_MAX]:
                # old_id == task_id 是「正在收尾的这条」：调用方在 finally 里已经先把
                # 本任务从 _bili_login_tasks 里 pop 掉了，所以下面那条豁免对它无效 ——
                # 一旦它恰好是最旧的一条，最终状态会被自己这次清理删掉，UI 再也读不到
                # 「登录成功」。所以本任务要单独豁免。
                if old_id == task_id or old_id in self._bili_login_tasks:
                    continue  # 本任务与还在跑的任务都不能丢，UI 要读它的进度
                self._bili_login_states.pop(old_id, None)

        async def _delayed_drop() -> None:
            try:
                await asyncio.sleep(LOGIN_STATE_TTL)
                self._bili_login_states.pop(task_id, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("回收扫码登录状态失败", exc_info=True)

        self._delayed_cleanup_tasks = [
            task for task in self._delayed_cleanup_tasks if not task.done()
        ]
        self._delayed_cleanup_tasks.append(asyncio.create_task(_delayed_drop()))

    def _schedule_qr_cleanup(self, qr_path: Path, delay: int = 120) -> None:
        """延迟删除二维码临时文件。

        二维码图片已经 yield 进消息管线，真正读取它的时刻在发送阶段，
        轮询一结束就删会与发送竞争、导致图片发不出去；所以推迟到
        ``delay`` 秒后再清理。顺带回收已完成的旧任务，避免列表无限增长。
        """
        async def _delayed_unlink() -> None:
            try:
                await asyncio.sleep(delay)
                qr_path.unlink(missing_ok=True)
            except Exception:
                logger.debug("清理二维码临时文件失败", exc_info=True)

        self._delayed_cleanup_tasks = [
            task for task in self._delayed_cleanup_tasks if not task.done()
        ]
        self._delayed_cleanup_tasks.append(asyncio.create_task(_delayed_unlink()))

    def _bili_apply_cookie_to_parser(self, cookie_str: str):
        parser = self.parsers.get("bilibili")
        if parser and hasattr(parser, "update_cookie"):
            parser.update_cookie(cookie_str)
            logger.info("B站 Cookie 已应用到解析器")

    async def _bili_check_cookie_valid(self) -> dict:
        if not self._bili_cookie or not self._bili_http_session:
            return {"valid": False, "error": "Cookie 为空或会话未初始化"}
        headers = {**self._bili_headers(), "Cookie": self._bili_cookie}
        try:
            async with self._bili_http_session.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=headers, timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
            if data.get("code") == 0 and data.get("data", {}).get("isLogin"):
                u = data["data"]
                return {"valid": True, "username": u.get("uname", ""), "uid": u.get("mid", 0)}
            return {"valid": False, "error": data.get("message", "未知错误")}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    async def _bili_load_cookie(self):
        # **配置项优先**：BILI_CK 是用户显式填的，持久化文件只是扫码登录的暂存。
        # 取值顺序要和 apply_runtime_config 与 parser._init_credential 保持一致，
        # 否则「网页上改了 BILI_CK」还是会被旧登录态压住。
        configured = str(get_config().BILI_CK or "")
        # 记下启动时生效的配置项值：apply_runtime_config 靠它判断「到底变没变」
        self._bili_ck_applied = configured
        if configured:
            self._bili_cookie = configured
            return
        try:
            if self._bili_cookie_file.exists():
                saved = json.loads(self._bili_cookie_file.read_text(encoding="utf-8")).get("cookie", "")
                if saved:
                    self._bili_cookie = saved
                    logger.info("已从持久化文件加载B站 Cookie")
        except Exception as e:
            logger.warning(f"加载B站 Cookie 失败: {e}")

    async def _bili_save_cookie(self, cookie_str: str):
        try:
            self._bili_cookie_file.write_text(
                json.dumps({"cookie": cookie_str}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"保存B站 Cookie 失败: {e}")

    async def _bili_notify(self, session: str, message: str):
        try:
            # session 期望是 unified_msg_origin（平台:消息类型:会话ID 三段式）；
            # 对历史调用方只传裸 QQ 号的情况做兼容兜底
            umo = session if ":" in session else f"default:FriendMessage:{session}"
            await self.context.send_message(umo, MessageChain().message(message))
        except Exception as e:
            logger.error(f"发送消息到 {session} 失败: {e}")

    # ==================== JSON 卡片工具 ====================

    @staticmethod
    def _has_json_component(event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for c in event.message_obj.message:
            if isinstance(c, Comp.Json):
                return True
            t = c.get("type") if isinstance(c, dict) else getattr(c, "type", None)
            if t and t != "reply" and "json" in str(t).lower():
                return True
        return False

    def _extract_links_from_event(self, event: AstrMessageEvent) -> list[str]:
        links: list[str] = []
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for c in event.message_obj.message:
                if isinstance(c, Comp.Json):
                    links.extend(self._extract_links_from_json(c.data))
                elif isinstance(c, Comp.Plain):
                    links.extend(URL_PATTERN.findall(c.text or ""))
                elif isinstance(c, dict) and "json" in str(c.get("type", "")).lower():
                    links.extend(self._extract_links_from_json(c.get("data", c)))
        links.extend(URL_PATTERN.findall(event.message_str or ""))
        return links

    @staticmethod
    def _extract_links_from_json(data) -> list[str]:
        links: list[str] = []
        try:
            if isinstance(data, str):
                data = json.loads(data)

            def search(obj):
                found = []
                if isinstance(obj, dict):
                    meta = obj.get("meta") or {}
                    if isinstance(meta, dict):
                        for dk in ("detail_1", "detail", "news", "music"):
                            d = meta.get(dk) or {}
                            if isinstance(d, dict):
                                for uk in ("qqdocurl", "url", "jumpUrl"):
                                    v = d.get(uk)
                                    if isinstance(v, str) and v:
                                        found.append(v)
                    for v in obj.values():
                        if isinstance(v, str) and v.startswith(("http://", "https://")):
                            found.append(v)
                        elif isinstance(v, (dict, list)):
                            found.extend(search(v))
                elif isinstance(obj, list):
                    for item in obj:
                        found.extend(search(item))
                return found

            links.extend(search(data))
        except Exception as e:
            logger.warning(f"解析 JSON 消息组件失败: {e}")
        return links

    # ==================== 管理命令 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_status")
    async def denia_status(self, event: AstrMessageEvent):
        """查看希望解析器的运行状态"""
        from . import __version__

        platforms = "、".join(self.parsers) if self.parsers else "（无）"
        backend = self.screenshot.backend
        shot_ready = "已就绪" if self.screenshot.is_configured else "未配置"
        yield event.plain_result(
            f"希望解析器 v{__version__}\n"
            f"已启用平台：{platforms}\n"
            f"卡片渲染：{'开' if self._renderer.enabled else '关'}\n"
            f"B站 Cookie：{'已配置' if self._bili_cookie else '未配置'}\n"
            f"截图后端：{backend}（{shot_ready}）"
            f"{'，链接兜底开' if self._screenshot_fallback else ''}\n"
            f"错误提示：{'开' if self._send_errors else '关'}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("denia_clear")
    async def denia_clear_cache(self, event: AstrMessageEvent):
        """立即清空解析缓存（含渲染卡片与已下载媒体）"""
        from .core.media_utils import clear_cache_dir

        try:
            cleaned = await clear_cache_dir(self.cache_dir)
        except Exception as e:
            yield event.plain_result(f"❌ 清空缓存失败: {str(e)[:120]}")
            return
        self._result_cache.clear()
        self._render_cache.clear()
        yield event.plain_result(f"🧹 已清空 {cleaned} 个缓存文件")

    async def terminate(self):
        if self._cache_cleanup_task is not None and not self._cache_cleanup_task.done():
            self._cache_cleanup_task.cancel()
            try:
                await self._cache_cleanup_task
            except asyncio.CancelledError:
                pass
        await self.downloader.aclose()

        for task in list(self._bili_login_tasks.values()):
            if not task.done():
                task.cancel()
        self._bili_login_tasks.clear()

        for task in list(self._delayed_cleanup_tasks):
            if not task.done():
                task.cancel()
        self._delayed_cleanup_tasks.clear()

        if self._bili_http_session and not self._bili_http_session.closed:
            await self._bili_http_session.close()

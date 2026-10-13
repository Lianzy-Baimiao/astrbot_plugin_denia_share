# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""BaseParser 基类 - 所有平台解析器的基类"""

import asyncio
import re
from abc import ABC
from pathlib import Path
from re import Match, Pattern, compile
from typing import Any, ClassVar, cast, final
from typing_extensions import Unpack

from .share_text import source_url
from .data import Platform, ParseResult, Author, VideoContent, ImageContent, AudioContent, ParseResultKwargs
from .task import PathTask
from .download import StreamDownloader
from .constants import IOS_HEADER, COMMON_HEADER, ANDROID_HEADER, COMMON_TIMEOUT, PlatformEnum
from .exception import ParseException, IgnoreException, DownloadException, SilentException

HandlerFunc = Any
KeyPatterns = list[tuple[str, Pattern[str]]]

_KEY_PATTERNS = "_key_patterns"

# 单条解析结果里最多下载多少张图。
#
# 远端返回的图集长度完全不受控（微博长文、抖音图文、NGA 帖），而 create_images
# 会对每个 URL 立刻 create_task，数量等于远端给的数量。渲染端最多只画 6 张
# （card_renderer 的图集网格上限 3×2），但聊天侧会把每张图都发出去，
# 所以不能按渲染端上限截断，只能设一个覆盖真实场景的宽松上限。
MAX_IMAGES_PER_RESULT = 50


def _retrieve_task_exception(task: "asyncio.Task") -> None:
    """把已结束任务的异常「取回」一次，避免无人 await 时 asyncio 打整段堆栈。

    ``Task`` 的异常只有在被 ``await`` / ``exception()`` 取回后才算已处理；
    ``PathTask`` 包出来的任务**有可能永远没人 await**（结果在交付前就被丢掉），
    这时 asyncio 会把异常打成「Task exception was never retrieved」+ traceback。

    取回**不改变语义**：之后 ``await`` 同一个任务照样抛原异常。
    只对「异常本身是设计好的信号（如按策略跳过）」的任务用，别拿它掩盖真故障 ——
    真故障的留痕在 ``PathTask.safe_get`` 与缺料审计里。
    """
    if not task.cancelled():
        task.exception()


def handle(keyword: str, pattern: str):
    """注册处理器装饰器"""
    def decorator(func):
        if not hasattr(func, _KEY_PATTERNS):
            setattr(func, _KEY_PATTERNS, [])
        key_patterns: KeyPatterns = getattr(func, _KEY_PATTERNS)
        key_patterns.append((keyword, compile(pattern)))
        return func
    return decorator


class BaseParser:
    platform: ClassVar[Platform]
    _registry: ClassVar[list[type["BaseParser"]]] = []

    def __init__(self, downloader: StreamDownloader):
        self.headers = COMMON_HEADER.copy()
        self.ios_headers = IOS_HEADER.copy()
        self.android_headers = ANDROID_HEADER.copy()
        self.downloader = downloader
        self.timeout = COMMON_TIMEOUT
        # 全局代理：自建 AsyncClient 的解析器（github/pixiv 等）必须带上，
        # 否则在直连不通的服务器环境里会直接失败
        try:
            from .config import get_config
            self.proxies = get_config().PROXY or None
        except Exception:
            # 不能只静默降级直连：代理配错时表现为「所有平台都解析失败」，
            # 排查方向会被带偏，所以这里至少留一条线索
            from astrbot.api import logger
            logger.warning(
                "读取全局代理配置失败，本次请求将直连（代理配错时会导致解析全部失败）",
                exc_info=True,
            )
            self.proxies = None

    @staticmethod
    def verify_ssl_enabled() -> bool:
        """HTTPS 证书校验开关。读配置失败时按最安全的一侧回退：校验。"""
        try:
            from .config import get_config
            return get_config().HTTP_VERIFY_SSL
        except Exception:
            return True

    @staticmethod
    def global_proxy() -> str | None:
        """全局代理地址（读不到配置就返回 None，由调用方决定怎么兜）。"""
        try:
            from .config import get_config
            return get_config().PROXY or None
        except Exception:
            return None

    @classmethod
    def client_kwargs(cls, **overrides) -> dict:
        """默认 httpx 客户端参数：超时 / 证书校验 / 全局代理。

        给那些**自己 new AsyncClient** 的解析器用（微博 / 小红书 / NGA / AcFun /
        快手 / 抖音）。这些地方原先只传 headers 与 timeout，于是「全局代理」对它们
        完全无效 —— 在直连不通的服务器上表现为「这几个平台解析失败」，而媒体下载
        却是通的（下载器有代理），排查方向很容易被带偏。

        调用方传的键覆盖默认值，所以 ``follow_redirects=False`` 这类特例照常可用。
        """
        kwargs: dict[str, object] = {
            "timeout": COMMON_TIMEOUT,
            "verify": cls.verify_ssl_enabled(),
            # 代理来源有两处：插件配置的 PROXY（下面显式传 proxy）与环境变量
            # （httpx 的 trust_env）。**这里显式写 True 并要求调用方别各写各的**：
            # 原先微博/小红书写了 trust_env=False，于是「只在环境变量里配代理」的部署中，
            # 这两个平台会悄悄直连 —— 表现为「就它俩解析失败」，排查时极难想到。
            "trust_env": True,
        }
        if proxy := cls.global_proxy():
            kwargs["proxy"] = proxy
        kwargs.update(overrides)
        return kwargs

    def new_client(self, **kwargs) -> "AsyncClient":
        """创建带代理与默认超时的 httpx 客户端。"""
        from httpx import AsyncClient
        merged = self.client_kwargs(
            timeout=self.timeout,
            follow_redirects=True,
            verify=self.verify_ssl_enabled(),
        )
        if self.proxies:
            merged["proxy"] = self.proxies
        merged.update(kwargs)
        return AsyncClient(**merged)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if ABC not in cls.__bases__:
            BaseParser._registry.append(cls)

        cls._handlers = {}
        cls._key_patterns = []

        for attr_name in dir(cls):
            attr = getattr(cls, attr_name)
            if callable(attr) and hasattr(attr, _KEY_PATTERNS):
                key_patterns: KeyPatterns = getattr(attr, _KEY_PATTERNS)
                for keyword, pattern in key_patterns:
                    # **按 pattern 索引，不能按 keyword**。同一个 keyword 下允许注册
                    # 多条 pattern（小红书 xiaohongshu.com 既有 explore/discovery 的
                    # 带 id 链接，又有安全落地页的 originalUrl 链接）。按 keyword 索引
                    # 时后遍历到的 handler 会覆盖先遍历到的，而 search_url 返回的是
                    # 「先匹配上的那条 pattern」—— 两者不是同一个 handler 时就会出现
                    # 「A 的 pattern 匹配上、却派发到 B」，B 里 group("...") 直接
                    # IndexError: no such group。按 pattern 索引让匹配与派发一一对应。
                    cls._handlers[pattern] = attr
                    cls._key_patterns.append((keyword, pattern))

        cls._key_patterns.sort(key=lambda x: -len(x[0]))

    @classmethod
    def get_all_subclass(cls) -> list[type["BaseParser"]]:
        return cls._registry

    @final
    async def parse(self, keyword: str, searched: Match[str]) -> ParseResult:
        # 用**匹配上的那条 pattern** 定位 handler，而不是 keyword —— 同一个 keyword
        # 可能对应多个 handler（见 __init_subclass__ 里的说明）。keyword 只用来报错。
        handler = self._handlers.get(searched.re)
        if handler is None:
            raise SilentException(f"无法匹配 {keyword}")
        result = await handler(self, searched)
        if not result.url:
            result.url = source_url(searched)
        return result

    @final
    async def parse_with_redirect(self, url: str, headers: dict[str, str] | None = None) -> ParseResult:
        redirect_url = await self.get_redirect_url(url, headers=headers or self.headers)
        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")
        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    @classmethod
    def search_url(cls, url: str) -> tuple[str, Match[str]]:
        for keyword, pattern in cls._key_patterns:
            if keyword not in url:
                continue
            if searched := pattern.search(url):
                return keyword, searched
        raise SilentException(f"无法匹配 {url}")

    @classmethod
    def result(cls, **kwargs: Unpack[ParseResultKwargs]) -> ParseResult:
        return ParseResult(platform=cls.platform, **kwargs)

    @staticmethod
    async def get_redirect_url(url: str, headers: dict[str, str] | None = None) -> str:
        from httpx import AsyncClient
        headers = headers or COMMON_HEADER.copy()
        async with AsyncClient(
            **BaseParser.client_kwargs(
                headers=headers,
                follow_redirects=False,
            )
        ) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                response.raise_for_status()
            return response.headers.get("Location", url)

    @staticmethod
    async def get_final_url(url: str, headers: dict[str, str] | None = None) -> str:
        from httpx import AsyncClient
        headers = headers or COMMON_HEADER.copy()
        async with AsyncClient(
            **BaseParser.client_kwargs(
                headers=headers,
                follow_redirects=True,
            )
        ) as client:
            response = await client.get(url)
            if response.status_code >= 400:
                response.raise_for_status()
            return str(response.url)

    def create_author(self, name: str, avatar_url: str | None = None, description: str | None = None):
        author = Author(name=name, description=description)
        if avatar_url:
            author.avatar = PathTask(self.downloader.download_img(avatar_url, ext_headers=self.headers))
        return author

    def create_video(self, url_or_task: str | asyncio.Task[Path] | PathTask, cover_url: str | None = None, duration: float | None = None, is_gif: bool = False):
        if duration is not None:
            from .config import get_config
            from .media_utils import fmt_duration
            pconfig = get_config()
            if duration > pconfig.VIDEO_DURATION_MAXIMUM:
                from astrbot.api import logger
                logger.warning(
                    f"视频时长 {fmt_duration(duration)} "
                    f"超过限制 {fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)}, 跳过下载"
                )
                # 提前返回前取消已调度的下载任务，避免后台协程跑完整个下载而无人回收
                if isinstance(url_or_task, asyncio.Task):
                    url_or_task.cancel()
                # 模仿 B站：创建一个 download_video 闭包，被 await 时抛出 IgnoreException
                # 这样解析结果能正常返回（标题/作者/封面），但实际不下载视频
                async def _skip_video_download():
                    raise IgnoreException(
                        f"视频时长({fmt_duration(duration)})超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，跳过下载"
                    )
                # 包成 Task 并**立刻取回一次异常**：这是「按策略跳过」的**信号**，
                # 不是故障。结果若在交付前就被丢掉（解析器随后抛了别的异常、
                # 三级兜底换了路径），就没有人 await 它，asyncio 会把这个设计好的
                # 信号打成「Task exception was never retrieved」+ 整段堆栈
                # （自检里已现过一次）。取回不影响语义：后面 await 它照样抛。
                path_task = asyncio.create_task(_skip_video_download())
                path_task.add_done_callback(_retrieve_task_exception)
                video_content = VideoContent(PathTask(path_task), duration=duration, is_gif=is_gif)
                if cover_url:
                    video_content.cover = PathTask(
                        self.downloader.download_img(cover_url, ext_headers=self.headers)
                    )
                return video_content

        # 统一先包成 PathTask：抽封面 / 转 GIF 与内容本体都要 await 同一个任务，
        # 直接 await 裸协程会导致重复 await 报错（上游 rika 2026-09 修复）
        if isinstance(url_or_task, str):
            path_task = PathTask(self.downloader.download_video(url_or_task, ext_headers=self.headers))
        elif isinstance(url_or_task, PathTask):
            path_task = url_or_task
        else:
            path_task = PathTask(url_or_task)

        video_content = VideoContent(path_task, duration=duration, is_gif=is_gif)

        if cover_url:
            cover_task = self.downloader.download_img(cover_url, ext_headers=self.headers)
        else:
            async def extract_cover():
                """从视频里抽第一帧当封面；失败返回 ``None``，**不向上抛**。

                这个协程有可能**没有人 await**（视频被策略跳过、结果在交付前就
                被丢弃、解析器随后抛了别的异常），异常若裸着出去就会以
                「Task exception was never retrieved」+ 一整段 ffmpeg stderr 的
                形态落进日志（自检里已经现过一次）。这里收口成 ``None``：
                与「封面没落盘」的既有语义一致（缺料审计照常记一笔「封面」），
                只是把不可读的噪音换成一条能定位的 warning。
                """
                from astrbot.api import logger
                from .media_utils import extract_video_first_frame

                try:
                    video_path = await path_task.get()
                except Exception as exc:  # noqa: BLE001 —— 视频本身就不可用
                    logger.warning(
                        f"抽取视频封面失败（视频不可用）: {type(exc).__name__}: {exc}"
                    )
                    return None
                try:
                    return await extract_video_first_frame(video_path)
                except Exception as exc:  # noqa: BLE001 —— ffmpeg 失败/未安装
                    logger.warning(f"抽取视频封面失败: {type(exc).__name__}: {exc}")
                    return None
            cover_task = extract_cover()

        video_content.cover = PathTask(cover_task)

        # 这里原有一段 `if is_gif: 生成 gif_path` 的自动转换，已删除：
        # 全仓没有任何地方读 gif_path，ffmpeg 每次都白跑一遍、产物没人取用，
        # 而它唯一的调用者（抖音图文帖）正是「图集被当成视频」那个 bug 的来源。
        # is_gif 标记本身保留 —— Twitter 用它表达「这是动图，不算视频作品」。
        return video_content

    def _add_limit_warning(self, result: ParseResult, duration: float | None):
        """检查视频时长，超限时添加 limit_warnings 到 result.extra（参考 B站实现）"""
        if duration is not None:
            from .config import get_config
            from .media_utils import fmt_duration
            pconfig = get_config()
            if duration > pconfig.VIDEO_DURATION_MAXIMUM:
                from astrbot.api import logger
                msg = (
                    f"⚠️ 视频时长({fmt_duration(duration)})"
                    f"超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，不会下载视频"
                )
                logger.warning(msg)
                result.extra.setdefault("limit_warnings", []).append(msg)

    def create_images(self, image_urls: list[str]):
        """把一批图片 URL 转成 ImageContent（每个 URL 会立刻起一条下载任务）。

        超过 :data:`MAX_IMAGES_PER_RESULT` 的部分直接丢弃 —— 远端列表长度不可控，
        不截断的话并发下载数与磁盘占用都跟着远端走。
        """
        contents: list[ImageContent] = []
        for url in self.cap_image_urls(image_urls):
            task = self.downloader.download_img(url, ext_headers=self.headers)
            contents.append(ImageContent(PathTask(task)))
        return contents

    @staticmethod
    def cap_image_urls(image_urls: list[str]) -> list[str]:
        """截断远端图集长度，超限时记一条警告。"""
        if len(image_urls) <= MAX_IMAGES_PER_RESULT:
            return image_urls
        from astrbot.api import logger
        logger.warning(
            f"图集条目 {len(image_urls)} 超过上限 {MAX_IMAGES_PER_RESULT}，"
            f"只处理前 {MAX_IMAGES_PER_RESULT} 张"
        )
        return image_urls[:MAX_IMAGES_PER_RESULT]

    def create_image(self, url_or_task: str | asyncio.Task[Path], alt: str | None = None):
        if isinstance(url_or_task, str):
            path_task = self.downloader.download_img(url_or_task, ext_headers=self.headers)
        else:
            path_task = url_or_task
        return ImageContent(PathTask(path_task), alt=alt)

    def create_audio(self, url_or_task: str | asyncio.Task[Path], duration: float = 0.0):
        if isinstance(url_or_task, str):
            path_task = self.downloader.download_audio(url_or_task, ext_headers=self.headers)
        else:
            path_task = url_or_task
        return AudioContent(PathTask(path_task), duration)

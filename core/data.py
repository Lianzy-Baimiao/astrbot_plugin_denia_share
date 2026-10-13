# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""数据模型 - 解析结果和媒体内容定义"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, TypedDict
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass
from collections.abc import Iterator, Awaitable, Callable

from astrbot.api import logger

from .task import PathTask
from .constants import PlatformEnum, platform_meta
from .exception import IgnoreException


@dataclass(repr=False, slots=True)
class MediaContent:
    path_task: PathTask

    async def get_path(self) -> Path:
        return await self.path_task.get()

    def __repr__(self) -> str:
        prefix = self.__class__.__name__
        return f"{prefix}({self.path_task})"


@dataclass(repr=False, slots=True)
class AudioContent(MediaContent):
    """音频内容"""
    duration: float | None = None


@dataclass(repr=False, slots=True)
class VideoContent(MediaContent):
    """视频内容"""
    cover: PathTask | None = None
    duration: float | None = None
    # 标记「这是一段动图」。Twitter 用它表达「动图不算视频作品」——
    # ``ParseResult.video`` 会跳过它，于是卡片按图文处理。
    # （原有一个 gif_path 字段用于存放 ffmpeg 转出的 GIF，全仓无读取点，已删除）
    is_gif: bool = False

    def __repr__(self) -> str:
        repr = f"VideoContent({self.path_task}"
        if self.cover is not None:
            repr += f", cover={self.cover}"
        if self.duration:
            repr += f", duration={self.duration}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ImageContent(MediaContent):
    """图片内容"""
    alt: str | None = None


@dataclass(slots=True)
class Platform:
    name: str
    display_name: str


def platform_of(key: str | PlatformEnum) -> Platform:
    """按平台键构造 ``Platform``。

    展示名一律取自 ``constants.PLATFORMS`` —— 那是平台名的唯一真相。
    **不要在解析器或 main.py 里再手写展示名字面量**：以前那样散着写，
    改一个名字要翻三四处，漏一处就出现「卡片写哔哩哔哩、列表写 B站」，
    而且不会有任何报错。
    """
    meta = platform_meta(key)
    return Platform(name=meta.key, display_name=meta.label)


@dataclass(repr=False, slots=True)
class Author:
    name: str
    avatar: PathTask | None = None
    description: str | None = None

    def __repr__(self) -> str:
        repr = f"Author(name={self.name}"
        if self.avatar:
            repr += f", avatar={self.avatar}"
        if self.description:
            repr += f", description={self.description}"
        return repr + ")"


@dataclass(repr=False, slots=True)
class ParseResult:
    """完整的解析结果"""
    platform: Platform
    author: Author | None = None
    title: str | None = None
    text: str | None = None
    timestamp: int | None = None
    url: str | None = None
    contents: list[MediaContent] = field(default_factory=list)
    graphics: list[str | ImageContent] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    repost: ParseResult | None = None
    render_image: Path | None = None
    # 非必要的卡片信息（如 B站 AI 总结、在线人数）延后加载，不阻塞基础解析/文字。
    card_details_loader: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _card_details_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    CARD_DETAILS_TIMEOUT: ClassVar[float] = 5.0

    async def prepare_card_details(self) -> None:
        """仅渲染时按需补充卡片字段；同一结果只加载一次，失败/超时不阻断发送。"""
        if self.card_details_loader is None:
            return
        if self._card_details_task is None:
            loader = self.card_details_loader

            async def load() -> None:
                try:
                    await asyncio.wait_for(loader(), timeout=self.CARD_DETAILS_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.warning("[denia_share] 卡片附加信息获取超时，使用已有信息继续渲染")
                except Exception as error:
                    logger.warning(
                        f"[denia_share] 卡片附加信息获取失败（{type(error).__name__}），继续渲染"
                    )

            self._card_details_task = asyncio.create_task(load())
        # 一个会话取消不能把另一个会话共用的补充信息任务一起取消；任务本身有上限。
        await asyncio.shield(self._card_details_task)

    @property
    def header(self) -> str | None:
        header = self.platform.display_name
        if self.author:
            header += f" @{self.author.name}"
        if self.title:
            header += f" | {self.title}"
        return header

    @property
    def display_url(self) -> str | None:
        return f"链接: {self.url}" if self.url else None

    @property
    def repost_display_url(self) -> str | None:
        return f"原帖: {self.repost.url}" if self.repost and self.repost.url else None

    @property
    def extra_info(self) -> str | None:
        return self.extra.get("info")

    @property
    def video(self) -> VideoContent | None:
        """结果里的首个视频内容（GIF 不算）。

        不能要求 ``len(contents) == 1``：同一条作品可以既有视频又有图集。
        推特的三条取流路径（``media_extended`` / ``media.videos`` / GraphQL
        ``media``）都是把视频和图片**一起** append 进 contents，微博
        ``_collect_result`` 也是两个独立的 if。要求长度为 1 会让这类作品
        拿不到封面 hero，而 ``skip_text`` 又因为 video_contents 非空把正文掐掉
        —— 卡片既没封面也没文字。
        """
        for cont in self.contents:
            if isinstance(cont, VideoContent) and not cont.is_gif:
                return cont
        return None

    @video.setter
    def video(self, video: VideoContent | None):
        """放入视频内容。

        放在最前面，这样「视频当封面 hero」的直觉与 ``video`` 属性的取值一致。
        旧实现是「contents 非空就静默什么都不做」——调用方察觉不到，视频就这么丢了。
        """
        if video is not None:
            self.contents.insert(0, video)

    @property
    def video_contents(self) -> list[VideoContent]:
        return [cont for cont in self.contents if isinstance(cont, VideoContent)]

    @property
    def img_contents(self) -> list[ImageContent]:
        return [cont for cont in self.contents if isinstance(cont, ImageContent)]

    @property
    def audio_contents(self) -> list[AudioContent]:
        return [cont for cont in self.contents if isinstance(cont, AudioContent)]

    @property
    def all_grid_images(self):
        covers: list[PathTask] = []
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                if cont.cover is not None:
                    covers.append(cont.cover)
            elif isinstance(cont, ImageContent):
                covers.append(cont.path_task)
        return covers

    @property
    def grid_medias(self):
        return [cont for cont in self.contents if isinstance(cont, (VideoContent, ImageContent))]

    @property
    def formartted_datetime(self) -> str | None:
        """发布时间格式化为本地时间字符串（无参数属性版）。"""
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def _iterate_download_coros(self, img_only: bool = False) -> Iterator[Awaitable[Path | None]]:
        if author := self.author:
            if author.avatar:
                yield author.avatar.get()
        for cont in self.contents:
            if not img_only or isinstance(cont, ImageContent):
                yield cont.path_task.get()
            if isinstance(cont, VideoContent) and cont.cover:
                yield cont.cover.get()
        for gra in self.graphics:
            if isinstance(gra, ImageContent):
                yield gra.path_task.get()
        if self.repost is not None:
            yield from self.repost._iterate_download_coros(img_only)

    async def ensure_downloads_complete(self, *, img_only: bool = False, suppress_errors: bool = True) -> None:
        await asyncio.gather(*self._iterate_download_coros(img_only), return_exceptions=suppress_errors)

    # 缺料提示里的量词：中文里「3 张图片」「1 个视频」比「3 图片」顺口
    _MEDIA_UNITS: ClassVar[dict[str, str]] = {
        "图片": "张", "封面": "张", "视频": "个", "音频": "个",
    }

    # 上一次审计写进 limit_warnings 的那条原文（存进 extra）。
    # 有了它才能做到「重复审计不叠加」——见 audit_missing_media。
    _AUDIT_WARNING_KEY: ClassVar[str] = "_missing_media_warning"

    def _iter_media_tasks(self) -> Iterator[tuple[str, PathTask]]:
        """遍历所有需要落盘的媒体任务，附带用途标签（供缺料审计用）。

        **不含头像**：头像只是装饰，缺了不影响内容完整性；把它算进缺料，会让
        「头像恰好挂了一次」也弹一条「1 个头像下载失败，未包含在本次内容中」——
        措辞也不对，头像本来就不是「内容」。头像的失败由 ``safe_get`` 的告警日志留痕。
        """
        for cont in self.contents:
            if isinstance(cont, VideoContent):
                yield "视频", cont.path_task
                if cont.cover is not None:
                    yield "封面", cont.cover
            elif isinstance(cont, AudioContent):
                yield "音频", cont.path_task
            else:
                yield "图片", cont.path_task
        for gra in self.graphics:
            if isinstance(gra, ImageContent):
                yield "图片", gra.path_task
        if self.repost is not None:
            yield from self.repost._iter_media_tasks()

    async def audit_missing_media(self) -> dict[str, int]:
        """结算所有媒体下载，把**真失败**按用途计数写进 ``extra["limit_warnings"]``。

        **为什么要有这个方法**：下载失败原先只在日志里留痕，产出物本身却在撒谎
        —— 少了 3 张图的消息和图一张不缺的消息长得一模一样，用户没法判断该不该
        重试。这里在卡片/媒体发送之前结算，把缺料写进既有的警告通道
        ``limit_warnings``，供卡片和 WebUI 展示；精简文字先发，不等待本方法，
        后发现的缺料不再补发第二条摘要。

        **为什么要把 ``IgnoreException`` 排除在外**：那是本仓「**故意不下载**」的信号
        （视频时长超限、媒体体积超限、0 字节响应、m3u8 分片超限）。这些情况各自已经
        有专门且准确的提示（如 ``_add_limit_warning`` 的「视频时长…不会下载视频」），
        再叠一条「下载失败」就是自相矛盾的两条警告。所以这里把 gather 的返回值与
        任务配对，按异常类型区分「策略跳过」与「真失败」。

        只在确有失败时才追加，成功路径零开销（一次 gather，本来也要等这些任务）。

        **本方法是幂等的**：同一个 ``ParseResult`` 会被结算多次 —— 聊天链路
        （``main._deliver``）发送卡片/媒体时调用，而 ``_result_cache`` 命中的结果会被
        再交付一次（同一条链接在群里发两遍）；WebUI 的「按当前外观重渲染」也会对
        缓存里的**同一个对象**再调一次（``webui._preview_payload``）。不做处理的话
        「⚠️ 3 张图片下载失败」会一次一条越叠越多，而 warnings 还参与产物缓存键，
        于是每重发一次就重渲染一版卡片。所以这里先撤掉自己上一次留下的那条，
        再按本轮结果重新追加 —— **只动自己那条**，B站受限说明、时长超限这些
        别人写的警告一律不碰。

        Returns:
            各用途的失败数量，如 ``{"图片": 3, "视频": 1}``；全部成功时为空字典。
        """
        tasks = list(self._iter_media_tasks())
        warnings = self.extra.setdefault("limit_warnings", [])

        # 撤回上一次审计留下的那条（只撤回自己写的，别误删别的来源）
        previous = self.extra.pop(self._AUDIT_WARNING_KEY, None)
        if previous is not None:
            try:
                warnings.remove(previous)
            except ValueError:
                pass  # 被外部清理过，忽略

        if not tasks:
            return {}

        # 返回值不能丢：它是区分「真失败」与「按策略跳过」的唯一依据
        results = await asyncio.gather(
            *[task.get() for _, task in tasks], return_exceptions=True
        )

        missing: dict[str, int] = {}
        for (kind, task), result in zip(tasks, results):
            if isinstance(result, IgnoreException):
                # 按策略跳过（超时长 / 超体积 / 空响应 / 分片超限），已有专门提示
                continue
            # get() 失败时不会缓存 _path，所以 resolved 为 None 就等于「没落盘」
            if task.resolved is None:
                missing[kind] = missing.get(kind, 0) + 1

        if missing:
            parts = [
                f"{count} {self._MEDIA_UNITS.get(kind, '个')}{kind}"
                for kind, count in missing.items()
            ]
            message = f"⚠️ {'、'.join(parts)}下载失败，未包含在本次内容中"
            warnings.append(message)
            # 记下原文，供下一次调用撤回（幂等的关键）
            self.extra[self._AUDIT_WARNING_KEY] = message
        return missing

    @property
    def content_type(self) -> str:
        content_type = self.extra.get("content_type")
        if content_type is None:
            if self.video:
                return "视频"
            # 图片既可能放在 graphics（微博/NGA 那种混排），也可能放在 contents
            # （抖音/小红书/快手/微博图集）。只看 graphics 会让后者掉到「动态」——
            # 卡片头把一条图集标成「动态」，是另一种「产物在撒谎」。
            if self.img_contents or self.graphics:
                return "图文"
            return "动态"
        return content_type

    def __repr__(self) -> str:
        return (
            f"platform: {self.platform.display_name}, "
            f"timestamp: {self.timestamp}, "
            f"title: {self.title}, "
            f"text: {self.text}, "
            f"url: {self.url}, "
            f"author: {self.author}, "
            f"video: {self.video}, "
            f"contents: {self.contents}, "
            f"graphics: {self.graphics}, "
            f"extra: {self.extra}, "
            f"repost: [[{self.repost}]], "
            f"render_image: {self.render_image.name if self.render_image else 'None'}"
        )


class ParseResultKwargs(TypedDict, total=False):
    title: str | None
    text: str | None
    contents: list[MediaContent]
    graphics: list[str | ImageContent]
    timestamp: int | None
    url: str | None
    author: Author | None
    extra: dict[str, Any]
    repost: ParseResult | None
    card_details_loader: Callable[[], Awaitable[None]] | None

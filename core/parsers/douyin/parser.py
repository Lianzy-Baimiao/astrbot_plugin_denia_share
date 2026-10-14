# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""抖音解析器。

双路径取数，任一成功即可：

1. **轻量 HTML 路径**：抓取 iesdouyin / m.douyin 分享页的
   ``window._ROUTER_DATA``。零签名成本，是首选路径。抖音只在请求带有效
   ``ttwid`` 时才把作品数据放进 SSR，所以抓取必须走 :mod:`.session` 里的会话
   （保持 cookie + 重试），不能每次新建 client 只发一次请求。
2. **签名 Web API 路径**（娅娅版移植）：``/aweme/v1/web/aweme/detail/`` 配合
   a_bogus 签名 + ttwid 会话，稳定性更好，是 HTML 路径失败后的兜底。
"""

import asyncio
import re
from typing import Any, ClassVar

import aiohttp
from httpx import AsyncClient
from astrbot.api import logger

from ...base_parser import BaseParser, PlatformEnum, ParseException, handle
from ...data import Platform, platform_of
from ...douyin_quality import low_bitrate_urls
from ...exception import DownloadException


class DouyinParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.DOUYIN)

    def __init__(self, downloader, *, prefer_low_quality: bool = False):
        super().__init__(downloader)
        self._web_client = None
        self.prefer_low_quality = prefer_low_quality

    # ------------------------------------------------------------------ #
    # URL 注册
    # ------------------------------------------------------------------ #

    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("douyin", r"douyin\.com/(?P<ty>video|note|slides)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("jingxuan.douyin", r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    async def _parse_douyin(self, searched: re.Match[str]):
        ty, vid = searched.group("ty"), searched.group("vid")

        # 重置本条作品的请求预算。**必须在这里、只重置一次**：预算要跨下面所有
        # URL 变体共享 —— 4 个 URL 各自重试一轮就是最坏 48 次 GET，
        # 正是最容易把出口 IP 打进限流页的形状（见 session.MAX_REQUESTS_PER_WORK）。
        from .session import get_share_session

        get_share_session(type(self)).start_work()

        # 1) HTML _ROUTER_DATA 路径。零签名成本，配会话 cookie 后最稳。
        #    slides 类型也试 /share/note/<id>/ —— 抖音两种写法都有，
        #    而 /share/slides/<id>/ 现在不返回 _ROUTER_DATA。
        html_types = (ty, "note") if ty == "slides" else (ty,)
        for html_type in html_types:
            for url in (
                self._build_m_douyin_url(html_type, vid),
                self._build_iesdouyin_url(html_type, vid),
            ):
                try:
                    return await self.parse_video(url)
                except ParseException as e:
                    logger.debug(f"[douyin] HTML 路径失败 {url}: {e}")
                    continue

        # 2) 图文页再试 slidesinfo 接口。
        #    注意：该接口 2026-09 起对绝大多数作品返回 aweme_details=null、
        #    filter_list reason=8，实际已失效，只作为兜底保留。
        if ty in ("slides", "note"):
            try:
                return await self.parse_slides(vid)
            except Exception as e:
                logger.debug(f"[douyin] slidesinfo 路径失败，转签名接口: {e}")

        # 3) 兜底：签名 Web API
        return await self.parse_by_web_api(vid)

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    # ------------------------------------------------------------------ #
    # 路径一：HTML _ROUTER_DATA
    # ------------------------------------------------------------------ #

    async def parse_video(self, url: str):
        from ...models.douyin.video import decoder as video_decoder
        from .session import get_share_session, looks_like_challenge

        text = await get_share_session(type(self)).fetch(url, self.ios_headers)

        if looks_like_challenge(text):
            raise ParseException("抖音风控挑战未通过，稍后再试")

        pattern = re.compile(pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", flags=re.DOTALL)
        matched = pattern.search(text)
        if not matched or not matched.group(1):
            raise ParseException("can't find _ROUTER_DATA in html")

        try:
            video_data = video_decoder.decode(matched.group(1).strip()).video_data
        except ParseException:
            raise
        except Exception as e:
            # msgspec 的 ValidationError 也走这里：SSR 结构变了就落到下一条路径，
            # 而不是抛一个非 ParseException 把整条解析链路炸掉
            raise ParseException(f"分享页数据结构无法识别({type(e).__name__})") from e

        author = self.create_author(video_data.author.nickname, video_data.avatar_url)
        result = self.result(title=video_data.desc, author=author, timestamp=video_data.create_time)

        urls = []
        if self.prefer_low_quality and video_data.video:
            urls.extend(low_bitrate_urls(video_data.video.bit_rate))
        if video_url := video_data.video_url:
            urls.append(video_url)
        urls = list(dict.fromkeys(urls))
        if image_urls := video_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
        elif urls:
            self._add_limit_warning(result, video_data.duration)
            result.video = self._create_selected_video(urls, video_data.cover_url, video_data.duration)
        else:
            # 既没有图片也没有视频：多半是 SSR 结构变了、或命中了另一种页面变体。
            # 必须抛出去让 _parse_douyin 继续走后面的路径与兜底接口 —— 返回一个
            # 「只有标题、零媒体、零警告」的空结果，用户会收到一张空卡片却不知道为什么。
            raise ParseException("分享页里既没有图片也没有视频")
        return result

    async def parse_slides(self, video_id: str):
        from ...models.douyin.slides import decoder as slides_decoder

        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{video_id}]", "request_source": "200"}
        async with AsyncClient(
            **self.client_kwargs(headers=self.android_headers)
        ) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()

        aweme_details = slides_decoder.decode(response.content).aweme_details
        if not aweme_details:
            # 裸下标 [] 会抛 IndexError —— 虽然这一条被 _parse_douyin 的裸
            # except Exception 接得住（会继续走签名 API），但日志只剩
            # "list index out of range"，看不出是「接口没给作品」。
            raise ParseException("图文接口没有返回任何作品")
        slides_data = aweme_details[0]
        author = self.create_author(slides_data.name, slides_data.avatar_url)
        result = self.result(title=slides_data.desc, author=author, timestamp=slides_data.create_time)

        # **静态图优先。** 实拍图集（slides）里每张图都同时带一份静态图和一段内嵌视频，
        # 原先是「有内嵌视频就用内嵌视频」，于是整条图集被当成一串视频发出去 ——
        # 用户看到的就是「解析图集变成了视频」，而静态图全被丢掉。
        # 内嵌视频只在**没有任何静态图**时才用，那种作品本质就是视频。
        if image_urls := slides_data.image_urls:
            result.contents.extend(self.create_images(image_urls))
        elif dynamic_urls := slides_data.dynamic_urls:
            for dynamic_url in dynamic_urls:
                result.contents.append(self.create_video(dynamic_url))
        else:
            # 静态图与内嵌视频段都没有：这条接口没给媒体。抛出去让 _parse_douyin
            # 接着走签名 Web API 兜底，而不是返回一个零媒体的空结果。
            raise ParseException("图文接口里既没有图片也没有视频段")
        return result

    # ------------------------------------------------------------------ #
    # 路径二：签名 Web API（娅娅版移植）
    # ------------------------------------------------------------------ #

    def _get_web_client(self):
        if self._web_client is None:
            from .web import DouyinWebClient

            self._web_client = DouyinWebClient()
        return self._web_client

    async def parse_by_web_api(self, item_id: str):
        client = self._get_web_client()
        async with aiohttp.ClientSession() as session:
            data = await client.fetch_detail(session, item_id)

        if not data:
            # 让 web.py 把真实原因带出来（403 风控 / 网络失败 / 内容确实没了）。
            # 一律报"分享已删除"会把排查方向带偏 —— 实测本机就是被 403 风控拦的。
            raise ParseException(
                getattr(client, "last_error", "")
                or "分享已删除或资源直链提取失败, 请稍后再试"
            )

        item = self._pick_item(data, item_id)
        if item is None:
            raise ParseException("响应中未包含目标作品")

        return self._build_result_from_item(item)

    @staticmethod
    def _pick_item(data: dict, item_id: str) -> dict | None:
        candidates: list[dict] = []
        detail = data.get("aweme_detail")
        if isinstance(detail, dict):
            candidates.append(detail)
        for key in ("aweme_details", "aweme_list", "item_list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(i for i in value if isinstance(i, dict))
        for item in candidates:
            if str(item.get("aweme_id") or item.get("id") or "") == str(item_id):
                return item
        # 匹配不上就返回 None，让调用方报错。
        # 旧实现在这里兜底返回 candidates[0]，会把「响应里没有目标作品」伪装成成功，
        # 静默解析出一条**完全不相干的作品**（实测会把另一个视频当成本次结果发出去）。
        return None

    # ---- 媒体 URL 提取 ---- #

    @staticmethod
    def _first_url(value: Any) -> str | None:
        """从 url_list / 字符串 / 嵌套结构中取出第一个 http(s) 链接。"""
        if isinstance(value, str):
            return value if value.startswith(("http://", "https://")) else None
        if isinstance(value, list):
            for item in value:
                if found := DouyinParser._first_url(item):
                    return found
            return None
        if isinstance(value, dict):
            for key in ("url_list", "urlList", "download_url_list", "url"):
                if found := DouyinParser._first_url(value.get(key)):
                    return found
        return None

    @staticmethod
    def _no_watermark(url: str) -> str:
        """抖音直链去水印：playwm → play。"""
        return url.replace("/playwm/", "/play/").replace("playwm?", "play?")

    @classmethod
    def _video_urls(cls, item: dict, *, prefer_low_quality: bool = False) -> list[str]:
        """返回按优先级排序的备用视频直链（已去水印）。"""
        urls: list[str] = []
        video = item.get("video") or {}
        for key in ("play_addr", "playAddr", "PlayAddrStruct", "download_addr", "play_addr_h264"):
            if found := cls._first_url(video.get(key)):
                urls.append(cls._no_watermark(found))
        for bitrate in video.get("bit_rate") or []:
            if isinstance(bitrate, dict) and (found := cls._first_url(bitrate.get("play_addr"))):
                urls.append(cls._no_watermark(found))
        if prefer_low_quality:
            urls = low_bitrate_urls(video.get("bit_rate")) + urls
        # 去重保序
        return list(dict.fromkeys(urls))

    @classmethod
    def _image_urls(cls, item: dict) -> list[str]:
        """返回作品里的**静态图**直链。

        不要把 ``images[].video.play_addr`` 混进来：实拍图集里每张图都带一段内嵌
        视频，混进图片列表后会被当图片下载与渲染，实际落地的是 mp4 ——
        表现就是「图集里冒出视频」，卡片上则是打不开的空图。
        内嵌视频段另有用途，见 ``_slide_video_urls``。
        """
        urls: list[str] = []
        for image in item.get("images") or []:
            if found := cls._first_url(image.get("url_list") or image):
                urls.append(found)
        return list(dict.fromkeys(urls))

    @classmethod
    def _slide_video_urls(cls, item: dict) -> list[str]:
        """图文帖里内嵌的实拍视频段（已去水印）。

        只在**没有静态图**时才该拿来用 —— 见 ``parse_slides`` 的说明。
        """
        urls: list[str] = []
        for image in item.get("images") or []:
            if not isinstance(image, dict):
                continue
            if found := cls._first_url((image.get("video") or {}).get("play_addr")):
                urls.append(cls._no_watermark(found))
        return list(dict.fromkeys(urls))

    @staticmethod
    def _cover_url(item: dict) -> str | None:
        video = item.get("video") or {}
        for key in ("cover", "origin_cover", "dynamic_cover"):
            if found := DouyinParser._first_url(video.get(key)):
                return found
        return None

    @staticmethod
    def _duration(item: dict) -> float:
        video = item.get("video") or {}
        for key in ("duration", "video_duration"):
            raw = video.get(key) or item.get(key)
            if raw:
                value = float(raw)
                # 抖音 Web 接口返回的 duration 单位是毫秒
                return value / 1000 if value > 1000 else value
        return 0.0

    def _create_selected_video(self, urls: list[str], cover: str | None, duration: float | None):
        if not self.prefer_low_quality:
            return self.create_video(urls[0], cover, duration)
        # create_video applies the existing duration limit and cancels the task
        # before it starts when the work is too long.
        task = asyncio.create_task(self._download_video_candidates(urls))
        return self.create_video(task, cover, duration)

    async def _download_video_candidates(self, urls: list[str]):
        # A stale low-bitrate CDN URL must not discard an otherwise usable video.
        # Bound retry work; cancellation and size-policy exceptions propagate.
        candidates = list(dict.fromkeys(urls))[:6]
        for index, url in enumerate(candidates):
            try:
                return await self.downloader.download_video(url, ext_headers=self.headers)
            except DownloadException:
                if index == len(candidates) - 1:
                    raise
                logger.info("[douyin] 低码率候选下载失败，尝试备用地址")
        raise DownloadException("抖音没有可用的视频地址")

    def _build_result_from_item(self, item: dict):
        author_info = item.get("author") or {}
        avatar = self._first_url(author_info.get("avatar_larger") or author_info.get("avatar_thumb"))
        author = self.create_author(author_info.get("nickname") or "抖音用户", avatar)

        result = self.result(
            title=item.get("desc") or item.get("item_title") or "",
            author=author,
            timestamp=int(item.get("create_time") or 0) or None,
            url=f"https://www.douyin.com/video/{item.get('aweme_id')}" if item.get("aweme_id") else None,
        )

        image_urls = self._image_urls(item)
        video_urls = self._video_urls(item, prefer_low_quality=self.prefer_low_quality)

        # **图片优先，与 parse_video 的判定保持一致。**
        # 图文帖（note / slides）的 item 里也有顶层 video —— 抖音会给图文生成一段
        # 轮播视频 —— 先判视频会把整条图文作品当成视频，图片全被丢掉。
        if image_urls:
            result.contents.extend(self.create_images(image_urls))
        elif video_urls:
            duration = self._duration(item)
            self._add_limit_warning(result, duration)
            result.video = self._create_selected_video(video_urls, self._cover_url(item), duration)
        elif slide_videos := self._slide_video_urls(item):
            # 没有任何静态图、只有内嵌视频段：这种作品本质就是视频
            for slide_url in slide_videos:
                result.contents.append(self.create_video(slide_url))
        else:
            raise ParseException("未从作品中提取到媒体内容")

        return result

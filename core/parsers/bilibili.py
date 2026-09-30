# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

"""Bilibili 解析器 - 支持视频、动态、直播、专栏、收藏夹"""

import re
import json
import asyncio
from typing import ClassVar

from astrbot.api import logger
from bilibili_api import HEADERS, Credential, select_client, request_settings
from bilibili_api.opus import Opus
from bilibili_api.video import Video
from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents
from msgspec import convert

from ..base_parser import BaseParser, PlatformEnum, ParseException, IgnoreException, DownloadException, handle
from ..exception import MediaProcessException
from ..bili_access import STATUS_BLOCKED, analyze_play_access
from ..data import Platform, ImageContent, MediaContent, platform_of
from ..cookie_utils import ck2dict
from ..media_utils import fmt_duration

try:
    select_client("curl_cffi")
    request_settings.set("impersonate", "chrome131")
except Exception:
    logger.warning("curl_cffi 未注册/未安装，B站解析器将使用默认 httpx 客户端")
    select_client("httpx")


class BilibiliParser(BaseParser):
    platform: ClassVar[Platform] = platform_of(PlatformEnum.BILIBILI)

    @staticmethod
    def _is_transient_api_error(error: Exception) -> bool:
        """判断 B站接口错误是否适合重试。

        -504 是 B站上游服务调用超时，通常是一过性的；网络层超时同样按此处理。
        """
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            return True

        code = getattr(error, "code", None)
        if code is None:
            code = getattr(error, "retcode", None)
        try:
            if int(code) == -504:
                return True
        except (TypeError, ValueError):
            pass

        message = str(error).lower()
        return (
            "-504" in message
            or "服务调用超时" in message
            or "timeout" in message
            or "timed out" in message
        )

    async def _call_bili_api_with_retry(self, call, *, operation: str, retries: int = 2):
        """对 B站临时超时做少量退避重试，避免瞬时故障直接导致解析失败。"""
        for attempt in range(retries + 1):
            try:
                return await call()
            except Exception as error:
                if not self._is_transient_api_error(error) or attempt >= retries:
                    raise

                delay = 0.8 * (attempt + 1)
                logger.warning(
                    f"B站{operation}接口临时超时，{delay:.1f}秒后重试 "
                    f"({attempt + 1}/{retries})：{error}"
                )
                await asyncio.sleep(delay)

    def __init__(self, downloader, bili_ck: str | None = None, config_dir=None):
        super().__init__(downloader)
        self.headers = HEADERS.copy()
        self._credential: Credential | None = None
        self._bili_ck = bili_ck
        self._cookies_file = (config_dir / "bilibili_cookies.json") if config_dir else None

    @handle("b23.tv", r"b23\.tv/[0-9a-zA-Z._?%&+-=/#]+")
    @handle("bili2233", r"bili2233\.cn/[0-9a-zA-Z._?%&+-=/#]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    @handle("BV", r"^(?P<bvid>BV[0-9a-zA-Z]{10})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/BV", r"bilibili\.com(?:/video)?/(?P<bvid>BV[0-9A-Za-z]{10})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_bv(self, searched: re.Match[str]):
        bvid = str(searched.group("bvid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(bvid=bvid, page_num=page_num)

    @handle("av", r"^av(?P<avid>\d{6,})(?:\s)?(?P<page_num>\d{1,3})?$")
    @handle("/av", r"bilibili\.com(?:/video)?/av(?P<avid>\d{6,})(?:.*?[?&]p=(?P<page_num>\d{1,3}))?")
    async def _parse_av(self, searched: re.Match[str]):
        avid = int(searched.group("avid"))
        page_num = int(searched.group("page_num") or 1)
        return await self.parse_video(avid=avid, page_num=page_num)

    @handle("/dynamic/", r"bilibili\.com/dynamic/(?P<dynamic_id>\d+)")
    @handle("/opus/", r"bilibili\.com/opus/(?P<dynamic_id>\d+)")
    @handle("t.bili", r"t\.bilibili\.com/(?P<dynamic_id>\d+)")
    async def _parse_dynamic(self, searched: re.Match[str]):
        dynamic_id = int(searched.group("dynamic_id"))
        return await self.parse_dynamic_or_opus(dynamic_id)

    @handle("live.bili", r"live\.bilibili\.com/(?P<room_id>\d+)")
    async def _parse_live(self, searched: re.Match[str]):
        room_id = int(searched.group("room_id"))
        return await self.parse_live(room_id)

    @handle("/favlist", r"favlist\?fid=(?P<fav_id>\d+)")
    async def _parse_favlist(self, searched: re.Match[str]):
        fav_id = int(searched.group("fav_id"))
        return await self.parse_favlist(fav_id)

    @handle("/read/", r"bilibili\.com/read/cv(?P<read_id>\d+)")
    async def _parse_read(self, searched: re.Match[str]):
        from bilibili_api.article import Article
        read_id = int(searched.group("read_id"))
        article = Article(read_id)
        opus = await article.turn_to_opus()
        return await self._parse_bilibli_api_opus(opus)

    async def parse_video(self, *, bvid: str | None = None, avid: int | None = None, page_num: int = 1):
        from ..models.bilibili.video import VideoInfo, AIConclusion

        credential = await self.credential
        video = Video(bvid=bvid, aid=avid, credential=credential)
        video_info = convert(
            await self._call_bili_api_with_retry(video.get_info, operation="视频信息"),
            VideoInfo,
        )
        author = self.create_author(video_info.owner.name, video_info.owner.face)
        page_info = video_info.extract_info_with_page(page_num)

        from ..config import get_config
        pconfig = get_config()

        cid = page_info.cid
        ai_summary = "B站 AI 总结暂时不可用"
        if credential and cid is not None:
            try:
                ai_result = await self._call_bili_api_with_retry(
                    lambda: video.get_ai_conclusion(cid=cid), operation="AI总结",
                )
                ai_conclusion = convert(ai_result, AIConclusion)
                ai_summary = ai_conclusion.summary
            except Exception as error:
                # AI 总结是附加信息，接口临时超时不应阻断标题、封面和视频解析
                logger.warning(f"B站 AI 总结获取失败，跳过该字段继续解析：{error}")
        else:
            ai_summary = "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"

        url = f"https://bilibili.com/{video_info.bvid}"
        if page_info.index > 0:
            url += f"?p={page_info.index + 1}"

        # 格式化时长
        duration_str = fmt_duration(page_info.duration)

        # 格式化统计数据
        s = video_info.stat
        stats_map = {
            "👍": s.like, "🪙": s.coin, "⭐": s.favorite,
            "↩️": s.share, "💬": s.reply, "👀": s.view, "💭": s.danmaku,
        }

        def fmt_num(n: int) -> str:
            return f"{n / 10000:.1f}万" if n >= 10000 else str(n)

        stats_line = " ".join(f"{k} {fmt_num(v)}" for k, v in stats_map.items() if v > 0)

        # 获取实时在线人数
        online_text = ""
        if cid is not None:
            try:
                online_data = await video.get_online(cid=cid)
                total = int(online_data.get("total", 0))
                count = int(online_data.get("count", 0))
                if total > 0:
                    online_text = f"🏄‍♂️ {total} 人正在观看，{count} 人在网页端观看"
            except Exception as e:
                pass
        else:
            logger.debug("cid 为 None，跳过在线人数获取")

        # 时长限制提示（大小限制在下载时动态检查）
        limit_warnings = []
        if page_info.duration > pconfig.VIDEO_DURATION_MAXIMUM:
            limit_warnings.append(f"⚠️ 视频时长({duration_str})超过限制({fmt_duration(pconfig.VIDEO_DURATION_MAXIMUM)})，不会下载视频")

        extra = {
            "info": ai_summary,
            "stats_line": stats_line,
            "duration": duration_str,
            "online": online_text,
            "content_type": "视频",
            "limit_warnings": limit_warnings,
        }

        async def download_video():
            output_path = pconfig.cache_dir / f"{video_info.bvid}-{page_num}.mp4"
            if output_path.exists():
                return output_path
            # 把缺料通道的 list 直接捕获进来，而不是闭包引用 result ——
            # 后者要靠「create_task 到 result 赋值之间没有 await」这个时序假设，
            # 而 extra["limit_warnings"] 在闭包定义之前就已经存在了。
            v_url, v_backups, a_url, a_backups = await self.extract_download_urls(
                video=video, page_index=page_info.index,
                warnings=limit_warnings, rights=video_info.rights,
            )
            if page_info.duration > pconfig.VIDEO_DURATION_MAXIMUM:
                raise IgnoreException

            url_pairs = [(v_url, a_url)]
            for i, v_bu in enumerate(v_backups):
                a_bu = a_backups[i] if i < len(a_backups) else a_url
                url_pairs.append((v_bu, a_bu))

            last_error = None
            for idx, (v_try, a_try) in enumerate(url_pairs):
                try:
                    if idx > 0:
                        logger.info(f"B站 CDN 重试 ({idx+1}/{len(url_pairs)})")
                    if a_try is not None:
                        return await self.downloader.download_av_and_merge(
                            v_try, a_try, output_path=output_path, ext_headers=self.headers,
                        )
                    else:
                        # 这是**视频**，必须占媒体池：不传 slots 会落到 _image_slots，
                        # 双池设计要防的「小图排队等大视频」就在这条路径上原样发生。
                        return await self.downloader._download_file(
                            v_try, file_name=output_path.name, ext_headers=self.headers,
                            slots=self.downloader._media_slots,
                        )
                except (IgnoreException, MediaProcessException):
                    # 这两类错误换备用地址都不会变好，直接打断、不进下一轮重试：
                    #  - IgnoreException：「体积超限 / 分片超限」是策略跳过，逐个重试只是把
                    #    同一个大文件重下若干遍，最后还报「已尝试所有CDN」（缺料审计会误判）；
                    #  - MediaProcessException：本机缺 ffmpeg 之类的环境问题 —— 备用 CDN 不会
                    #    让本机长出一个 ffmpeg，重试只会把整段视频再下一遍。
                    raise
                except Exception as e:
                    if idx > 0:
                        logger.warning(f"B站 CDN 重试 ({idx+1}/{len(url_pairs)}) 失败: {e}")
                    last_error = e
                    continue

            raise DownloadException("视频下载失败，已尝试所有CDN") from last_error

        video_content = self.create_video(
            asyncio.create_task(download_video()),
            page_info.cover, page_info.duration,
        )

        return self.result(
            url=url, title=page_info.title, timestamp=page_info.timestamp,
            text=video_info.desc, author=author, contents=[video_content],
            extra=extra,
        )

    async def parse_dynamic_or_opus(self, dynamic_id: int):
        from bilibili_api.dynamic import Dynamic
        from ..models.bilibili.dynamic import DynamicWrapper

        dynamic = Dynamic(dynamic_id, await self.credential)
        if await dynamic.is_article():
            return await self._parse_bilibli_api_opus(dynamic.turn_to_opus())

        dynamic_info = convert(await dynamic.get_info(), DynamicWrapper).item
        return await self._parse_dynamic_info(dynamic_info)

    async def _parse_dynamic_info(self, dynamic_info):
        from ..models.bilibili.dynamic import DynamicInfo

        if dynamic_info.is_video():
            if (major := dynamic_info.modules.major) and (archive := major.archive):
                result = await self.parse_video(bvid=archive.bvid)
                result.text = dynamic_info.text
                result.extra["content_type"] = "动态"
                return result

        author = self.create_author(dynamic_info.name, dynamic_info.avatar)
        contents: list[MediaContent] = []
        contents.extend(self.create_images(dynamic_info.image_urls))

        repost = None
        if dynamic_info.type == "DYNAMIC_TYPE_FORWARD" and dynamic_info.orig is not None:
            repost = await self._parse_dynamic_info(dynamic_info.orig)

        return self.result(
            title=dynamic_info.title, text=dynamic_info.text,
            timestamp=dynamic_info.timestamp, author=author,
            contents=contents, repost=repost, extra={"content_type": "动态"},
        )

    async def parse_opus_by_id(self, opus_id: int):
        opus = Opus(opus_id, await self.credential)
        return await self._parse_bilibli_api_opus(opus)

    async def _parse_bilibli_api_opus(self, bili_opus: Opus):
        from ..models.bilibili.opus import OpusItem

        opus_info = await bili_opus.get_info()
        if not isinstance(opus_info, dict):
            raise ParseException("获取图文动态信息失败")

        opus_data = convert(opus_info, OpusItem)
        author = self.create_author(*opus_data.name_avatar)

        result = self.result(author=author, title=opus_data.title, timestamp=opus_data.timestamp)
        for node in opus_data.extract_nodes():
            if isinstance(node, str):
                result.graphics.append(node)
            else:
                result.graphics.append(self.create_image(node.url, alt=node.alt))
        return result

    async def parse_live(self, room_id: int):
        from bilibili_api.live import LiveRoom
        from ..models.bilibili.live import RoomData

        room = LiveRoom(room_display_id=room_id, credential=await self.credential)
        info_dict = await room.get_room_info()
        room_data = convert(info_dict, RoomData)
        contents: list[MediaContent] = []
        if cover := room_data.cover:
            contents.append(self.create_image(self.downloader.download_img(cover, ext_headers=self.headers)))
        if keyframe := room_data.keyframe:
            contents.append(self.create_image(self.downloader.download_img(keyframe, ext_headers=self.headers)))
        author = self.create_author(room_data.name, room_data.avatar)
        url = f"https://www.bilibili.com/blackboard/live/live-activity-player.html?enterTheRoom=0&cid={room_id}"
        return self.result(url=url, title=room_data.title, text=room_data.detail,
                          contents=contents, author=author, extra={"content_type": "直播"})

    async def parse_favlist(self, fav_id: int):
        from bilibili_api.favorite_list import get_video_favorite_list_content
        from ..models.bilibili.favlist import FavData

        fav_dict = await get_video_favorite_list_content(fav_id)
        if fav_dict["medias"] is None:
            raise ParseException("收藏夹内容为空, 或被风控")
        favdata = convert(fav_dict, FavData)
        author = self.create_author(favdata.info.upper.name, favdata.info.upper.face)
        graphics: list[str | ImageContent] = []
        for fav in favdata.medias:
            graphics.append(self.create_image(fav.cover, alt=fav.desc))
            graphics.append(fav.desc)
        return self.result(title=favdata.title, timestamp=favdata.timestamp,
                          author=author, graphics=graphics, extra={"content_type": "收藏夹"})

    @staticmethod
    def _append_access_warning(warnings: list[str] | None, message: str) -> None:
        """把可访问性结论写进缺料通道（同一条只记一次）。

        ``warnings`` 为 None 时什么都不做 —— ``tools/probe_bili_e2e.py`` 之类的
        探测脚本只关心 URL，不该被这一步绊住。
        """
        if not message or warnings is None:
            return
        if message not in warnings:
            warnings.append(message)

    async def extract_download_urls(self, video: Video | None = None, *, bvid: str | None = None,
                                     avid: int | None = None, page_index: int = 0,
                                     warnings: list[str] | None = None,
                                     rights: dict | None = None):
        from bilibili_api.video import (
            AudioStreamDownloadURL, VideoStreamDownloadURL, FLVStreamDownloadURL,
            MP4StreamDownloadURL, VideoDownloadURLDataDetecter, VideoQuality, VideoCodecs,
        )
        from ..config import get_config

        # 清晰度字符串 → VideoQuality 枚举映射
        QUALITY_MAP = {
            "360P": VideoQuality._360P,
            "480P": VideoQuality._480P,
            "720P": VideoQuality._720P,
            "1080P": VideoQuality._1080P,
            "1080P+": VideoQuality._1080P_PLUS,
            "4K": VideoQuality._4K,
            "8K": VideoQuality._8K,
        }

        # 从配置读取用户设置的清晰度，不区分大小写
        pconfig = get_config()
        raw_quality = pconfig.BILI_QUALITY.strip().upper().replace("＋", "+")
        target_quality = QUALITY_MAP.get(raw_quality, VideoQuality._1080P)

        credential = await self.credential
        if video is None:
            video = Video(bvid=bvid, aid=avid, credential=credential)

        try:
            download_url_data = await self._call_bili_api_with_retry(
                lambda: video.get_download_url(page_index=page_index),
                operation="视频流地址",
            )
        except IgnoreException:
            raise
        except Exception as error:
            # **受限视频在这里是抛异常**（`ResponseCodeException`，如 code=-10403
            # 大会员专享）。不接住的话会被 download_video 的 CDN 重试循环当成网络故障，
            # 逐个备用地址重试一遍，最后报「视频下载失败，已尝试所有CDN」——
            # 把「没权限」说成「网络问题」，用户不知道该去登录还是该重试。
            # 这里分析成一句人话原因，再抛 IgnoreException（策略跳过），
            # 缺料审计就不会再加一条「下载失败」的自相矛盾警告。
            access = analyze_play_access(
                error=error, rights=rights, has_cookie=credential is not None,
            )
            self._append_access_warning(warnings, access["message"])
            raise IgnoreException(access["message"] or "无法获取视频流") from error

        access = analyze_play_access(
            download_url_data, rights=rights, has_cookie=credential is not None,
        )
        self._append_access_warning(warnings, access["message"])
        if access["status"] == STATUS_BLOCKED:
            # 有响应但拿不到可播放流：同样是「知道原因的跳过」，不是 CDN 故障
            raise IgnoreException(access["message"] or "无法获取视频流")

        detecter = VideoDownloadURLDataDetecter(download_url_data)
        # 编码按配置过滤，默认只取 H.264（AVC）：
        # B站 4K/8K 档只有 HEVC/AV1 流，而插件是 `ffmpeg -c copy` 零转码把流装进
        # .mp4 的，电脑版 QQ 解不了 HEVC/AV1 —— 表现是花屏 / 放不出来，手机却正常。
        # 1080P 及以下 AVC 都有，所以默认档位拿不到 4K/8K 换的是「到处都能放」。
        # 过滤后拿不到流时，下面还有 html5 单文件 MP4（本来就是 H.264）回退。
        if pconfig.BILI_CODEC == "全部编码":
            codecs = [VideoCodecs.AV1, VideoCodecs.AVC, VideoCodecs.HEV]
        else:
            codecs = [VideoCodecs.AVC]
        streams = detecter.detect_best_streams(
            video_max_quality=target_quality,
            codecs=codecs,
            no_dolby_video=True, no_hdr=True,
        )

        # 筛选视频流和音频流
        video_stream = next(
            (s for s in streams if isinstance(s, (VideoStreamDownloadURL, FLVStreamDownloadURL, MP4StreamDownloadURL))),
            None,
        )
        audio_stream = next(
            (s for s in streams if isinstance(s, AudioStreamDownloadURL)), None,
        )

        # DASH 拿不到流、或拿到的流低于目标清晰度时（典型：未登录），
        # 回退 html5 单文件 MP4。
        # 实测（2026-09-17，匿名）：DASH 只给到 480P，html5 durl 却是 720P——
        # 同一个 5 分钟视频 35.3MB，而 DASH 360P 只有 11.2MB。
        # 娅娅版正是靠这条回退，在未登录时拿到比纯 DASH 更高的清晰度。
        need_mp4_fallback = video_stream is None or self._below_target(video_stream, target_quality)
        if need_mp4_fallback:
            # 只在 html5 档位确实高于 DASH 时才替换，避免登录后反而被降级
            dash_qid = getattr(getattr(video_stream, "video_quality", None), "value", None)
            mp4_url, mp4_backups = await self._try_html5_mp4(
                video, page_index, target_quality, better_than=dash_qid,
            )
            if mp4_url:
                # html5 durl 是音视频合并好的单文件，没有独立音轨
                return mp4_url, mp4_backups, None, []

        if video_stream is None:
            # 编码过滤后一条流都不剩（4K/8K 只有 HEVC/AV1），且 html5 单文件 MP4
            # 也没拿到。这是**策略跳过**而不是网络故障：报 DownloadException 会让
            # 缺料审计和 CDN 重试循环把它当成「下载失败」逐地址重试一遍。
            if pconfig.BILI_CODEC != "全部编码":
                raise IgnoreException(
                    "该视频没有 H.264 流（仅 HEVC/AV1），电脑版 QQ 播放会花屏；"
                    "需要它的话把「B站视频编码」改为「全部编码」"
                )
            raise DownloadException("未找到可下载的视频流")

        v_backups = video_stream.backup_url if isinstance(video_stream, VideoStreamDownloadURL) else []
        a_backups = audio_stream.backup_url if audio_stream and isinstance(audio_stream, AudioStreamDownloadURL) else []

        if audio_stream is None:
            return video_stream.url, v_backups, None, []

        return video_stream.url, v_backups, audio_stream.url, a_backups

    # 清晰度档位 → 期望高度（用于判断 DASH 实际给到的是否够用）
    _QUALITY_HEIGHT = {
        "360P": 360, "480P": 480, "720P": 720,
        "1080P": 1080, "1080P+": 1080, "4K": 2160, "8K": 4320,
    }

    def _below_target(self, video_stream, target_quality) -> bool:
        """DASH 实际流高度是否明显低于目标档位。"""
        try:
            height = int(getattr(video_stream, "scale", (0, 0))[1] or 0)
        except (TypeError, ValueError, IndexError):
            height = 0
        if not height:
            return False
        target_height = self._QUALITY_HEIGHT.get(
            str(getattr(target_quality, "name", "")).replace("_PLUS", "").replace("_", "").upper(),
            self._QUALITY_HEIGHT.get(str(target_quality).upper()),
        )
        if not target_height:
            return False
        return height < target_height

    async def _try_html5_mp4(self, video, page_index: int, target_quality, better_than: int | None = None):
        """尝试 html5 接口拿音视频合并的单文件 MP4，返回 (url, backups)。

        ``better_than`` 为 DASH 已拿到的清晰度 id；若 html5 档位不高于它则放弃，
        避免「目标设 4K、DASH 给了 1080P、html5 只给 720P」时反而被降级。
        """
        from astrbot.api import logger

        try:
            data = await video.get_download_url(page_index=page_index, html5=True)
        except Exception as e:
            logger.debug(f"[bili] html5 MP4 回退不可用: {e}")
            return None, []

        served = data.get("quality")
        if better_than is not None and isinstance(served, int) and served <= better_than:
            logger.debug(
                f"[bili] html5 档位 {served} 不高于 DASH 的 {better_than}，放弃回退"
            )
            return None, []

        durl = data.get("durl") or []
        urls = [d.get("url") for d in durl if isinstance(d, dict) and d.get("url")]
        if not urls:
            return None, []
        logger.info(
            f"[bili] 使用 html5 单文件 MP4 回退（服务端 quality={served}，"
            f"目标 {getattr(target_quality, 'name', target_quality)}）"
        )
        return urls[0], urls[1:]

    def _save_credential(self):
        if self._credential is None or self._cookies_file is None:
            return
        self._cookies_file.write_text(json.dumps(self._credential.get_cookies()))

    def _load_credential(self):
        if self._cookies_file is None or not self._cookies_file.exists():
            return
        try:
            self._credential = Credential.from_cookies(json.loads(self._cookies_file.read_text()))
        except Exception as e:
            logger.error(f"加载已保存的凭证失败: {e}")

    def _save_cookie_str(self, cookie_str: str):
        """将cookie字符串持久化保存，供下次启动时自动加载"""
        if self._cookies_file is None:
            return
        try:
            ck_dict = ck2dict(cookie_str)
            self._cookies_file.parent.mkdir(parents=True, exist_ok=True)
            self._cookies_file.write_text(json.dumps(ck_dict))
            logger.info("B站 Cookie 已持久化保存")
        except Exception as e:
            logger.error(f"保存 Cookie 失败: {e}")

    async def _init_credential(self):
        # **配置项优先**（与 main.apply_runtime_config 的取值顺序保持一致）：
        # BILI_CK 是用户显式填的，持久化文件只是扫码登录的暂存。
        # 反过来的话，网页上改了 BILI_CK 会被旧登录态压住 —— 接口报成功、实际不生效。
        if self._bili_ck:
            credential = Credential.from_cookies(ck2dict(self._bili_ck))
            if await credential.check_valid():
                logger.info(f"B站配置中的 Cookie 有效, 已持久化保存")
                self._credential = credential
                self._save_credential()
                self._save_cookie_str(self._bili_ck)
                return
            logger.info("B站配置中的 Cookie 已过期")

        # 其次才是扫码登录持久化下来的文件
        self._load_credential()
        if self._credential is not None:
            if await self._credential.check_valid():
                logger.info("从持久化文件加载的B站 Cookie 有效")
                return
            logger.info("持久化文件中的 Cookie 已过期")

    def update_cookie(self, cookie_str: str):
        """运行时更新B站Cookie，立即生效"""
        if not cookie_str:
            return
        self._bili_ck = cookie_str
        self._credential = None
        # 持久化保存，重启后自动生效
        self._save_cookie_str(cookie_str)
        logger.info("B站 Cookie 已更新，将在下次请求时重新初始化凭证")

    def clear_cookie(self):
        """清除运行时与持久化的 Cookie。

        ``update_cookie("")`` 是空值早退的，清不掉内存里的凭据，
        所以「清除 Cookie」必须走这个方法：连同自己持久化的那份一起删。
        """
        self._bili_ck = None
        self._credential = None
        if self._cookies_file is not None:
            try:
                self._cookies_file.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除 B站 Cookie 文件失败", exc_info=True)
        logger.info("B站 Cookie 已清除")

    @property
    async def credential(self) -> Credential | None:
        if self._credential is None:
            await self._init_credential()
            if self._credential is None:
                return None
            return self._credential

        # 已过期时尝试重新初始化
        if not await self._credential.check_valid():
            logger.warning("哔哩哔哩凭证已过期, 尝试重新初始化")
            self._credential = None
            await self._init_credential()
            if self._credential is None:
                return None

        # 尝试刷新
        if self._credential and await self._credential.check_refresh():
            logger.info("哔哩哔哩凭证需要刷新")
            if self._credential.has_ac_time_value() and self._credential.has_bili_jct():
                await self._credential.refresh()
                self._save_credential()

        return self._credential

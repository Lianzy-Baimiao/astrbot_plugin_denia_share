# 本文件包含衍生自 astrbot_plugin_rika_share（MIT License）的代码，
# 上游项目：https://github.com/iris1598/astrbot_plugin_rika_share
# 本仓库对其做过修改；完整归属见项目根目录 README「许可与致谢」。

from random import choice
from typing import Any
from msgspec import Struct, field
from msgspec.json import Decoder
from ...exception import ParseException


def first_url(url_list: list[str] | None) -> str | None:
    """从 ``url_list`` 里随机取一条；空列表返回 ``None``。

    **不要裸用 ``choice()``**：空列表会抛 ``IndexError``，而调用它的属性
    （``video_url`` / ``cover_url`` / ``avatar_url``）都在解析器的 try **之外**
    被读 —— ``IndexError`` 不是 ``ParseException`` 的子类，``_parse_douyin``
    的 ``except ParseException`` 接不住，会把「换下一条路径」打断，
    slidesinfo 与签名 Web API 两级兜底**完全不执行**。
    也就是说：一个装饰性字段（头像）为空，就能让整条抖音链路从三级退化到一级。
    """
    return choice(url_list) if url_list else None


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int
    # Preserve rendition metadata from SSR instead of dropping unknown fields.
    bit_rate: Any = None


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None

    @property
    def image_urls(self) -> list[str]:
        # 跳过 url_list 为空的条目：choice([]) 会抛 IndexError 把整条解析炸掉
        return [choice(image.url_list) for image in self.images if image.url_list] if self.images else []

    @property
    def video_url(self) -> str | None:
        if not self.video:
            return None
        url = first_url(self.video.play_addr.url_list)
        # playwm 是带水印的播放地址，play 是去水印的
        return url.replace("playwm", "play") if url else None

    @property
    def cover_url(self) -> str | None:
        return first_url(self.video.cover.url_list) if self.video else None

    @property
    def duration(self) -> int | None:
        return self.video.duration // 1000 if self.video else None

    @property
    def avatar_url(self) -> str | None:
        # 两个字段都试：thumb 存在但 url_list 为空时退到 medium。
        # 原先是「thumb 存在就 choice 它的 url_list」—— 空列表直接抛，头像这个
        # 纯装饰字段能把整条解析链打断（详见 first_url 的说明）。
        for avatar in (self.author.avatar_thumb, self.author.avatar_medium):
            if avatar and (url := first_url(avatar.url_list)):
                return url
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(name="videoInfoRes", default_factory=VideoInfoRes)
    # 图文页的另外两种写法。**必须与 session.has_work_data 认的字段名一致**：
    # 那边用「有没有 item_list」决定要不要继续重试，这边负责真正读出来。
    # 两边不一致的后果是「会话以为拿到了数据 → 停止重试 → 解析却读不出来」，
    # 明明在页面里的作品就这么丢了。
    slides_info_res: VideoInfoRes = field(name="slidesInfoRes", default_factory=VideoInfoRes)
    note_detail_res: VideoInfoRes = field(name="noteDetailRes", default_factory=VideoInfoRes)

    @property
    def work(self) -> VideoInfoRes:
        """三个字段里第一个真带 item_list 的；都没有就返回 videoInfoRes 那个空的，
        由 ``video_data`` 抛出统一的「找不到数据」。"""
        for candidate in (self.video_info_res, self.slides_info_res, self.note_detail_res):
            if candidate.item_list:
                return candidate
        return self.video_info_res


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)
    slides_page: VideoOrNotePage | None = field(name="slides_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        for page in (
            self.loader_data.video_page,
            self.loader_data.note_page,
            self.loader_data.slides_page,
        ):
            if page is not None:
                return page.work.video_data
        raise ParseException("can't find video_(id)/page or note_(id)/page in router data")


decoder = Decoder(RouterData)

"""媒体与文件工具函数

涵盖缓存清理、ffmpeg 处理（音视频合并 / 转码 / 抽帧 / 转 GIF）、
文件名生成，以及时长格式化。
"""

import os
import asyncio
import hashlib
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

from .exception import MediaProcessException

# 防解压炸弹：PIL 的默认上限约 89M 像素，且只在超过**两倍**时才抛错，
# 1~2 倍之间仅发一条 warning。本仓的图全部来自远端，一张 30000×30000 的 PNG
# 解压后能吃掉几 GB 内存 —— 显式压到 64M 像素：超过 128M 像素直接抛
# DecompressionBombError，而不是把进程拖死。
try:
    from PIL import Image as _PILImage

    _PILImage.MAX_IMAGE_PIXELS = 64 * 1024 * 1024
except Exception:  # PIL 缺失时本模块的其他功能仍要可用
    pass


def fmt_duration(duration: float) -> str:
    """格式化媒体时长，超过 1 小时后显示为 h:mm:ss。"""
    total_seconds = max(int(duration), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


async def safe_unlink(path: Path):
    """安全删除文件（失败只记日志，不向上抛）"""
    try:
        if path.exists():
            path.unlink()
    except Exception:
        logger.warning(f"删除文件失败: {path}", exc_info=True)


# 缓存目录哨兵文件名。
#
# 为什么需要它：清理是**递归删除**，而「共享缓存目录」是个自由文本配置项，
# 填错了就会把无关目录清空。哨兵的作用是把「这是个缓存目录」变成可验证的事实，
# 而不是靠配置项里填了什么来推断。目录里已经有别的东西、又没有这个文件时，
# 清理函数一律拒绝执行。
CACHE_MARKER_NAME = ".denia_share_cache"


def is_cache_dir_trusted(cache_dir: Path) -> bool:
    """目录里是否有缓存哨兵（没有就说明不该对它做递归清理）。"""
    try:
        return (cache_dir / CACHE_MARKER_NAME).is_file()
    except OSError:
        return False


def ensure_cache_marker(cache_dir: Path, *, allow_nonempty: bool = False) -> bool:
    """确保缓存目录带哨兵文件。

    Args:
        cache_dir: 目标目录（应已存在）。
        allow_nonempty: 目录非空时是否仍然补写哨兵。插件自己管的目录
            （默认数据目录下的 cache/）传 True；用户填的共享目录传 False，
            这样「指向一个已有内容的目录」会被拒绝而不是被清空。

    Returns:
        是否可安全用作缓存目录。
    """
    marker = cache_dir / CACHE_MARKER_NAME
    try:
        if marker.is_file():
            return True
        if not allow_nonempty and any(cache_dir.iterdir()):
            return False
        marker.touch()
        return True
    except OSError as exc:
        logger.warning(f"写入缓存哨兵失败: {cache_dir} ({exc})")
        return False


async def cleanup_cache_dir(cache_dir: Path, ttl_hours: int) -> int:
    """清理缓存目录中超过 TTL 的过期文件（**递归**，含子目录）。

    目录没有缓存哨兵时直接返回 0 —— 说明它不是插件认领的缓存目录，
    宁可不清也不能清错。

    Args:
        cache_dir: 缓存目录路径。
        ttl_hours: 文件存活阈值（小时）。文件最后修改时间早于
                   (now - ttl_hours) 则视为过期。

    Returns:
        清理的文件数量。
    """
    if not cache_dir.exists():
        return 0

    if not is_cache_dir_trusted(cache_dir):
        logger.warning(
            f"缓存目录 {cache_dir} 缺少 {CACHE_MARKER_NAME} 哨兵，已跳过清理"
        )
        return 0

    cutoff = time.time() - ttl_hours * 3600
    cleaned = 0
    # 哨兵必须豁免。它是个空文件、mtime 就是创建时间，TTL 一到就会被当过期文件删掉，
    # 后果是连锁的：
    #   1. 下一轮 is_cache_dir_trusted 变 False → 清理**永久停摆**，
    #      「清除缓存」按钮也一起失效；
    #   2. 共享缓存目录更糟 —— 哨兵没了、目录里又有内容，重启时
    #      ensure_cache_marker(allow_nonempty=False) 会拒绝补写 → 永久回退到插件数据
    #      目录，媒体共享/中转静默失效（默认目录能自愈，因为它传的是 allow_nonempty=True）。
    # clear_cache_dir 一直是豁免的，只有这里漏了。
    marker = cache_dir / CACHE_MARKER_NAME

    # **递归遍历**：截图落在 cache_dir/screenshot/ 这类子目录里，只看顶层的话
    # 它们永远不会过期 —— 缓存页的体积一直涨，而「清理过期文件」清不动它。
    # clear_cache_dir 一直用的是 rglob，两者语义本来就该一致。
    for f in cache_dir.rglob("*"):
        if not f.is_file() or f == marker:
            continue
        try:
            if f.stat().st_mtime < cutoff:
                await safe_unlink(f)
                cleaned += 1
        except Exception:
            continue

    # 双保险：上面的 trusted 检查已通过（说明这确实是插件认领的目录），
    # 所以补写哨兵是安全的。万一它被外部删掉，至少能自愈而不是永久停摆。
    if not ensure_cache_marker(cache_dir, allow_nonempty=True):
        logger.warning(f"缓存哨兵补写失败，下轮清理可能被跳过: {cache_dir}")

    if cleaned > 0:
        logger.info(f"缓存清理完成: 已清理 {cleaned} 个过期文件 ({cache_dir})")
    return cleaned


async def clear_cache_dir(cache_dir: Path) -> int:
    """清空缓存目录中的所有文件，并删除其中的空目录。

    只保留目录本身与哨兵文件。**目录没有缓存哨兵时直接返回 0**：
    清理是递归删除，而缓存目录可被配置项指向任意路径，所以必须先确认
    「这确实是插件认领的缓存目录」再动手，而不是反过来事后补救。
    """
    if not cache_dir.exists():
        return 0

    if not is_cache_dir_trusted(cache_dir):
        logger.warning(
            f"缓存目录 {cache_dir} 缺少 {CACHE_MARKER_NAME} 哨兵，已跳过清理。"
            f"如果确实要把它当缓存目录，请先在该目录下建一个空的 {CACHE_MARKER_NAME} 文件"
        )
        return 0

    cleaned = 0
    # 深路径优先，保证子目录先被清空才能 rmdir
    entries = sorted(cache_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    # 只跳过**缓存目录根下**的那个哨兵：按文件名比较会把子目录里恰好同名的文件
    # 一起放过（无害，但会让「哨兵」的语义变得含糊）
    marker = cache_dir / CACHE_MARKER_NAME

    for entry in entries:
        if entry == marker:
            continue
        try:
            # 先判断符号链接，避免误将链接目标当作缓存目录递归处理
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
                cleaned += 1
            elif entry.is_dir():
                entry.rmdir()
        except FileNotFoundError:
            # 清理过程中可能已被下载任务或其他清理任务先行删除
            continue
        except OSError as exc:
            logger.warning(f"清理缓存失败: {entry} ({exc})")

    logger.info(f"缓存清空完成: 已清理 {cleaned} 个文件 ({cache_dir})")
    return cleaned


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """尽力杀掉子进程并回收。

    清理路径里**不能再抛新异常**（会盖掉调用方要向上抛的那个），所以这里连
    ``BaseException`` 一起吞：取消中的第二次 await 可能再抛一次 CancelledError。
    """
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await process.wait()
    except BaseException:
        logger.debug("回收 ffmpeg 子进程失败", exc_info=True)


async def exec_ffmpeg_cmd(cmd: list[str]) -> None:
    """执行 ffmpeg 命令。

    取消/退出时**先收掉子进程再向上抛**：ffmpeg 被留在后台会继续写目标文件，
    而调用方紧接着要 unlink 它 —— Windows 上文件被占用会 PermissionError，
    被 ``safe_unlink`` 吞成一条日志，于是「删掉残缺输出」这层保护静默失效。
    """
    logger.debug(f"Executing ffmpeg command: {' '.join(cmd)}")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        # 本机没装 ffmpeg（或它不在 PATH 里）。抛专门的类型而不是 RuntimeError：
        # B站的 CDN 重试循环要能认出「换地址也没用」，从而直接打断、不再重下整段视频。
        raise MediaProcessException("ffmpeg 未安装或无法找到可执行文件")

    try:
        _, stderr = await process.communicate()
    except BaseException:
        # 必须写 BaseException：CancelledError 不是 Exception 的子类
        await _terminate_process(process)
        raise
    return_code = process.returncode

    if return_code != 0:
        error_msg = stderr.decode().strip()
        raise RuntimeError(f"ffmpeg 执行失败: {error_msg}")


async def merge_av(
    *,
    v_path: Path,
    a_path: Path,
    output_path: Path,
) -> None:
    """合并视频和音频"""
    logger.info(f"Merging {v_path.name} and {a_path.name} to {output_path.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(v_path),
        "-i",
        str(a_path),
        "-c",
        "copy",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        str(output_path),
    ]

    try:
        await exec_ffmpeg_cmd(cmd)
    except BaseException:
        # 失败时删掉可能残留的 0 字节/残缺输出，避免被 encode_video_to_h264
        # 等下游的 exists() 缓存命中当成成品。
        # 捕 BaseException 而不是 RuntimeError：CancelledError 不是 Exception 的子类，
        # 取消时同样会留下残缺输出（与 download.py 的 .part 是同一形状）。
        await safe_unlink(output_path)
        raise
    await asyncio.gather(safe_unlink(v_path), safe_unlink(a_path))
    logger.info(f"Merged {output_path.name}, {fmt_size(output_path)}")


# AstrBot 框架的「QQ 官方 Bot 分片上传阈值」：`QQOFFICIAL_CHUNKED_UPLOAD_THRESHOLD`
# （qqofficial_chunked_upload.py，4.28.x 为 10MB）。超过它走分片上传（预签名 URL + 5MB
# 分片 + 4 路并发，单请求超时 300s），不超过则是**单次 base64 POST 到 API 主机、
# 客户端超时只有 20s**（qqofficial_platform_adapter.py 的 BotHttp(timeout=20)）——
# 慢上行下这个请求必然超时，botpy 超时后 `return None`，框架把它当成
# 「上传文件API返回None」重试 5 次（约 2 分钟）后放弃。
#
# QQ 官方文档明确说本地文件**推荐分片上传**，所以下面这个补位函数是把「本来就该走
# 分片」的小文件送回分片的轨道上。
QQ_CHUNKED_UPLOAD_THRESHOLD_BYTES = 10 * 1024 * 1024

# 补到「阈值 + 这么多字节」，保证严格大于阈值（框架用的是 `>`）
_QQ_PAD_EXTRA_BYTES = 64 * 1024


def should_pad_for_chunked(
    size_bytes: int,
    *,
    pad_min_mb: int,
    single_shot_broken: bool,
    threshold: int = QQ_CHUNKED_UPLOAD_THRESHOLD_BYTES,
) -> bool:
    """这个文件要不要补到分片阈值（纯判定，与 I/O 分开以便直接断言）。

    三种情况补：
    * 体积超过手动设的「分片上传起点」（用户在按自己的上行带宽取舍）；
    * 本机已经被证明**单次 base64 上传不可靠**（见 main.py 的记忆文件），
      那么此后所有不超过阈值的文件都直接走分片，不必每次白等两分钟重试；
    * 其余（含超过阈值的）不补 —— 超过阈值的本来就走分片。
    """
    if size_bytes <= 0 or size_bytes > threshold:
        return False
    if single_shot_broken:
        return True
    return pad_min_mb > 0 and size_bytes >= pad_min_mb * 1024 * 1024


async def pad_to_chunked_upload(src: Path, *, threshold: int = QQ_CHUNKED_UPLOAD_THRESHOLD_BYTES) -> Path:
    """把不超过 ``threshold`` 的视频补到略高于它，让 QQ 官方 Bot 走**分片上传**。

    补的是一个**尾部 ``free`` box**（ISO/IEC 14496-12 里 `free` 就是「可跳过的空闲
    区」）：

    * 放在文件**末尾**是关键 —— 它不移动任何已有字节，``moov`` 里的绝对偏移
      (``stco``/``co64``) 全部保持有效，播放器与 QQ 的校验都不受影响；
    * 反过来，把填充插在 ``ftyp`` 之后会平移 ``mdat``，必须同步修补 ``stco``，
      修错一点整段视频就废了 —— 所以这里只在尾部追加。

    顺便说明为什么值得补：单次 base64 的 20s 超时是**按请求**算的，分片上传的
    每个请求都很小、且有 300s 预算，于是「总时长受带宽限制」不再等于「必然超时」。

    返回补位后的文件路径；原文件大于阈值时原样返回。补位失败时也原样返回
    （宁可走老路径，也不让发送直接失败）。
    """
    try:
        size = src.stat().st_size
    except OSError:
        return src
    if size > threshold:
        return src

    padded = src.with_name(f"{src.stem}.qqchunk{src.suffix}")
    try:
        if padded.exists() and padded.stat().st_size > threshold:
            return padded  # 已经补过：同一条链接重复发送时不必再拷一遍

        # 大文件拷贝走线程，别堵事件循环（≤10MB，几十毫秒级）
        await asyncio.to_thread(shutil.copyfile, src, padded)
        missing = threshold + _QQ_PAD_EXTRA_BYTES - size
        # box：4 字节大端长度（含 8 字节头）+ 类型 + 载荷
        box = int(missing).to_bytes(4, "big") + b"free" + b"\x00" * (missing - 8)
        with padded.open("ab") as fh:
            fh.write(box)
    except Exception:
        logger.warning(
            f"补位到分片上传阈值失败，改用原文件发送: {src.name}", exc_info=True
        )
        return src

    logger.info(
        f"已补位到分片上传阈值（{size / 1024 / 1024:.1f}MB → "
        f"{padded.stat().st_size / 1024 / 1024:.1f}MB）: {padded.name}"
    )
    return padded


async def remux_ts_to_mp4(src: Path, dst: Path) -> None:
    """把 m3u8 拼接出来的 TS / fMP4 裸流重封装成规范的 MP4（**零转码**）。

    为什么必须做：m3u8 分片直接拼接得到的文件只是个字节串，扩展名写成 ``.mp4``
    并不会让它变成 MP4。播放器只能靠嗅探硬猜，猜不准就把时长和预览尺寸显示错，
    电脑版 QQ 干脆拒绝播放。``-c copy`` 只重写容器，不解码重编码，代价近乎为零；
    ``+faststart`` 把 moov 盒挪到文件头，边下边播的预览才能立刻拿到元数据。

    失败时删掉残缺输出再向上抛（形状与 :func:`merge_av` 一致），
    由调用方决定是否回退。
    """
    logger.info(f"Remuxing {src.name} -> {dst.name}")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]

    try:
        await exec_ffmpeg_cmd(cmd)
    except BaseException:
        # 同 merge_av：捕 BaseException，取消时也要清掉残缺输出，
        # 否则半截 .remux.mp4 会被下游的 exists() 当成成品。
        await safe_unlink(dst)
        raise
    logger.info(f"Remuxed {dst.name}, {fmt_size(dst)}")


async def encode_video_to_h264(video_path: Path) -> Path:
    """将视频重新编码到 h264"""
    output_path = video_path.with_name(f"{video_path.stem}_h264{video_path.suffix}")
    if output_path.exists():
        return output_path
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        str(output_path),
    ]
    try:
        await exec_ffmpeg_cmd(cmd)
    except BaseException:
        # 同 merge_av：捕 BaseException，取消时也要清掉残缺输出
        await safe_unlink(output_path)
        raise
    logger.info(f"视频重新编码为 H.264 成功: {output_path}, {fmt_size(output_path)}")
    await safe_unlink(video_path)
    return output_path


async def extract_video_first_frame(video_path: Path) -> Path:
    """从视频中提取第一帧"""
    first_frame_path = video_path.with_suffix(".jpg")
    if first_frame_path.exists():
        return first_frame_path

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-ss",
        "00:00:01",
        "-vframes",
        "1",
        str(first_frame_path),
    ]

    await exec_ffmpeg_cmd(cmd)
    return first_frame_path


def fmt_size(file_path: Path) -> str:
    """格式化文件大小"""
    return f"大小: {file_path.stat().st_size / 1024 / 1024:.2f} MB"


def generate_file_name(url: str, default_suffix: str = "") -> str:
    """根据 url 生成文件名"""
    path = Path(urlparse(url).path)
    suffix = path.suffix if path.suffix else default_suffix
    url_hash = hashlib.md5(url.encode()).hexdigest()[:16]
    file_name = f"{url_hash}{suffix}"
    return file_name


def is_module_available(module_name: str) -> bool:
    """检查模块是否可用"""
    import importlib.util
    return importlib.util.find_spec(module_name) is not None


def is_docker_environment() -> bool:
    """当前是否跑在 Docker 容器里（供 WebUI 与缓存目录提示使用）。"""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

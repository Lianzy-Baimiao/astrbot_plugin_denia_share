"""Bounded, cancellable QQ video transcoding. Original downloads stay intact."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from astrbot.api import logger
from .exception import MediaProcessException

# Decimal bytes: below both interpretations of QQ's documented 30 MB limit.
QQ_VIDEO_TARGET_BYTES = 28_000_000
_PROCESS_TIMEOUT = 600


async def _run(*args: str) -> bytes:
    """Never leave an encoder running after timeout or task cancellation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise MediaProcessException("无法启动 ffmpeg/ffprobe，请检查安装与执行权限") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), _PROCESS_TIMEOUT)
    except (asyncio.CancelledError, asyncio.TimeoutError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise MediaProcessException("视频压缩超时，请稍后重试或选择更短的视频") from exc
    if proc.returncode:
        logger.warning("[denia_share] 视频处理退出码 %s: %s", proc.returncode,
                       stderr.decode("utf-8", errors="replace")[-1500:])
        raise MediaProcessException("视频压缩失败，请检查 ffmpeg 日志")
    return stdout


async def _probe(binary: str, source: Path) -> dict:
    raw = await _run(binary, "-v", "error", "-show_entries",
                     "format=duration:stream=codec_type,codec_name,width,height,duration",
                     "-of", "json", str(source))
    try:
        data = json.loads(raw)
        duration = float(data["format"]["duration"])
        streams = data["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        return {"duration": duration, "video": video,
                "audio": any(s.get("codec_type") == "audio" for s in streams)}
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise MediaProcessException("无法读取视频时长或视频轨道，未发送超限原文件") from exc


class OfficialVideoOptimizer:
    """At most one encoder per plugin; publish only verified complete MP4s."""

    def __init__(self):
        self._slot = asyncio.Semaphore(1)

    async def prepare(self, source: Path) -> Path:
        try:
            if 0 < source.stat().st_size <= QQ_VIDEO_TARGET_BYTES:
                return source
            async with self._slot:
                return await self._compress(source)
        except OSError as exc:
            raise MediaProcessException("无法读写视频缓存，未发送超限原文件") from exc

    async def _compress(self, source: Path) -> Path:
        original = source.stat()
        if original.st_size <= 0:
            raise MediaProcessException("视频文件为空")
        if original.st_size <= QQ_VIDEO_TARGET_BYTES:
            return source
        identity = f"qqsmall-v1:{original.st_size}:{original.st_mtime_ns}"
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:12]
        output = source.with_name(f"{source.stem}.qqsmall-{suffix}.mp4")
        # Only this module atomically publishes this versioned filename.
        if output.is_file() and 0 < output.stat().st_size <= QQ_VIDEO_TARGET_BYTES:
            return output
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise MediaProcessException(
                "视频超过 28 MB，需要安装 ffmpeg 和 ffprobe 才能压缩；未发送超限原文件"
            )
        metadata = await _probe(ffprobe, source)
        duration = metadata["duration"]
        audio_rate = 48_000 if metadata["audio"] else 0
        # Reserve 8% for container overhead/rate-control deviations. No -fs/-t:
        # those would meet a byte target by cutting off the end of the video.
        bitrate = int(QQ_VIDEO_TARGET_BYTES * 8 * 0.92 / duration) - audio_rate
        if bitrate < 32_000:
            raise MediaProcessException("视频过长，无法在保留完整内容的同时压到 28 MB")
        logger.info("[denia_share] 官方 Bot 压缩视频 %.2f MB → ≤28 MB，时长 %.1f 秒",
                    original.st_size / 1_000_000, duration)
        with tempfile.TemporaryDirectory(prefix=".qq-video-", dir=source.parent) as temp:
            pending = Path(temp) / "video.mp4"
            for attempt in range(2):
                passlog = str(Path(temp) / f"pass-{attempt}")
                common = [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(source), "-map", "0:v:0", "-sn", "-dn",
                    "-vf", "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                    "-r", "24", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-threads", "2", "-b:v", str(bitrate),
                    "-passlogfile", passlog,
                ]
                await _run(*common, "-pass", "1", "-an", "-f", "null", os.devnull)
                await _run(*common, "-pass", "2", "-map", "0:a:0?",
                           "-c:a", "aac", "-b:a", "48k", "-ac", "2",
                           "-map_metadata", "-1", "-movflags", "+faststart", str(pending))
                if not pending.is_file() or pending.stat().st_size == 0:
                    raise MediaProcessException("压缩未生成有效视频")
                if pending.stat().st_size <= QQ_VIDEO_TARGET_BYTES:
                    checked = await _probe(ffprobe, pending)
                    if (checked["video"].get("codec_name") != "h264"
                            or checked["audio"] != metadata["audio"]
                            or abs(checked["duration"] - duration) > max(1.0, duration * 0.01)):
                        raise MediaProcessException("压缩后视频完整性校验失败，未发送残缺视频")
                    current = source.stat()
                    if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                        raise MediaProcessException("压缩期间源视频发生变化，请重试")
                    pending.replace(output)
                    logger.info("[denia_share] 官方 Bot 视频压缩完成：%.2f MB", output.stat().st_size / 1_000_000)
                    return output
                bitrate = max(32_000, int(bitrate * 0.75))
        raise MediaProcessException("视频压缩后仍超过 28 MB，未发送超限原文件")

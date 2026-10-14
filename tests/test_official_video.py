"""Offline regressions; optional real ffmpeg smoke test when tools are on PATH."""
from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock, Mock, patch

from test_share_output import ROOT, api, config
with patch.dict(sys.modules, {"astrbot.api": api}):
    from core import official_video as media
    from core.douyin_quality import low_bitrate_urls
    from core.exception import DownloadException, IgnoreException, ParseException, SilentException, MediaProcessException


def compile_methods(relative, names, scope):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8-sig"))
    methods = [m for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body
               if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in names]
    cls = ast.ClassDef(name="Subject", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls,
    ], type_ignores=[]))
    exec(compile(module, relative, "exec"), scope)
    if relative == "core/parsers/douyin/parser.py":
        scope["DouyinParser"] = scope["Subject"]
    return scope["Subject"]()


def variant(url, rate=None, size=None, width=None, height=None):
    return {"bit_rate": rate, "play_addr": {"url_list": [url], "data_size": size,
                                            "width": width, "height": height}}


class QualityTests(unittest.TestCase):
    def test_lowest_bitrate_first_and_unknown_last(self):
        self.assertEqual(low_bitrate_urls([
            variant("https://cdn/high", 2_000_000), variant("https://cdn/unknown"),
            variant("https://cdn/low", "400000"), variant("https://cdn/mid", 800_000),
        ]), ["https://cdn/low", "https://cdn/mid", "https://cdn/high", "https://cdn/unknown"])

    def test_size_and_resolution_fallback(self):
        self.assertEqual(low_bitrate_urls([variant("https://h", size=200), variant("https://l", size=100)])[0], "https://l")
        self.assertEqual(low_bitrate_urls([variant("https://h", width=1920, height=1080), variant("https://l", width=640, height=360)])[0], "https://l")

    def test_malformed_metadata_and_deduplication(self):
        self.assertEqual(low_bitrate_urls(None), [])
        self.assertEqual(low_bitrate_urls([None, "x", {}, variant("file:///etc/passwd", 1)]), [])
        self.assertEqual(low_bitrate_urls([variant("https://cdn/playwm/x", "nan"), variant("https://cdn/play/x", -1)]), ["https://cdn/play/x"])

    def test_web_api_default_and_low_modes(self):
        parser = compile_methods("core/parsers/douyin/parser.py", {"_first_url", "_no_watermark", "_video_urls"}, {"low_bitrate_urls": low_bitrate_urls})
        item = {"video": {"play_addr": {"url_list": ["https://original"]}, "bit_rate": [variant("https://high", 200), variant("https://low", 100)]}}
        self.assertEqual(parser._video_urls(item)[0], "https://original")
        self.assertEqual(parser._video_urls(item, prefer_low_quality=True)[0], "https://low")

    def test_config_defaults_and_schema(self):
        cfg = config.ParserConfig({}, ROOT, ROOT)
        self.assertTrue(cfg.OFFICIAL_VIDEO_OPTIMIZE)
        self.assertFalse(config.ParserConfig({"OFFICIAL_VIDEO_OPTIMIZE": False}, ROOT, ROOT).OFFICIAL_VIDEO_OPTIMIZE)
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(config.verify_schema_alignment(schema), [])
        self.assertTrue(schema["媒体发送"]["items"]["OFFICIAL_VIDEO_OPTIMIZE"]["default"])


class ParserTests(unittest.IsolatedAsyncioTestCase):
    def parser(self):
        names = {"parse_video", "_download_video_candidates", "_create_selected_video"}
        parser = compile_methods("core/parsers/douyin/parser.py", names,
            {"__package__": "core.parsers.douyin", "re": re, "asyncio": asyncio,
             "logger": api.logger, "ParseException": ParseException,
             "DownloadException": DownloadException, "low_bitrate_urls": low_bitrate_urls})
        parser.prefer_low_quality = True
        parser.headers = {}
        parser.downloader = SimpleNamespace(download_video=AsyncMock(side_effect=[DownloadException(), Path("ok.mp4")]))
        return parser

    async def test_stale_low_url_falls_back(self):
        parser = self.parser()
        self.assertEqual(await parser._download_video_candidates(["https://low", "https://original"]), Path("ok.mp4"))
        self.assertEqual(parser.downloader.download_video.await_args_list[0].args[0], "https://low")

    async def test_size_limit_and_cancellation_not_swallowed(self):
        for error in (IgnoreException(), asyncio.CancelledError()):
            parser = self.parser()
            parser.downloader.download_video.side_effect = error
            with self.assertRaises(type(error)):
                await parser._download_video_candidates(["https://low", "https://original"])
            self.assertEqual(parser.downloader.download_video.await_count, 1)

    @unittest.skipUnless(importlib.util.find_spec("msgspec"), "msgspec not installed")
    async def test_real_ssr_decoder_keeps_renditions_and_images(self):
        parser = self.parser()
        item = {"create_time": 1, "desc": "example", "author": {"nickname": "author"},
                "video": {"duration": 6000, "cover": {"url_list": []}, "play_addr": {"url_list": ["https://original"]},
                          "bit_rate": [variant("https://high", 200), variant("https://low", 100)]}}
        parser.ios_headers = {}
        parser.create_author = Mock(return_value=None)
        parser.result = lambda **kw: SimpleNamespace(contents=[], **kw)
        parser._add_limit_warning = Mock()
        parser._create_selected_video = Mock(return_value="video")
        parser.create_images = Mock(return_value=["image"])
        session = ModuleType("core.parsers.douyin.session")
        session.looks_like_challenge = lambda text: False
        fake_session = SimpleNamespace(fetch=AsyncMock())
        session.get_share_session = lambda cls: fake_session
        for images in (None, [{"url_list": ["https://image"]}]):
            item["images"] = images
            fake_session.fetch.return_value = "window._ROUTER_DATA = " + json.dumps({"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [item]}}}}) + "</script>"
            with patch.dict(sys.modules, {"core.parsers.douyin.session": session}):
                result = await parser.parse_video("https://share")
            if images:
                self.assertEqual(result.contents, ["image"])
                self.assertEqual(parser._create_selected_video.call_count, 1)
            else:
                self.assertEqual(parser._create_selected_video.call_args.args[0][0], "https://low")

    async def test_request_quality_and_cache_do_not_leak_to_onebot(self):
        class Parser:
            def __init__(self, downloader=None, *, prefer_low_quality=False):
                self.prefer_low_quality = prefer_low_quality
            def search_url(self, url):
                return "douyin", None
            async def parse(self, *_):
                return "low" if self.prefer_low_quality else "original"
        cfg = SimpleNamespace(OFFICIAL_VIDEO_OPTIMIZE=True)
        scope = dict(DouyinParser=Parser, get_config=lambda: cfg, hashlib=hashlib,
                     logger=api.logger, SilentException=SilentException, ParseException=ParseException)
        plugin = compile_methods("main.py", {"_process_url", "result_cache_key"}, scope)
        plugin._access_denied = lambda ev: False
        plugin._is_qq_official = lambda ev: ev.official
        plugin._result_cache = {}
        plugin._render_cache = {}
        plugin._remember_result = lambda key, result: plugin._result_cache.__setitem__(key, result)
        plugin._record_history = AsyncMock()
        plugin.downloader = None
        async def deliver(ev, result, key):
            yield result
        plugin._deliver = deliver
        parser = Parser()
        async def run(official):
            return [r async for r in plugin._process_url(SimpleNamespace(official=official, message_str="https://douyin"), parser)]
        self.assertEqual(await run(False), ["original"])
        self.assertEqual(await run(True), ["low"])
        self.assertEqual(await run(False), ["original"])
        self.assertFalse(parser.prefer_low_quality)
        self.assertEqual(len(plugin._result_cache), 2)


class OptimizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.mp4"
        self.source.write_bytes(b"x" * 101)
        self.optimizer = media.OfficialVideoOptimizer()
        self.meta = {"duration": 8.0, "video": {"codec_name": "h264"}, "audio": True}

    async def test_small_video_does_not_require_ffmpeg(self):
        with patch.object(media.shutil, "which", return_value=None):
            self.assertEqual(await self.optimizer.prepare(self.source), self.source)

    async def test_large_video_without_ffmpeg_has_actionable_error(self):
        with patch.object(media, "QQ_VIDEO_TARGET_BYTES", 100), patch.object(media.shutil, "which", return_value=None):
            with self.assertRaisesRegex(MediaProcessException, "ffmpeg"):
                await self.optimizer.prepare(self.source)

    async def test_compression_verified_cached_and_source_preserved(self):
        # Use a realistic byte budget so the minimum bitrate remains meaningful.
        self.source.write_bytes(b"x" * 300_001)
        calls = []
        async def run(*args):
            calls.append(args)
            if args[-1].endswith(".mp4"):
                Path(args[-1]).write_bytes(b"mp4" * 50_000)
        with patch.object(media, "QQ_VIDEO_TARGET_BYTES", 300_000), patch.object(media.shutil, "which", side_effect=lambda s: s), patch.object(media, "_probe", AsyncMock(return_value=self.meta)), patch.object(media, "_run", run):
            output = await self.optimizer.prepare(self.source)
            self.assertLessEqual(output.stat().st_size, 300_000)
            self.assertEqual(await self.optimizer.prepare(self.source), output)
            self.assertEqual(len(calls), 2)
            self.assertFalse(any("-fs" in call or "-t" in call for call in calls))
        self.assertEqual(self.source.read_bytes(), b"x" * 300_001)
        self.assertFalse(list(Path(self.temp.name).glob(".qq-video-*")))

    async def test_bad_duration_and_oversize_never_published(self):
        for oversize in (False, True):
            self.source.write_bytes(b"x" * 300_001)
            async def run(*args):
                if args[-1].endswith(".mp4"):
                    Path(args[-1]).write_bytes(b"x" * (300_001 if oversize else 50))
            invalid = dict(self.meta, duration=2)
            probe = AsyncMock(side_effect=[self.meta, invalid]) if not oversize else AsyncMock(return_value=self.meta)
            with patch.object(media, "QQ_VIDEO_TARGET_BYTES", 300_000), patch.object(media.shutil, "which", side_effect=lambda s: s), patch.object(media, "_probe", probe), patch.object(media, "_run", run):
                with self.assertRaises(MediaProcessException):
                    await self.optimizer.prepare(self.source)
            self.assertEqual(list(Path(self.temp.name).iterdir()), [self.source])

    async def test_cancellation_kills_subprocess(self):
        proc = SimpleNamespace(returncode=None, kill=Mock(), communicate=AsyncMock(side_effect=[asyncio.CancelledError(), (b"", b"")]))
        with patch.object(media.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaises(asyncio.CancelledError):
                await media._run("ffmpeg")
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.await_count, 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    async def test_real_encoder_size_duration_audio_and_cache(self):
        await media._run(shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", str(self.source))
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(media, "QQ_VIDEO_TARGET_BYTES", 220_000):
            output = await self.optimizer.prepare(self.source)
            self.assertLessEqual(output.stat().st_size, 220_000)
            checked = await media._probe(shutil.which("ffprobe"), output)
            self.assertEqual(checked["video"]["codec_name"], "h264")
            self.assertTrue(checked["audio"])
            self.assertAlmostEqual(checked["duration"], 6, delta=0.25)
            self.assertLessEqual(max(checked["video"]["width"], checked["video"]["height"]), 640)
            self.assertEqual(await self.optimizer.prepare(self.source), output)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)


class SendTests(unittest.IsolatedAsyncioTestCase):
    async def test_optimized_path_used_for_local_and_relay(self):
        for relay_first in (False, True):
            register = AsyncMock(return_value="https://relay/small.mp4")
            scope = dict(logger=api.logger, MediaProcessException=MediaProcessException,
                Comp=SimpleNamespace(Video=SimpleNamespace(fromFileSystem=lambda p: ("local", p), fromURL=lambda p: ("url", p))),
                resolve_callback_base=lambda b: b, register_file=register,
                pad_to_chunked_upload=AsyncMock(side_effect=lambda p: p))
            plugin = compile_methods("main.py", {"_send_official_video"}, scope)
            plugin._official_video_optimizer = SimpleNamespace(prepare=AsyncMock(return_value=Path("small.mp4")))
            plugin._prepare_official_video = AsyncMock(side_effect=lambda p, cfg: p)
            plugin._post_official_media = AsyncMock(side_effect=[False, True])
            cfg = SimpleNamespace(OFFICIAL_VIDEO_OPTIMIZE=True, OFFICIAL_RELAY_FIRST=relay_first, MEDIA_RELAY_CALLBACK_URL="https://relay", MEDIA_RELAY_TTL=300)
            result = [r async for r in plugin._send_official_video(SimpleNamespace(), Path("large.mp4"), "https://original", cfg)]
            self.assertEqual(result, [])
            register.assert_awaited_once_with(Path("small.mp4"), "https://relay", 300)
            plugin._prepare_official_video.assert_awaited_once_with(Path("small.mp4"), cfg)
            self.assertEqual(plugin._post_official_media.await_count, 2)

    async def test_failed_compression_never_sends_original(self):
        plugin = compile_methods("main.py", {"_send_official_video"}, dict(logger=api.logger, MediaProcessException=MediaProcessException))
        plugin._official_video_optimizer = SimpleNamespace(prepare=AsyncMock(side_effect=MediaProcessException("need ffmpeg")))
        plugin._post_official_media = AsyncMock()
        result = [r async for r in plugin._send_official_video(SimpleNamespace(plain_result=lambda s: s), Path("large.mp4"), "https://original", SimpleNamespace(OFFICIAL_VIDEO_OPTIMIZE=True))]
        self.assertIn("ffmpeg", result[0])
        self.assertIn("https://original", result[0])
        plugin._post_official_media.assert_not_called()

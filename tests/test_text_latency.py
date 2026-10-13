"""Latency regressions with controlled pending I/O, no network or bot required."""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from test_share_output import ROOT, api, config, load_delivery, Event, Plain
with patch.dict(sys.modules, {"astrbot.api": api}):
    from core.data import ParseResult, Platform, VideoContent
    from core.task import PathTask
    from core.exception import IgnoreException, DownloadException, MediaProcessException


def bili_harness(cache_dir, *, block_credentials=False):
    """Compile real parser methods, fake only SDK metadata/download boundaries."""
    tree = ast.parse((ROOT / 'core/parsers/bilibili.py').read_text(encoding='utf-8-sig'))
    wanted = {'parse_video', '_credential_for_metadata', '_call_bili_api_with_retry', '_is_transient_api_error', 'extract_download_urls'}
    methods = [m for c in tree.body if isinstance(c, ast.ClassDef) and c.name == 'BilibiliParser' for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in wanted]
    model = ModuleType('core.models.bilibili.video')
    model.VideoInfo = type('VideoInfo', (), {})
    model.AIConclusion = type('AIConclusion', (), {})
    gate = asyncio.Event()
    optional_gate = asyncio.Event()
    credential_calls = []
    credential = object()
    class Base:
        @property
        async def credential(self):
            credential_calls.append(True)
            if block_credentials:
                await gate.wait()
            return credential
    page = SimpleNamespace(index=0, title='山东青岛王哥庄新鲜大馒头 一个卖188', duration=168, timestamp=1, cid=123, cover=None)
    info = SimpleNamespace(bvid='BV1G4bw6eEWC', owner=SimpleNamespace(name='作者', face=None), desc='长简介', stat=SimpleNamespace(like=1, coin=1, favorite=1, share=1, reply=1, view=1075000, danmaku=1), rights={}, extract_info_with_page=lambda n: page)
    async def ai(**kwargs):
        await optional_gate.wait()
        return SimpleNamespace(summary='AI摘要')
    async def online(**kwargs):
        await optional_gate.wait()
        return {'total': 10, 'count': 2}
    video = SimpleNamespace(get_info=AsyncMock(return_value=info), get_ai_conclusion=AsyncMock(side_effect=ai), get_online=AsyncMock(side_effect=online))
    sdk = Mock(return_value=video)
    scope = dict(__package__='core.parsers', Base=Base, asyncio=asyncio, logger=api.logger, Video=sdk, convert=lambda obj, kind: obj, fmt_duration=lambda s:f'{int(s)//60}:{int(s)%60:02d}', IgnoreException=IgnoreException, DownloadException=DownloadException, MediaProcessException=MediaProcessException, Credential=SimpleNamespace(from_cookies=Mock(return_value=credential)), ck2dict=lambda value: {'cookie':value})
    cls = ast.ClassDef(name='Parser', bases=[ast.Name(id='Base', ctx=ast.Load())], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), cls], type_ignores=[]))
    exec(compile(module, str(ROOT / 'core/parsers/bilibili.py'), 'exec'), scope)
    parser = scope['Parser']()
    parser._credential = credential
    parser._bili_ck = None
    parser._load_credential = Mock()
    parser.create_author = lambda *a: None
    parser.headers = {}
    tasks = []
    def create_video(task, cover, duration):
        tasks.append(task)
        return VideoContent(PathTask(task), duration=duration)
    parser.create_video = create_video
    parser.result = lambda **kwargs: ParseResult(platform=Platform('bilibili', '哔哩哔哩'), **kwargs)
    async def extract(**kwargs):
        await gate.wait()
        return 'video-url', [], None, []
    parser.real_extract_download_urls = parser.extract_download_urls
    parser.extract_download_urls = AsyncMock(side_effect=extract)
    parser.downloader = SimpleNamespace(_media_slots=None, _download_file=AsyncMock(return_value=cache_dir / 'video.mp4'))
    pconfig = config.ParserConfig({}, cache_dir, cache_dir)
    return SimpleNamespace(scope=scope, credential=credential, parser=parser, video=video, sdk=sdk, gate=gate, optional_gate=optional_gate, credential_calls=credential_calls, tasks=tasks, pconfig=pconfig, model=model)


class BilibiliLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_metadata_returns_before_optional_apis_and_media(self):
        await self.check_fast_metadata(block_credentials=False)

    async def test_credential_validation_and_refresh_do_not_gate_metadata(self):
        await self.check_fast_metadata(block_credentials=True)

    async def test_media_url_uses_validated_credential_not_metadata_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = bili_harness(Path(tmp))
            sdk = ModuleType("bilibili_api.video")
            for name in ("AudioStreamDownloadURL", "VideoStreamDownloadURL", "FLVStreamDownloadURL",
                         "MP4StreamDownloadURL", "VideoDownloadURLDataDetecter", "VideoCodecs"):
                setattr(sdk, name, Mock())
            sdk.VideoQuality = SimpleNamespace(**{name: name for name in (
                "_360P", "_480P", "_720P", "_1080P", "_1080P_PLUS", "_4K", "_8K")})
            stale = object()
            h.video.credential = stale
            async def request_url(**kwargs):
                self.assertIs(h.video.credential, h.credential)
                raise IgnoreException("stop after verifying credential")
            h.video.get_download_url = AsyncMock(side_effect=request_url)
            with patch.dict(sys.modules, {"bilibili_api.video": sdk, "core.config": config}), patch.object(config, "get_config", return_value=h.pconfig):
                with self.assertRaises(IgnoreException):
                    await h.parser.real_extract_download_urls(video=h.video)
            self.assertEqual(h.credential_calls, [True])
            h.video.get_download_url.assert_awaited_once()

    async def test_metadata_cookie_sources_do_not_validate_or_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = bili_harness(Path(tmp), block_credentials=True)
            self.assertIs(h.parser._credential_for_metadata(), h.credential)
            h.parser._load_credential.assert_not_called()
            h.parser._credential = None
            h.parser._bili_ck = "configured-cookie"
            self.assertIs(h.parser._credential_for_metadata(), h.credential)
            h.scope["Credential"].from_cookies.assert_called_once()
            h.parser._load_credential.assert_not_called()
            h.parser._bili_ck = None
            def load_saved():
                h.parser._credential = h.credential
            h.parser._load_credential.side_effect = load_saved
            self.assertIs(h.parser._credential_for_metadata(), h.credential)
            h.parser._load_credential.assert_called_once()
            self.assertEqual(h.credential_calls, [])

    async def check_fast_metadata(self, *, block_credentials):
        with tempfile.TemporaryDirectory() as tmp:
            h = bili_harness(Path(tmp), block_credentials=block_credentials)
            try:
                with patch.dict(sys.modules, {'core.models.bilibili.video': h.model, 'core.config': config, 'astrbot.api': api}), patch.object(config, 'get_config', return_value=h.pconfig):
                    parsed = await asyncio.wait_for(h.parser.parse_video(bvid='BV1G4bw6eEWC'), timeout=0.1)
                    h.video.get_info.assert_awaited_once()
                    self.assertEqual(h.credential_calls, [])
                    h.video.get_ai_conclusion.assert_not_awaited()
                    h.video.get_online.assert_not_awaited()
                    self.assertFalse(h.tasks[0].done())
                    plugin = load_delivery(['文本', '富媒体'])
                    stream = plugin._deliver(Event(), parsed, 'key')
                    try:
                        first = await asyncio.wait_for(anext(stream), timeout=0.1)
                        self.assertIsInstance(first[0], Plain)
                        self.assertEqual(first[0].text, '山东青岛王哥庄新鲜大馒头 一个卖188\nhttps://bilibili.com/BV1G4bw6eEWC\n⏱ 2:48 · 播放 107.5万')
                        self.assertFalse(h.tasks[0].done(), 'Text must be sent before download completes')
                        h.gate.set()
                        remaining = [msg async for msg in stream]
                        self.assertFalse(any(isinstance(c, Plain) for msg in remaining for c in msg))
                    finally:
                        await stream.aclose()
                    self.assertTrue(callable(parsed.card_details_loader))
                    h.optional_gate.set()
                    await parsed.prepare_card_details()
                    h.video.get_ai_conclusion.assert_awaited_once()
                    h.video.get_online.assert_awaited_once()
                    self.assertEqual(parsed.extra['info'], 'AI摘要')
                    self.assertIn('10 人', parsed.extra['online'])
            finally:
                for task in h.tasks:
                    task.cancel()
                await asyncio.gather(*h.tasks, return_exceptions=True)


class CardDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_details_are_lazy_and_shared_by_concurrent_renders(self):
        gate = asyncio.Event()
        calls = []
        async def load():
            calls.append(True)
            await gate.wait()
        parsed = ParseResult(Platform('bilibili', '哔哩哔哩'), card_details_loader=load)
        self.assertEqual(calls, [])
        first = asyncio.create_task(parsed.prepare_card_details())
        second = asyncio.create_task(parsed.prepare_card_details())
        gate.set()
        await asyncio.gather(first, second)
        await parsed.prepare_card_details()
        self.assertEqual(calls, [True])
        json.dumps(parsed.extra)

    async def test_optional_failure_does_not_break_card(self):
        load = AsyncMock(side_effect=RuntimeError('optional failure'))
        parsed = ParseResult(Platform('bilibili', '哔哩哔哩'), card_details_loader=load)
        with patch.dict(sys.modules, {'astrbot.api': api}):
            await parsed.prepare_card_details()
            await parsed.prepare_card_details()
        load.assert_awaited_once()

    async def test_optional_timeout_is_bounded_and_cancels_loader(self):
        gate = asyncio.Event()
        canceled = []
        async def load():
            try:
                await gate.wait()
            finally:
                canceled.append(True)
        parsed = ParseResult(Platform('bilibili', '哔哩哔哩'), card_details_loader=load)
        with patch.dict(sys.modules, {'astrbot.api': api}), patch.object(ParseResult, 'CARD_DETAILS_TIMEOUT', 0.01):
            await asyncio.wait_for(parsed.prepare_card_details(), timeout=0.1)
        self.assertEqual(canceled, [True])


class RendererDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_render_entry_loads_details_only_when_needed(self):
        with patch.dict(sys.modules, {"astrbot.api": api}):
            from core.card_renderer import ShareCardRenderer
        with tempfile.TemporaryDirectory() as tmp:
            renderer = ShareCardRenderer(Path(tmp))
            self.assertTrue(renderer.enabled, "Pillow is required for the render-entry regression")
            parsed = ParseResult(Platform("bilibili", "哔哩哔哩"), title="标题")
            async def load():
                parsed.extra["info"] = "补充摘要"
            parsed.card_details_loader = AsyncMock(side_effect=load)
            path = Path(tmp) / "card.png"
            def draw(result, images, output):
                self.assertEqual(result.extra["info"], "补充摘要")
                output.write_bytes(b"cached-card")
                return output
            with patch.object(renderer, "_collect_images", AsyncMock(return_value={})), patch.object(renderer, "_render_sync", Mock(side_effect=draw)) as render, patch.object(renderer, "_output_path", return_value=path):
                self.assertEqual(await renderer.render(parsed), path)
                render.assert_called_once()
                parsed.card_details_loader.assert_awaited_once()
                # Disk-cache and explicit-cache hits do not request details again.
                parsed.card_details_loader = AsyncMock(side_effect=AssertionError("cache must skip loading"))
                self.assertEqual(await renderer.render(parsed), path)
                self.assertEqual(await renderer.render(parsed, existing=path), path)
                parsed.card_details_loader.assert_not_awaited()
                renderer.enabled = False
                self.assertIsNone(await renderer.render(parsed))
                parsed.card_details_loader.assert_not_awaited()

    async def test_optional_failure_still_renders_card(self):
        with patch.dict(sys.modules, {"astrbot.api": api}):
            from core.card_renderer import ShareCardRenderer
        with tempfile.TemporaryDirectory() as tmp:
            renderer = ShareCardRenderer(Path(tmp))
            parsed = ParseResult(Platform("bilibili", "哔哩哔哩"), card_details_loader=AsyncMock(side_effect=RuntimeError("optional failure")))
            path = Path(tmp) / "card.png"
            with patch.object(renderer, "_collect_images", AsyncMock(return_value={})), patch.object(renderer, "_render_sync", Mock(return_value=path)) as render:
                self.assertEqual(await renderer.render(parsed), path)
                render.assert_called_once()


if __name__ == '__main__':
    unittest.main()

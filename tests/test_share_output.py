"""Offline output-contract tests: run with python -m unittest discover -s tests -v.

Only AstrBot boundary objects are faked. The delivery/build methods are compiled
from main.py unchanged so the real send path runs without installing the bot,
starting its web server, or downloading media.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import itertools
import json
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.share_text import build_share_text, clean_share_url, source_url, split_markdown
api = ModuleType("astrbot.api")
api.logger = Mock()  # 上架检查只允许 astrbot.api 的 logger，测试里用替身
with patch.dict(sys.modules, {"astrbot": ModuleType("astrbot"), "astrbot.api": api}):
    from core import config, send_mode


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, file):
        self.file = file

    @classmethod
    def fromFileSystem(cls, file):
        return cls(file)

    @classmethod
    def fromURL(cls, url):
        return cls(url)


class Node:
    def __init__(self, content):
        self.content = content


class Nodes:
    def __init__(self, nodes):
        self.nodes = nodes


class ImageContent:
    def __init__(self, path):
        self.path_task = SimpleNamespace(safe_get=AsyncMock(return_value=path))


class Event:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"
    message_str = ""

    def get_group_id(self):
        return "123"

    def get_sender_name(self):
        return "tester"

    def get_sender_id(self):
        return "456"

    def plain_result(self, text):
        return [Plain(text)]

    def chain_result(self, chain):
        return chain


def result(**overrides):
    data = dict(
        title="视频标题", text="不应发送的长简介", url="https://www.bilibili.com/video/BV123?p=2&vd_source=tracking&spm_id_from=333.999",
        platform=SimpleNamespace(name="bilibili", display_name="哔哩哔哩"),
        author=SimpleNamespace(name="作者"), content_type="视频",
        extra={"stats_line": "👍 1.2万 🪙 300 👀 12.3万 💭 200", "duration": "3:25", "online": "100 人正在观看", "info": "不应发送的 AI 总结"},
        video_contents=[SimpleNamespace(duration=205)], audio_contents=[],
        img_contents=[], graphics=[], audit_missing_media=AsyncMock(return_value={}), video=None,
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def load_delivery(mode, *, onebot=True, card=None, render_enabled=True, overrides="", template=""):
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    names = {"_deliver", "_build_output", "_send_plain_output", "_build_nodes_result", "_is_media_item", "_markdown_chain"}
    methods = [m for c in tree.body if isinstance(c, ast.ClassDef) for m in c.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in names]
    cls = ast.ClassDef(name="Delivery", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[]))
    cfg = config.ParserConfig({"SEND_MODE": mode, "RENDER_ENABLED": render_enabled, "SEND_MODE_OVERRIDES": overrides, "TEXT_TEMPLATE": template}, ROOT, ROOT)
    scope = dict(
        Comp=SimpleNamespace(Plain=Plain, Image=Image, Node=Node, Nodes=Nodes),
        ImageContent=ImageContent, logger=api.logger, get_config=lambda: cfg,
        Path=Path, split_nodes=send_mode.split_nodes, pick_items=send_mode.pick_items,
    )
    if (ROOT / "core/share_text.py").exists():
        share_text = importlib.import_module("core.share_text")
        scope.update(build_share_text=share_text.build_share_text, split_markdown=share_text.split_markdown)
    exec(compile(module, str(ROOT / "main.py"), "exec"), scope)
    plugin = scope["Delivery"]()
    plugin._renderer = SimpleNamespace(enabled=True, render=AsyncMock(return_value=card))
    plugin._render_cache = {}
    plugin._send_image = AsyncMock()
    plugin._platform_name = lambda event: "aiocqhttp" if onebot else "qq_official"
    plugin._is_onebot = lambda event: onebot
    plugin._is_qq_official = lambda event: not onebot
    plugin.media_sent = []

    async def send_media(event, parsed):
        plugin.media_sent.extend(parsed.video_contents + parsed.audio_contents)
        if False:
            yield None

    plugin._try_send_media = send_media
    return plugin


class ModeTests(unittest.TestCase):
    def test_modes(self):
        for mode, flags in {
            "全部发送": (True, True, True),
            "文本+富媒体": (False, True, True),
            "仅文本": (False, True, False),
            "仅富媒体": (False, False, True),
        }.items():
            with self.subTest(mode=mode):
                self.assertTrue(send_mode.is_send_mode(mode))
                self.assertEqual(send_mode.mode_flags(mode), flags)

    def test_override_priority_and_new_mode(self):
        overrides = "qq_official=仅文本,123=仅富媒体,qq_official:GroupMessage:123=文本+富媒体"
        self.assertEqual(send_mode.resolve_mode_flags(overrides, "全部发送", "QQ_OFFICIAL:GroupMessage:123", "123", "qq_official"), (False, True, True))
        self.assertEqual(send_mode.resolve_mode_flags(overrides, "全部发送", "other", "123", "qq_official"), (False, False, True))
        self.assertEqual(send_mode.resolve_mode_flags(overrides, "全部发送", "other", "other", "qq_official"), (False, True, False))

    def test_schema_and_webui_options_agree(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        item = next(item for item in config.CONFIG_META if item["key"] == "SEND_MODE")
        stored = next(group["items"]["SEND_MODE"] for group in schema.values() if "SEND_MODE" in group.get("items", {}))
        self.assertEqual(item["type"], "multiselect")
        self.assertEqual(stored["type"], "list")
        self.assertEqual(item["options"], ["文本", "卡片", "富媒体"])
        self.assertEqual(item["options"], stored["options"])
        self.assertEqual(item["default"], stored["default"])
        self.assertEqual(config.verify_schema_alignment(schema), [])


class SelectionConfigTests(unittest.TestCase):
    def test_all_eight_combinations(self):
        for text, card, media in itertools.product((False, True), repeat=3):
            picked = [name for name, yes in zip(("文本", "卡片", "富媒体"), (text, card, media)) if yes]
            with self.subTest(picked=picked):
                self.assertTrue(send_mode.is_send_mode(picked))
                self.assertEqual(send_mode.mode_flags(picked), (card, text, media))
                cfg = config.ParserConfig({"SEND_MODE": picked}, ROOT, ROOT)
                self.assertEqual(cfg.SEND_MODE, picked)
                self.assertEqual(cfg.current_values()["SEND_MODE"], picked)

    def test_validation_and_canonical_order(self):
        item = next(item for item in config.CONFIG_META if item["key"] == "SEND_MODE")
        for raw, expected in (([], []), (["卡片", "文本", "文本"], ["文本", "卡片"]), ("仅文本", ["文本"]), ("文本+卡片", ["文本", "卡片"])):
            with self.subTest(raw=raw):
                self.assertEqual(config.coerce_value(item, raw), (expected, None))
        for raw in (None, {}, ["无效"], ["文本", 1], ["文本", []], "", "文本+", "任意"):
            with self.subTest(raw=raw):
                self.assertFalse(send_mode.is_send_mode(raw))
                self.assertIsNotNone(config.coerce_value(item, raw)[1])
                self.assertEqual(send_mode.mode_flags(raw), (True, True, True))

    def test_legacy_migration_and_precedence(self):
        all_modes = ["文本", "卡片", "富媒体"]
        cases = [
            ({"SEND_MODE": "仅文本"}, ["文本"]),
            ({"媒体发送": {"SEND_MODE": "仅富媒体"}}, ["富媒体"]),
            ({"SEND_MODE": "仅文本", "媒体发送": {"SEND_MODE": "全部发送"}}, ["文本"]),
            ({"SEND_MODE": "仅文本", "媒体发送": {"SEND_MODE": all_modes}}, ["文本"]),
            ({"SEND_MODE": "仅文本", "媒体发送": {"SEND_MODE": "文本+富媒体"}}, ["文本", "富媒体"]),
            ({"SEND_MODE": "全部发送", "媒体发送": {"SEND_MODE": []}}, []),
            ({"SEND_MODE": [], "媒体发送": {"SEND_MODE": "全部发送"}}, []),
            ({"媒体发送": {"SEND_MODE": "全部发送"}}, all_modes),
        ]
        for stored, expected in cases:
            with self.subTest(stored=stored):
                cfg = config.ParserConfig(stored, ROOT, ROOT)
                self.assertEqual(cfg.SEND_MODE, expected)
                self.assertEqual(cfg.current_values()["SEND_MODE"], expected)
                self.assertTrue(config.migrate_grouped_config(stored))
                self.assertEqual(stored["媒体发送"]["SEND_MODE"], expected)
                self.assertEqual(cfg.SEND_MODE, expected)
                self.assertFalse(config.migrate_grouped_config(stored))

    def test_save_roundtrip_including_empty_selection(self):
        class StoredConfig(dict):
            def save_config(self):
                self.saved = json.dumps(self, ensure_ascii=False)
        stored = StoredConfig({"SEND_MODE": "仅富媒体"})
        cfg = config.ParserConfig(stored, ROOT, ROOT)
        with patch.dict(sys.modules, {"astrbot.api": api}):
            for selection in (["文本", "卡片"], [], ["文本", "卡片", "富媒体"]):
                with self.subTest(selection=selection):
                    changed, errors = cfg.apply_updates({"SEND_MODE": selection})
                    self.assertEqual(errors, [])
                    self.assertEqual(changed, ["SEND_MODE"])
                    reloaded = config.ParserConfig(json.loads(stored.saved), ROOT, ROOT)
                    self.assertEqual(reloaded.SEND_MODE, selection)
                    self.assertEqual(cfg.apply_updates({"SEND_MODE": selection}), ([], []))

    def test_returned_selection_does_not_mutate_defaults(self):
        cfg = config.ParserConfig({}, ROOT, ROOT)
        cfg.SEND_MODE.clear()
        cfg.current_values()["SEND_MODE"].clear()
        self.assertEqual(cfg.SEND_MODE, ["文本", "卡片", "富媒体"])

    def test_new_overrides_and_card_master_switch(self):
        overrides = "aiocqhttp=文本+富媒体,123=文本+卡片,aiocqhttp:GroupMessage:123=卡片,456=不发送,bad=文本+错误"
        self.assertEqual(send_mode.resolve_mode_flags(overrides, [], "123", "aiocqhttp"), (True, True, False))
        self.assertEqual(send_mode.resolve_mode_flags(overrides, [], "aiocqhttp:GroupMessage:123", "123", "aiocqhttp"), (True, False, False))
        self.assertEqual(send_mode.resolve_mode_flags(overrides, "全部发送", "456"), (False, False, False))
        stored = {"SEND_MODE": ["文本", "卡片", "富媒体"], "SEND_MODE_OVERRIDES": overrides, "RENDER_ENABLED": False}
        cfg = config.ParserConfig(stored, ROOT, ROOT)
        self.assertEqual(cfg.send_mode_flags, (False, True, True))
        self.assertEqual(cfg.send_mode_flags_for("123"), (False, True, False))
        stored["RENDER_ENABLED"] = True
        self.assertEqual(cfg.send_mode_flags_for("123"), (True, True, False))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, plugin, parsed):
        return [message async for message in plugin._deliver(Event(), parsed, "key")]

    async def test_text_is_first_even_while_media_download_is_pending(self):
        for mode in (["文本"], ["文本", "富媒体"], ["文本", "卡片", "富媒体"]):
            with self.subTest(mode=mode):
                release = asyncio.Event()
                async def pending_audit():
                    await release.wait()
                    return {}
                parsed = result(audit_missing_media=AsyncMock(side_effect=pending_audit))
                plugin = load_delivery(mode, card=Path("card.png"))
                stream = plugin._deliver(Event(), parsed, "key")
                try:
                    first = await asyncio.wait_for(anext(stream), timeout=0.1)
                    self.assertIsInstance(first[0], Plain)
                    self.assertIn("视频标题", first[0].text)
                    parsed.audit_missing_media.assert_not_awaited()
                    plugin._renderer.render.assert_not_awaited()
                    release.set()
                    remaining = [item async for item in stream]
                    self.assertFalse(any(isinstance(part, Plain) for item in remaining for part in item))
                finally:
                    release.set()
                    await stream.aclose()

    async def test_text_only_and_no_output_finish_without_waiting_for_media(self):
        for mode in ([], ["文本"], ["文本", "卡片"]):
            with self.subTest(mode=mode):
                gate = asyncio.Event()
                parsed = result(audit_missing_media=AsyncMock(side_effect=gate.wait))
                plugin = load_delivery(mode, render_enabled=False)
                messages = await asyncio.wait_for(self.collect(plugin, parsed), timeout=0.1)
                self.assertEqual(len(messages), int("文本" in mode))
                parsed.audit_missing_media.assert_not_awaited()
                plugin._renderer.render.assert_not_awaited()

    async def test_slow_card_and_image_do_not_delay_first_text(self):
        for boundary in ("card", "image"):
            with self.subTest(boundary=boundary):
                gate = asyncio.Event()
                image = ImageContent(Path("photo.jpg"))
                parsed = result(img_contents=[image])
                plugin = load_delivery(["文本", "卡片", "富媒体"])
                blocked = plugin._renderer.render if boundary == "card" else image.path_task.safe_get
                blocked.side_effect = gate.wait
                stream = plugin._deliver(Event(), parsed, "key")
                try:
                    first = await asyncio.wait_for(anext(stream), timeout=0.1)
                    self.assertIsInstance(first[0], Plain)
                    blocked.assert_not_awaited()
                finally:
                    gate.set()
                    await stream.aclose()

    async def test_late_media_failure_does_not_send_second_text(self):
        parsed = result()
        async def fail_media():
            parsed.extra["limit_warnings"] = ["视频下载失败"]
            return {"视频": 1}
        parsed.audit_missing_media.side_effect = fail_media
        plugin = load_delivery(["文本", "卡片", "富媒体"])
        messages = await self.collect(plugin, parsed)
        texts = [part.text for msg in messages for part in msg if isinstance(part, Plain)]
        self.assertEqual(len(texts), 1)
        self.assertNotIn("下载失败", texts[0])
        self.assertEqual(parsed.extra["limit_warnings"], ["视频下载失败"])
        plugin._renderer.render.assert_awaited_once()

    async def test_every_combination_and_card_master_switch(self):
        for onebot, enabled, text, card, media in itertools.product((False, True), repeat=5):
            picked = [name for name, yes in zip(("文本", "卡片", "富媒体"), (text, card, media)) if yes]
            with self.subTest(picked=picked, enabled=enabled, onebot=onebot):
                plugin = load_delivery(picked, card=Path("card.png"), render_enabled=enabled, onebot=onebot)
                messages = await self.collect(plugin, result(img_contents=[ImageContent(Path("photo.jpg"))]))
                parts = [part for message in messages for part in message]
                self.assertEqual(sum(isinstance(part, Plain) for part in parts), int(text))
                self.assertEqual(sum(isinstance(part, Image) for part in parts), int(media))
                self.assertEqual(plugin._renderer.render.await_count, int(card and enabled))
                self.assertEqual(plugin._send_image.await_count, int(card and enabled))
                self.assertEqual(len(plugin.media_sent), int(media))

    async def test_unavailable_renderer_does_not_force_text_or_block_media(self):
        for picked, text_count, media_count in ((["卡片"], 0, 0), (["文本", "卡片", "富媒体"], 1, 1)):
            with self.subTest(picked=picked):
                plugin = load_delivery(picked, card=Path("card.png"))
                plugin._renderer.enabled = False
                messages = await self.collect(plugin, result())
                self.assertEqual(len(messages), text_count)
                self.assertEqual(len(plugin.media_sent), media_count)
                plugin._renderer.render.assert_not_awaited()
                plugin._send_image.assert_not_awaited()

    async def test_override_still_respects_card_master_switch(self):
        plugin = load_delivery([], card=Path("card.png"), render_enabled=False, overrides="123=文本+卡片")
        messages = await self.collect(plugin, result())
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0][0], Plain)
        plugin._renderer.render.assert_not_awaited()
        plugin._send_image.assert_not_awaited()
        self.assertEqual(plugin.media_sent, [])

    async def test_one_text_message_on_both_adapters(self):
        for onebot in (True, False):
            with self.subTest(onebot=onebot):
                plugin = load_delivery("文本+富媒体", onebot=onebot)
                messages = await self.collect(plugin, result())
                self.assertEqual(len(messages), 1)
                self.assertEqual(len(messages[0]), 1)
                self.assertIsInstance(messages[0][0], Plain)
                self.assertEqual(messages[0][0].text, "视频标题\nhttps://www.bilibili.com/video/BV123?p=2\n⏱ 3:25 · 播放 12.3万")
                plugin._renderer.render.assert_not_awaited()
                plugin._send_image.assert_not_awaited()
                self.assertEqual(len(plugin.media_sent), 1)

    async def test_template_with_summary_waits_for_card_details_only_then(self):
        for template, waits in (("{title}\n{summary}", True), ("**{title}**", False)):
            with self.subTest(template=template):
                parsed = result()
                async def load():
                    parsed.extra["summary"] = "AI 总结内容"
                parsed.prepare_card_details = AsyncMock(side_effect=load)
                plugin = load_delivery(["文本"], template=template, onebot=False)
                messages = await self.collect(plugin, parsed)
                self.assertEqual(parsed.prepare_card_details.await_count, int(waits))
                self.assertEqual(messages[0][0].text, "视频标题\nAI 总结内容" if waits else "**视频标题**")

    async def test_napcat_gets_real_cover_image_and_plain_links(self):
        template = "![封面 #560px #315px]({cover})\n\n**{title}**\n\n{info}\n\n[立即播放]({play_url})  [原网页]({url})"
        extra = {"duration": "3:25", "cover": "https://i0.hdslb.com/c.jpg", "play_url": "https://player.bilibili.com/player.html?bvid=BV123&p=2&autoplay=true"}
        for onebot in (True, False):
            with self.subTest(onebot=onebot):
                cover = ImageContent(Path("cover.jpg")).path_task
                parsed = result(extra=dict(extra), video=SimpleNamespace(cover=cover))
                messages = await self.collect(load_delivery(["文本"], template=template, onebot=onebot), parsed)
                self.assertEqual(len(messages), 1)
                if onebot:
                    image, text = messages[0]
                    # 用解析时已下载的本地封面，不再按地址重下
                    self.assertEqual(image.file, "cover.jpg")
                    self.assertEqual(text.text, "视频标题\n⏱ 3:25\n立即播放：https://player.bilibili.com/player.html?bvid=BV123&p=2&autoplay=true\n原网页：https://www.bilibili.com/video/BV123?p=2")
                else:
                    self.assertEqual(len(messages[0]), 1)
                    self.assertTrue(messages[0][0].text.startswith("![封面 #560px #315px](https://i0.hdslb.com/c.jpg)\n\n**视频标题**"))
                    cover.safe_get.assert_not_awaited()
        # 封面没下载成功：只发文字，不因一张封面丢掉整条消息
        cover = SimpleNamespace(safe_get=AsyncMock(return_value=None))
        parsed = result(extra=dict(extra), video=SimpleNamespace(cover=cover))
        messages = await self.collect(load_delivery(["文本"], template=template), parsed)
        self.assertEqual([type(c) for c in messages[0]], [Plain])

    async def test_text_only_is_really_text_only(self):
        image = ImageContent(Path("photo.jpg"))
        plugin = load_delivery("仅文本", card=Path("card.png"))
        messages = await self.collect(plugin, result(img_contents=[image]))
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0][0], Plain)
        plugin._renderer.render.assert_not_awaited()
        plugin._send_image.assert_not_awaited()
        image.path_task.safe_get.assert_not_awaited()
        self.assertEqual(plugin.media_sent, [])

    async def test_media_only_never_sends_plain_or_forward_card(self):
        plugin = load_delivery("仅富媒体")
        parsed = result(img_contents=[ImageContent(Path("photo.jpg"))], graphics=["图文长文", ImageContent(Path("graphic.jpg"))])
        messages = await self.collect(plugin, parsed)
        components = [c for message in messages for c in message]
        self.assertEqual([c.file for c in components], ["photo.jpg", "graphic.jpg"])
        self.assertTrue(all(isinstance(c, Image) for c in components))
        plugin._renderer.render.assert_not_awaited()
        self.assertEqual(len(plugin.media_sent), 1)

    async def test_all_mode_keeps_compact_text_even_with_video_card(self):
        plugin = load_delivery("全部发送", card=Path("card.png"))
        messages = await self.collect(plugin, result())
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0][0], Plain)
        self.assertEqual(len(messages[0][0].text.splitlines()), 3)
        plugin._send_image.assert_awaited_once()
        self.assertEqual(len(plugin.media_sent), 1)

    async def test_mixed_post_has_one_summary_and_preserves_all_media(self):
        plugin = load_delivery("文本+富媒体")
        parsed = result(img_contents=[ImageContent(Path("photo.jpg"))], graphics=["长图文内容", ImageContent(Path("graphic.jpg"))])
        parsed.extra["limit_warnings"] = ["⚠️ 图片下载失败 1 张"]
        messages = await self.collect(plugin, parsed)
        components = [c for message in messages for c in message]
        texts = [c.text for c in components if isinstance(c, Plain)]
        self.assertEqual(len(texts), 1)
        self.assertIn("图片下载失败 1 张", texts[0])
        self.assertNotIn("长图文内容", texts[0])
        self.assertEqual([c.file for c in components if isinstance(c, Image)], ["photo.jpg", "graphic.jpg"])
        self.assertEqual(len(plugin.media_sent), 1)


class ShareTextTests(unittest.TestCase):
    def test_clean_url_keeps_episode_and_playback_position(self):
        self.assertEqual(
            clean_share_url("https://www.bilibili.com/video/BV123?spm_id_from=123&p=2&t=30&vd_source=secret&share_source=copy&timestamp=123&UTM_source=chat"),
            "https://www.bilibili.com/video/BV123?p=2&t=30",
        )

    def test_clean_url_preserves_access_tokens_and_encoding(self):
        url = "https://www.xiaohongshu.com/explore/123?xsec_token=Ab%2Bcd%2F%3d&xsec_source=pc_share&utm_source=copy"
        self.assertEqual(clean_share_url(url), url.rsplit("&", 1)[0])

    def test_clean_url_preserves_functional_query_and_fragments(self):
        for url in (
            "https://bbs.nga.cn/read.php?tid=123&page=2#pid456",
            "https://github.com/owner/repo/tree/main?tab=readme-ov-file#section",
            "https://custom.example/watch?id=12&ts=123&timestamp=456&share_id=123&share_token=access%2Bkey&token=&token=second",
        ):
            with self.subTest(url=url):
                self.assertEqual(clean_share_url(url), url)

    def test_twitter_tracking_rules_do_not_apply_to_other_hosts(self):
        self.assertEqual(clean_share_url("https://x.com/user/status/123?s=46&t=track"), "https://x.com/user/status/123")
        self.assertEqual(clean_share_url("https://custom.example/watch?s=46&t=30"), "https://custom.example/watch?s=46&t=30")

    def test_invalid_or_private_url_is_not_echoed(self):
        for url in (None, "", "javascript:alert(1)", "file:///private/file", "https://[invalid", "https://user:secret@example.com/"):
            with self.subTest(url=url):
                self.assertEqual(clean_share_url(url), "")

    def test_compact_text_omits_missing_fields(self):
        parsed = result(title="纯图文", url=None, extra={}, video_contents=[])
        self.assertEqual(build_share_text(parsed), "纯图文")

    def test_title_is_one_bounded_line_and_description_is_only_a_fallback(self):
        parsed = result(title="第一行\n第二行 " + "长" * 150, extra={}, video_contents=[])
        text = build_share_text(parsed)
        self.assertEqual(len(text.splitlines()[0]), 100)
        self.assertTrue(text.splitlines()[0].endswith("…"))
        self.assertNotIn("不应发送", text)
        parsed.title = "  "
        parsed.text = "正文\n作标题"
        self.assertEqual(build_share_text(parsed).splitlines()[0], "正文 作标题")
        parsed.text = None
        self.assertEqual(build_share_text(parsed).splitlines()[0], "哔哩哔哩 · 视频")

    def test_duration_uses_media_metadata_without_extra_duration(self):
        for duration, expected in ((65.9, "1:05"), (3661, "1:01:01")):
            with self.subTest(duration=duration):
                parsed = result(extra={}, video_contents=[SimpleNamespace(duration=duration)])
                self.assertEqual(build_share_text(parsed).splitlines()[-1], "⏱ " + expected)
        parsed = result(extra={}, video_contents=[], audio_contents=[SimpleNamespace(duration=125)])
        self.assertEqual(build_share_text(parsed).splitlines()[-1], "⏱ 2:05")

    def test_numeric_and_unusable_durations(self):
        self.assertEqual(build_share_text(result(extra={"duration": 125})).splitlines()[-1], "⏱ 2:05")
        for duration in (None, float("nan"), float("inf"), -1):
            with self.subTest(duration=duration):
                parsed = result(extra={}, video_contents=[SimpleNamespace(duration=duration)])
                self.assertEqual(len(build_share_text(parsed).splitlines()), 2)

    def test_warnings_are_deduplicated_bounded_and_in_same_message(self):
        warnings = ["⚠️ 图片缺失\n1 张", "⚠️ 图片缺失\n1 张", "⚠️ " + "长" * 150, "⚠️ 视频超限"]
        parsed = result(extra={"limit_warnings": warnings}, video_contents=[])
        lines = build_share_text(parsed).splitlines()
        # 标题 / 地址 / 两条限制 / 「另有」：每条限制单独一行，不再挤在播放信息后面
        self.assertEqual(len(lines), 5)
        self.assertEqual(sum(line.count("图片缺失") for line in lines), 1)
        self.assertEqual(lines[-1], "另有 1 项限制")
        self.assertTrue(all(len(line) <= 100 for line in lines))
        self.assertEqual(parsed.extra["limit_warnings"], warnings)

    def test_limit_warning_gets_its_own_line(self):
        warning = "⚠️ 视频时长(14:46)超过限制(8:00)，不会下载视频"
        parsed = result(extra={"stats_line": "👀 6064", "duration": "14:46", "limit_warnings": [warning]})
        self.assertEqual(build_share_text(parsed).splitlines()[-2:], ["⏱ 14:46 · 播放 6064", warning])

    def test_markdown_template_drops_lines_whose_fields_are_empty(self):
        template = "**{title}**\n\n{info}\n\n{warnings}\n\nAI 总结：{summary}\n\n![封面]({cover})\n\n[原网页]({url}) {unknown}"
        parsed = result(extra={"duration": "3:25", "summary": "一段\n总结", "cover": "https://i0.hdslb.com/c.jpg"})
        self.assertEqual(
            build_share_text(parsed, template),
            "**视频标题**\n\n⏱ 3:25\n\nAI 总结：一段 总结\n\n![封面](https://i0.hdslb.com/c.jpg)\n\n"
            "[原网页](https://www.bilibili.com/video/BV123?p=2) {unknown}",
        )
        # 未知占位符原样保留（上面），方便发现拼写错误；地址 / 封面（非 http）为空则整行省略
        bare = build_share_text(result(url=None, extra={"cover": "file:///tmp/c.jpg"}, video_contents=[]), template)
        self.assertEqual(bare, "**视频标题**")
        self.assertEqual(build_share_text(parsed, "  \n "), build_share_text(parsed))

    def test_split_markdown_for_platforms_without_markdown(self):
        text = "![封面 #560px #315px](https://a.example/c.jpg)\n\n**标题**\n\n[立即播放](https://p.example/x)  [](https://b.example/y)\n\n![本地](file:///etc/passwd)"
        # 非 http(s) 图片不当图片发，原样留在文字里
        self.assertEqual(split_markdown(text), [
            ("image", "https://a.example/c.jpg"),
            ("text", "标题\n立即播放：https://p.example/x\nhttps://b.example/y\n![本地](file:///etc/passwd)"),
        ])

    def test_empty_link_targets_are_removed(self):
        parsed = result(extra={})
        self.assertEqual(build_share_text(parsed, "[立即播放]({play_url})  [原网页]({url})"), "[原网页](https://www.bilibili.com/video/BV123?p=2)")
        self.assertEqual(build_share_text(parsed, "{info}\n[{title}]({play_url})"), "⏱ 3:25")

    def test_source_url_selects_the_parsed_link_and_handles_bare_domains(self):
        for text, expected in (
            ("别的 https://example.com/other 分享 https://www.acfun.cn/v/ac123?utm_source=copy，看看", "https://www.acfun.cn/v/ac123?utm_source=copy"),
            ("分享 www.acfun.cn/v/ac123", "https://www.acfun.cn/v/ac123"),
        ):
            with self.subTest(text=text):
                self.assertEqual(source_url(re.search(r"/ac[0-9]+", text)), expected)
        self.assertIsNone(source_url(re.search(r"ac[0-9]+", "ac123")))


class ParserUrlTests(unittest.IsolatedAsyncioTestCase):
    async def parse(self, parsed, source):
        tree = ast.parse((ROOT / "core/base_parser.py").read_text(encoding="utf-8-sig"))
        method = next(m for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "BaseParser" for m in c.body if isinstance(m, ast.AsyncFunctionDef) and m.name == "parse")
        cls = ast.ClassDef(name="Parser", bases=[], keywords=[], body=[method], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[]))
        scope = dict(source_url=source_url, final=lambda f: f)
        exec(compile(module, str(ROOT / "core/base_parser.py"), "exec"), scope)
        parser = scope["Parser"]()
        matched = re.search(r"/ac[0-9]+", source)
        parser._handlers = {matched.re: AsyncMock(return_value=parsed)}
        return await parser.parse("acfun.cn", matched)

    async def test_missing_result_url_is_filled_at_real_parse_boundary(self):
        parsed = await self.parse(result(url=None), "https://example.com/ignore https://www.acfun.cn/v/ac123")
        self.assertEqual(parsed.url, "https://www.acfun.cn/v/ac123")

    async def test_canonical_result_url_is_not_overwritten(self):
        parsed = await self.parse(result(url="https://www.acfun.cn/v/ac456"), "https://www.acfun.cn/v/ac123")
        self.assertEqual(parsed.url, "https://www.acfun.cn/v/ac456")


if __name__ == "__main__":
    unittest.main()

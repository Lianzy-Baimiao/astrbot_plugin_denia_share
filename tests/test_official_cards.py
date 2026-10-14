"""Offline card-routing regressions; raw payloads below are synthetic, not captures.

The summary reproduces the user-visible QQ official message. Structured payloads
exercise the missing raw_message seam; real-server availability needs a resend.
Run: python -m unittest discover -s tests -p test_official_cards.py -v
"""
from __future__ import annotations

import ast
import importlib
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BVID = "BV1YQHt6oE5Z"
LINK = "https://bilibili.com/" + BVID
SUMMARY = (
    "[卡片消息] 小程序 摘要: [QQ小程序]魔兽世界之我的滑板鞋 source: 哔哩哔哩 "
    "source_logo: https://open.gtimg.cn/open/app_icon/icon.png?t=1791448425 "
    "title: 魔兽世界之我的滑板鞋 preview: https://qq.ugcimg.cn/v1/preview"
)


class Plain:
    def __init__(self, text):
        self.text = text


class Json:
    def __init__(self, data):
        self.data = data


class ParseException(Exception):
    pass


def event(raw=None, *, text=SUMMARY, components=None, platform="qq_official"):
    return SimpleNamespace(
        message_str=text,
        message_obj=SimpleNamespace(
            message=[Plain(text)] if components is None else components,
            raw_message=raw,
        ),
        get_platform_name=lambda: platform,
    )


def load_plugin():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8-sig"))
    names = {
        "json_card_handler", "_dispatch", "bilibili_handler",
        "_has_json_component", "_has_share_card", "_official_card_input",
        "_extract_links_from_event", "_extract_links_from_json", "_is_qq_official",
    }
    methods = [
        m for c in tree.body if isinstance(c, ast.ClassDef)
        for m in c.body
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name in names
    ]
    for method in methods:
        method.decorator_list = [
            d for d in method.decorator_list
            if isinstance(d, ast.Name) and d.id in {"staticmethod", "classmethod"}
        ]
    constants = [
        n for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "URL_PATTERN" for t in n.targets)
    ]
    wrapper = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_EventUrlWrapper")
    cls = ast.ClassDef(name="Plugin", bases=[], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *constants, wrapper, cls,
    ], type_ignores=[]))
    scope = {
        "re": re, "json": json,
        "Comp": SimpleNamespace(Plain=Plain, Json=Json),
        "logger": Mock(), "ParseException": ParseException, "SilentException": ParseException,
    }
    if (ROOT / "core/card_input.py").exists():
        card_input = importlib.import_module("core.card_input")
        scope["inspect_official_card"] = card_input.inspect_official_card
    exec(compile(module, str(ROOT / "main.py"), "exec"), scope)
    plugin = scope["Plugin"]()
    plugin._access_denied = Mock(return_value=False)
    plugin.received = []
    plugin.logger = scope["logger"]

    def search_url(value):
        if "bilibili.com/" not in value and not re.fullmatch(r"BV1[0-9A-Za-z]{9}", value):
            raise ParseException()
        return "bilibili", None

    parser = SimpleNamespace(search_url=search_url)
    plugin.parsers = {"bilibili": parser}

    async def process(ev, selected):
        plugin.received.append(ev.message_str)
        yield "parsed"

    plugin._process_url = process
    return plugin


class CardRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, plugin, ev):
        return [r async for r in plugin.json_card_handler(ev)]

    async def test_official_sdk_raw_data_ark_routes(self):
        plugin = load_plugin()
        raw = SimpleNamespace(raw_data={"ark": {"kv": [{"key": "#META_SHARE_URL#", "value": LINK}]}})
        self.assertEqual(await self.send(plugin, event(raw)), ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_webhook_dict_envelope_routes(self):
        plugin = load_plugin()
        raw = {"d": {"ark": {"kv": [{"key": "#META_SHARE_URL#", "value": LINK}]}}}
        self.assertEqual(await self.send(plugin, event(raw, platform="qq_official_webhook")), ["parsed"])

    async def test_sdk_ark_attribute_routes(self):
        plugin = load_plugin()
        self.assertEqual(await self.send(plugin, event(SimpleNamespace(ark={"url": LINK}))), ["parsed"])

    async def test_napcat_json_still_routes(self):
        plugin = load_plugin()
        ev = event(components=[Json({"meta": {"detail_1": {"qqdocurl": LINK}}})], text="", platform="aiocqhttp")
        self.assertEqual(await self.send(plugin, ev), ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_summary_only_logs_without_guessing_or_sending(self):
        plugin = load_plugin()
        self.assertEqual(await self.send(plugin, event()), [])
        self.assertEqual(plugin.received, [])
        self.assertTrue(plugin.logger.info.called)
        logged = str(plugin.logger.info.call_args)
        self.assertIn("QQ官方卡片", logged)
        self.assertNotIn("魔兽世界", logged)
        self.assertNotIn("qq.ugcimg.cn", logged)

    async def test_raw_data_cannot_activate_on_other_platform(self):
        plugin = load_plugin()
        self.assertEqual(await self.send(plugin, event({"ark": {"url": LINK}}, platform="aiocqhttp")), [])

    async def test_regular_bvid_keeps_builtin_route(self):
        plugin = load_plugin()
        ev = event(text=BVID, raw={"content": BVID})
        self.assertEqual(await self.send(plugin, ev), [])
        self.assertEqual([r async for r in plugin.bilibili_handler(ev)], ["parsed"])

    async def test_official_card_and_builtin_route_do_not_duplicate(self):
        plugin = load_plugin()
        ev = event({"ark": {"url": LINK}}, text=SUMMARY + " " + LINK)
        builtin = [r async for r in plugin.bilibili_handler(ev)]
        cards = await self.send(plugin, ev)
        self.assertEqual(builtin + cards, ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_denied_card_has_no_output_or_diagnostic(self):
        plugin = load_plugin()
        plugin._access_denied.return_value = True
        self.assertEqual(await self.send(plugin, event()), [])
        self.assertFalse(plugin.logger.info.called)

    async def test_quote_does_not_parse_referenced_card(self):
        plugin = load_plugin()
        raw = {"message_type": 103, "content": "好好好", "msg_elements": [{"ark": {"url": LINK}}]}
        self.assertEqual(await self.send(plugin, event(raw, text="好好好")), [])
        self.assertFalse(plugin.logger.info.called)


    async def test_ark_sdk_kv_objects(self):
        plugin = load_plugin()
        ark = SimpleNamespace(kv=[SimpleNamespace(key="#META_SHARE_URL#", value=LINK)])
        self.assertEqual(await self.send(plugin, event(SimpleNamespace(ark=ark), text="")), ["parsed"])

    async def test_serialized_card_in_content(self):
        plugin = load_plugin()
        content = json.dumps({"app": "com.tencent.miniapp_01", "meta": {"detail_1": {"qqdocurl": LINK}}})
        self.assertEqual(await self.send(plugin, event({"content": content}, text="")), ["parsed"])

    async def test_typed_official_component(self):
        plugin = load_plugin()
        component = {"type": "ark", "data": {"url": LINK}}
        self.assertEqual(await self.send(plugin, event(components=[component], text="")), ["parsed"])

    async def test_miniprogram_percent_encoded_path(self):
        plugin = load_plugin()
        raw = {"mini_program": {"page_path": "pages/video/video%3Fbvid%3D" + BVID}}
        self.assertEqual(await self.send(plugin, event(raw)), ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_msg_elements_typed_card_without_summary(self):
        plugin = load_plugin()
        raw = {"msg_elements": [{"type": "ark", "data": {"url": LINK}}]}
        self.assertEqual(await self.send(plugin, event(raw, text="")), ["parsed"])

    async def test_summary_msg_elements_with_unfamiliar_card_shape(self):
        plugin = load_plugin()
        raw = {"msg_elements": [{"type": 123, "data": {"jump_url": LINK}}]}
        self.assertEqual(await self.send(plugin, event(raw)), ["parsed"])

    async def test_summary_quote_elements_are_not_used(self):
        plugin = load_plugin()
        raw = {"msg_elements": [{"type": "reply", "data": {"ark": {"url": LINK}}}]}
        self.assertEqual(await self.send(plugin, event(raw)), [])

    async def test_preview_bvid_is_not_a_video_identifier(self):
        plugin = load_plugin()
        text = SUMMARY + "/" + BVID
        self.assertEqual(await self.send(plugin, event(text=text)), [])

    async def test_ark_thumbnail_and_title_are_not_targets(self):
        plugin = load_plugin()
        raw = {"ark": {"kv": [
            {"key": "#META_THUMB#", "value": LINK},
            {"key": "#META_TITLE#", "value": BVID},
        ], "title": LINK, "preview": LINK, "source_logo": LINK}}
        self.assertEqual(await self.send(plugin, event(raw)), [])

    async def test_signed_link_and_part_remain_unchanged(self):
        plugin = load_plugin()
        link = LINK + "?p=2&access_key=a%2Fb&foo=x%3Dy"
        self.assertEqual(await self.send(plugin, event({"ark": {"url": link}})), ["parsed"])
        self.assertEqual(plugin.received, [link])

    async def test_disabled_parser_does_not_parse(self):
        plugin = load_plugin()
        plugin.parsers = {}
        self.assertEqual(await self.send(plugin, event({"ark": {"url": LINK}})), [])
        self.assertTrue(plugin.logger.info.called)

    async def test_missing_link_diagnostic_never_serializes_api_client(self):
        plugin = load_plugin()
        class Raw:
            content = SUMMARY
            raw_data = {"token": "secret-token", "author": {"id": "private-user"}}
            @property
            def api(self):
                raise AssertionError("must not read API client")
            def __str__(self):
                raise AssertionError("must not stringify SDK object")
        self.assertEqual(await self.send(plugin, event(Raw())), [])
        logged = str(plugin.logger.info.call_args)
        self.assertNotIn("secret-token", logged)
        self.assertNotIn("private-user", logged)

    async def test_cyclic_card_is_bounded(self):
        plugin = load_plugin()
        card = {"url": LINK}
        card["data"] = card
        self.assertEqual(await self.send(plugin, event({"ark": card})), ["parsed"])

    async def test_large_payload_is_truncated_without_parsing(self):
        plugin = load_plugin()
        raw = {"ark": {"data": "x" * 40000 + LINK}}
        self.assertEqual(await self.send(plugin, event(raw)), [])
        self.assertTrue(plugin._official_card_input(event(raw)).truncated)

    async def test_malformed_url_does_not_break_card(self):
        plugin = load_plugin()
        raw = {"ark": {"bad": "http://[invalid", "url": LINK}}
        self.assertEqual(await self.send(plugin, event(raw)), ["parsed"])


    async def test_denied_card_with_link_never_dispatches(self):
        plugin = load_plugin()
        plugin._access_denied.return_value = True
        self.assertEqual(await self.send(plugin, event({"ark": {"url": LINK}})), [])
        self.assertEqual(plugin.received, [])
        self.assertFalse(plugin.logger.info.called)

    async def test_raw_ark_template_dict(self):
        plugin = load_plugin()
        raw = {"template_id": 23, "kv": [{"key": "#META_SHARE_URL#", "value": LINK}]}
        self.assertEqual(await self.send(plugin, event(raw, text="")), ["parsed"])

    async def test_json_card_and_plain_url_still_only_send_once(self):
        plugin = load_plugin()
        ev = event(components=[Json({"url": LINK}), Plain(LINK)], text=LINK, platform="aiocqhttp")
        self.assertEqual([r async for r in plugin.bilibili_handler(ev)], [])
        self.assertEqual(await self.send(plugin, ev), ["parsed"])
        self.assertEqual(plugin.received, [LINK])


class OfficialArkDataTests(unittest.IsolatedAsyncioTestCase):
    """Documented Tencent ARKData schema, NOT a captured user payload.

    Reference (checked 2026-10-13):
    https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html
    A link-bearing fixture tests the adapter gap. The metadata-only fixture
    models the observed missing-target symptom and MUST NOT start parsing.
    """

    def ark(self, *, link=True):
        fields = {"preview": "https://qq.ugcimg.cn/preview", "source": "哔哩哔哩",
                  "source_logo": "https://open.gtimg.cn/icon", "title": "魔兽世界之我的滑板鞋"}
        if link:
            fields["jump_url"] = LINK
        return {"ark_type": "miniapp", "ark_name": "小程序", "fields": fields,
                "prompt": "[QQ小程序]魔兽世界之我的滑板鞋"}

    async def send(self, raw, *, text=SUMMARY, platform="qq_official"):
        plugin = load_plugin()
        outputs = [r async for r in plugin.json_card_handler(event(raw, text=text, platform=platform))]
        return plugin, outputs

    async def test_sdk_raw_data_documented_jump_url(self):
        raw = SimpleNamespace(raw_data={"message_type": 3, "ark_data": self.ark()}, content=SUMMARY)
        plugin, outputs = await self.send(raw)
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [LINK])
        self.assertFalse(plugin.logger.info.called)

    async def test_webhook_ark_data_without_rendered_summary(self):
        plugin, outputs = await self.send({"d": {"message_type": 3, "ark_data": self.ark()}},
                                          text="", platform="qq_official_webhook")
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_sdk_ark_data_attribute(self):
        raw = SimpleNamespace(ark_data=SimpleNamespace(fields={"jump_url": LINK}))
        plugin, outputs = await self.send(raw, text="")
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_documented_current_msg_element(self):
        raw = {"message_type": 101, "msg_elements": [{"message_type": 3, "ark_data": self.ark()}]}
        plugin, outputs = await self.send(raw, text="")
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_metadata_only_still_no_guess_or_group_message(self):
        raw = SimpleNamespace(raw_data={"message_type": 3, "ark_data": self.ark(link=False)}, content=SUMMARY)
        plugin, outputs = await self.send(raw)
        self.assertEqual(outputs, [])
        self.assertEqual(plugin.received, [])
        args = plugin.logger.info.call_args.args
        log = args[0] % args[1:]
        self.assertIn("raw_message.raw_data.ark_data", log)
        self.assertNotIn("DEBUG-qqcard-schema", log)
        self.assertNotIn("结构=", log)
        self.assertNotIn("魔兽世界", log)
        self.assertLess(len(log), 600)

    async def test_presentation_fields_and_message_scene_are_not_targets(self):
        card = self.ark(link=False)
        card["fields"].update({"tag": LINK, "tags": [LINK], "tag_icon": LINK,
                               "nickname": BVID, "avatar": LINK, "address": LINK})
        raw = {"ark_data": card, "message_scene": {"ext": ["auth_token=" + LINK]}}
        plugin, outputs = await self.send(raw)
        self.assertEqual(outputs, [])
        self.assertEqual(plugin.received, [])

    async def test_nested_quoted_ark_is_not_current_card(self):
        raw = {"message_type": 101, "msg_elements": [{
            "message_type": 103, "msg_elements": [{"message_type": 3, "ark_data": self.ark()}]
        }]}
        plugin, outputs = await self.send(raw)
        self.assertEqual(outputs, [])
        self.assertEqual(plugin.received, [])

    async def test_referenced_ark_and_current_ark_choose_current(self):
        old = self.ark()
        old["fields"]["jump_url"] = "https://bilibili.com/BV1G4bw6eEWC"
        raw = {"msg_elements": [
            {"message_type": 103, "msg_elements": [{"message_type": 3, "ark_data": old}]},
            {"message_type": 3, "ark_data": self.ark()},
        ]}
        plugin, outputs = await self.send(raw)
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [LINK])

    async def test_ark_jump_url_retains_signed_query_and_part(self):
        card = self.ark()
        signed = LINK + "?p=2&access_key=a%2Fb"
        card["fields"]["jump_url"] = signed
        plugin, outputs = await self.send({"ark_data": card})
        self.assertEqual(outputs, ["parsed"])
        self.assertEqual(plugin.received, [signed])


if __name__ == "__main__":
    unittest.main()

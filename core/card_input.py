"""QQ official card input, independent of AstrBot/qq-botpy versions.

Only known current-message fields are read. Never serialize the SDK object (it
can reference an API client), follow quoted messages, fetch preview images, or
search by title. Payload walking is bounded and has no network side effects.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_CARD_FIELDS = ("ark", "ark_data", "card", "miniapp", "mini_app", "mini_program", "json")
_LINK_FIELDS = ("url", "jump_url", "jumpUrl", "qqdocurl", "bvid", "page", "path", "page_path")
_OBJECT_FIELDS = (*_CARD_FIELDS, *_LINK_FIELDS, "meta", "app", "kv", "key", "value", "obj", "obj_kv", "content", "data", "fields")
_SKIP_FIELDS = {
    "title", "desc", "summary", "prompt", "source", "source_logo", "preview",
    "cover", "cover_url", "icon", "icon_url", "image", "image_url", "thumbnail",
    "thumb", "thumb_url", "author", "mentions", "attachments", "reply",
    "message_reference", "referenced_message", "raw_data", "token", "secret",
    "cookie", "authorization", "access_token", "message_scene",
    "ark_type", "ark_name", "tag", "tags", "tag_icon", "nickname", "avatar", "address",
}
_CARD_TYPES = {"ark", "card", "miniapp", "mini_app", "mini_program", "json"}
_URL = re.compile(r"https?://[^\s<>\"'\\]+", re.IGNORECASE)
_BVID = re.compile(r"(?<![A-Za-z0-9])BV1[A-Za-z0-9]{9}(?![A-Za-z0-9])")
_ASSET_HOSTS = {"open.gtimg.cn", "qq.ugcimg.cn"}
_MAX_TEXT = 32768
_MAX_ITEMS = 512


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _decode(value):
    if isinstance(value, str) and len(value) <= _MAX_TEXT:
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def _summary(value):
    return isinstance(value, str) and any(
        marker in value[:_MAX_TEXT] for marker in ("[卡片消息]", "[QQ小程序]")
    )


def _component_kind(value):
    kind = _field(value, "type", "")
    return str(getattr(kind, "value", kind)).lower().rsplit(".", 1)[-1]


def _is_reference(value):
    # Tencent MsgElement: 102 is chat history, 103 references an older message.
    message_type = _field(value, "message_type")
    return _component_kind(value) in {"reply", "quote", "forward"} or (
        type(message_type) in (int, str) and message_type in (102, 103, "102", "103")
    )


def _structured(value):
    value = _decode(value)
    return isinstance(value, dict) and (
        _component_kind(value) in _CARD_TYPES
        or any(value.get(key) for key in _CARD_FIELDS)
        or ("meta" in value and "app" in value)
        or ("template_id" in value and "kv" in value)
    )


@dataclass(frozen=True)
class OfficialCardInput:
    links: tuple[str, ...]
    sources: tuple[str, ...]
    raw_kind: str
    truncated: bool


class _Links:
    def __init__(self):
        self.links: dict[str, None] = {}
        self.remaining = _MAX_ITEMS
        self.seen: set[int] = set()
        self.truncated = False

    def walk(self, value, depth=0):
        if depth > 10 or self.remaining <= 0:
            self.truncated = True
            return
        self.remaining -= 1
        if isinstance(value, str):
            if len(value) > _MAX_TEXT:
                self.truncated = True
                return
            decoded = _decode(value)
            if decoded is not value and not isinstance(decoded, (int, float, bool, type(None))):
                self.walk(decoded, depth + 1)
                return
            text = html.unescape(value)
            for match in _URL.finditer(text):
                url = match.group().rstrip(".,;!?，。；！）、]}")
                try:
                    host = (urlsplit(url).hostname or "").lower()
                except ValueError:
                    continue
                if host and host not in _ASSET_HOSTS:
                    self.links.setdefault(url, None)
            # Explicit BV identifiers / mini-program page paths only; title,
            # summary and preview fields have already been excluded below.
            # A BV-looking segment in an icon/preview HTTP URL is not a video
            # identifier. URLs above already preserve actual video targets.
            bare_text = _URL.sub(" ", text)
            for match in _BVID.finditer(bare_text):
                self.links.setdefault("https://bilibili.com/" + match.group(), None)
            # Decode page paths / embedded URLs, but keep original HTTP URLs
            # first so signed query parameters and part numbers aren't changed.
            decoded_url = unquote(text)
            if decoded_url != text:
                self.walk(decoded_url, depth + 1)
            return
        if value is None or isinstance(value, (int, float, bool, bytes)):
            return
        identity = id(value)
        if identity in self.seen:
            return
        self.seen.add(identity)
        if isinstance(value, (list, tuple)):
            for item in value[:_MAX_ITEMS]:
                self.walk(item, depth + 1)
                if self.remaining <= 0:
                    break
            if len(value) > _MAX_ITEMS:
                self.truncated = True
            return
        if _is_reference(value):
            return
        if not isinstance(value, dict):
            value = {key: item for key in _OBJECT_FIELDS if (item := _field(value, key)) is not None}
        # Ark KV uses key/value records; don't parse its thumbnail/title values.
        ark_key = str(value.get("key") or "").strip("#").lower()
        if ark_key in {"meta_thumb", "meta_title", "meta_desc", "meta_description", "meta_icon"}:
            return
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS or self.remaining <= 0:
                self.truncated = True
                break
            if str(key).lower() not in _SKIP_FIELDS:
                self.walk(item, depth + 1)


def inspect_official_card(message_obj, message_str: str) -> OfficialCardInput | None:
    """Read current-message card fields; caller must check the platform first.

    AstrBot's official adapter stores the SDK object in raw_message; newer SDK
    wrappers also preserve raw_data. Dict/envelope and component forms are
    accepted without importing SDK-specific types. None means ordinary text.
    """
    raw = _field(message_obj, "raw_message")
    raw_kind = "none" if raw is None else "dict" if isinstance(raw, dict) else "object"
    roots = [("raw_message", raw)]
    seen: set[int] = set()
    payloads = []
    is_card = _summary(message_str)
    # Only unwrap known transport containers, not API clients / SDK internals.
    for _ in range(3):
        pending = []
        for label, root in roots:
            root = _decode(root)
            if root is None or id(root) in seen:
                continue
            seen.add(id(root))
            for key in ("raw_data", "d"):
                nested = _field(root, key)
                if isinstance(nested, (dict, str)):
                    pending.append((label + "." + key, nested))
            content = _field(root, "content")
            if _summary(content) or _structured(content):
                is_card = True
            if content:
                payloads.append((label + ".content", content))
            if _structured(root):
                is_card = True
                # Select card fields below, rather than walking the whole event.
                if isinstance(root, dict) and "app" in root and "meta" in root:
                    payloads.append((label + ".meta", root["meta"]))
                if isinstance(root, dict) and "template_id" in root and "kv" in root:
                    payloads.append((label + ".kv", root["kv"]))
            for key in (*_CARD_FIELDS, *_LINK_FIELDS):
                value = _field(root, key)
                if value:
                    payloads.append((label + "." + key, value))
                    if key in _CARD_FIELDS:
                        is_card = True
            # Quoted messages / chat history are not a current share.
            if not _is_reference(root):
                elements = _field(root, "msg_elements")
                if isinstance(elements, list):
                    for element in elements[:_MAX_ITEMS]:
                        if _is_reference(element):
                            continue
                        content = _field(element, "content")
                        if is_card or _structured(element) or _structured(content) or _summary(content):
                            is_card = True
                            payloads.append((label + ".msg_elements", element))

        roots = pending
    components = _field(message_obj, "message", [])
    if isinstance(components, (list, tuple)):
        for component in components[:_MAX_ITEMS]:
            if _component_kind(component) in _CARD_TYPES:
                is_card = True
                payloads.append(("message.card", _field(component, "data", component)))
    if not is_card:
        return None
    payloads.append(("message_str", message_str))
    collector = _Links()
    for _, payload in payloads:
        collector.walk(payload)
    return OfficialCardInput(
        links=tuple(collector.links), sources=tuple(dict.fromkeys(label for label, _ in payloads)),
        raw_kind=raw_kind, truncated=collector.truncated,
    )

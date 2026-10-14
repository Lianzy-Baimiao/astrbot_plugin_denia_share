"""Order Douyin rendition URLs without depending on the API/SSR transport."""
from __future__ import annotations

import math


def _positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def low_bitrate_urls(variants) -> list[str]:
    """Prefer the lowest advertised bitrate; use file size/resolution as fallback.

    Unknown renditions never beat a rendition with usable metadata. Only URLs
    actually supplied by Douyin are used; changing a URL parameter is not a
    reliable way to request a lower-quality encode.
    """
    if not isinstance(variants, list):
        return []
    candidates = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            continue
        address = variant.get("play_addr") or variant.get("playAddr")
        if not isinstance(address, dict):
            continue
        bitrate = _positive(variant.get("bit_rate") or variant.get("bitRate"))
        size = _positive(address.get("data_size") or address.get("dataSize"))
        pixels = _positive(address.get("width")) * _positive(address.get("height"))
        candidates.append((variant, address, bitrate, size, pixels, index))
    # Compare like with like. Bitrate is the best common predictor of size;
    # metadata coverage varies by API, so fall back to size, then pixel count.
    metric = next((i for i in (2, 3, 4) if any(c[i] for c in candidates)), 2)
    candidates.sort(key=lambda c: (not bool(c[metric]), c[metric], c[5]))
    urls = []
    for _, address, *_ in candidates:
        values = address.get("url_list") or address.get("urlList") or []
        if not isinstance(values, list):
            continue
        for url in values:
            if isinstance(url, str) and url.startswith(("https://", "http://")):
                url = url.replace("/playwm/", "/play/").replace("playwm?", "play?")
                if url not in urls:
                    urls.append(url)
    return urls

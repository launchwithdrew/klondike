"""Read-only verification of live post links and configured public accounts."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

from . import brand

PROFILE_URLS = {
    "youtube": "https://www.youtube.com/@{}",
    "instagram": "https://www.instagram.com/{}/",
    "tiktok": "https://www.tiktok.com/@{}",
}
_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "tiktok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"},
}
_HANDLE = re.compile(r"^[\w.-]{1,100}$")


def account(platform: str) -> tuple[str, str]:
    settings = brand.load()["accounts"].get(platform) or {}
    handle = settings.get("handle" if platform == "youtube" else "username") or ""
    return handle.strip().lstrip("@"), settings.get("channelId") or ""


def live_url(platform: str, url: str) -> str:
    """Reject other sites, profile URLs, and embedded credentials before making a request."""
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or host not in _HOSTS[platform] or parsed.netloc.lower() != host:
        raise ValueError(f"use the HTTPS link to the live {platform} post")
    path = parsed.path.rstrip("/")
    if platform == "youtube":
        if host == "youtu.be":
            video = path.lstrip("/")
        elif path == "/watch":
            video = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live)/([\w-]+)", path)
            video = match[1] if match else ""
        if re.fullmatch(r"[A-Za-z0-9_-]+", video):
            return "https://www.youtube.com/watch?" + urlencode({"v": video})
    if platform == "instagram" and re.fullmatch(r"/(?:reel|p)/[\w-]+", path):
        return f"https://www.instagram.com{path}/"
    if platform == "tiktok":
        if re.fullmatch(r"/@[\w.-]+/video/\d+", path):
            return f"https://www.tiktok.com{path}"
        if host in {"vm.tiktok.com", "vt.tiktok.com"} and re.fullmatch(r"/[\w-]+", path):
            return f"https://{host}{path}/"
    raise ValueError(f"use a {platform} link to the specific post")


def _json(url: str) -> dict:
    with urlopen(url, timeout=8) as response:
        result = json.loads(response.read(256_000))
    if not isinstance(result, dict):
        raise ValueError("the platform returned an invalid oEmbed response")
    return result


def post(platform: str, url: str, confirmed: bool = False) -> str:
    """Return the verification method or raise a reason suitable for the attention line."""
    try:
        canonical = live_url(platform, url)
    except (ValueError, TypeError):
        raise ValueError(f"use the HTTPS link to the specific live {platform} post") from None
    expected, channel_id = account(platform)
    if not _HANDLE.fullmatch(expected):
        raise ValueError(f"{platform} handle is missing or invalid in brand/brand.json")
    if platform == "instagram":
        if not confirmed:
            raise ValueError(f"open the link and confirm it's @{expected}, then rerun with --confirmed")
        return "operator-confirmed"
    if platform == "tiktok":                                 # a full link names its account: it must be ours
        m = re.fullmatch(r"https://www\.tiktok\.com/@([\w.-]+)/video/\d+", canonical)
        if m and m[1].casefold() != expected.casefold():
            raise ValueError(f"the link is @{m[1]}'s post, not @{expected}")
    target = PROFILE_URLS[platform].split("@", 1)[0].rstrip("/") + "/oembed"
    query = {"url": canonical}
    if platform == "youtube":
        query["format"] = "json"
    try:
        info = _json(target + "?" + urlencode(query))
    except (OSError, ValueError) as exc:
        status = getattr(exc, "code", None)
        suffix = f" (HTTP {status})" if status else ""
        if confirmed:            # the platform won't answer this host (TikTok blocks automated reads): the operator saw it
            return f"operator-confirmed (oEmbed unavailable{suffix})"
        raise ValueError(f"{platform} oEmbed unavailable{suffix} — the platform won't confirm the author from here; "
                         f"open the link, check it plays as @{expected}, then rerun with --confirmed") from None
    if platform == "tiktok":
        actual = str(info.get("author_unique_id") or "").strip().lstrip("@")
    else:
        author = urlsplit(str(info.get("author_url") or ""))
        path = author.path.rstrip("/")
        if author.hostname not in _HOSTS["youtube"]:
            raise ValueError("YouTube oEmbed did not return a YouTube author URL")
        if path.startswith("/channel/") and channel_id:
            if path == f"/channel/{channel_id}":
                return "oembed"
            raise ValueError(f"the YouTube channel ID does not match @{expected}")
        actual = path[2:] if path.startswith("/@") else str(info.get("author_name") or "").strip().lstrip("@")
    if not actual or actual.casefold() != expected.casefold():
        raise ValueError(f"{platform} author does not match @{expected}")
    return "oembed"


def public_accounts() -> list[tuple[str, str, bool, str]]:
    results = []
    for platform, url_format in PROFILE_URLS.items():
        handle, _ = account(platform)
        if not _HANDLE.fullmatch(handle):
            results.append((platform, handle, False, "configure the handle in brand/brand.json"))
            continue
        url = url_format.format(handle)
        try:
            with urlopen(url, timeout=8) as response:
                final = urlsplit(response.geturl())
                body = response.read(1_000_000).decode("utf-8", "replace")
            ok = final.hostname in _HOSTS[platform] and final.path.rstrip("/").casefold() == urlsplit(url).path.rstrip("/").casefold()
            ok = ok and bool(re.search(r'<meta[^>]+(?:og:url|og:title|twitter:title)|<link[^>]+rel=["\']canonical', body, re.I))
            why = "" if ok else "profile did not resolve publicly"
        except (OSError, ValueError) as exc:
            status = getattr(exc, "code", None)
            ok, why = False, f"profile unavailable (HTTP {status})" if status else "profile could not be reached"
        results.append((platform, handle, ok, why))
    return results

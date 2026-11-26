"""Fetch a link's title/description/preview image, and make local thumbnails."""
import html
import io
import json
import os
import re
from urllib.parse import quote, urljoin, urlparse

import httpx

URL_RE = re.compile(r"https?://\S+", re.I)
MAX_BYTES = 256 * 1024
MAX_IMG_BYTES = 6 * 1024 * 1024
THUMB_PX = 480
UA = "Mozilla/5.0 (X11; Linux x86_64) idea-bot/1.0"
JUNK = ("simple page", "just a moment", "access denied", "attention required", "403 forbidden",
        "404 not found", "page not found", "are you a robot", "captcha", "security check",
        "before you continue", "error")
YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)([\w-]{11})")


def _junk(t):
    return not t or any(t.lower().startswith(j) for j in JUNK)


def find_url(text):
    m = URL_RE.search(text)
    return m.group(0).rstrip(").,>]'\"") if m else None


def _meta(doc, *names):
    for n in names:
        m = re.search(
            r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]*content=["\']([^"\']*)' % re.escape(n),
            doc, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']%s["\']' % re.escape(n),
            doc, re.I)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return None


async def _youtube(cl, url, vid):
    title = None
    try:
        r = await cl.get("https://www.youtube.com/oembed?format=json&url=" + quote(url, safe=""))
        if r.status_code == 200:
            title = json.loads(r.text).get("title")
    except Exception:
        pass
    return title, None, f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


async def fetch_info(url):
    """Returns (title, description, image_url); any may be None. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cl:
            m = YT_RE.search(url)
            if m:
                return await _youtube(cl, url, m.group(1))
            async with cl.stream("GET", url) as r:
                final = str(r.url)
                ctype = r.headers.get("content-type", "html")
                if ctype.startswith("image/"):
                    return None, None, url
                if "html" not in ctype:
                    return None, None, None
                buf = b""
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) >= MAX_BYTES:
                        break
        doc = buf.decode("utf-8", "replace")
        title = _meta(doc, "og:title", "twitter:title")
        if not title:
            t = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
            title = html.unescape(" ".join(t.group(1).split())) if t else None
        if _junk(title):
            return None, None, None
        desc = _meta(doc, "og:description", "description", "twitter:description")
        img = _meta(doc, "og:image:secure_url", "og:image", "twitter:image", "twitter:image:src")
        if img:
            img = urljoin(final, img)
            if urlparse(img).scheme not in ("http", "https"):
                img = None
        return title[:200], (desc[:300] if desc else None), img
    except Exception:
        return None, None, None


async def make_thumb(image_url, dest):
    """Download image_url, write a small JPEG to dest. Returns True on success."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 40_000_000
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers={"User-Agent": UA}) as cl:
            async with cl.stream("GET", image_url) as r:
                if r.status_code != 200 or not r.headers.get("content-type", "image").startswith("image"):
                    return False
                buf = b""
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_IMG_BYTES:
                        return False
        im = Image.open(io.BytesIO(buf))
        im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))
        im = im.convert("RGB")
        im.thumbnail((THUMB_PX, THUMB_PX))
        tmp = dest + ".tmp"
        im.save(tmp, "JPEG", quality=80, optimize=True)
        os.replace(tmp, dest)
        return True
    except Exception:
        return False

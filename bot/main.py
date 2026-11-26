"""Signal <-> handler bridge via signal-cli-rest-api (json-rpc mode)."""
import asyncio
import time
import json
import logging
import os

import httpx
import websockets

import handler
import links
from db import DB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

API = os.environ.get("SIGNAL_API", "http://127.0.0.1:8099")
BOT = os.environ["BOT_NUMBER"]
ALLOWED = {x.strip() for x in os.environ.get("ALLOWED_SENDERS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "/data/ideas.db")


async def send(to, text):
    async with httpx.AsyncClient(timeout=30) as cl:
        r = await cl.post(f"{API}/v2/send",
                          json={"number": BOT, "recipients": [to], "message": text})
        if r.status_code >= 300:
            log.error("send failed %s: %s", r.status_code, r.text[:300])


async def typing(to, on=True):
    try:
        async with httpx.AsyncClient(timeout=10) as cl:
            await cl.request("PUT" if on else "DELETE", f"{API}/v1/typing-indicator/{BOT}",
                             json={"recipient": to})
    except Exception:
        pass


async def process(db, env):
    dm = env.get("dataMessage") or {}
    text = dm.get("message")
    if not text:
        return
    number, uuid = env.get("sourceNumber"), env.get("sourceUuid")
    if not ({number, uuid} & ALLOWED):
        log.warning("ignoring message from unlisted sender number=%s uuid=%s", number, uuid)
        return
    to = number or uuid
    await typing(to)
    try:
        reply = await handler.handle(db, text, dm.get("timestamp"))
    except Exception:
        log.exception("handler crashed")
        reply = "Sorry, something broke saving that. Try again?"
    await typing(to, False)
    if reply:
        await send(to, reply)


THUMBS = os.path.join(os.path.dirname(DB_PATH), "thumbs")


async def preview_worker(db):
    """Fill in link previews for new, edited, or older items (one DB connection, same loop)."""
    os.makedirs(THUMBS, exist_ok=True)
    while True:
        try:
            for it in db.pending_previews():
                title, image_url = it["url_title"], it["image_url"]
                if not image_url:
                    t, _, image_url = await links.fetch_info(it["url"])
                    title = title or t
                thumb = None
                if image_url:
                    name = f'{it["id"]}-{int(time.time())}.jpg'
                    if await links.make_thumb(image_url, os.path.join(THUMBS, name)):
                        thumb = name
                db.set_preview(it["id"], title, image_url, thumb)
                log.info("preview #%s: title=%r thumb=%s", it["id"], title, thumb)
        except Exception:
            log.exception("preview worker")
        await asyncio.sleep(10)


async def main():
    db = DB(DB_PATH)
    asyncio.create_task(preview_worker(db))
    url = API.replace("http", "ws", 1) + f"/v1/receive/{BOT}"
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ping_interval=30, ping_timeout=30,
                                          max_size=2**22) as ws:
                log.info("connected to %s", url)
                backoff = 2
                async for raw in ws:
                    try:
                        env = json.loads(raw).get("envelope", {})
                    except ValueError:
                        continue
                    await process(db, env)
        except Exception as e:
            log.warning("websocket error: %s; retrying in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())

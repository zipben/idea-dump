"""End-to-end handler test against the real model, using a throwaway DB.
Run: docker compose run --rm --no-deps -e DB_PATH=/tmp/t.db idea-bot python test_flow.py"""
import asyncio
import logging
logging.basicConfig(level=logging.INFO, format='  LOG %(message)s')
logging.getLogger('httpx').setLevel(logging.WARNING)
import os
import time

import handler
from db import DB

MSGS = [
    "help",
    "https://www.seriouseats.com/the-food-lab-best-chili-recipe",
    "build a router table out of the old workbench top",
    "idea: app that tells you when your library holds are ready",
    "garlic chili crisp noodles, try with the leftover pork",
    "wrong, put it in Dinner Ideas",
    "Get Dad a fly fishing reel for Christmas",
    "that's wrong",
    "Gift Ideas",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "app idea: reminder to call friends I haven't talked to in a while",
    "heated blanket for mom for christmas",
    "watch Severance season 2",
    "fix the squeaky hinge on the back door",
    "mv #3 Business Ideas",
    "what's in dinner ideas?",
    "did I save anything about chili?",
    "cats",
    "undo",
    "thanks!",
]


async def main():
    path = os.environ.get("DB_PATH", "/tmp/t.db")
    if os.path.exists(path):
        os.remove(path)
    db = DB(path)
    for m in MSGS:
        t = time.time()
        r = await handler.handle(db, m)
        print(f"> {m}\n{r}\n  ({time.time() - t:.1f}s)\n")


asyncio.run(main())

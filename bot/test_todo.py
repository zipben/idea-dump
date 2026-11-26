"""Idea-vs-todo accuracy + to-do conversation flow, on a throwaway DB.
Run: docker compose run --rm --no-deps -e DB_PATH=/tmp/t.db -e BOT_NUMBER=x idea-bot python test_todo.py"""
import asyncio
import os
import time

import handler
from db import DB

CLASSIFY = [
    ("call the dentist to reschedule my cleaning", "todo"),
    ("buy milk and eggs", "todo"),
    ("pay the electric bill by friday", "todo"),
    ("need to renew my drivers license", "todo"),
    ("pick up dry cleaning tomorrow", "todo"),
    ("email Sarah about the invoice", "todo"),
    ("return the amazon package", "todo"),
    ("schedule an oil change", "todo"),
    ("don't forget to water the plants", "todo"),
    ("fix the leaking kitchen faucet", "todo"),
    ("app that tells you when your library holds are ready", "idea"),
    ("garlic chili crisp noodles, try with the leftover pork", "idea"),
    ("watch Severance season 2", "idea"),
    ("a board game where players trade time instead of money", "idea"),
    ("learn to make sourdough someday", "idea"),
    ("quote: the obstacle is the way", "idea"),
    ("build a router table out of the old workbench top", "idea"),
    ("podcast idea: interviewing retired tradespeople", "idea"),
    ("visit Iceland in winter", "idea"),
    ("Get Dad a fly fishing reel for Christmas", "idea"),
]

FLOW = [
    "buy milk and eggs",
    "call the plumber about the water heater",
    "a board game where players trade time instead of money",
    "that's a to-do",
    "that's not a todo, it's an idea",
    "undo",
    "what do I need to do?",
    "I bought the milk",
    "plumber is coming thursday, called him",
    "undo",
    "done t2",
    "undone t2",
    "todo: renew passport",
    "idea: pinball machine made from an old door",
    "rm t4",
    "undo",
    "todo",
    "thanks",
]


async def main():
    path = os.environ.get("DB_PATH", "/tmp/t.db")
    if os.path.exists(path):
        os.remove(path)
    db = DB(path)
    ok = 0
    for text, want in (CLASSIFY if os.environ.get("SKIP_CLASSIFY") != "1" else []):
        r = await handler.handle(db, text)
        got = "todo" if r.startswith("☐") else "idea" if r.startswith("Saved") else "?"
        ok += got == want
        print(f'{"OK " if got == want else "XX "} want={want:4} got={got:4}  {text}')
    print(f"\nidea/todo accuracy: {ok}/{len(CLASSIFY)}\n")

    os.remove(path)
    db = DB(path)
    handler.UNDO.clear()
    for m in FLOW:
        t = time.time()
        r = await handler.handle(db, m)
        print(f"> {m}\n{r}\n  ({time.time() - t:.1f}s)\n")


asyncio.run(main())

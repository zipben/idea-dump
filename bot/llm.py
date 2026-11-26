"""Ollama calls with JSON-schema constrained output."""
import json
import os

import httpx

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:4b-instruct")
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")


async def _chat(system, user, schema):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0.1, "num_ctx": 4096, "num_gpu": 99},
    }
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(f"{OLLAMA}/api/chat", json=body)
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [
            "idea", "todo", "recategorize", "make_todo", "make_idea", "done",
            "list_todos", "query", "chat"]},
        "item_number": {"type": "integer"},
        "todo_number": {"type": "integer"},
        "category": {"type": "string"},
        "query": {"type": "string"},
    },
    "required": ["intent", "item_number", "todo_number", "category", "query"],
}

ROUTE_SYSTEM = """You are the message router for a personal capture bot. The user texts it things to save. Saved things are either IDEAS (numbered like #12) or TO-DOS (numbered like t3). Decide what THIS message is.

intents:
- todo: a concrete task the user must do themselves soon (today / this week / this month): errands, chores, calls, emails, appointments, buying a specific thing they need, paying, returning, renewing, fixing something broken. Often starts with a verb ("call", "buy", "pick up", "email", "schedule", "pay", "renew", "return", "book") or says "need to", "remember to", "don't forget", "tomorrow", "by Friday".
- idea: anything to think about, explore, make, learn, read, watch, try or keep for reference, with no near-term obligation: project ideas, app/business ideas, recipes to try, gift ideas, books/shows/music, places, quotes, links, observations. If unsure between idea and todo, choose idea.
- recategorize: the user says the most recent IDEA is in the wrong category, or names where it should go ("wrong, put it in Gifts", "no, that's a recipe", "#12 goes in Books"). Set category to the category they want (Title Case) or "" if they don't name one. item_number = the # they mention, or 0 for the most recent.
- make_todo: the user says something saved as an idea should be a to-do ("that's a todo", "put that on my to-do list", "#4 is a task").  item_number = the # they mention, or 0.
- make_idea: the user says a to-do should be an idea instead ("that's not a todo", "that's just an idea", "t2 is an idea"). todo_number = the t-number they mention, or 0.
- done: the user says they finished a to-do ("done t3", "finished calling the dentist", "bought the milk", "check off the oil change"). Set todo_number to the matching open to-do from the list, or 0 if none matches.
- list_todos: the user wants to see their to-do list ("what do I need to do?", "show my todos").
- query: the user asks to see or find saved IDEAS. Put a category name in category if they name one, and search words in query.
- chat: greetings, thanks, questions about the bot.

Follow the user's past idea/to-do corrections when deciding between idea and todo.
Unused fields: numbers 0, strings ""."""


async def route(text, last, open_todos, kind_corrections):
    ctx = []
    if last:
        if last["kind"] == "idea":
            ctx.append(f'Most recent save: IDEA #{last["id"]} "{last["title"]}" in "{last["category"]}".')
        else:
            ctx.append(f'Most recent save: TO-DO t{last["id"]} "{last["text"]}".')
    else:
        ctx.append("Nothing saved recently.")
    if open_todos:
        ctx.append("Open to-dos:\n" + "\n".join(f't{t["id"]}: {t["text"]}' for t in open_todos))
    else:
        ctx.append("Open to-dos: none.")
    if kind_corrections:
        ctx.append("Past idea/to-do corrections by the user (most recent first):\n" + "\n".join(
            f'- "{k["text"]}" should be a {"to-do" if k["to_kind"] == "todo" else "idea"}'
            for k in kind_corrections))
    return await _chat(ROUTE_SYSTEM, "\n\n".join(ctx) + f"\n\nMessage: {text}", ROUTE_SCHEMA)


CAT_SYSTEM = """You file a person's saved ideas and links into categories (buckets).

Fields:
- title: a short 3-8 word summary of the item.
- topic: what kind of thing this is, in a few words.
- choice: the existing category this item belongs in, or "NEW" if none is the same kind of thing. Only pick an existing category if a typical item in it is the same kind of thing as this one. When in doubt, choose "NEW".
- new_name: if choice is "NEW", a broad, reusable bucket name of 1-3 words in Title Case, general enough that many future items could share it (drop specifics: an app idea about libraries goes in a general app-ideas bucket, not a library-app bucket). If choice is not "NEW", "".
Follow the user's past corrections: they show how this person likes things filed."""


SUGGESTED = ["App Ideas", "Business Ideas", "Recipes", "Home Projects", "Gift Ideas",
             "Movies & TV", "Music", "Videos", "Books", "Podcasts", "Games", "Travel",
             "Places", "Shopping", "Health & Fitness", "Learning", "Tech", "Work",
             "Hobbies", "Events", "Quotes", "People", "Finance", "Reminders"]


def _cat_schema(names):
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "topic": {"type": "string"},
            "choice": {"type": "string", "enum": names + ["NEW"]},
            "new_name": {"type": "string"},
        },
        "required": ["title", "topic", "choice", "new_name"],
    }


async def categorize(text, url_title, url_desc, cats, corrections):
    """Returns {title, topic, category, is_new}."""
    cats = [c for c in cats if c["name"].lower() != "inbox"]
    lines = ["Existing categories:" if cats else "Existing categories: none yet, so choose NEW."]
    for c in cats:
        ex = "; ".join(c["samples"]) if c["samples"] else "empty"
        lines.append(f'- {c["name"]} (items like: {ex})')
    if corrections:
        lines.append("\nUser's past corrections (most recent first):")
        for co in corrections:
            lines.append(f'- "{co["title"] or "item"}": {co["from_cat"]} -> {co["to_cat"]}')
    unused = [n for n in SUGGESTED if n.lower() not in {c["name"].lower() for c in cats}]
    lines.append("\nIf you choose NEW, prefer one of these names when it fits: "
                 + ", ".join(unused))
    lines.append("\nItem to file:")
    lines.append(text)
    if url_title:
        lines.append(f"Link page title: {url_title}")
    if url_desc:
        lines.append(f"Link page description: {url_desc}")
    r = await _chat(CAT_SYSTEM, "\n".join(lines), _cat_schema([c["name"] for c in cats]))
    is_new = r.get("choice") == "NEW"
    name = (r.get("new_name") if is_new else r.get("choice")) or ""
    if name.strip().lower() in ("", "none", "new", "inbox", "n/a"):
        name = (r.get("topic") or "Inbox").title() if is_new else "Inbox"
    return {"title": r.get("title", ""), "topic": r.get("topic", ""),
            "category": name.strip(), "is_new": is_new}


async def ping():
    async with httpx.AsyncClient(timeout=5) as cl:
        r = await cl.get(f"{OLLAMA}/api/tags")
        return r.status_code == 200

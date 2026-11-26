"""Transport-agnostic message handling: text in, reply text out."""
import difflib
import logging
import re
import time

import links
import llm
from db import INBOX

log = logging.getLogger("handler")
RECENT_SECS = 15 * 60  # "that's wrong" refers to the last save if it's this recent
PENDING = {}  # {"item": id, "until": ts} after we ask "where should #N go?"
UNDO = []     # stack of (description, callable returning reply text)

HELP = """Text me anything. Ideas get filed into categories (#12); short-term tasks go on your to-do list (t3).
Fix things in plain words: "wrong, put it in Gifts", "that's a to-do", "that's an idea", "done t3".
Commands:
todo - show your to-do list
todo: <task> - add a to-do
idea: <text> - save as an idea
done t3 / undone t3 - check off / reopen
cats - list idea categories
ls <category> - last 10 ideas there
find <words> - search ideas
mv [#N] <category> - move last (or #N) idea
rm #N / rm t3 - delete
undo - undo the last action
help - this message"""


def _push_undo(desc, fn):
    UNDO.append((desc, fn))
    del UNDO[:-20]


def _match_category(db, name):
    """Exact (case-insensitive) or close fuzzy match to an existing category."""
    if not name:
        return None
    row = db.category_by_name(name)
    if row:
        return row
    cats = {c["name"].lower(): c for c in db.categories()}
    norm = name.lower().strip()
    for cand in (norm.rstrip("s"), norm + "s"):
        if cand in cats:
            return cats[cand]
    close = difflib.get_close_matches(norm, list(cats), n=1, cutoff=0.85)
    return cats[close[0]] if close else None


def _fmt_items(rows, show_cat=False):
    out = []
    for r in rows:
        line = f'#{r["id"]} {r["title"]}'
        if show_cat:
            line += f' [{r["category"]}]'
        if r["url"]:
            line += f'\n   {r["url"]}'
        out.append(line)
    return "\n".join(out)


def _list_cats(db):
    rows = db.categories()
    return "Categories:\n" + "\n".join(f'{c["name"]} ({c["n"]})' for c in rows)


def _list_todos(db):
    rows = db.open_todos()
    if not rows:
        return "Your to-do list is empty. 🎉"
    return f"To-do ({len(rows)}):\n" + "\n".join(f't{t["id"]} {t["text"]}' for t in rows)


def _last_saved(db):
    """Most recent idea or to-do, tagged with kind, or None."""
    it, td = db.last_item(), db.last_todo()
    cands = []
    if it:
        cands.append((it["created_at"], it["id"], "idea", it))
    if td:
        cands.append((td["created_at"], td["id"], "todo", td))
    if not cands:
        return None
    _, _, kind, row = max(cands, key=lambda c: (c[0], c[2] == "todo"))
    d = dict(row)
    d["kind"] = kind
    return d


def _recent(last):
    return last if last and time.time() - last["created_at"] < RECENT_SECS else None


def _move(db, item, cat_name, user_text):
    existing = _match_category(db, cat_name)
    name = existing["name"] if existing else cat_name.strip().title()
    res = db.move_item(item["id"], name, user_text)
    if not res:
        return "I couldn't find that item."
    before, cat, created = res
    if before["category_id"] == cat["id"]:
        return f'#{item["id"]} is already in {cat["name"]}.'

    def undo(iid=item["id"]):
        co = db.last_correction_for(iid)
        r = db.undo_correction(co) if co else None
        return f'Undid move: #{iid} is back in {r[1]["name"]}.' if r else "Couldn't undo that move."
    _push_undo("move", undo)
    return (f'Moved #{item["id"]} "{item["title"]}" → {cat["name"]}'
            + (" (new category)" if created else ""))


async def _query(db, cat_name, terms):
    cat = _match_category(db, cat_name) if cat_name else None
    if cat:
        rows = db.items_in(cat["id"])
        if not rows:
            return f'{cat["name"]} is empty.'
        return f'{cat["name"]} (latest {len(rows)}):\n' + _fmt_items(rows)
    if terms:
        rows = db.search(terms)
        if rows:
            return f'Matches for "{terms}":\n' + _fmt_items(rows, show_cat=True)
        return f'Nothing saved matching "{terms}".'
    return _list_cats(db)


async def _save(db, text, signal_ts):
    url = links.find_url(text)
    url_title = url_desc = image_url = None
    if url:
        url_title, url_desc, image_url = await links.fetch_info(url)
    cats = []
    for c in db.categories():
        cats.append({"name": c["name"], "description": c["description"],
                     "samples": db.sample_titles(c["id"])})
    try:
        res = await llm.categorize(text, url_title, url_desc, cats, db.recent_corrections())
    except Exception:
        log.exception("categorize failed")
        res = {"title": (url_title or text)[:60], "category": INBOX, "is_new": False}
    log.info("categorize %r -> %s", text[:60], res)
    title = (res.get("title") or url_title or text).strip()[:120]
    wanted = (res.get("category") or INBOX).strip()[:40]
    existing = _match_category(db, wanted)
    created = False
    if existing:
        cat = existing
    else:
        cat, created = db.ensure_category(wanted.title() if wanted.islower() else wanted)
    item_id = db.add_item(text, title, cat["id"], url, url_title, signal_ts, image_url)

    def undo(iid=item_id, t=title):
        db.delete_item(iid)
        return f'Undid save of #{iid} "{t}".'
    _push_undo("save", undo)
    return (f'Saved #{item_id} → {cat["name"]}' + (" (new category)" if created else "")
            + f'\n"{title}"')


def _add_todo(db, text, signal_ts):
    text = text.strip()[:500]
    tid = db.add_todo(text, signal_ts)

    def undo(t=tid):
        db.delete_todo(t)
        return f"Undid to-do t{t}."
    _push_undo("todo", undo)
    n = len(db.open_todos())
    return f'☐ To-do t{tid}: {text}\n({n} open)'


def _done(db, tid, done=True):
    t = db.todo(tid)
    if not t:
        return f"No to-do t{tid}."
    if bool(t["done"]) == done:
        return f't{tid} is already {"done" if done else "open"}.'
    db.set_todo_done(tid, done)

    def undo(t=tid, d=done):
        db.set_todo_done(t, not d)
        return f't{t} is {"open" if d else "done"} again.'
    _push_undo("done", undo)
    if done:
        n = len(db.open_todos())
        return f'✔ Done: t{tid} {t["text"]}\n({n} left)'
    return f'☐ Reopened t{tid} {t["text"]}'


def _make_todo(db, item, user_text):
    db.delete_item(item["id"])
    text = item["raw_text"]
    if item["url"] and item["url_title"] and item["url_title"] not in text:
        text = f'{item["url_title"]} {text}'
    tid = db.add_todo(text, item["signal_ts"])
    db.add_kind_correction(item["raw_text"], "idea", "todo")

    def undo(row=item, t=tid):
        db.delete_todo(t)
        db.restore_item(row)
        db.delete_last_kind_correction()
        return f'Undid: #{row["id"]} is an idea again (in {row["category"]}).'
    _push_undo("make_todo", undo)
    return f'Moved #{item["id"]} to your to-do list as t{tid}: {text}'


async def _make_idea(db, todo, signal_ts):
    db.delete_todo(todo["id"])
    db.add_kind_correction(todo["text"], "todo", "idea")
    reply = await _save(db, todo["text"], todo["signal_ts"])
    _, save_undo = UNDO.pop()  # fold the save into one undo step

    def undo(row=todo, su=save_undo):
        su()
        db.restore_todo(row)
        db.delete_last_kind_correction()
        return f'Undid: t{row["id"]} is a to-do again.'
    _push_undo("make_idea", undo)
    return f'Moved t{todo["id"]} to ideas. ' + reply


STOP = {"the", "and", "for", "with", "about", "that", "this", "just", "got", "have", "has", "had",
        "did", "done", "finished", "already", "was", "were", "off", "out", "from", "my", "her", "him",
        "them", "it", "its", "today", "yesterday", "now", "all", "too", "also"}


def _words(t):
    return {w[:4] for w in re.findall(r"[a-z0-9']+", t.lower()) if len(w) >= 3 and w not in STOP}


def _guess_todo(db, text, recent=None):
    """Pick the open to-do matching free text by shared words; fall back to the recent to-do."""
    rows = db.open_todos()
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    said = _words(text)
    scored = sorted(((len(said & _words(r["text"])), r["id"], r) for r in rows), reverse=True)
    if scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][2]
    if recent and recent["kind"] == "todo" and not recent["done"]:
        return db.todo(recent["id"])
    return None


async def handle(db, text, signal_ts=None):
    text = (text or "").strip()
    if not text:
        return None
    low = text.lower()

    # ---- explicit commands (no model) ----
    if low in ("help", "?", "commands"):
        return HELP
    if low in ("cats", "categories", "ls"):
        return _list_cats(db)
    if low in ("todo", "todos", "to-do", "to do", "to-dos", "todo list", "to-do list", "to do list",
               "list", "tasks", "td"):
        return _list_todos(db)
    if low == "undo":
        if UNDO:
            _, fn = UNDO.pop()
            return fn()
        kind, row = db.last_action()  # after a restart: fall back to DB history for ideas
        if kind == "add":
            db.delete_item(row["id"])
            return f'Undid save of #{row["id"]} "{row["title"]}".'
        if kind == "move":
            res = db.undo_correction(row)
            if res:
                it, cat = res
                return f'Undid move: #{it["id"]} is back in {cat["name"]}.'
        return "Nothing to undo."
    m = re.fullmatch(r"(?:to-?do|to do|td)\s*[:\-]\s*(.+)|(?:to-?do|td)\s+(.+)", text, re.I | re.S)
    if m:
        return _add_todo(db, m.group(1) or m.group(2), signal_ts)
    m = re.fullmatch(r"idea\s*[:\-]\s*(.+)", text, re.I | re.S)
    if m:
        return await _save(db, m.group(1).strip(), signal_ts)
    m = re.fullmatch(r"(?:done|did|x|check|✓|✔)\s+t?(\d+)", low)
    if m:
        return _done(db, int(m.group(1)))
    m = re.fullmatch(r"(?:undone|reopen|uncheck)\s+t?(\d+)", low)
    if m:
        return _done(db, int(m.group(1)), done=False)
    m = re.fullmatch(r"(?:rm|del|delete)\s+t(\d+)", low)
    if m:
        t = db.delete_todo(int(m.group(1)))
        if not t:
            return "No such to-do."
        _push_undo("rm_todo", lambda row=t: (db.restore_todo(row), f't{row["id"]} restored.')[1])
        return f'Deleted t{t["id"]} {t["text"]}.'
    m = re.fullmatch(r"(?:rm|del|delete)\s+#?(\d+)", low)
    if m:
        it = db.delete_item(int(m.group(1)))
        if not it:
            return "No such item."
        _push_undo("rm", lambda row=it: (db.restore_item(row), f'#{row["id"]} restored.')[1])
        return f'Deleted #{it["id"]} "{it["title"]}".'
    m = re.fullmatch(r"(?:mv|move)\s+(?:#(\d+)\s+)?(?:to\s+)?(.+)", text, re.I)
    if m and not re.search(r"to-?do|to do|idea", m.group(2), re.I):
        it = db.item(int(m.group(1))) if m.group(1) else db.last_item()
        if not it:
            return "No item to move."
        return _move(db, it, m.group(2), text)
    m = re.fullmatch(r"ls\s+(.+)", text, re.I)
    if m:
        return await _query(db, m.group(1), "")
    m = re.fullmatch(r"(?:find|search)\s+(.+)", text, re.I)
    if m:
        return await _query(db, "", m.group(1))

    # ---- a bare link is an idea; a link with words gets routed ----
    url = links.find_url(text)
    if url and len(text.replace(url, "").split()) < 3:
        PENDING.clear()
        return await _save(db, text, signal_ts)

    # ---- answer to "where should #N go?" ----
    if PENDING and time.time() < PENDING["until"]:
        item_id = PENDING.pop("item")
        PENDING.clear()
        m = re.fullmatch(r"(?:(?:put|move|file)\s+(?:it\s+)?(?:in(?:to)?|under|to)\s+)?(.{1,40})", text, re.I)
        it = db.item(item_id)
        if it and m and len(m.group(1).split()) <= 4:
            return _move(db, it, m.group(1).strip(" .!"), text)
    PENDING.clear()

    # ---- let the model decide what the message means ----
    last = _last_saved(db)
    recent = _recent(last)
    try:
        r = await llm.route(text, recent, db.open_todos(limit=20), db.recent_kind_corrections())
    except Exception:
        log.exception("route failed")
        return await _save(db, text, signal_ts)
    intent = r.get("intent")
    log.info("route %r -> %s", text[:60], r)
    num, tnum = r.get("item_number") or 0, r.get("todo_number") or 0

    if intent == "todo":
        return _add_todo(db, text, signal_ts)
    if intent == "recategorize":
        it = db.item(num) if num else (db.item(recent["id"]) if recent and recent["kind"] == "idea" else None)
        if not it:
            if recent and recent["kind"] == "todo":
                return (f't{recent["id"]} is a to-do, so it has no category. '
                        'Say "that\'s an idea" to move it to your ideas.')
            return await _save(db, text, signal_ts)
        cat = (r.get("category") or "").strip()
        if not cat:
            PENDING.update(item=it["id"], until=time.time() + RECENT_SECS)
            return (f'Where should #{it["id"]} "{it["title"]}" go? '
                    'Reply e.g. "put it in Recipes".')
        return _move(db, it, cat, text)
    if intent == "make_todo":
        it = db.item(num) if num else (db.item(recent["id"]) if recent and recent["kind"] == "idea" else None)
        if not it:
            if recent and recent["kind"] == "todo":
                return f't{recent["id"]} is already on your to-do list.'
            return "Which idea? Say e.g. \"#12 is a to-do\"."
        return _make_todo(db, it, text)
    if intent == "make_idea":
        t = db.todo(tnum) if tnum else (db.todo(recent["id"]) if recent and recent["kind"] == "todo" else None)
        if not t:
            if recent and recent["kind"] == "idea":
                return f'#{recent["id"]} is already an idea.'
            return "Which to-do? Say e.g. \"t3 is an idea\"."
        return await _make_idea(db, t, signal_ts)
    if intent == "done":
        t = db.todo(tnum) if tnum else None
        if not t or t["done"]:
            t = _guess_todo(db, text, recent)
        if not t or t["done"]:
            return "Which one did you finish?\n" + _list_todos(db)
        return _done(db, t["id"])
    if intent == "list_todos":
        return _list_todos(db)
    if intent == "query":
        return await _query(db, r.get("category") or "", r.get("query") or "")
    if intent == "chat":
        if re.match(r"(thanks|thank you|thx|ty|cool|ok|great|nice)\b", low):
            return "👍"
        return ("I save your ideas (filed by category) and to-dos. "
                "Text me anything, or send \"help\" for commands.")
    return await _save(db, text, signal_ts)

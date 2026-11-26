"""Idea dump viewer/editor. Stdlib only; plain HTML forms."""
import hashlib
import hmac
import html
import os
import secrets
import sys
import threading
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, quote, urlencode, urlparse

DB_PATH = os.environ.get("DB_PATH", "/data/ideas.db")
THUMBS = os.path.join(os.path.dirname(DB_PATH), "thumbs")
PORT = int(os.environ.get("PORT", "9001"))
INBOX = "Inbox"
THUMB_RE = re.compile(r"\d+(?:-\d+)?\.jpg")
JUNK_TITLES = ("simple page", "just a moment", "access denied")

CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e4e4df;--acc:#2f6fdb;--sel:#e9effb;--bad:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#e8e8e3;--mut:#9a9a93;--card:#1f1f1d;--line:#33332f;--acc:#7aa7ff;--sel:#24304a;--bad:#ff7b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0;margin-right:auto}header form{margin:0}header h1 a{color:var(--fg)}
input,textarea,select,button{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
header input{width:220px;max-width:60vw}button{cursor:pointer}
.wrap{display:grid;grid-template-columns:200px minmax(0,1fr) 340px;grid-template-areas:"nav ideas todos";gap:20px;padding:16px;max-width:1400px;margin:0 auto}
.wrap>nav{grid-area:nav}.wrap>main{grid-area:ideas}.wrap>aside{grid-area:todos}
aside{align-self:start;position:sticky;top:12px;max-height:calc(100vh - 24px);overflow-y:auto}
nav ul{list-style:none;margin:0;padding:0}nav li a{display:flex;justify-content:space-between;padding:6px 10px;border-radius:6px;color:var(--fg)}
nav li a.on{background:var(--sel);font-weight:600}nav li a:hover{background:var(--sel);text-decoration:none}
.n{color:var(--mut);font-variant-numeric:tabular-nums}
h2{font-size:16px;margin:0 0 10px}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:8px;display:flex;gap:12px}
.item .body{flex:1;min-width:0}.item img{width:120px;height:80px;object-fit:cover;border-radius:6px;flex:none;background:var(--line)}
.item .t{font-weight:600}.item .meta{color:var(--mut);font-size:13px;margin-top:2px}
.item .raw{margin-top:4px;white-space:pre-wrap;word-break:break-word}.item .url{font-size:13px;word-break:break-all}
.tag{background:var(--sel);border-radius:4px;padding:0 6px;font-size:12px}
.empty{color:var(--mut)}
.edit{max-width:640px;margin:16px auto;padding:0 16px}.edit label{display:block;margin:12px 0 4px;font-weight:600}
.edit input,.edit textarea,.edit select{width:100%}.edit textarea{min-height:120px}
.row{display:flex;gap:8px;align-items:center;margin-top:16px;flex-wrap:wrap}
.del{margin-top:28px;padding-top:12px;border-top:1px solid var(--line)}.del button{color:var(--bad)}
.hint{color:var(--mut);font-size:13px;margin-top:4px}
.todo{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:6px}
.todo .txt{word-break:break-word}.todo .acts{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.todo form{margin:0}.todo button{padding:2px 9px;font-size:12px}
details summary{cursor:pointer;color:var(--mut);font-size:14px;margin:16px 0 8px}
.todo.done .txt{text-decoration:line-through;color:var(--mut)}.addtodo{display:flex;gap:8px;margin-bottom:14px}.addtodo input{flex:1}
nav li.sep{border-bottom:1px solid var(--line);margin:6px 0}
h3{font-size:14px;margin:20px 0 8px;color:var(--mut)}.pv{max-width:240px;max-height:160px;object-fit:cover;border-radius:6px;display:block;margin-top:8px}
@media (max-width:1000px){.wrap{grid-template-columns:1fr;grid-template-areas:"nav" "todos" "ideas"}
aside{position:static;max-height:none;overflow:visible}}
@media (max-width:700px){nav ul{display:flex;flex-wrap:wrap;gap:4px}nav li a{gap:6px}
.item img{width:84px;height:60px}}
"""


def db():
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c


def e(s):
    return html.escape(str(s or ""))


def ago(ts):
    d = time.time() - ts
    if d < 3600:
        return f"{max(1, int(d // 60))}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    if d < 86400 * 14:
        return f"{int(d // 86400)}d ago"
    return time.strftime("%b %-d, %Y", time.localtime(ts))


# ---------------- auth ----------------
AUTH_DIR = os.environ.get("AUTH_DIR", "/auth")
SESSION_DAYS = 90
PBKDF2_ITERS = 600_000
_FAILS = {}  # ip -> [timestamps]
_FAILS_LOCK = threading.Lock()


def _read(name):
    try:
        with open(os.path.join(AUTH_DIR, name)) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _write(name, value):
    os.makedirs(AUTH_DIR, exist_ok=True)
    path = os.path.join(AUTH_DIR, name)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(value + "\n")
    os.replace(path + ".tmp", path)


def _secret():
    s = _read("secret")
    if not s:
        s = secrets.token_hex(32)
        _write("secret", s)
    return s.encode()


def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), PBKDF2_ITERS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt}${h}"


def check_password(pw):
    stored = _read("password")
    try:
        _, iters, salt, h = stored.split("$")
    except ValueError:
        return False
    got = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(iters)).hex()
    return hmac.compare_digest(got, h)


def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return exp + "." + hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()


def valid_session(tok):
    try:
        exp, sig = tok.split(".", 1)
        good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def locked_out(ip):
    now = time.time()
    with _FAILS_LOCK:
        recent = [t for t in _FAILS.get(ip, []) if now - t < 900]
        _FAILS[ip] = recent
        return len(recent) >= 10


def note_fail(ip):
    with _FAILS_LOCK:
        _FAILS.setdefault(ip, []).append(time.time())


def safe_next(n):
    return n if n.startswith("/") and not n.startswith("//") and not n.startswith("/login") else "/"


def page_login(nxt, err=""):
    err_html = f'<p style="color:var(--bad)">{e(err)}</p>' if err else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Log in · Idea Dump</title>
<style>{CSS}</style></head><body>
<div class="edit" style="max-width:360px;margin-top:12vh"><h2>Idea Dump</h2>{err_html}
<form method="post" action="/login"><input type="hidden" name="next" value="{e(nxt)}">
<label for="pw">Password</label><input id="pw" type="password" name="password" autocomplete="current-password" required autofocus>
<div class="row"><button>Log in</button></div></form></div></body></html>"""


def shell(title, inner, q=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body>
<header><h1><a href="/">Idea Dump</a></h1>
<form method="get" action="/"><input name="q" value="{e(q)}" placeholder="Search ideas"> <button>Search</button></form>
<form method="post" action="/logout"><button>Log out</button></form></header>
{inner}</body></html>"""


def link_label(r):
    t = r["url_title"]
    if not t or t.lower().startswith(JUNK_TITLES):
        return r["url"]
    return t


def item_html(r, show_cat, back):
    raw = r["raw_text"]
    if r["url"]:
        raw = raw.replace(r["url"], "").strip()
    out = ['<div class="item">']
    if r["thumb"]:
        img = f'<img src="/thumb/{e(r["thumb"])}" alt="" loading="lazy">'
        out.append(f'<a href="{e(r["url"])}" rel="noreferrer" target="_blank">{img}</a>'
                   if r["url"] else img)
    out.append(f'<div class="body"><div class="t">{e(r["title"])}</div>')
    if r["url"]:
        dom = urlparse(r["url"]).netloc.removeprefix("www.")
        out.append(f'<div class="url"><a href="{e(r["url"])}" rel="noreferrer" target="_blank">'
                   f'{e(link_label(r))}</a> <span class="n">· {e(dom)}</span></div>')
    if raw and raw.lower() != (r["title"] or "").lower():
        out.append(f'<div class="raw">{e(raw)}</div>')
    meta = ago(r["created_at"])
    if show_cat:
        meta += f' · <a class="tag" href="/?cat={r["category_id"]}">{e(r["category"])}</a>'
    meta += f' · <a href="/edit?{urlencode({"id": r["id"], "back": back})}">Edit</a>'
    out.append(f'<div class="meta">{meta}</div></div></div>')
    return "".join(out)


def nav_html(c, active):
    cats = c.execute(
        "SELECT c.id, c.name, COUNT(i.id) n FROM categories c "
        "LEFT JOIN items i ON i.category_id=c.id GROUP BY c.id "
        "HAVING n > 0 ORDER BY n DESC, c.name").fetchall()
    total = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    nav = [f'<li><a class="{"on" if active == "all" else ""}" href="/">All ideas<span class="n">{total}</span></a></li>']
    for x in cats:
        on = "on" if active == str(x["id"]) else ""
        nav.append(f'<li><a class="{on}" href="/?cat={x["id"]}">{e(x["name"])}'
                   f'<span class="n">{x["n"]}</span></a></li>')
    return f'<nav><ul>{"".join(nav)}</ul></nav>'


def page_list(qs, back):
    cat_id = qs.get("cat", [""])[0]
    q = qs.get("q", [""])[0].strip()
    with db() as c:
        nav = nav_html(c, cat_id if cat_id.isdigit() else ("" if q else "all"))
        todos = todo_panel(c, back)
        base = ("SELECT i.*, c.name AS category FROM items i "
                "JOIN categories c ON c.id=i.category_id ")
        if q:
            like = f"%{q}%"
            rows = c.execute(base + "WHERE i.raw_text LIKE ? OR i.title LIKE ? OR i.url_title LIKE ? "
                             "ORDER BY i.id DESC LIMIT 500", (like, like, like)).fetchall()
            heading = f'Search: “{e(q)}” <span class="n">({len(rows)})</span>'
        elif cat_id.isdigit():
            rows = c.execute(base + "WHERE i.category_id=? ORDER BY i.id DESC",
                             (int(cat_id),)).fetchall()
            r = c.execute("SELECT name FROM categories WHERE id=?", (int(cat_id),)).fetchone()
            heading = e(r["name"] if r else "Unknown category")
        else:
            rows = c.execute(base + "ORDER BY i.id DESC LIMIT 200").fetchall()
            heading = "Everything (newest first)"
    show_cat = not cat_id.isdigit()
    body = "".join(item_html(r, show_cat, back) for r in rows) or '<p class="empty">Nothing here yet.</p>'
    return shell("Idea Dump", f'<div class="wrap">{nav}'
                              f'<main><h2>{heading}</h2>{body}</main>{todos}</div>', q)


def btn(action, tid, label, back):
    return (f'<form method="post" action="{action}"><input type="hidden" name="id" value="{tid}">'
            f'<input type="hidden" name="back" value="{e(back)}"><button>{label}</button></form>')


def todo_panel(c, back):
    open_rows = c.execute("SELECT * FROM todos WHERE done=0 ORDER BY id").fetchall()
    done_rows = c.execute("SELECT * FROM todos WHERE done=1 ORDER BY done_at DESC LIMIT 20").fetchall()
    rows = []
    for t in open_rows:
        rows.append(f'<div class="todo"><div class="txt">{e(t["text"])}</div>'
                    '<div class="acts">' + btn("/todo/done", t["id"], "Done", back)
                    + btn("/todo/to-idea", t["id"], "Move to ideas", back)
                    + btn("/todo/delete", t["id"], "Delete", back) + '</div></div>')
    body = "".join(rows) or '<p class="empty">Nothing to do. 🎉</p>'
    done = "".join(
        f'<div class="todo done"><div class="txt">{e(t["text"])}'
        f' <span class="n">· {ago(t["done_at"])}</span></div><div class="acts">'
        + btn("/todo/reopen", t["id"], "Reopen", back) + btn("/todo/delete", t["id"], "Delete", back)
        + '</div></div>' for t in done_rows)
    done_html = (f'<details><summary>Recently done ({len(done_rows)})</summary>{done}</details>'
                 if done else "")
    return f"""<aside><h2>To-do <span class="n">({len(open_rows)})</span></h2>
<form class="addtodo" method="post" action="/todo/add"><input type="hidden" name="back" value="{e(back)}">
<input name="text" placeholder="Add a to-do" required maxlength="500"><button>Add</button></form>
{body}{done_html}</aside>"""


def todo_action(path, f, back):
    tid = int(f.get("id", "0") or 0)
    now = int(time.time())
    with db() as c:
        if path == "/todo/add":
            text = " ".join(f.get("text", "").split())[:500]
            if text:
                c.execute("INSERT INTO todos(text, created_at) VALUES (?,?)", (text, now))
        elif path == "/todo/done":
            c.execute("UPDATE todos SET done=1, done_at=? WHERE id=?", (now, tid))
        elif path == "/todo/reopen":
            c.execute("UPDATE todos SET done=0, done_at=NULL WHERE id=?", (tid,))
        elif path == "/todo/delete":
            c.execute("DELETE FROM todos WHERE id=?", (tid,))
        elif path == "/todo/to-idea":
            t = c.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()
            if t:
                inbox = c.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE", (INBOX,)).fetchone()
                if not inbox:
                    c.execute("INSERT INTO categories(name, description, created_at) VALUES (?,?,?)",
                              (INBOX, "Unsorted / unclear items", now))
                    inbox = c.execute("SELECT id FROM categories WHERE name=?", (INBOX,)).fetchone()
                cur = c.execute("INSERT INTO items(raw_text, title, category_id, created_at) VALUES (?,?,?,?)",
                                (t["text"], t["text"][:120], inbox["id"], now))
                c.execute("DELETE FROM todos WHERE id=?", (tid,))
                c.execute("INSERT INTO kind_corrections(text, from_kind, to_kind, created_at) VALUES (?,?,?,?)",
                          (t["text"][:300], "todo", "idea", now))
                return "/edit?" + urlencode({"id": cur.lastrowid, "back": back})
        elif path == "/to-todo":
            it = c.execute("SELECT * FROM items WHERE id=?", (tid,)).fetchone()
            if it:
                text = it["raw_text"]
                if it["url"] and it["url_title"] and it["url_title"] not in text:
                    text = f'{it["url_title"]} {text}'
                c.execute("INSERT INTO todos(text, created_at) VALUES (?,?)", (text[:500], now))
                c.execute("DELETE FROM items WHERE id=?", (tid,))
                c.execute("INSERT INTO kind_corrections(text, from_kind, to_kind, created_at) VALUES (?,?,?,?)",
                          (it["raw_text"][:300], "idea", "todo", now))
                drop_if_empty(c, it["category_id"])
                remove_thumb(it["thumb"])
    return back


def page_edit(item_id, back, err=""):
    with db() as c:
        r = c.execute("SELECT i.*, c.name AS category FROM items i JOIN categories c "
                      "ON c.id=i.category_id WHERE i.id=?", (item_id,)).fetchone()
        cats = c.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    if not r:
        return None
    opts = "".join(f'<option value="{x["id"]}"{" selected" if x["id"] == r["category_id"] else ""}>'
                   f'{e(x["name"])}</option>' for x in cats)
    pv = f'<img class="pv" src="/thumb/{e(r["thumb"])}" alt="">' if r["thumb"] else ""
    status = ""
    if r["url"] and not r["preview_checked"]:
        status = '<div class="hint">Fetching link preview…</div>'
    elif r["url"] and not r["thumb"]:
        status = '<div class="hint">No preview image available for this link.</div>'
    err_html = f'<p style="color:var(--bad)">{e(err)}</p>' if err else ""
    inner = f"""<div class="edit"><h2>Edit #{r["id"]}</h2>{err_html}
<form method="post" action="/edit">
<input type="hidden" name="id" value="{r["id"]}"><input type="hidden" name="back" value="{e(back)}">
<label for="title">Title</label><input id="title" name="title" value="{e(r["title"])}" required maxlength="200">
<label for="raw">Text</label><textarea id="raw" name="raw_text">{e(r["raw_text"])}</textarea>
<label for="url">Link</label><input id="url" name="url" value="{e(r["url"])}" placeholder="https://…">
<div class="hint">Changing the link fetches a new title and preview.</div>{pv}{status}
<label for="cat">Category</label><select id="cat" name="category_id">{opts}</select>
<label for="newcat">…or a new category</label><input id="newcat" name="new_category" maxlength="40" placeholder="Leave blank to use the one above">
<div class="row"><button>Save</button><a href="{e(back)}">Cancel</a></div>
</form>
<form class="del" method="post" action="/to-todo">
<input type="hidden" name="id" value="{r["id"]}"><div class="hint">This is really a task?</div>
<div class="row"><button>Move to to-do list</button></div></form>
<form class="del" method="post" action="/delete">
<input type="hidden" name="id" value="{r["id"]}"><input type="hidden" name="back" value="{e(back)}">
<label><input type="checkbox" name="confirm" value="1" style="width:auto" required> Yes, delete this item</label>
<div class="row"><button>Delete</button></div></form></div>"""
    return shell(f"Edit #{r['id']}", inner)


def drop_if_empty(c, cat_id):
    row = c.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
    if row and row["name"].lower() != INBOX.lower():
        if not c.execute("SELECT 1 FROM items WHERE category_id=? LIMIT 1", (cat_id,)).fetchone():
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))


def remove_thumb(name):
    if name and THUMB_RE.fullmatch(name):
        try:
            os.remove(os.path.join(THUMBS, name))
        except OSError:
            pass


def save_edit(f):
    item_id = int(f.get("id", "0") or 0)
    title = f.get("title", "").strip()[:200]
    raw = f.get("raw_text", "").strip()
    url = f.get("url", "").strip() or None
    if url and not re.match(r"https?://", url, re.I):
        return "Link must start with http:// or https://"
    if not title:
        return "Title can't be empty."
    with db() as c:
        it = c.execute("SELECT i.*, c.name AS category FROM items i JOIN categories c "
                       "ON c.id=i.category_id WHERE i.id=?", (item_id,)).fetchone()
        if not it:
            return "That item no longer exists."
        new_name = " ".join(f.get("new_category", "").split())[:40]
        created = 0
        if new_name:
            cat = c.execute("SELECT * FROM categories WHERE name=? COLLATE NOCASE", (new_name,)).fetchone()
            if not cat:
                c.execute("INSERT INTO categories(name, description, created_at) VALUES (?,?,?)",
                          (new_name, "", int(time.time())))
                cat = c.execute("SELECT * FROM categories WHERE name=? COLLATE NOCASE", (new_name,)).fetchone()
                created = 1
        else:
            cat = c.execute("SELECT * FROM categories WHERE id=?",
                            (int(f.get("category_id", "0") or 0),)).fetchone()
            if not cat:
                return "Pick a category."
        c.execute("UPDATE items SET title=?, raw_text=?, category_id=? WHERE id=?",
                  (title, raw or title, cat["id"], item_id))
        if url != it["url"]:
            remove_thumb(it["thumb"])
            c.execute("UPDATE items SET url=?, url_title=NULL, image_url=NULL, thumb=NULL, "
                      "preview_checked=0 WHERE id=?", (url, item_id))
        if cat["id"] != it["category_id"]:
            c.execute("INSERT INTO corrections(item_id,from_cat,to_cat,user_text,created_cat,created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (item_id, it["category"], cat["name"], "(edited on website)", created, int(time.time())))
            drop_if_empty(c, it["category_id"])
    return None


def delete_item(f):
    item_id = int(f.get("id", "0") or 0)
    if f.get("confirm") != "1":
        return
    with db() as c:
        it = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if it:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
            drop_if_empty(c, it["category_id"])
            remove_thumb(it["thumb"])


def safe_back(b):
    return b if b.startswith("/") and not b.startswith("//") and not b.startswith("/edit") else "/"


class H(BaseHTTPRequestHandler):
    def send(self, code, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "session" in c and valid_session(c["session"].value)

    def to_login(self):
        if self.command == "GET" and not self.path.startswith("/thumb/"):
            nxt = self.path
        else:
            nxt = "/"
        self.send(303, b"", headers={"Location": "/login?next=" + quote(nxt, safe="")})

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/login":
            return self.send(200, page_login(safe_next(qs.get("next", ["/"])[0])).encode())
        if not self.authed():
            return self.to_login()
        try:
            m = re.fullmatch(r"/thumb/(.+)", u.path)
            if m and THUMB_RE.fullmatch(m.group(1)):
                p = os.path.join(THUMBS, m.group(1))
                if not os.path.isfile(p):
                    return self.send(404, b"", "text/plain")
                with open(p, "rb") as fh:
                    return self.send(200, fh.read(), "image/jpeg",
                                     {"Cache-Control": "max-age=31536000, immutable"})
            if u.path == "/":
                return self.send(200, page_list(qs, self.path).encode())
            if u.path == "/todo":
                return self.send(303, b"", headers={"Location": "/"})
            if u.path == "/edit":
                iid = qs.get("id", [""])[0]
                out = page_edit(int(iid), safe_back(qs.get("back", ["/"])[0])) if iid.isdigit() else None
                return self.send(200, out.encode()) if out else self.send(404, b"Not found", "text/plain")
            self.send(404, b"Not found", "text/plain")
        except sqlite3.Error as ex:
            self.send(503, f"<p>Database not available: {e(ex)}</p>".encode())

    def do_POST(self):
        # same-origin check: a page on another site can't submit edits here
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send(403, b"Cross-origin form rejected", "text/plain")
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > 200_000:
            return self.send(413, b"Too large", "text/plain")
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"),
                                          keep_blank_values=True).items()}
        if self.path == "/login":
            return self.login(f)
        if self.path == "/logout":
            return self.send(303, b"", headers={
                "Location": "/login", "Set-Cookie": "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        if not self.authed():
            return self.to_login()
        back = safe_back(f.get("back", "/"))
        try:
            if self.path == "/edit":
                err = save_edit(f)
                if err:
                    out = page_edit(int(f.get("id", "0") or 0), back, err)
                    return self.send(400, (out or e(err)).encode())
            elif self.path == "/delete":
                delete_item(f)
            elif self.path.startswith("/todo/") or self.path == "/to-todo":
                back = todo_action(self.path, f, back)
            else:
                return self.send(404, b"Not found", "text/plain")
        except sqlite3.Error as ex:
            return self.send(503, f"<p>Database busy, try again: {e(ex)}</p>".encode())
        self.send(303, b"", headers={"Location": back})

    def login(self, f):
        ip = self.client_address[0]
        nxt = safe_next(f.get("next", "/"))
        if locked_out(ip):
            return self.send(429, page_login(nxt, "Too many attempts. Try again in 15 minutes.").encode())
        if check_password(f.get("password", "")):
            with _FAILS_LOCK:
                _FAILS.pop(ip, None)
            cookie = (f"session={make_session()}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                      "HttpOnly; SameSite=Lax")
            return self.send(303, b"", headers={"Location": nxt, "Set-Cookie": cookie})
        note_fail(ip)
        time.sleep(1)
        print(f"failed login from {ip}", flush=True)
        return self.send(401, page_login(nxt, "Wrong password.").encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["set-password"]:
        pw = sys.stdin.readline().rstrip("\n")
        if len(pw) < 8:
            sys.exit("Password must be at least 8 characters.")
        _write("password", hash_password(pw))
        _write("secret", secrets.token_hex(32))  # logs out every existing session
        print("Password set. All existing logins were signed out.")
        sys.exit(0)
    if not _read("password"):
        print("WARNING: no password set; run ./set-password.sh. Every login will fail.", flush=True)
    _secret()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

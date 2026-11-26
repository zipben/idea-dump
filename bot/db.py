"""SQLite storage for the idea dump. Part 2 (viewer) reads the same file."""
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    raw_text TEXT NOT NULL,
    url TEXT,
    url_title TEXT,
    title TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    created_at INTEGER NOT NULL,
    signal_ts INTEGER
);
CREATE INDEX IF NOT EXISTS items_cat ON items(category_id);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    from_cat TEXT NOT NULL,
    to_cat TEXT NOT NULL,
    user_text TEXT NOT NULL DEFAULT '',
    created_cat INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""

TODO_SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    done_at INTEGER,
    signal_ts INTEGER
);
CREATE TABLE IF NOT EXISTS kind_corrections (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    from_kind TEXT NOT NULL,
    to_kind TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

INBOX = "Inbox"


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.execute("PRAGMA busy_timeout=5000")
        self.c.executescript(SCHEMA)
        self.c.executescript(TODO_SCHEMA)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(items)")}
        for col, ddl in (("image_url", "TEXT"), ("thumb", "TEXT"),
                         ("preview_checked", "INTEGER NOT NULL DEFAULT 0")):
            if col not in cols:
                self.c.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
        self.ensure_category(INBOX, "Unsorted / unclear items")
        self.c.commit()

    # ---- categories ----
    def categories(self):
        return self.c.execute(
            "SELECT c.id, c.name, c.description, COUNT(i.id) AS n "
            "FROM categories c LEFT JOIN items i ON i.category_id = c.id "
            "GROUP BY c.id ORDER BY n DESC, c.name").fetchall()

    def category_by_name(self, name):
        return self.c.execute(
            "SELECT * FROM categories WHERE name = ? COLLATE NOCASE",
            (name.strip(),)).fetchone()

    def ensure_category(self, name, description=""):
        """Returns (row, created)."""
        row = self.category_by_name(name)
        if row:
            return row, False
        self.c.execute(
            "INSERT INTO categories(name, description, created_at) VALUES (?,?,?)",
            (name.strip(), description, int(time.time())))
        self.c.commit()
        return self.category_by_name(name), True

    def drop_if_empty(self, cat_id):
        row = self.c.execute("SELECT name FROM categories WHERE id=?", (cat_id,)).fetchone()
        if not row or row["name"].lower() == INBOX.lower():
            return
        n = self.c.execute("SELECT COUNT(*) FROM items WHERE category_id=?", (cat_id,)).fetchone()[0]
        if n == 0:
            self.c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            self.c.commit()

    def sample_titles(self, cat_id, k=3):
        return [r[0] for r in self.c.execute(
            "SELECT title FROM items WHERE category_id=? ORDER BY id DESC LIMIT ?",
            (cat_id, k))]

    # ---- items ----
    def add_item(self, raw_text, title, cat_id, url=None, url_title=None, signal_ts=None,
                 image_url=None):
        cur = self.c.execute(
            "INSERT INTO items(raw_text,url,url_title,title,category_id,created_at,signal_ts,"
            "image_url) VALUES (?,?,?,?,?,?,?,?)",
            (raw_text, url, url_title, title, cat_id, int(time.time()), signal_ts, image_url))
        self.c.commit()
        return cur.lastrowid

    def item(self, item_id):
        return self.c.execute(
            "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id=i.category_id "
            "WHERE i.id=?", (item_id,)).fetchone()

    def last_item(self):
        return self.c.execute(
            "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id=i.category_id "
            "ORDER BY i.id DESC LIMIT 1").fetchone()

    def items_in(self, cat_id, limit=10):
        return self.c.execute(
            "SELECT * FROM items WHERE category_id=? ORDER BY id DESC LIMIT ?",
            (cat_id, limit)).fetchall()

    def search(self, term, limit=10):
        like = f"%{term}%"
        return self.c.execute(
            "SELECT i.*, c.name AS category FROM items i JOIN categories c ON c.id=i.category_id "
            "WHERE i.raw_text LIKE ? OR i.title LIKE ? OR i.url_title LIKE ? "
            "ORDER BY i.id DESC LIMIT ?", (like, like, like, limit)).fetchall()

    def delete_item(self, item_id):
        it = self.item(item_id)
        if not it:
            return None
        self.c.execute("DELETE FROM items WHERE id=?", (item_id,))
        self.c.commit()
        self.drop_if_empty(it["category_id"])
        return it

    def move_item(self, item_id, cat_name, user_text="", description=""):
        """Move item; returns (item_before, new_cat_row, created) or None."""
        it = self.item(item_id)
        if not it:
            return None
        cat, created = self.ensure_category(cat_name, description)
        if cat["id"] == it["category_id"]:
            return it, cat, False
        self.c.execute("UPDATE items SET category_id=? WHERE id=?", (cat["id"], item_id))
        self.c.execute(
            "INSERT INTO corrections(item_id,from_cat,to_cat,user_text,created_cat,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, it["category"], cat["name"], user_text, int(created), int(time.time())))
        self.c.commit()
        self.drop_if_empty(it["category_id"])
        return it, cat, created

    def restore_item(self, row):
        """Re-insert a deleted item (row from item()) with its original id."""
        cat, _ = self.ensure_category(row["category"])
        self.c.execute(
            "INSERT INTO items(id,raw_text,url,url_title,title,category_id,created_at,signal_ts,"
            "image_url,thumb,preview_checked) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["raw_text"], row["url"], row["url_title"], row["title"], cat["id"],
             row["created_at"], row["signal_ts"], row["image_url"], None, 0))
        self.c.commit()

    def last_correction_for(self, item_id):
        return self.c.execute("SELECT * FROM corrections WHERE item_id=? ORDER BY id DESC LIMIT 1",
                              (item_id,)).fetchone()

    def delete_last_kind_correction(self):
        self.c.execute("DELETE FROM kind_corrections WHERE id=(SELECT MAX(id) FROM kind_corrections)")
        self.c.commit()

    # ---- to-dos ----
    def add_todo(self, text, signal_ts=None):
        cur = self.c.execute("INSERT INTO todos(text, created_at, signal_ts) VALUES (?,?,?)",
                             (text, int(time.time()), signal_ts))
        self.c.commit()
        return cur.lastrowid

    def todo(self, todo_id):
        return self.c.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()

    def last_todo(self):
        return self.c.execute("SELECT * FROM todos ORDER BY id DESC LIMIT 1").fetchone()

    def open_todos(self, limit=50):
        return self.c.execute("SELECT * FROM todos WHERE done=0 ORDER BY id LIMIT ?",
                              (limit,)).fetchall()

    def set_todo_done(self, todo_id, done=True):
        self.c.execute("UPDATE todos SET done=?, done_at=? WHERE id=?",
                       (int(done), int(time.time()) if done else None, todo_id))
        self.c.commit()

    def delete_todo(self, todo_id):
        t = self.todo(todo_id)
        if t:
            self.c.execute("DELETE FROM todos WHERE id=?", (todo_id,))
            self.c.commit()
        return t

    def restore_todo(self, row):
        self.c.execute("INSERT INTO todos(id, text, done, created_at, done_at, signal_ts) "
                       "VALUES (?,?,?,?,?,?)", tuple(row[k] for k in
                       ("id", "text", "done", "created_at", "done_at", "signal_ts")))
        self.c.commit()

    def add_kind_correction(self, text, from_kind, to_kind):
        self.c.execute("INSERT INTO kind_corrections(text, from_kind, to_kind, created_at) "
                       "VALUES (?,?,?,?)", (text[:300], from_kind, to_kind, int(time.time())))
        self.c.commit()

    def recent_kind_corrections(self, k=8):
        return self.c.execute("SELECT * FROM kind_corrections ORDER BY id DESC LIMIT ?",
                              (k,)).fetchall()

    def pending_previews(self, limit=5):
        return self.c.execute(
            "SELECT * FROM items WHERE url IS NOT NULL AND preview_checked=0 "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def set_preview(self, item_id, url_title, image_url, thumb):
        self.c.execute(
            "UPDATE items SET url_title=COALESCE(?, url_title), image_url=?, thumb=?, "
            "preview_checked=1 WHERE id=?", (url_title, image_url, thumb, item_id))
        self.c.commit()

    def recent_corrections(self, k=10):
        return self.c.execute(
            "SELECT co.*, i.title FROM corrections co LEFT JOIN items i ON i.id=co.item_id "
            "ORDER BY co.id DESC LIMIT ?", (k,)).fetchall()

    def last_action(self):
        """Most recent of: item creation or correction. Returns ('add', item) / ('move', corr)."""
        it = self.last_item()
        co = self.c.execute("SELECT * FROM corrections ORDER BY id DESC LIMIT 1").fetchone()
        if co and (not it or co["created_at"] >= it["created_at"]):
            return "move", co
        if it:
            return "add", it
        return None, None

    def undo_correction(self, co):
        it = self.item(co["item_id"])
        self.c.execute("DELETE FROM corrections WHERE id=?", (co["id"],))
        self.c.commit()
        if not it:
            return None
        cat, _ = self.ensure_category(co["from_cat"])
        self.c.execute("UPDATE items SET category_id=? WHERE id=?", (cat["id"], it["id"]))
        self.c.commit()
        self.drop_if_empty(it["category_id"])
        return it, cat

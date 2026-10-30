"""History-based recommendations for CrateMatch.

Third recommendation layer: learn from Keith's past DJ sets.

For the currently playing track X:
  1. Parse ALL past .session files in ~/Music/_Serato_/History/Sessions/
     (reuses parse_session from serato_watcher — no duplicated parser)
  2. Find every past occurrence of X (matched by normalized artist+title,
     falling back to file path)
  3. Collect the 5 tracks before and 5 after each occurrence
  4. Weight by proximity: +-1 = 1.0, +-2 = 0.8, +-3 = 0.6, +-4 = 0.4, +-5 = 0.2
  5. Aggregate into a per-track history score, normalized 0..1 per seed

Also provides "don't repeat tonight": the set of tracks already played in
the current (newest) session, so the live window can exclude them.

Caching: edges are stored in SQLite (same DB file as the library) and only
new/changed session files are re-parsed. Fast even with years of sets.
"""
import os
import sqlite3

from serato_watcher import (
    default_sessions_dir,
    newest_session_file,
    parse_session,
)

# proximity weights for +-1 .. +-5
WINDOW = 5
PROXIMITY = {1: 1.0, 2: 0.8, 3: 0.6, 4: 0.4, 5: 0.2}

SCHEMA_SESSIONS = """
CREATE TABLE IF NOT EXISTS history_sessions (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER
)"""

SCHEMA_EDGES = """
CREATE TABLE IF NOT EXISTS history_edges (
    session_path TEXT,
    from_key TEXT,
    to_key TEXT,
    weight REAL
)"""

SCHEMA_EDGE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_history_edges_from
ON history_edges (from_key)"""


def track_key(artist, title, path=None):
    """Canonical track identity: normalized artist|title, path fallback."""
    a = (artist or "").strip().lower()
    t = (title or "").strip().lower()
    if a or t:
        return f"{a}|{t}"
    return f"path:{path or '?'}"


def _ordered_keys(entries):
    """Session entries -> ordered, consecutive-deduped list of track keys.

    Serato rewrites entries out of order, so start_time is the clock
    (entry_index breaks ties). Consecutive duplicates (same play event
    recorded twice, e.g. deck load + play) collapse to one.
    """
    def sort_k(e):
        st = e.get("start_time")
        return (st is None, st or 0, e.get("entry_index") or 0)

    ordered = sorted(entries, key=sort_k)
    keys = []
    for e in ordered:
        k = track_key(e.get("artist"), e.get("title"), e.get("path"))
        if not keys or keys[-1] != k:
            keys.append(k)
    return keys


class HistoryGraph:
    """Parses Serato sessions into a proximity-weighted transition graph,
    cached in SQLite."""

    def __init__(self, db_path, sessions_dir=None):
        self.db_path = db_path
        self.sessions_dir = sessions_dir or default_sessions_dir()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: the graph is built on a background
        # thread but queried from the UI thread — with the default, the
        # first cross-thread query raises ProgrammingError.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(SCHEMA_SESSIONS)
        self.conn.execute(SCHEMA_EDGES)
        self.conn.execute(SCHEMA_EDGE_INDEX)
        self.conn.commit()

    # ---------- incremental sync ----------
    def _session_files(self):
        try:
            files = [f for f in os.listdir(self.sessions_dir)
                     if f.endswith(".session")]
        except OSError:
            return []
        return [os.path.join(self.sessions_dir, f) for f in files]

    def sync(self):
        """Parse new/changed session files only. Returns (# parsed, # total)."""
        files = self._session_files()
        cur = self.conn.execute("SELECT path, mtime, size FROM history_sessions")
        known = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        parsed = 0
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = known.get(path)
            if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
                continue  # unchanged
            self._parse_file(path, st.st_mtime, st.st_size)
            parsed += 1
        # drop sessions that no longer exist on disk
        on_disk = set(files)
        for old_path in known:
            if old_path not in on_disk:
                self.conn.execute(
                    "DELETE FROM history_edges WHERE session_path = ?",
                    (old_path,))
                self.conn.execute(
                    "DELETE FROM history_sessions WHERE path = ?",
                    (old_path,))
        self.conn.commit()
        return parsed, len(files)

    def _parse_file(self, path, mtime, size):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return
        entries = parse_session(data)
        keys = _ordered_keys(entries)

        # rebuild this file's edges from scratch
        self.conn.execute(
            "DELETE FROM history_edges WHERE session_path = ?", (path,))
        rows = []
        n = len(keys)
        for i, fk in enumerate(keys):
            for d in range(1, WINDOW + 1):
                w = PROXIMITY[d]
                if i - d >= 0 and keys[i - d] != fk:
                    rows.append((path, fk, keys[i - d], w))
                if i + d < n and keys[i + d] != fk:
                    rows.append((path, fk, keys[i + d], w))
        if rows:
            self.conn.executemany(
                "INSERT INTO history_edges "
                "(session_path, from_key, to_key, weight) VALUES (?,?,?,?)",
                rows)
        self.conn.execute(
            "INSERT OR REPLACE INTO history_sessions (path, mtime, size) "
            "VALUES (?,?,?)", (path, mtime, size))
        self.conn.commit()

    # ---------- queries ----------
    def history_scores(self, seed_key):
        """{track_key: 0..1} — proximity-weighted co-occurrence with seed.

        Normalized per seed so the strongest neighbor scores 1.0.
        Empty dict when the seed has no history (caller falls back to
        audio+pocket).
        """
        cur = self.conn.execute(
            "SELECT to_key, SUM(weight) FROM history_edges "
            "WHERE from_key = ? GROUP BY to_key",
            (seed_key,))
        rows = cur.fetchall()
        if not rows:
            return {}
        mx = max(w for _, w in rows)
        if mx <= 0:
            return {}
        return {k: w / mx for k, w in rows}

    def tonight_keys(self, session_path=None):
        """Set of track keys already played in the current session.

        Used for "don't repeat tonight". Defaults to the newest session file.
        """
        sess = session_path or newest_session_file(self.sessions_dir)
        if not sess:
            return set()
        try:
            with open(sess, "rb") as f:
                data = f.read()
        except OSError:
            return set()
        return set(_ordered_keys(parse_session(data)))

    def stats(self):
        """(session_count, edge_count) — for diagnostics."""
        s = self.conn.execute(
            "SELECT COUNT(*) FROM history_sessions").fetchone()[0]
        e = self.conn.execute(
            "SELECT COUNT(*) FROM history_edges").fetchone()[0]
        return s, e

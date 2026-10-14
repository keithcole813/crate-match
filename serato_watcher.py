"""Watch Serato DJ's live history to detect the currently playing track.

Serato writes every loaded track to
    ~/Music/_Serato_/History/Sessions/<n>.session
as it plays (~1-2s lag).

Session format (verified against dj-banger's reverse engineering):
  Top level: ASCII-tag TLV  [4-byte tag][u32 BE len][payload]
    - `vrsn` chunk, then repeated `oent` (one played entry each)
    - each `oent` holds a single `adat` blob
  `adat` payload uses a DIFFERENT inner encoding: numeric field IDs
    [u32 field-id][u32 length][payload], UTF-16BE strings:
    - 2  = absolute file path
    - 6  = title, 7 = artist, 8 = album, 9 = genre
    - 28 = play start time (unix s), 29 = play end time (absent = still playing)
    - 31 = deck number (1-4)

"Now playing" = entry with no end time, else the entry with the latest
start time. (Serato does NOT keep entries in play order — it rewrites
older entries in place, so start time is the reliable clock.)
"""
import os
import struct
import threading
import time

# adat numeric field ids
F_PATH = 2
F_TITLE = 6
F_ARTIST = 7
F_ALBUM = 8
F_GENRE = 9
F_START = 28
F_END = 29
F_DECK = 31


def default_sessions_dir():
    return os.path.expanduser("~/Music/_Serato_/History/Sessions")


def _u16be(b):
    try:
        s = b.decode("utf-16-be")
    except Exception:
        return None
    s = s.rstrip("\x00")
    return s if s else None


def parse_adat(buf):
    """Decode adat numeric-TLV payload -> {field_id: bytes}."""
    fields = {}
    off = 0
    n = len(buf)
    while off + 8 <= n:
        fid = struct.unpack(">I", buf[off:off + 4])[0]
        ln = struct.unpack(">I", buf[off + 4:off + 8])[0]
        start, end = off + 8, off + 8 + ln
        if end > n:
            break
        fields[fid] = buf[start:end]
        off = end
    return fields


def parse_session(data):
    """Parse a .session buffer -> list of entry dicts (file order)."""
    entries = []
    off, n = 0, len(data)
    idx = 0
    while off + 8 <= n:
        tag = data[off:off + 4]
        try:
            tag_s = tag.decode("ascii")
        except Exception:
            break
        ln = struct.unpack(">I", data[off + 4:off + 8])[0]
        start, end = off + 8, off + 8 + ln
        if end > n:
            break
        if tag_s == "oent":
            # scan children for adat
            co = start
            while co + 8 <= end:
                ctag = data[co:co + 4]
                try:
                    ctag_s = ctag.decode("ascii")
                except Exception:
                    break
                clen = struct.unpack(">I", data[co + 4:co + 8])[0]
                cstart, cend = co + 8, co + 8 + clen
                if cend > end:
                    break
                if ctag_s == "adat":
                    fields = parse_adat(data[cstart:cend])
                    path = _u16be(fields[F_PATH]) if F_PATH in fields else None
                    if path:
                        entry = {
                            "path": path if path.startswith("/") else "/" + path,
                            "title": _u16be(fields[F_TITLE]) if F_TITLE in fields else None,
                            "artist": _u16be(fields[F_ARTIST]) if F_ARTIST in fields else None,
                            "album": _u16be(fields[F_ALBUM]) if F_ALBUM in fields else None,
                            "genre": _u16be(fields[F_GENRE]) if F_GENRE in fields else None,
                            "start_time": None,
                            "end_time": None,
                            "deck": None,
                            "entry_index": idx,
                        }
                        if F_START in fields and len(fields[F_START]) >= 4:
                            entry["start_time"] = struct.unpack(
                                ">I", fields[F_START][:4])[0]
                        if F_END in fields and len(fields[F_END]) >= 4:
                            entry["end_time"] = struct.unpack(
                                ">I", fields[F_END][:4])[0]
                        if F_DECK in fields and len(fields[F_DECK]) >= 1:
                            b = fields[F_DECK]
                            entry["deck"] = struct.unpack(">I", b[:4])[0] if len(b) >= 4 else b[0]
                        entries.append(entry)
                co = cend
            idx += 1
        off = end
    return entries


def _is_more_current(a, b):
    """True if entry a is newer/more-current than b."""
    a_playing = a["end_time"] is None
    b_playing = b["end_time"] is None
    if a_playing != b_playing:
        return a_playing
    sa, sb = a["start_time"] or 0, b["start_time"] or 0
    if sa != sb:
        return sa > sb
    return a["entry_index"] > b["entry_index"]


def newest_session_file(sessions_dir=None):
    """Return path of most-recently-modified .session file, or None."""
    d = sessions_dir or default_sessions_dir()
    try:
        files = [f for f in os.listdir(d) if f.endswith(".session")]
    except OSError:
        return None
    best, best_mt = None, -1
    for f in files:
        p = os.path.join(d, f)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if mt > best_mt:
            best, best_mt = p, mt
    return best


def get_now_playing(sessions_dir=None):
    """Return the current track dict {artist,title,path,deck,...} or None."""
    sess = newest_session_file(sessions_dir)
    if not sess:
        return None
    try:
        with open(sess, "rb") as f:
            data = f.read()
    except OSError:
        return None
    entries = parse_session(data)
    if not entries:
        return None
    # check the newest few sessions in case the newest file is empty
    # (Serato creates a fresh session on launch with no plays yet)
    if len(entries) < 1:
        return None
    cur = entries[0]
    for e in entries[1:]:
        if _is_more_current(e, cur):
            cur = e
    cur = dict(cur)
    cur["session_file"] = sess
    return cur


class SeratoWatcher(threading.Thread):
    """Background thread polling Serato history every `interval` seconds.

    Calls `on_change(track_dict)` whenever the now-playing track changes.
    track_dict has {artist, title, path, deck, ...} or None.
    """

    def __init__(self, on_change, interval=2.0, sessions_dir=None):
        super().__init__(daemon=True)
        self.on_change = on_change
        self.interval = interval
        self.sessions_dir = sessions_dir
        self._stop = threading.Event()
        self._last_key = None
        self.current = None

    def _key(self, t):
        if not t:
            return None
        return (t.get("path"), t.get("start_time"))

    def run(self):
        while not self._stop.is_set():
            try:
                t = get_now_playing(self.sessions_dir)
                k = self._key(t)
                if k != self._last_key:
                    self._last_key = k
                    self.current = t
                    try:
                        self.on_change(t)
                    except Exception:
                        pass
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

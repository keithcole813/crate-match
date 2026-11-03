"""First-scan metadata bake-in (v1.6.0).

Keith's rule: the database numbers (Energy %, Danceability %, and the
Discogs genre/style data behind the best-version pocket) are fetched
ONCE — in the background, during/after the first scan — and stored in
the local SQLite library, so DJing afterwards works fully offline.
No live calls at DJ time except topping up a brand-new seed.

Pipeline per track (throttled, resumable, missing-rows-only):
  1. TheAudioDB searchtrack (artist+title) — one call returns
     intEnergy / intDanceability / Spotify ID / ISRC / genre / style.
     Free shared test key allows 30 req/min; we stay under it. A
     personal premium key (settings) raises the limit, so we can go
     a little faster with one.
  2. ReccoBeats audio-features — free, NO key, and BATCHED: many
     Spotify IDs per call (verified). Its numbers are Spotify's own,
     so when both sources answer, ReccoBeats wins.
  3. Discogs (only when the user saved a personal token, and only
     for tracks whose genre data is thin) — Genre + Style lists for
     the best-version pocket. 60 req/min authenticated; we use ~50.

Every attempted track is stamped (meta_tried) so a song no database
knows isn't re-queried on every launch; misses retry after 30 days.
"""
import re
import time
import urllib.parse

from netutil import get_json

UA = "CrateMatchLive/1.6 ( https://github.com/keithcole813/crate-match )"
ADB_BASE = "https://www.theaudiodb.com/api/v1/json"
RECCO = "https://api.reccobeats.com/v1/audio-features"
DISCOGS = "https://api.discogs.com/database/search"
RETRY_AFTER = 30 * 24 * 3600  # seconds before a miss is retried


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _pct(v):
    """TheAudioDB ints are 0..100 (sometimes strings); ReccoBeats
    floats are 0..1. Normalize either to 0..1 float or None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        f = f / 100.0
    return f if 0.0 <= f <= 1.0 else None


def _artist_variants(artist):
    """File tags carry full credits ('Cardi B, Bad Bunny & J Balvin')
    that the search endpoint can't match — fall back to the first
    billed artist, then the first name before feat./&/x."""
    out = []
    a = (artist or "").strip()
    if a:
        out.append(a)
    for sep in (",", " feat", " ft.", " & ", " x ", " with "):
        if sep in a.lower():
            first = re.split(re.escape(sep), a, flags=re.I)[0].strip()
            if first and first not in out:
                out.append(first)
            break
    return out


def _title_variants(title):
    t = (title or "").strip()
    out = [t] if t else []
    clean = re.sub(r"\s*[\(\[][^\)\]]*(feat|ft\.|remix|edit|version)[^\)\]]*[\)\]]",
                   "", t, flags=re.I).strip()
    if clean and clean not in out:
        out.append(clean)
    return out


def adb_resolve(artist, title, key="123", timeout=15):
    """TheAudioDB lookup -> dict or None.

    Keys: energy, dance (0..1), spotify_id, isrc, genre, style.
    The returned entry must plausibly BE the requested song — a
    fuzzy hit for a different track would bake wrong numbers in.
    """
    if not artist or not title:
        return None
    for a in _artist_variants(artist):
        for t in _title_variants(title):
            got = _adb_resolve_once(a, t, a, t, key, timeout)
            if got:
                return got
    return None


def _adb_resolve_once(artist, title, want_artist, want_title,
                      key, timeout):
    url = (f"{ADB_BASE}/{urllib.parse.quote(str(key))}/searchtrack.php"
           f"?s={urllib.parse.quote(artist)}"
           f"&t={urllib.parse.quote(title)}")
    try:
        data = get_json(url, UA, timeout=timeout)
    except Exception:
        return None
    entries = (data or {}).get("track") or []
    want_t, want_a = _norm(want_title), _norm(want_artist)
    best = None
    for e in entries:
        et, ea = _norm(e.get("strTrack")), _norm(e.get("strArtist"))
        if not et:
            continue
        title_ok = want_t and (want_t in et or et in want_t)
        artist_ok = want_a and (want_a in ea or ea in want_a)
        if title_ok and artist_ok:
            best = e
            break
        if title_ok and best is None:
            best = e
    if best is None:
        return None
    return {
        "energy": _pct(best.get("intEnergy")),
        "dance": _pct(best.get("intDanceability")),
        "spotify_id": (best.get("strSpotifyID") or "").strip() or None,
        "isrc": (best.get("strISRC") or "").strip() or None,
        "genre": (best.get("strGenre") or "").strip(),
        "style": (best.get("strStyle") or "").strip(),
    }


def recco_batch(spotify_ids, timeout=25):
    """{spotify_id: (energy, dance)} from ReccoBeats — batched, free,
    no key. Chunks of 50 IDs per call."""
    out = {}
    ids = [i for i in dict.fromkeys(spotify_ids) if i]
    for j in range(0, len(ids), 50):
        chunk = ids[j:j + 50]
        url = RECCO + "?ids=" + ",".join(chunk)
        try:
            data = get_json(url, UA, timeout=timeout)
        except Exception:
            continue
        for item in (data or {}).get("content") or []:
            href = item.get("href") or ""
            sid = href.rstrip("/").rsplit("/", 1)[-1] if href else None
            if not sid:
                continue
            e, d = _pct(item.get("energy")), _pct(item.get("danceability"))
            if e is not None or d is not None:
                out[sid] = (e, d)
        if j + 50 < len(ids):
            time.sleep(0.4)
    return out


def discogs_pocket(artist, title, token, timeout=15):
    """(genres, styles) ';'-joined from the best-matching Discogs
    master/release, or (None, None). Discogs data is per RELEASE, so
    this is approximate for a single track — used only to enrich
    pockets whose genre data is thin."""
    if not token or not artist:
        return None, None
    q = urllib.parse.quote(f"{artist} {title}")
    url = (f"{DISCOGS}?q={q}&type=master"
           f"&token={urllib.parse.quote(str(token))}")
    try:
        data = get_json(url, UA, timeout=timeout)
    except Exception:
        return None, None
    results = (data or {}).get("results") or []
    want_a = _norm(artist)
    for r in results[:5]:
        if want_a and want_a not in _norm(r.get("title")):
            continue
        genres = [g.strip().lower() for g in (r.get("genre") or [])
                  if g and g.strip()]
        styles = [s.strip() for s in (r.get("style") or [])
                  if s and s.strip()]
        if genres or styles:
            return (";".join(genres) or None,
                    ";".join(styles) or None)
    return None, None


def _thin_genres(track):
    """True when the track's genre data is too thin to name a real
    pocket (the year-only '2020s' cases) — only those cost a
    Discogs call."""
    words = set()
    for src in (track.get("genres") or "", track.get("db_genres") or ""):
        for g in src.split(";"):
            words.update(g.strip().lower().split())
    return len(words) < 3


def _needs_bake(track, now):
    if track.get("db_energy") is not None:
        return False
    tried = track.get("meta_tried")
    return not tried or (now - tried) > RETRY_AFTER


def bake_library(lib, audiodb_key="123", discogs_token="",
                 progress_cb=None, should_stop=None, limit=None):
    """Bake database numbers into every library row that lacks them.

    Returns (filled, attempted). Safe to call again — baked rows are
    skipped, so later scans only top up new/missing tracks.
    """
    key = (audiodb_key or "123").strip() or "123"
    # shared test keys: stay under the 30/min free limit; a personal
    # premium key allows more, so its owners bake faster
    adb_interval = 2.3 if key in ("123", "2") else 1.1
    now = time.time()
    rows = [t for t in lib.all_tracks() if _needs_bake(t, now)]
    if limit:
        rows = rows[:limit]
    total = len(rows)
    filled = 0
    pending = []  # (path, spotify_id) awaiting the ReccoBeats batch
    last_adb = [0.0]
    last_dg = [0.0]

    def throttle(last, interval):
        wait = interval - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()

    def flush_recco():
        nonlocal filled
        if not pending:
            return
        feats = recco_batch([sid for _p, sid in pending])
        for path, sid in pending:
            if sid in feats:
                e, d = feats[sid]
                lib.set_db_features(path, energy=e, dance=d)
                filled += 1
        pending.clear()

    for i, t in enumerate(rows):
        if should_stop and should_stop():
            break
        if progress_cb:
            progress_cb(i + 1, total,
                        f"{t.get('artist') or ''} — {t.get('title') or ''}")
        path = t["path"]
        throttle(last_adb, adb_interval)
        got = adb_resolve(t.get("artist"), t.get("title"), key=key)
        if got:
            lib.set_db_features(
                path, energy=got["energy"], dance=got["dance"],
                spotify_id=got["spotify_id"], isrc=got["isrc"])
            extra = ";".join(x for x in
                             (got["genre"].lower(), got["style"].lower())
                             if x)
            if extra and _thin_genres(t):
                lib.set_pocket_extra(path, db_genres=extra)
            if got["energy"] is not None or got["dance"] is not None:
                filled += 1
            if got["spotify_id"]:
                pending.append((path, got["spotify_id"]))
                if len(pending) >= 40:
                    flush_recco()
        if discogs_token and _thin_genres(t):
            throttle(last_dg, 1.2)
            genres, styles = discogs_pocket(
                t.get("artist"), t.get("title"), discogs_token)
            if genres or styles:
                lib.set_pocket_extra(path, db_genres=genres,
                                     pocket_styles=styles)
        lib.mark_meta_tried(path)
    flush_recco()
    return filled, total


def bake_one(lib, track, audiodb_key="123", discogs_token=""):
    """Bake a single track NOW (the now-playing seed — DJ time can't
    wait for the background pass). Returns True if numbers landed."""
    if not track or not track.get("path"):
        return False
    if track.get("db_energy") is not None:
        return True
    got = adb_resolve(track.get("artist"), track.get("title"),
                      key=(audiodb_key or "123"))
    path = track["path"]
    if got:
        lib.set_db_features(
            path, energy=got["energy"], dance=got["dance"],
            spotify_id=got["spotify_id"], isrc=got["isrc"])
        if got["spotify_id"]:
            feats = recco_batch([got["spotify_id"]])
            if got["spotify_id"] in feats:
                e, d = feats[got["spotify_id"]]
                lib.set_db_features(path, energy=e, dance=d)
        extra = ";".join(x for x in
                         (got["genre"].lower(), got["style"].lower())
                         if x)
        if extra and _thin_genres(track):
            lib.set_pocket_extra(path, db_genres=extra)
    if discogs_token and _thin_genres(track):
        genres, styles = discogs_pocket(
            track.get("artist"), track.get("title"), discogs_token)
        if genres or styles:
            lib.set_pocket_extra(path, db_genres=genres,
                                 pocket_styles=styles)
    lib.mark_meta_tried(path)
    row = lib.get(path)
    return bool(row and row.get("db_energy") is not None)

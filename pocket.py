"""Pocket classification + two-layer recommendation scoring.

Layer 1 (audio): the existing matcher — BPM, Camelot key, energy, spectral.
Layer 2 (pocket): genre / era / vibe context — "you're in a classic-rock
pocket, stay in the pocket."

Combined:  score = 0.6 * audio_similarity + 0.4 * pocket_match

Also: key-format conversions (Camelot <-> Classic <-> Open Key) for the
live window's key display switcher.
"""
import re

from matcher import key_compat, similarity

# ---------- key format conversions ----------
# Reverse of analyzer.CAMELOT: camelot code -> (root, mode)
_CAMELOT_TO_KEY = {
    "8B": ("C", "major"), "9B": ("G", "major"), "10B": ("D", "major"),
    "11B": ("A", "major"), "12B": ("E", "major"), "1B": ("B", "major"),
    "2B": ("F#", "major"), "3B": ("Db", "major"), "4B": ("Ab", "major"),
    "5B": ("Eb", "major"), "6B": ("Bb", "major"), "7B": ("F", "major"),
    "8A": ("A", "minor"), "9A": ("E", "minor"), "10A": ("B", "minor"),
    "11A": ("F#", "minor"), "12A": ("C#", "minor"), "1A": ("G#", "minor"),
    "2A": ("Eb", "minor"), "3A": ("Bb", "minor"), "4A": ("F", "minor"),
    "5A": ("C", "minor"), "6A": ("G", "minor"), "7A": ("D", "minor"),
}


def camelot_to_classic(camelot):
    """'8A' -> 'Am', '8B' -> 'C'. Returns '?' when unknown."""
    info = _CAMELOT_TO_KEY.get((camelot or "").strip().upper())
    if not info:
        return "?"
    root, mode = info
    return root + ("m" if mode == "minor" else "")


def camelot_to_openkey(camelot):
    """'8A' -> '1m', '8B' -> '1d'. Returns '?' when unknown."""
    c = (camelot or "").strip().upper()
    m = re.fullmatch(r"(\d{1,2})([AB])", c)
    if not m:
        return "?"
    num = m.group(1)
    # Open Key numbers run opposite to Camelot: Camelot 8A == OpenKey 1m
    open_num = ((int(num) - 8) % 12) + 1
    suffix = "m" if m.group(2) == "A" else "d"
    return f"{open_num}{suffix}"


def format_key(camelot, style="camelot"):
    """style in {'camelot','classic','openkey'}."""
    if style == "classic":
        return camelot_to_classic(camelot)
    if style == "openkey":
        return camelot_to_openkey(camelot)
    return camelot or "?"


# ---------- pocket classification ----------
def _parse_genres(track):
    g = track.get("genres") or ""
    if isinstance(g, str):
        return [x.strip().lower() for x in g.split(";") if x.strip()]
    return []


def _era_of(track):
    """Bucket a year into an era label."""
    y = track.get("year")
    try:
        y = int(y)
    except (TypeError, ValueError):
        return None
    if y < 1960:
        return "pre-60s"
    if y < 1970:
        return "60s"
    if y < 1980:
        return "70s"
    if y < 1990:
        return "80s"
    if y < 2000:
        return "90s"
    if y < 2010:
        return "2000s"
    if y < 2020:
        return "2010s"
    return "2020s"


_ERA_ORDER = ["pre-60s", "60s", "70s", "80s", "90s", "2000s", "2010s", "2020s"]


def classify_pocket(track):
    """Return {genres, primary_genre, era, label} for a track dict."""
    genres = _parse_genres(track)
    era = _era_of(track)
    primary = genres[0] if genres else None
    parts = []
    if primary:
        parts.append(primary.title())
    if era:
        parts.append(era)
    label = " / ".join(parts) if parts else "Unknown pocket"
    return {
        "genres": genres,
        "primary_genre": primary,
        "era": era,
        "label": label,
    }


def pocket_match(seed_pocket, cand_pocket):
    """0..1 — how well the candidate sits in the seed's pocket."""
    sg = set(seed_pocket.get("genres") or [])
    cg = set(cand_pocket.get("genres") or [])
    if sg and cg:
        inter = sg & cg
        union = sg | cg
        genre_s = len(inter) / len(union)
        # bonus when primary genres agree
        if (seed_pocket.get("primary_genre") and
                seed_pocket["primary_genre"] == cand_pocket.get("primary_genre")):
            genre_s = min(1.0, genre_s + 0.25)
    elif not sg and not cg:
        genre_s = 0.5  # neither tagged — neutral
    else:
        genre_s = 0.2  # one side untagged — weak

    se, ce = seed_pocket.get("era"), cand_pocket.get("era")
    if se and ce and se in _ERA_ORDER and ce in _ERA_ORDER:
        d = abs(_ERA_ORDER.index(se) - _ERA_ORDER.index(ce))
        era_s = 1.0 if d == 0 else (0.6 if d == 1 else 0.3)
    else:
        era_s = 0.5

    return 0.7 * genre_s + 0.3 * era_s


def pocket_score(seed, cand, w_audio=0.6, w_pocket=0.4):
    """Two-layer score. Returns (score, audio_s, pocket_s)."""
    audio_s = similarity(seed, cand)
    sp = classify_pocket(seed)
    cp = classify_pocket(cand)
    pocket_s = pocket_match(sp, cp)
    return w_audio * audio_s + w_pocket * pocket_s, audio_s, pocket_s


def find_pocket_matches(seed, tracks, top_n=20, w_audio=0.6, w_pocket=0.4):
    """Rank tracks by two-layer score. Returns [(score, track, pocket_label)]."""
    seed_pocket = classify_pocket(seed)
    out = []
    for t in tracks:
        if t.get("path") == seed.get("path"):
            continue
        score, _, _ = pocket_score(seed, t, w_audio, w_pocket)
        out.append((score, t, classify_pocket(t)["label"]))
    out.sort(key=lambda x: x[0], reverse=True)
    return out[:top_n]


def pocket_counts(tracks):
    """{pocket_label: count} — used to find thin pockets."""
    counts = {}
    for t in tracks:
        label = classify_pocket(t)["label"]
        counts[label] = counts.get(label, 0) + 1
    return counts

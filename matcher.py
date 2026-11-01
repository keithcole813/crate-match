"""Similarity scoring for CrateMatch.

Score = weighted blend of BPM proximity, Camelot key compatibility,
energy proximity, and spectral character. All components 0..1.
"""


def key_compat(c1, c2):
    """Camelot wheel compatibility: 1.0 = same key, harmonic neighbors high."""
    if not c1 or not c2 or c1 == "?" or c2 == "?":
        return 0.4
    if c1 == c2:
        return 1.0
    try:
        n1, l1 = int(c1[:-1]), c1[-1]
        n2, l2 = int(c2[:-1]), c2[-1]
    except ValueError:
        return 0.3
    if n1 == n2:
        return 0.85  # relative major/minor
    d = abs(n1 - n2)
    d = min(d, 12 - d)
    if d == 1 and l1 == l2:
        return 0.75  # perfect fifth / fourth
    if d == 1:
        return 0.5   # diagonal move
    if d == 2 and l1 == l2:
        return 0.35
    return 0.15


def _bpm_score(b1, b2):
    """BPM proximity with tempo folding: slow songs are routinely
    detected at double tempo (68 read as 136). A folded match is
    possible to mix but is NOT the same pocket (Keith, v1.4.1 test:
    Maxwell's double-timed ballad 'Fortunate' ranked beside uptempo
    Destiny's Child) — so folding carries a real penalty (x0.70) and
    a true-tempo match always wins."""
    if not b1 or not b2:
        return 0.4
    d = abs(b1 - b2)
    folded = min(abs(b1 - 2 * b2), abs(2 * b1 - b2))
    if folded < d:
        return max(0.0, 1.0 - folded / 20.0) * 0.70
    return max(0.0, 1.0 - d / 20.0)


def similarity(seed, cand,
               w_bpm=0.35, w_key=0.35, w_energy=0.15, w_spectral=0.15):
    if seed["path"] == cand["path"]:
        return 0.0
    bpm_s = _bpm_score(seed.get("bpm"), cand.get("bpm"))
    key_s = key_compat(seed.get("camelot"), cand.get("camelot"))
    e1, e2 = seed.get("energy") or 0, cand.get("energy") or 0
    energy_s = max(0.0, 1.0 - abs(e1 - e2) / (max(e1, e2) + 1e-9))
    c1, c2 = seed.get("centroid") or 0, cand.get("centroid") or 0
    z1, z2 = seed.get("zcr") or 0, cand.get("zcr") or 0
    spec_d = abs(c1 - c2) / 4000.0 + abs(z1 - z2) * 8.0
    spectral_s = max(0.0, 1.0 - min(spec_d, 1.0))
    return (w_bpm * bpm_s + w_key * key_s +
            w_energy * energy_s + w_spectral * spectral_s)


def find_similar(seed, tracks, top_n=20):
    scored = [(similarity(seed, t), t) for t in tracks]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:top_n]

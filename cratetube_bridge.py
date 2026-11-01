"""Bridge: when the current pocket is thin in Keith's library (<5 tracks),
suggest well-known tracks in that pocket he doesn't own.

Output is JSON CrateTube can consume for YouTube download:
    [{"artist": ..., "title": ..., "pocket": ..., "reason": ...}]

The knowledge base is curated iconic tracks per pocket (genre + era).
Matching is by pocket label from pocket.classify_pocket().
"""
import argparse
import json
import os
import re

from pocket import classify_pocket

# pocket keyword -> [(artist, title), ...]  (iconic, widely-available tracks)
KNOWLEDGE = {
    "classic rock": [
        ("Led Zeppelin", "Stairway to Heaven"),
        ("The Rolling Stones", "Start Me Up"),
        ("AC/DC", "Back in Black"),
        ("Lynyrd Skynyrd", "Sweet Home Alabama"),
        ("Eagles", "Hotel California"),
        ("Queen", "Bohemian Rhapsody"),
        ("Creedence Clearwater Revival", "Proud Mary"),
        ("The Who", "Baba O'Riley"),
        ("Boston", "More Than a Feeling"),
        ("Journey", "Don't Stop Believin'"),
    ],
    "old school hip-hop": [
        ("Run-D.M.C.", "Walk This Way"),
        ("LL Cool J", "Mama Said Knock You Out"),
        ("Eric B. & Rakim", "Paid in Full"),
        ("Public Enemy", "Fight the Power"),
        ("Beastie Boys", "Sabotage"),
        ("De La Soul", "Me Myself and I"),
        ("A Tribe Called Quest", "Can I Kick It?"),
        ("Grandmaster Flash", "The Message"),
    ],
    "90s hip-hop": [
        ("The Notorious B.I.G.", "Juicy"),
        ("2Pac", "California Love"),
        ("Nas", "N.Y. State of Mind"),
        ("Wu-Tang Clan", "C.R.E.A.M."),
        ("Snoop Dogg", "Gin and Juice"),
        ("Outkast", "Rosa Parks"),
        ("Jay-Z", "Hard Knock Life"),
        ("DMX", "Ruff Ryders' Anthem"),
        ("Missy Elliott", "Get Ur Freak On"),
        ("Lauryn Hill", "Doo Wop (That Thing)"),
    ],
    "2000s hip-hop": [
        ("50 Cent", "In Da Club"),
        ("Kanye West", "Gold Digger"),
        ("Outkast", "Hey Ya!"),
        ("Eminem", "Lose Yourself"),
        ("Lil Wayne", "A Milli"),
        ("T.I.", "Whatever You Like"),
        ("Ludacris", "Stand Up"),
    ],
    "reggae": [
        ("Bob Marley", "Three Little Birds"),
        ("Bob Marley", "Is This Love"),
        ("Dennis Brown", "Here I Come"),
        ("Gregory Isaacs", "Night Nurse"),
        ("Burning Spear", "Marcus Garvey"),
        ("Steel Pulse", "Your House"),
        ("Chronixx", "Skankin' Sweet"),
        ("Protoje", "Who Knows"),
    ],
    "dancehall": [
        ("Sean Paul", "Get Busy"),
        ("Shaggy", "It Wasn't Me"),
        ("Beenie Man", "Who Am I"),
        ("Vybz Kartel", "Fever"),
        ("Popcaan", "Only Man She Want"),
    ],
    "r&b": [
        ("Marvin Gaye", "What's Going On"),
        ("Stevie Wonder", "Superstition"),
        ("Usher", "Yeah!"),
        ("Alicia Keys", "Fallin'"),
        ("Beyonce", "Crazy in Love"),
        ("D'Angelo", "Brown Sugar"),
    ],
    "soul": [
        ("Aretha Franklin", "Respect"),
        ("Otis Redding", "Sittin' On The Dock of the Bay"),
        ("Sam Cooke", "A Change Is Gonna Come"),
        ("Al Green", "Let's Stay Together"),
    ],
    "funk": [
        ("James Brown", "Get Up Offa That Thing"),
        ("Parliament", "Give Up the Funk"),
        ("Rick James", "Super Freak"),
        ("Zapp", "More Bounce to the Ounce"),
    ],
    "disco": [
        ("Donna Summer", "Hot Stuff"),
        ("Chic", "Le Freak"),
        ("Bee Gees", "Stayin' Alive"),
        ("Earth, Wind & Fire", "September"),
    ],
    "house": [
        ("Daft Punk", "One More Time"),
        ("Robin S", "Show Me Love"),
        ("Crystal Waters", "Gypsy Woman"),
        ("Black Box", "Ride on Time"),
    ],
    "soca": [
        ("Machel Montano", "Like Ah Boss"),
        ("Kes", "Hello"),
        ("Bunji Garlin", "Differentology"),
    ],
    "pop": [
        ("Bruno Mars", "Uptown Funk"),
        ("Dua Lipa", "Levitating"),
        ("Ed Sheeran", "Shape of You"),
        ("Justin Timberlake", "Can't Stop the Feeling!"),
        ("Maroon 5", "Sugar"),
        ("OneRepublic", "Counting Stars"),
        ("The Weeknd", "Blinding Lights"),
        ("Harry Styles", "As It Was"),
        ("Pharrell Williams", "Happy"),
        ("Katy Perry", "Firework"),
    ],
    "afrobeats": [
        ("Wizkid", "Essence"),
        ("Burna Boy", "Ye"),
        ("Rema", "Calm Down"),
        ("Tems", "Free Mind"),
        ("Davido", "Fall"),
        ("Ayra Starr", "Rush"),
        ("Fireboy DML", "Peru"),
        ("CKay", "Love Nwantiti"),
    ],
    "amapiano": [
        ("Kabza De Small", "eMcimbini"),
        ("Uncle Waffles", "Tanzania"),
        ("Young Stunna", "Adiwele"),
        ("Focalistic", "Ke Star"),
        ("DBN Gogo", "Khuza Gogo"),
    ],
    "latin": [
        ("Bad Bunny", "Tití Me Preguntó"),
        ("Daddy Yankee", "Gasolina"),
        ("Luis Fonsi", "Despacito"),
        ("Karol G", "Provenza"),
        ("Shakira", "Hips Don't Lie"),
        ("J Balvin", "Mi Gente"),
    ],
    "dance": [
        ("Calvin Harris", "Summer"),
        ("David Guetta", "Titanium"),
        ("Avicii", "Wake Me Up"),
        ("Zedd", "Clarity"),
        ("Dua Lipa", "Don't Start Now"),
        ("Joel Corry", "Head & Heart"),
    ],
    "country": [
        ("Morgan Wallen", "Last Night"),
        ("Luke Combs", "Fast Car"),
        ("Chris Stapleton", "White Horse"),
        ("Lainey Wilson", "Heart Like a Truck"),
        ("Shaboozey", "A Bar Song (Tipsy)"),
        ("Johnny Cash", "Ring of Fire"),
    ],
    "rock": [
        ("Foo Fighters", "Everlong"),
        ("The Killers", "Mr. Brightside"),
        ("Arctic Monkeys", "Do I Wanna Know?"),
        ("Kings of Leon", "Sex on Fire"),
        ("Green Day", "American Idiot"),
        ("Linkin Park", "In the End"),
    ],
}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def library_keys(db_path):
    """{(norm_artist, norm_title)} of everything in the library."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT artist, title FROM tracks").fetchall()
    except Exception:
        return set()
    finally:
        conn.close()
    return {(_norm(a), _norm(t)) for a, t in rows}


def pocket_track_count(db_path, pocket_label):
    """How many library tracks sit in this pocket?"""
    import sqlite3
    from pocket import classify_pocket as cp
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT artist, title, genres, year FROM tracks").fetchall()
    except Exception:
        return 0
    finally:
        conn.close()
    n = 0
    for artist, title, genres, year in rows:
        if cp({"genres": genres or "", "year": year})["label"] == pocket_label:
            n += 1
    return n


def _pocket_keywords(pocket_label):
    return pocket_label.lower()


def suggest_for_pocket(pocket_label, owned_keys, max_n=10):
    """Return suggestion dicts for a pocket, excluding owned tracks."""
    kw_words = set(_pocket_keywords(pocket_label).replace("/", " ").split())
    scored = []
    for pocket_kw, tracks in KNOWLEDGE.items():
        kw2_words = set(pocket_kw.split())
        overlap = kw_words & kw2_words
        if not overlap:
            continue
        # more shared words = more relevant
        relevance = len(overlap)
        for artist, title in tracks:
            if (_norm(artist), _norm(title)) in owned_keys:
                continue
            scored.append((relevance, artist, title))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, artist, title in scored:
        out.append({
            "artist": artist,
            "title": title,
            "pocket": pocket_label,
            "reason": f"Fits your {pocket_label} pocket — not in your library",
        })
        if len(out) >= max_n:
            break
    return out


def bridge_for_seed(seed_track, db_path, thin_threshold=5, max_n=10):
    """If the seed's pocket is thin in the library, suggest fill tracks.

    Returns {"pocket": label, "library_count": n, "suggestions": [...]}.
    """
    pocket_label = classify_pocket(seed_track)["label"]
    owned = library_keys(db_path)
    count = pocket_track_count(db_path, pocket_label)
    suggestions = []
    if count < thin_threshold:
        suggestions = suggest_for_pocket(pocket_label, owned, max_n)
    return {
        "pocket": pocket_label,
        "library_count": count,
        "thin": count < thin_threshold,
        "suggestions": suggestions,
    }


def main():
    ap = argparse.ArgumentParser(
        description="Suggest tracks for thin pockets (JSON for CrateTube)")
    ap.add_argument("--artist", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--genres", default="",
                    help="semicolon-separated, e.g. 'reggae;dancehall'")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--threshold", type=int, default=5)
    ap.add_argument("--db", default=os.path.expanduser("~/.crate-match/library.db"))
    args = ap.parse_args()
    seed = {"artist": args.artist, "title": args.title,
            "genres": args.genres, "year": args.year}
    print(json.dumps(bridge_for_seed(seed, args.db, args.threshold, args.max),
                     indent=2))


if __name__ == "__main__":
    main()

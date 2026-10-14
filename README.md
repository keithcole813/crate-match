# CrateMatch

djay-style "find similar songs" for your Serato offline library — on your MacBook.

## What it does

- Scans your music folder and analyzes every track: BPM, musical key
  (Camelot code), energy, and spectral character — cached in SQLite so
  rescans are instant
- Pick any track → ranked list of similar tracks, scored on BPM proximity,
  harmonic key compatibility, and energy
- Preview matches right in the app
- Export the set as a Serato `.crate` — drop it into Serato's
  `_Serato_/Subcrates` folder and DJ it

## Setup (MacBook Pro)

```
pip install -r requirements.txt
python crate_match.py
```

Also needs:
- **ffmpeg** on PATH (`brew install ffmpeg`)

## Notes

- Analysis runs once per track (~10-20s each on first scan), then it's instant
- Key detection uses the Krumhansl-Schmuckler algorithm on chroma features
- First scan of a big library takes a while — let it run, grab a coffee

## Sibling apps

- **crate-to-apple-music** — convert a Serato crate to phone-ready M4A files
  for workouts and listening away from the decks
- **crate-tube** — check a crate against YouTube, download 10 matching songs
  as MP3 straight to your MacBook

## CrateMatch Live (live DJ window)

`python live_window.py` — a small always-on-top window that sits next to
Serato while you DJ:

- **Watches Serato's history** (`~/Music/_Serato_/History/Sessions/`) and
  detects the currently playing track within ~2 seconds
- **Pocket-aware recommendations** — two-layer scoring:
  - 60% audio similarity (BPM, Camelot key, energy, spectral character)
  - 40% pocket match (genre + era context, so classic rock stays classic rock)
- **Drag straight onto a Serato deck** — grab any result and drop it on an
  open deck or into the queue (needs `tkinterdnd2`)
- **Sortable columns** — click Title, Artist, BPM, Key, Energy, or Score
- **Match count picker** — 10 / 15 / 20 / 25 tracks
- **Key format switcher** — Camelot (8A), Classic (Am), or Open Key (1m),
  just like Serato's display options
- **Thin-pocket fill** — if your library has <5 tracks in the current
  pocket, a "Fill pocket" button writes YouTube-ready suggestions
  (`cratematch_pocket_suggestions.json`) for CrateTube to download

### Genre tagging (one-time setup for pockets)

Pockets need genre data. Tag your library once with MusicBrainz (free, no
API key):

```
python genre_tagger.py
```

Respects MusicBrainz's 1 req/sec limit. Run it overnight for a big
library — results are cached in SQLite, and re-runs only tag new tracks.
`--limit N` to tag in batches, `--force` to re-tag everything.

### How it works

```
live_window.py      always-on-top dark UI (tkinter + tkinterdnd2)
serato_watcher.py   tails Serato history .session files, parses the
                    binary format (oent/adat TLV), reports now-playing
pocket.py           pocket classifier + 0.6*audio + 0.4*pocket scoring,
                    Camelot/Classic/Open Key conversions
genre_tagger.py     MusicBrainz batch genre + year tagging
cratetube_bridge.py suggests iconic tracks for thin pockets as JSON
                    for CrateTube
```

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

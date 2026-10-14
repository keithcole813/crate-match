#!/bin/bash
# Build CrateMatch Live as a macOS .app and .dmg
# Run on your MacBook:  bash build_dmg.sh
set -e

echo "=== Installing PyInstaller ==="
pip install pyinstaller

echo "=== Building .app bundle ==="
pyinstaller --clean --noconfirm \
  --name "CrateMatch Live" \
  --windowed \
  --onedir \
  --add-data "README.md:." \
  --hidden-import tkinterdnd2 \
  --hidden-import librosa \
  --hidden-import mutagen \
  --hidden-import numpy \
  --hidden-import scipy \
  --exclude-module torch \
  --exclude-module tensorflow \
  live_window.py

APP="dist/CrateMatch Live.app"

echo "=== Creating DMG ==="
DMG="dist/CrateMatch-Live.dmg"
rm -f "$DMG"
hdiutil create -volname "CrateMatch Live" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo ""
echo "Done! DMG at: $DMG"
echo ""
echo "NOTE: The app is unsigned, so macOS Gatekeeper will warn on first launch."
echo "Right-click the app > Open > Open to bypass."
echo ""
echo "ffmpeg must be installed separately:  brew install ffmpeg"

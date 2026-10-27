#!/usr/bin/env bash
# Build the release package for Pen Setup.
#
#   ./scripts/package.sh            -> dist/Pen-Setup-<version>.zip
#
# The zip contains one top-level folder (Pen-Setup-<version>) so it unpacks
# the same way the published releases do:
#
#   unzip Pen-Setup-<version>.zip && cd Pen-Setup-<version> && ./install.sh
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' \
  "$HERE/files/.config/omarchy/plugins/dbeaudry.pensetup/manifest.json")"
NAME="Pen-Setup-$VERSION"
DIST="$HERE/dist"
STAGE="$DIST/$NAME"

cd "$HERE"

rm -rf "$DIST"
mkdir -p "$STAGE"

# Everything the installer and docs need, and nothing else.
cp -a install.sh uninstall.sh README.md LICENSE "$STAGE/"
cp -a files docs "$STAGE/"
find "$STAGE" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.pyc' -delete

( cd "$DIST" && zip -qr "$NAME.zip" "$NAME" )
( cd "$DIST" && sha256sum "$NAME.zip" >SHA256SUMS )

echo "built $DIST/$NAME.zip"
( cd "$DIST" && sha256sum -c SHA256SUMS )

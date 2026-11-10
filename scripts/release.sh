#!/usr/bin/env bash
# Cut a Pen Setup release: bump the version, rebuild the zip, commit, tag,
# push, and publish a GitHub release.
#
# Workflow for a new release:
#   1. make your changes and commit them (leave the working tree clean);
#   2. run:  ./scripts/release.sh <next-version>
#
# The version lives in exactly one place — the plugin manifest — so the
# package name, the panel's on-screen version, and the release all agree.
#
# Requirements: `zip`, and `gh` authenticated to the repo.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

MANIFEST="$HERE/files/.config/omarchy/plugins/pen-setup/manifest.json"
REPO="DanielBeaudryGIThub/pen-setup"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "usage: ./scripts/release.sh <version>   (e.g. 1.2.0)" >&2
  exit 2
fi

python3 -c 'import sys,re; sys.exit(0 if re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]) else 1)' \
  "$VERSION" || { echo "error: version must look like 1.2.0" >&2; exit 2; }

for bin in zip gh; do
  command -v "$bin" >/dev/null 2>&1 || { echo "error: $bin is required" >&2; exit 1; }
done

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "error: the working tree has uncommitted changes; commit them first" >&2
  exit 1
fi

OLD_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$MANIFEST")"

# 1. bump the manifest version (only when it changed) -----------------------
if [ "$OLD_VERSION" != "$VERSION" ]; then
  python3 - "$MANIFEST" "$VERSION" <<'PY'
import json, sys
path, version = sys.argv[1], sys.argv[2]
with open(path) as fh:
    data = json.load(fh)
data["version"] = version
with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"  manifest version -> {version}")
PY
  git add "$MANIFEST"
  git commit -m "Pen Setup $VERSION"
else
  echo "  manifest already at $VERSION (assuming the release commit is in place)"
fi

# 2. build the package ------------------------------------------------------
./scripts/package.sh
ZIP="$HERE/dist/Pen-Setup-$VERSION.zip"
SUMS="$HERE/dist/SHA256SUMS"
[ -f "$ZIP" ] || { echo "error: package.sh did not produce $ZIP" >&2; exit 1; }

# 3. commit (guarded), tag and push ------------------------------------------
# `git add` re-stages everything so a version bump done above is included.
git add -A
if ! git diff --cached --quiet; then
  git commit -m "Pen Setup $VERSION"
fi

if git rev-parse "v$VERSION" >/dev/null 2>&1; then
  echo "  tag v$VERSION already exists; skipping tag"
else
  git tag -a "v$VERSION" -m "Pen Setup $VERSION"
fi
git push origin HEAD
git push origin "v$VERSION"

# 4. publish (or refresh) the GitHub release ---------------------------------
NOTES="Pen Setup $VERSION — auto-built package ($(basename "$ZIP") and $(basename "$SUMS"))."
if gh release view "v$VERSION" --repo "$REPO" >/dev/null 2>&1; then
  echo "  release v$VERSION already exists; uploading assets"
  gh release upload "v$VERSION" "$ZIP" "$SUMS" --repo "$REPO" --clobber
else
  gh release create "v$VERSION" --repo "$REPO" --title "Pen Setup $VERSION" \
    --notes "$NOTES" "$ZIP" "$SUMS"
fi

echo
echo "published Pen Setup $VERSION"

#!/bin/bash
# Fetch prebuilt llama.cpp binaries for macOS on Apple Silicon.
#
# The macOS arm64 asset is already a Metal build - there is no separate
# "cuda" style variant to pick, and no runtime to install alongside it.
#
# Note: the repo's "latest" release is a marker tag (v0.2.0) that carries no
# binaries -- the real builds live on the b##### tags. So walk recent releases
# and take the newest one that actually has a macos-arm64 asset.
#
#   usage: get-llama-mac.sh [dest-dir]
#
# Falls back to Homebrew, then to a source build, because the prebuilt zip
# is signed-but-unnotarized: Gatekeeper quarantines it on first run. We strip
# the quarantine attribute after extracting, which needs no admin rights.

set -u

DEST="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/llama}"
REPO="ggml-org/llama.cpp"
ARCH="$(uname -m)"

say()  { printf '  %s\n' "$*"; }

if [ "$ARCH" != "arm64" ]; then
  say "! this Mac reports arch '$ARCH', not arm64."
  say "  The macos-arm64 build will not run. Use an Apple Silicon Mac, or make"
  say "  sure this shell is not running under Rosetta (arch -arm64 bash)."
fi

mkdir -p "$DEST"
STAMP="$DEST/.build"
SERVER="$DEST/llama-server"

# --------------------------------------------------------------------
# 1. prebuilt release asset
# --------------------------------------------------------------------
say "[llama.cpp] looking for the newest macos-arm64 build ..."

JSON="$(curl -fsSL -H 'User-Agent: bookreel' \
        "https://api.github.com/repos/$REPO/releases?per_page=30" 2>/dev/null)"

TAG=""
URL=""
if [ -n "$JSON" ]; then
  # No jq on a stock macOS. Pull the asset URLs out with grep and take the
  # first one whose name matches - the API returns releases newest first.
  URL="$(printf '%s' "$JSON" \
        | grep -o '"browser_download_url": *"[^"]*llama-b[0-9]*-bin-macos-arm64\.zip"' \
        | head -n 1 | sed 's/.*"\(https[^"]*\)"/\1/')"
  TAG="$(printf '%s' "$URL" | sed -n 's/.*llama-\(b[0-9]*\)-bin-macos-arm64\.zip/\1/p')"
fi

if [ -n "$URL" ] && [ -f "$STAMP" ] && [ -x "$SERVER" ] && [ "$(cat "$STAMP")" = "$TAG" ]; then
  say "[llama.cpp] $TAG already installed"
  exit 0
fi

if [ -n "$URL" ]; then
  TMP="$(mktemp -d /tmp/bookreel-llama.XXXXXX)"
  trap 'rm -rf "$TMP"' EXIT
  say "[llama.cpp] downloading $TAG ..."
  if curl -fL --progress-bar -o "$TMP/llama.zip" "$URL" \
     && ditto -x -k "$TMP/llama.zip" "$TMP/x" 2>/dev/null; then
    # Some builds nest everything one folder deep - flatten so the path in
    # setup.command stays stable.
    SRC="$(dirname "$(find "$TMP/x" -type f -name llama-server -print -quit)")"
    if [ -n "$SRC" ] && [ "$SRC" != "." ]; then
      cp -R "$SRC"/* "$DEST"/ 2>/dev/null
      # build/bin layouts keep the dylibs beside the binary; if they landed a
      # level up, bring those too or the server dies on a missing libggml.
      find "$TMP/x" -type f \( -name '*.dylib' -o -name '*.metal' \) \
        -exec cp {} "$DEST"/ \; 2>/dev/null
    fi
  fi
  if [ -x "$SERVER" ]; then
    # Downloaded binaries carry com.apple.quarantine; without this the first
    # run pops "cannot be opened because the developer cannot be verified".
    xattr -dr com.apple.quarantine "$DEST" 2>/dev/null
    chmod +x "$DEST"/llama-* 2>/dev/null
    printf '%s\n' "$TAG" > "$STAMP"
    say "[llama.cpp] ready: $SERVER  ($TAG)"
    exit 0
  fi
  say "! the prebuilt zip did not yield a working llama-server - trying Homebrew"
else
  say "! no macos-arm64 asset in the last 30 releases (or GitHub unreachable)"
fi

# --------------------------------------------------------------------
# 2. Homebrew
# --------------------------------------------------------------------
if command -v brew >/dev/null 2>&1; then
  say "[llama.cpp] installing via Homebrew ..."
  if brew list llama.cpp >/dev/null 2>&1 || brew install llama.cpp; then
    BREW_SERVER="$(command -v llama-server || true)"
    if [ -n "$BREW_SERVER" ]; then
      ln -sf "$BREW_SERVER" "$SERVER"
      printf '%s\n' "brew" > "$STAMP"
      say "[llama.cpp] ready: $BREW_SERVER (Homebrew, symlinked into $DEST)"
      exit 0
    fi
  fi
  say "! Homebrew install did not produce llama-server"
else
  say "! Homebrew not installed - skipping that route"
fi

# --------------------------------------------------------------------
# 3. build from source (needs Xcode command line tools + cmake)
# --------------------------------------------------------------------
if command -v cmake >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
  say "[llama.cpp] building from source - this takes a few minutes ..."
  SRCDIR="$DEST/src"
  if [ -d "$SRCDIR/.git" ]; then
    git -C "$SRCDIR" pull --ff-only >/dev/null 2>&1
  else
    git clone --depth 1 "https://github.com/$REPO" "$SRCDIR" || exit 1
  fi
  # GGML_METAL_EMBED_LIBRARY bakes the Metal shaders into the binary, so the
  # server does not have to find ggml-metal.metal at runtime.
  cmake -S "$SRCDIR" -B "$SRCDIR/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_METAL=ON -DGGML_METAL_EMBED_LIBRARY=ON \
        -DLLAMA_CURL=OFF >/dev/null || exit 1
  cmake --build "$SRCDIR/build" --config Release -j "$(sysctl -n hw.ncpu)" \
        --target llama-server llama-cli || exit 1
  cp "$SRCDIR/build/bin/llama-server" "$SRCDIR/build/bin/llama-cli" "$DEST"/ 2>/dev/null
  find "$SRCDIR/build/bin" -name '*.dylib' -exec cp {} "$DEST"/ \; 2>/dev/null
  if [ -x "$SERVER" ]; then
    printf '%s\n' "source" > "$STAMP"
    say "[llama.cpp] ready: $SERVER (built from source)"
    exit 0
  fi
fi

say "x could not get a llama-server. Options:"
say "    brew install llama.cpp"
say "    xcode-select --install && brew install cmake, then rerun this script"
exit 1

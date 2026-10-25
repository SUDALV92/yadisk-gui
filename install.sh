#!/bin/sh
# Install (or re-point) the app into the user layout:
#   ~/.local/lib/yadisk  -> <this repo>/yadisk   (symlink to the checkout)
#   ~/.local/bin/yadisk  -> <this repo>/bin/yadisk
# Re-run after moving the checkout. Nothing outside these two paths is touched,
# and no runtime data (config, logs, pins) lives in either of them.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
LIB="$HOME/.local/lib/yadisk"
BIN="$HOME/.local/bin/yadisk"

mkdir -p "$HOME/.local/bin" "$HOME/.local/lib"

# A previous copy-style install leaves a real directory / real file in place;
# `ln -sfn` would then nest a link inside it instead of replacing it. Clear only
# our own paths first — they hold no data, just code from an older checkout.
[ -d "$LIB" ] && [ ! -L "$LIB" ] && rm -rf "$LIB"
[ -f "$BIN" ] && [ ! -L "$BIN" ] && rm -f "$BIN"

ln -sfn "$HERE/yadisk" "$LIB"
ln -sf "$HERE/bin/yadisk" "$BIN"

echo "linked $LIB -> $HERE/yadisk"
echo "linked $BIN -> $HERE/bin/yadisk"
echo
echo "next: yadisk setup    (then)    yadisk mount    (then)    yadisk gui"
echo "      Dolphin badges: dolphin-plugin/build.sh, then"
echo "      sudo install -m644 dolphin-plugin/build/yadisk_vcs.so /usr/lib/qt6/plugins/dolphin/vcs/"
echo "      dolphin-plugin/install-icons.sh --hook"

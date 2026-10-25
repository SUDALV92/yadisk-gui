#!/bin/bash
# Install the Yandex Disk state badges (blue cloud / green check) where
# Dolphin's Qt icon lookup actually finds them.
#
# Why /usr/local/share/icons and not ~/.local/share/icons: Dolphin paints the
# version-control overlays with QIcon::fromTheme() (KStandardItemListWidget::
# drawOverlays), and on this box that lookup keeps resolving the Breeze files in
# /usr/share even when the same icon name exists in ~/.local/share/icons (tested
# by swapping in magenta/orange placeholders at every emblem size).  /usr/local
# comes before /usr/share in the icon search path and belongs to no package, so a
# breeze-icons upgrade never fights us.
#
# Only the two emblems the plugin can report are installed (vcs-normal,
# vcs-locally-modified).  vcs-added is left alone on purpose: the plugin never
# returns AddedVersion, and letting Breeze keep the stock file means other VCS
# plugins (git, svn, hg) still show their normal "added" badge.
#
# Usage: install-icons.sh [--hook]
#   --hook  also install /usr/local/bin/yadisk-vcs-icons and a pacman
#           PostTransaction hook that re-applies the badges after every
#           breeze-icons upgrade (recommended for Arch users).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
NAMES="vcs-normal vcs-locally-modified"

# 1. staging copy, so re-applying needs no access to this checkout
sudo install -d /usr/local/share/yadisk-vcs-icons
for n in $NAMES; do
    sudo install -m644 "$HERE/icons/$n.svg" "/usr/local/share/yadisk-vcs-icons/$n.svg"
done

# 2. copy into every Breeze theme at every emblem size Dolphin may ask for
apply() {
    local theme sz
    for theme in breeze breeze-dark; do
        [[ -d /usr/share/icons/$theme ]] || continue
        for sz in 16 16@2x 16@3x 22 22@2x 22@3x 24 24@2x 24@3x; do
            sudo install -d "/usr/local/share/icons/$theme/emblems/$sz"
            for n in $NAMES; do
                sudo install -m644 "/usr/local/share/yadisk-vcs-icons/$n.svg" \
                    "/usr/local/share/icons/$theme/emblems/$sz/$n.svg"
            done
        done
        # an icon theme is only searched when it has its own index.theme
        [[ -f /usr/share/icons/$theme/index.theme ]] && \
            sudo install -Dm644 "/usr/share/icons/$theme/index.theme" \
                "/usr/local/share/icons/$theme/index.theme"
    done
}

if [[ "${1:-}" == "--hook" ]]; then
    sudo install -Dm755 /dev/stdin /usr/local/bin/yadisk-vcs-icons <<'EOS'
#!/bin/bash
# Re-apply the Yandex Disk state badges to the Breeze icon themes.
# Sources: /usr/local/share/yadisk-vcs-icons (installed by install-icons.sh).
set -euo pipefail
SRC=/usr/local/share/yadisk-vcs-icons
for theme in breeze breeze-dark; do
    [[ -d /usr/share/icons/$theme ]] || continue
    for sz in 16 16@2x 16@3x 22 22@2x 22@3x 24 24@2x 24@3x; do
        install -d "/usr/local/share/icons/$theme/emblems/$sz"
        for n in vcs-normal vcs-locally-modified; do
            install -m644 "$SRC/$n.svg" "/usr/local/share/icons/$theme/emblems/$sz/$n.svg"
        done
    done
    [[ -f /usr/share/icons/$theme/index.theme ]] && \
        install -Dm644 "/usr/share/icons/$theme/index.theme" "/usr/local/share/icons/$theme/index.theme"
done
EOS
    sudo install -Dm644 /dev/stdin /usr/share/libalpm/hooks/yadisk-vcs-icons.hook <<'EOH'
[Trigger]
Operation = Install
Operation = Upgrade
Type = Package
Target = breeze-icons

[Action]
Description = Restoring the Yandex Disk status badges in the Breeze icon themes...
When = PostTransaction
Exec = /usr/local/bin/yadisk-vcs-icons
EOH
    sudo /usr/local/bin/yadisk-vcs-icons
else
    apply
fi

echo "icons installed; restart Dolphin (or reopen the window) to see them"

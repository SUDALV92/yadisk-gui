# Yandex Disk indicators in Dolphin

Status badges on every file and folder inside `~/YandexDisk`, like the Windows
Yandex Disk client shows in Explorer.

Two states only, exactly like the Windows client — a file is either kept on
this PC or it lives in the cloud. There is no user-visible "downloaded but not
kept" state: bytes sitting in the LRU cache are an implementation detail of the
rclone mount, so such files still read as cloud.

| Badge | Meaning | Comes from |
|---|---|---|
| blue cloud | **available online only** — lives in Yandex Disk, downloads on open | `NormalVersion` → `vcs-normal` |
| green check | **kept on this device** — pinned, never evicted | `LocallyModifiedVersion` → `vcs-locally-modified` |

Bytes never pile up invisibly either: `yadisk-tidy.timer` runs `yadisk tidy`
every 10 minutes and drops any cached bytes that are not kept offline (skipping
files that are open right now), so "cloud" really does mean zero bytes on disk.

Dolphin also tints the name of a kept file green (built into
`DolphinFileItemListWidget`, cannot be switched off). Only these two emblems are
overridden — `vcs-added` is left to Breeze, because the plugin never reports
`AddedVersion` and other VCS plugins (git/svn/hg) still use the stock badge.

## Right-click menu

For any selection inside the mount, the context menu gains:

* **Keep on this device** – `yadisk pin` (pins, then hydrates)
* **Download Now (do not keep)** – `yadisk hydrate`
* **Available online only** – `yadisk evict`: drops the local bytes **and** the
  offline flag, so the file goes back to a cloud shadow (Windows' "free up
  space" behaves the same way; keeping the pin would make `yadisk-pins.timer`
  fetch the bytes right back)
* **Yandex Disk Settings…** – `yadisk gui`

## How it works

1. `yadisk state` writes three plain text files into `~/.local/share/yadisk/state/`:
   `local.txt` (relative paths with bytes on disk), `pins.txt`, `mount.txt`.
   `yadisk-state.timer` refreshes them every 60 s; `yadisk-mount.service` also
   publishes them right after the FUSE mount comes up, and the CLI republishes
   after every pin/unpin/hydrate/evict.
2. `yadisk_vcs.so` is a `KVersionControlPlugin` installed in
   `/usr/lib/qt6/plugins/dolphin/vcs/`. It is the API Dolphin really consults on
   KF6/Plasma 6 (`KOverlayIconPlugin` is dead there — no installed KF6 library
   even contains the string `overlayicon`). `itemVersion()` only does hash
   lookups into the two text files, so it never blocks the update thread.
3. The plugin must be enabled, otherwise Dolphin never instantiates it:

   ```sh
   kwriteconfig6 --file dolphinrc --group VersionControl --key enabledPlugins YandexDisk
   ```

4. Dolphin maps the reported state to the emblem names `vcs-normal` /
   `vcs-locally-modified` and paints them in a corner of the icon
   (`KStandardItemListWidget::drawOverlays`, via `QIcon::fromTheme`).

## Icons: why they live in /usr/local/share/icons

`~/.local/share/icons/{breeze,breeze-dark}/emblems/*/vcs-*.svg` is **ignored** by
Dolphin's overlay lookup — verified by swapping in magenta/orange/yellow
placeholders at every emblem size: the badges never changed. The same files are
picked up by `KIconLoader` (icontest), so this is a Qt-side lookup quirk, not a
permissions problem.

Therefore the badges are installed into `/usr/local/share/icons/…`, which comes
*before* `/usr/share` in the icon search path and is owned by no package:

```sh
~/Projects/yadisk-gui/dolphin-plugin/install-icons.sh   # copies + installs
/usr/local/bin/yadisk-vcs-icons                         # the actual copier
```

`/usr/share/libalpm/hooks/yadisk-vcs-icons.hook` re-runs the copier after every
`breeze-icons` install/upgrade, so a package update cannot silently bring Breeze's
own glyphs back (Breeze's `vcs-normal` is a green check, i.e. it reads as
"synced" — exactly the wrong meaning for a cloud-only file).

## Rebuilding the plugin (no cmake/ECM on this box)

```sh
~/Projects/yadisk-gui/dolphin-plugin/build.sh      # moc + g++ only
sudo install -m644 build/yadisk_vcs.so /usr/lib/qt6/plugins/dolphin/vcs/
```

`build.sh` passes the KF6/Qt include paths to **moc** as well — without that,
`K_PLUGIN_CLASS_WITH_JSON` is not expanded and the `.so` ends up with no embedded
JSON metadata, so `KPluginMetaData::findPlugins("dolphin/vcs")` returns 0 plugins
and the file has no `.qtmetadata` section. Use `-ldolphinvcs` (the CMake target
`DolphinVcs` is the library `libdolphinvcs.so`).

To verify without restarting Dolphin, load the `.so` from a tiny harness the same
way Dolphin does (`KPluginMetaData::findPlugins("dolphin/vcs")`), using
`QGuiApplication` — not `QCoreApplication` — and run it with
`QT_QPA_PLATFORM=offscreen` so no window is needed.

Dolphin caches the plugin list per window — restart Dolphin after rebuilding or
after enabling the plugin.

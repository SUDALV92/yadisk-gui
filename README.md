# Yandex.Disk GUI

**v1.0**

A Yandex Disk client for Linux that behaves like the Windows one. The official
Linux story is "here is a folder, everything is really there"; this one gives you
**shadow files** instead: every file and folder in your disk is visible in your
mount folder (default `~/YandexDisk`, changeable in Settings) and in Dolphin,
nothing is downloaded until you open it, and each item is in one of two states.

```
☁  available online only   bytes live in Yandex Disk, fetched when you open it
✔  kept on this device     pinned, never dropped, survives reboots
```

That's the whole model — the same two options the Windows client gives you. Bytes
that happen to be sitting on disk after you opened a file are temporary: a timer
drops them again, so "available online only" really does mean zero bytes.

## What you get

* Cloud-only placeholders that open transparently, like Dropbox smart sync.
* **Keep on this device** / **Available online only** on files and folders, from
  Dolphin's context menu or from the app.
* Dolphin emblems — a blue cloud or a green check on files *and* folders.
* A small Qt window with sizes and states, a file list, keep/free buttons, the
  mount status and a tray icon. Closing the window keeps it in the tray.
* Everything runs on `rclone`'s FUSE mount plus systemd user units: mount at
  login, pin enforcement, state publishing for Dolphin, periodic tidy.

## Setup

You need `rclone`, `python-pyside6` and `fuse3`. Dolphin is optional — only the
badges need it.

```sh
git clone <this repo> ~/Projects/yadisk-gui
~/Projects/yadisk-gui/install.sh     # puts yadisk in ~/.local/bin
yadisk gui
```

That's it — the rest happens in the window: **Mount** brings the folder up,
**Account…** connects your own Yandex OAuth app (see below), **Settings** picks
the mount folder and whether it mounts at login. Closing the window leaves the
app in the tray.

`install.sh` symlinks the checkout into `~/.local`, so `git pull` is enough to
update the app. Run it again if you move the folder.

### Authorization

Yandex blocks the OAuth client id that rclone ships with, so you need your own
(one minute of clicking):

1. <https://oauth.yandex.ru/client/new> → app type *For API access*
2. Redirect URI `http://localhost:53682/`, scopes `cloud_api:disk.read`,
   `cloud_api:disk.write`, `cloud_api:disk.info`
3. Paste the client id and secret into **Account…**; the dialog does the code
   exchange and restarts the mount with the new token.

### Dolphin badges

Needs Qt6/KF6 headers to compile the plugin:

```sh
cd ~/Projects/yadisk-gui/dolphin-plugin
./build.sh
sudo install -m644 build/yadisk_vcs.so /usr/lib/qt6/plugins/dolphin/vcs/
./install-icons.sh --hook
```

Restart Dolphin afterwards. See [`dolphin-plugin/README.md`](dolphin-plugin/README.md)
for what the plugin does and how it maps files to emblems.

## Command line (optional)

`yadisk` is this project's own command line — `bin/yadisk` → `yadisk/cli.py`, the
same engine the window drives, not a Yandex or rclone tool. The window covers
mounting, keeping and freeing files, settings and authorization, so you can
ignore all of this. What only exists on the command line:

| Command | What it does |
|---|---|
| `yadisk list [-r] [--pinned] [--json]` | the same list the window shows, for scripts |
| `yadisk tidy [--dry-run] [--grace MIN]` | drop downloaded bytes now instead of waiting for the timer |
| `yadisk hydrate <path…>` | download something without keeping it |
| `yadisk pins --enforce` | re-download kept files that went missing |
| `yadisk doctor` | check environment, token and mount |
| `yadisk status` | one-screen summary of the above |

## Where things live

```
bin/yadisk            launcher, works from a checkout or from ~/.local
install.sh            symlink the checkout into ~/.local
yadisk/engine.py      config, rclone mount and units, pin/tidy/state logic
yadisk/cli.py         command line
yadisk/gui.py         PySide6 window, tray, settings, About
dolphin-plugin/       KVersionControlPlugin behind the Dolphin badges
```

Your data stays out of the repo, in `~/.local/share/yadisk/` (config, pinned
state, rclone.conf, logs) and `~/.cache/yadisk/` (downloaded files).

## Notes

* Dolphin only loads emblems from the icon theme, so the badges go into
  `/usr/local/share/icons` (searched before `/usr/share`) and a pacman hook
  re-applies them when `breeze-icons` updates.
* Files kept offline are ordinary files in the rclone VFS store; the pin list is
  what keeps the tidy pass and the LRU from dropping them.
* Not affiliated with Yandex.

## License

MIT — see [LICENSE](LICENSE).

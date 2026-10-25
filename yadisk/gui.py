"""Yandex Disk window + tray (PySide6)."""

from __future__ import annotations

import os
import sys
import time

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt
except ImportError as e:                                    # pragma: no cover
    print(f"PySide6 is required for the GUI: {e}", file=sys.stderr)
    raise SystemExit(3)

from . import engine as E

STATE_STYLE = {
    "local":   ("●  on this PC", "#2ea043"),
    "cloud":   ("☁  cloud only", "#4da3ff"),
    "partial": ("◐  partial",     "#d29922"),
    "missing": ("?  missing",     "#8b949e"),
}


# --------------------------------------------------------------------------
# workers# --------------------------------------------------------------------------
# workers
# --------------------------------------------------------------------------

class ScanWorker(QtCore.QThread):
    batch = QtCore.Signal(list)
    finished_scan = QtCore.Signal(int, int, int)      # local, cloud, bytes_local

    def __init__(self, root: str = "", parent=None, recursive: bool = False):
        super().__init__(parent)
        self.root = root
        self.recursive = recursive
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        buf, loc, cl, lbytes = [], 0, 0, 0
        try:
            for e in E.scan(self.root, cancel=lambda: self._cancel,
                            recursive=self.recursive):
                buf.append(e)
                if e["state"] == "local":
                    loc += 1
                    lbytes += e["local_bytes"]
                else:
                    cl += 1
                if len(buf) >= 150:
                    self.batch.emit(buf)
                    buf = []
        except Exception as ex:                                   # noqa: BLE001
            print(f"scan error: {ex}", file=sys.stderr)
        if buf:
            self.batch.emit(buf)
        self.finished_scan.emit(loc, cl, lbytes)


class OpWorker(QtCore.QThread):
    """pin / evict / hydrate in the background."""
    progress = QtCore.Signal(int, int)
    done = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, kind: str, paths: list[str], parent=None):
        super().__init__(parent)
        self.kind, self.paths = kind, paths
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self.kind == "pin":
                added, errs = E.pin(self.paths, progress=lambda d, t: self.progress.emit(d, t),
                                    cancel=lambda: self._cancel)
                msg = f"kept offline: {len(added)} item(s)"
                if errs:
                    msg += f", {len(errs)} problem(s): " + "; ".join(f"{a} ({b})" for a, b in errs[:3])
                self.done.emit(msg)
            elif self.kind == "evict":
                # Windows-client semantics: dropping the bytes also drops the
                # offline flag, otherwise enforce_pins would fetch them back.
                E.unpin(self.paths)
                freed = 0
                for rel in self.paths:
                    mp = E.mount_path(rel)
                    targets = [e["rel"] for e in E.scan(rel) if e["state"] == "local"] \
                        if os.path.isdir(mp) else [rel]
                    for t in targets:
                        freed += E.local_bytes(t)
                        E.evict(t)
                self.done.emit(f"freed {E.human_bytes(freed)}")
            elif self.kind == "hydrate":
                n, errs = E.hydrate_many(self.paths, progress=lambda d, t: self.progress.emit(d, t),
                                         cancel=lambda: self._cancel)
                self.done.emit(f"fetched {E.human_bytes(n)}")
        except E.Cancelled:
            self.failed.emit("cancelled")
        except Exception as ex:                                   # noqa: BLE001
            self.failed.emit(str(ex))


class AuthWorker(QtCore.QThread):
    """Runs the OAuth dance in the background (browser opens itself)."""
    url = QtCore.Signal(str)
    done = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, client_id: str, client_secret: str, parent=None):
        super().__init__(parent)
        self.client_id, self.client_secret = client_id, client_secret

    def run(self):
        try:
            ok, msg = E.authorize_flow(self.client_id, self.client_secret,
                                       timeout=600, url_cb=self.url.emit)
            self.done.emit() if ok else self.failed.emit(msg)
        except Exception as ex:                                   # noqa: BLE001
            self.failed.emit(str(ex))


class CodeWorker(QtCore.QThread):
    """Exchanges a pasted confirmation code for a token."""
    done = QtCore.Signal()
    failed = QtCore.Signal(str)

    def __init__(self, client_id: str, client_secret: str, code: str,
                 redirect: str | None = None, parent=None):
        super().__init__(parent)
        self.client_id, self.client_secret = client_id, client_secret
        self.code, self.redirect = code, redirect

    def run(self):
        try:
            E.exchange_code(self.client_id, self.client_secret, self.code, self.redirect)
            self.done.emit()
        except Exception as ex:                                   # noqa: BLE001
            self.failed.emit(str(ex))


AUTH_BLURB = """Yandex blocks the OAuth client id that rclone ships with: every file
request from it answers “410 Gone”. So the app needs its own OAuth app —
one-time, about two minutes.

1. open  https://oauth.yandex.ru/client/new
2. app type “For API access”, platform “Web services”
3. tick cloud_api:disk.read, cloud_api:disk.write, cloud_api:disk.info
4. paste ClientID + Client secret below"""

CODE_BLURB = """Leave Callback URL empty: Yandex then uses the URL registered for your
app, which is the only value it accepts (“redirect_uri does not match the
Callback URL defined for the client” means we asked for a different one).

1. press “Open the consent page”, log in, press Allow
2. Yandex lands on your registered Callback URL:
   • a page showing a short code → copy it
   • “127.0.0.1 refused to connect” → the code is the ?code=… part of the
     address bar; copy everything after code= up to &
3. paste it here and press Finish"""


class AuthDialog(QtWidgets.QDialog):
    authorized = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Authorize Yandex Disk")
        self.resize(620, 470)
        self.worker = None
        self.codeworker = None
        prev = E.remote_conf()

        v = QtWidgets.QVBoxLayout(self)

        form = QtWidgets.QFormLayout()
        self.cid = QtWidgets.QLineEdit(prev.get("client_id", ""))
        self.cid.setPlaceholderText("ClientID, e.g. 0234567890abcdef…")
        self.csec = QtWidgets.QLineEdit(prev.get("client_secret", ""))
        self.csec.setPlaceholderText("Client secret")
        self.csec.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow("ClientID", self.cid)
        form.addRow("Client secret", self.csec)
        v.addLayout(form)

        self.tabs = QtWidgets.QTabWidget()
        v.addWidget(self.tabs, 1)

        # ---- tab 1: local callback (needs Redirect URI http://127.0.0.1:53682/) ----
        t1 = QtWidgets.QWidget()
        l1 = QtWidgets.QVBoxLayout(t1)
        b1 = QtWidgets.QLabel(AUTH_BLURB + "\n\nRedirect URI must be exactly "
                              "http://127.0.0.1:53682/")
        b1.setWordWrap(True)
        b1.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l1.addWidget(b1)
        row0 = QtWidgets.QHBoxLayout()
        b_open = QtWidgets.QPushButton("Open oauth.yandex.ru/client/new")
        b_open.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl("https://oauth.yandex.ru/client/new")))
        row0.addWidget(b_open)
        row0.addStretch(1)
        l1.addLayout(row0)
        self.url = QtWidgets.QLineEdit()
        self.url.setReadOnly(True)
        self.url.setPlaceholderText("the confirmation link appears here")
        l1.addWidget(self.url)
        self.btn_browser = QtWidgets.QPushButton("Authorize with browser callback")
        self.btn_browser.clicked.connect(self.start_browser)
        l1.addWidget(self.btn_browser)
        l1.addStretch(1)
        self.tabs.addTab(t1, "Browser callback")

        # ---- tab 2: confirmation code (no callback) ----
        t2 = QtWidgets.QWidget()
        l2 = QtWidgets.QVBoxLayout(t2)
        b2 = QtWidgets.QLabel(CODE_BLURB)
        b2.setWordWrap(True)
        b2.setTextInteractionFlags(Qt.TextSelectableByMouse)
        l2.addWidget(b2)
        f2 = QtWidgets.QFormLayout()
        self.redirect = QtWidgets.QLineEdit()
        self.redirect.setPlaceholderText("leave empty — Yandex then uses the URL registered for your app")
        rrow = QtWidgets.QHBoxLayout()
        rrow.addWidget(self.redirect, 1)
        f2.addRow("Callback URL", rrow)
        l2.addLayout(f2)
        self.btn_consent = QtWidgets.QPushButton("Open the consent page")
        self.btn_consent.clicked.connect(self.open_consent)
        l2.addWidget(self.btn_consent)
        self.code = QtWidgets.QLineEdit()
        self.code.setPlaceholderText("paste the code shown by Yandex")
        l2.addWidget(self.code)
        self.btn_finish = QtWidgets.QPushButton("Finish")
        self.btn_finish.clicked.connect(self.finish_code)
        l2.addWidget(self.btn_finish)
        l2.addStretch(1)
        self.tabs.addTab(t2, "Confirmation code (recommended)")

        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QtWidgets.QPushButton("Close")
        self.btn_cancel.clicked.connect(self.reject)
        row.addWidget(self.btn_cancel)
        v.addLayout(row)

    # ---- helpers ----
    def _creds(self) -> tuple[str, str] | None:
        cid, csec = self.cid.text().strip(), self.csec.text().strip()
        if not cid or not csec:
            self.msg.setText("Both ClientID and secret are required.")
            self.tabs.setCurrentIndex(1)
            return None
        return cid, csec

    def open_consent(self):
        c = self._creds()
        if not c:
            return
        red = self.redirect.text().strip() or None
        url = E.code_authorize_url(c[0], red)
        self.url.setText(url)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        self.msg.setText("log in, press Allow, then paste the code you see "
                         "(or the ?code=… part of the address bar).")

    def finish_code(self):
        c = self._creds()
        if not c:
            return
        if not self.code.text().strip():
            self.msg.setText("paste the code from the browser page first.")
            return
        self.btn_finish.setEnabled(False)
        self.msg.setText("exchanging the code for a token…")
        self.codeworker = CodeWorker(c[0], c[1], E.parse_code(self.code.text()),
                                     self.redirect.text().strip() or None, self)
        self.codeworker.done.connect(self._ok)
        self.codeworker.failed.connect(self._bad_code)
        self.codeworker.start()

    def start_browser(self):
        c = self._creds()
        if not c:
            return
        self.btn_browser.setEnabled(False)
        self.msg.setText("waiting for you to confirm in the browser…")
        self.worker = AuthWorker(c[0], c[1], self)
        self.worker.url.connect(self.url.setText)
        self.worker.done.connect(self._ok)
        self.worker.failed.connect(self._bad)
        self.worker.start()

    def _ok(self):
        self.msg.setText("Authorized. Token installed.")
        self.authorized.emit()
        QtCore.QTimer.singleShot(700, self.accept)

    def _bad(self, why: str):
        self.msg.setText(f"failed: {why}")
        self.btn_browser.setEnabled(True)

    def _bad_code(self, why: str):
        self.msg.setText(f"failed: {why}")
        self.btn_finish.setEnabled(True)

    def reject(self):
        for w in (self.worker, self.codeworker):
            if w and w.isRunning():
                w.terminate()
        super().reject()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Yandex Disk — settings")
        self.setMinimumWidth(560)
        self.cfg = E.load_cfg()

        form = QtWidgets.QFormLayout()
        self.mount_edit = QtWidgets.QLineEdit(E.mountpoint(self.cfg))
        browse = QtWidgets.QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._pick)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.mount_edit)
        row.addWidget(browse)
        form.addRow("Mount point", row)

        store = QtWidgets.QLabel("Kept-offline files only — everything else lives in the cloud")
        store.setStyleSheet("color:#8b949e")
        store.setToolTip("There is no cache size to manage: a file is either kept on this "
                         "device or it lives in Yandex Disk. Bytes pulled in by opening a "
                         "file are cleared automatically within 10 minutes.")
        form.addRow("Stored here", store)

        self.autostart = QtWidgets.QCheckBox("Mount at login")
        self.autostart.setChecked(E.unit_enabled())
        form.addRow("", self.autostart)

        self.pinwatch = QtWidgets.QCheckBox("Re-download kept files if they ever disappear")
        self.pinwatch.setChecked(bool(self.cfg.get("pin_watch", True)))
        form.addRow("", self.pinwatch)

        self.token_edit = QtWidgets.QPlainTextEdit()
        self.token_edit.setPlaceholderText(
            'OAuth token JSON from "rclone authorize yandex" — leave empty to keep the current one')
        self.token_edit.setMaximumHeight(80)
        tok_state = "token configured" if E.has_token() else "NO TOKEN — paste one below"
        form.addRow("Token", QtWidgets.QLabel(tok_state))
        form.addRow("", self.token_edit)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(form)
        hint = QtWidgets.QLabel("Files kept on this device are stored in "
                                f"{os.path.join(E.cache_root(self.cfg), 'vfs')}")
        hint.setStyleSheet("color:#8b949e; font-size:11px")
        lay.addWidget(hint)
        lay.addWidget(btns)

    def _pick(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose mount point",
                                                       self.mount_edit.text())
        if d:
            self.mount_edit.setText(d)

    def _save(self):
        cfg = E.load_cfg()
        new_mp = self.mount_edit.text().strip()
        if new_mp:
            cfg["mountpoint"] = os.path.abspath(os.path.expanduser(new_mp))
        cfg["pin_watch"] = self.pinwatch.isChecked()
        if self.token_edit.toPlainText().strip():
            E.write_remote(self.token_edit.toPlainText().strip(), cfg)
        E.save_cfg(cfg)
        E.ensure_dirs(cfg)
        E.enable_autostart(self.autostart.isChecked(), cfg)
        self.accept()


def app_pixmap(size: int = 96) -> QtGui.QPixmap:
    """The Yandex Disk cloud, drawn from the bundled SVG (no theme lookup)."""
    try:
        from PySide6 import QtSvg
        renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(E.ICON_SVG.encode()))
        pm = QtGui.QPixmap(size, size)
        pm.fill(Qt.transparent)
        painter = QtGui.QPainter(pm)
        renderer.render(painter)
        painter.end()
        if not pm.isNull():
            return pm
    except Exception:                                     # noqa: BLE001
        pass
    path = os.path.join(os.path.expanduser("~/.local/share/icons/hicolor/scalable/apps"),
                        "yadisk.svg")
    pm = QtGui.QPixmap(path)
    if not pm.isNull():
        return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return QtGui.QIcon.fromTheme("folder-cloud").pixmap(size, size)


class AboutDialog(QtWidgets.QDialog):
    """About page: icon, name, version, release date, author."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from . import __date__, __version__
        self.setWindowTitle("About Yandex Disk")
        self.setMinimumWidth(430)
        released = ".".join(reversed(__date__.split("-")))  # 2026-10-26 -> 26.10.2026

        icon = QtWidgets.QLabel()
        icon.setPixmap(app_pixmap(96))
        icon.setAlignment(Qt.AlignCenter)

        title = QtWidgets.QLabel("Yandex.Disk GUI")
        f = title.font()
        f.setPointSize(f.pointSize() + 8)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)

        version = QtWidgets.QLabel(f"Version {__version__}")
        fb = version.font()
        fb.setPointSize(fb.pointSize() + 1)
        version.setFont(fb)
        version.setAlignment(Qt.AlignCenter)

        muted = self.palette().color(QtGui.QPalette.PlaceholderText).name()
        author = QtWidgets.QLabel(f"SUDALV · {released}")
        author.setStyleSheet(f"color:{muted}")
        author.setAlignment(Qt.AlignCenter)

        blurb = QtWidgets.QLabel(
            "Yandex Disk on Linux with real shadow files: every file and folder in "
            "the disk is visible in Dolphin, opens on demand, and can be kept "
            "offline or dropped back to the cloud.")
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignCenter)

        engines = QtWidgets.QLabel(
            "Engine: rclone FUSE mount · Qt 6 / PySide6 · KDE Dolphin indicator plugin\n"
            "Not affiliated with Yandex.")
        engines.setWordWrap(True)
        engines.setAlignment(Qt.AlignCenter)
        engines.setStyleSheet(f"color:{muted}; font-size:11px")

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(24, 22, 24, 16)
        v.setSpacing(4)
        v.addWidget(icon)
        v.addSpacing(6)
        v.addWidget(title)
        v.addWidget(version)
        v.addWidget(author)
        v.addSpacing(10)
        v.addWidget(blurb)
        v.addSpacing(10)
        v.addWidget(engines)
        v.addSpacing(12)
        v.addWidget(btns)


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Yandex Disk")
        self.setWindowIcon(QtGui.QIcon.fromTheme("yadisk"))
        self.resize(980, 620)
        self.cfg = E.load_cfg()
        self.scanner: ScanWorker | None = None
        self.worker: OpWorker | None = None
        self._rows = 0
        self.root = ""
        self._data: list[dict] = []
        self._sort_col = 0
        self._sort_desc = False

        self._build_ui()
        self._build_tray()
        self.refresh_status()
        QtCore.QTimer(self, timeout=self.refresh_status, interval=5000).start()
        QtCore.QTimer.singleShot(300, self.reload)

    # ---- ui ----

    def _build_ui(self):
        tb = QtWidgets.QToolBar("main")
        tb.setIconSize(QtCore.QSize(18, 18))
        tb.setMovable(False)
        self.addToolBar(tb)

        self.act_mount = QtGui.QAction(QtGui.QIcon.fromTheme("media-playback-start"), "Mount", self)
        self.act_mount.triggered.connect(self.toggle_mount)
        self.act_unmount = QtGui.QAction(QtGui.QIcon.fromTheme("media-playback-stop"), "Unmount", self)
        self.act_unmount.triggered.connect(self.do_unmount)
        tb.addAction(self.act_mount)
        tb.addAction(self.act_unmount)
        tb.addSeparator()

        act_pin = QtGui.QAction(QtGui.QIcon.fromTheme("cloud-download"), "Keep on this device", self)
        act_pin.setToolTip("Download the selection and keep it here for good (available offline)")
        act_pin.triggered.connect(lambda: self.run_op("pin"))
        tb.addAction(act_pin)

        act_free = QtGui.QAction(QtGui.QIcon.fromTheme("cloud-check"), "Available online only", self)
        act_free.setToolTip("Remove the local copy; the file stays in Yandex Disk and "
                            "downloads again when you open it")
        act_free.triggered.connect(lambda: self.run_op("evict"))
        tb.addAction(act_free)

        act_all = QtGui.QAction(QtGui.QIcon.fromTheme("edit-clear-all"), "Free up space (nothing kept offline)", self)
        act_all.triggered.connect(self.free_all_unpinned)
        tb.addAction(act_all)
        tb.addSeparator()

        act_refresh = QtGui.QAction(QtGui.QIcon.fromTheme("view-refresh"), "Refresh", self)
        act_refresh.triggered.connect(self.reload)
        tb.addAction(act_refresh)

        self.act_up = QtGui.QAction(QtGui.QIcon.fromTheme("go-up"), "Up", self)
        self.act_up.setToolTip("Parent folder")
        self.act_up.triggered.connect(
            lambda: self.navigate(os.path.dirname(self.root)))
        tb.addAction(self.act_up)

        self.act_deep = QtGui.QAction(QtGui.QIcon.fromTheme("view-list-tree"), "Deep scan", self)
        self.act_deep.setCheckable(True)
        self.act_deep.setToolTip("Also walk every subfolder (slow on a big disk)")
        self.act_deep.toggled.connect(lambda _on: self.reload())
        tb.addAction(self.act_deep)

        self.lbl_path = QtWidgets.QLabel("/")
        self.lbl_path.setStyleSheet("color:#8b949e; padding:0 8px")
        tb.addWidget(self.lbl_path)

        act_open = QtGui.QAction(QtGui.QIcon.fromTheme("folder-open"), "Open folder", self)
        act_open.triggered.connect(lambda: self.open_path(""))
        tb.addAction(act_open)

        act_settings = QtGui.QAction(QtGui.QIcon.fromTheme("configure"), "Settings", self)
        act_settings.triggered.connect(self.open_settings)
        tb.addAction(act_settings)

        act_about = QtGui.QAction(QtGui.QIcon.fromTheme("help-about"), "About", self)
        act_about.setToolTip("About Yandex.Disk GUI")
        act_about.triggered.connect(self.open_about)
        tb.addAction(act_about)

        act_account = QtGui.QAction(QtGui.QIcon.fromTheme("im-user"), "Account…", self)
        act_account.setToolTip("Authorize with your own Yandex OAuth app (needed when Yandex "
                               "answers 410 Gone for the bundled rclone client id)")
        act_account.triggered.connect(self.open_auth)
        tb.addAction(act_account)

        # header
        head = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(head)
        hl.setContentsMargins(12, 8, 12, 4)
        self.lbl_state = QtWidgets.QLabel("…")
        f = self.lbl_state.font()
        f.setPointSize(f.pointSize() + 2)
        f.setBold(True)
        self.lbl_state.setFont(f)
        hl.addWidget(self.lbl_state)
        hl.addStretch(1)
        self.lbl_counts = QtWidgets.QLabel("")
        self.lbl_counts.setStyleSheet("color:#8b949e")
        hl.addWidget(self.lbl_counts)

        # no cache bar on purpose: there is no cache budget to watch. A file is
        # either kept on this PC or it lives in the cloud.
        self.bar = None

        # filter + table
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter by name…")
        self.filter.textChanged.connect(self._apply_filter)
        filt_wrap = QtWidgets.QWidget()
        fw = QtWidgets.QHBoxLayout(filt_wrap)
        fw.setContentsMargins(12, 0, 12, 4)
        fw.addWidget(self.filter)
        self.cb_hide_local = QtWidgets.QCheckBox("cloud only")
        self.cb_hide_local.toggled.connect(self._apply_filter)
        fw.addWidget(self.cb_hide_local)
        self.cb_hide_cloud = QtWidgets.QCheckBox("on this PC")
        self.cb_hide_cloud.toggled.connect(self._apply_filter)
        fw.addWidget(self.cb_hide_cloud)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "State", "Modified"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(False)             # we sort ourselves: folders first
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        hh.setSectionsClickable(True)
        hh.setSortIndicator(0, Qt.AscendingOrder)
        hh.setSortIndicatorShown(True)
        hh.sectionClicked.connect(self._sort_by)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.doubleClicked.connect(self._activate_row)

        central = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(head)
        v.addWidget(filt_wrap)
        v.addWidget(self.table, 1)
        self.setCentralWidget(central)

        self.status = self.statusBar()
        self.status.showMessage("ready")

    def _build_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon.fromTheme("yadisk"), self)
        m = QtWidgets.QMenu()
        self.tray_status = m.addAction("…")
        self.tray_status.setEnabled(False)
        m.addSeparator()
        m.addAction("Open Yandex Disk", self.show_window)
        self.tray_mount = m.addAction("Mount", self.toggle_mount)
        m.addAction("Free up space (nothing kept offline)", self.free_all_unpinned)
        m.addAction("Settings…", self.open_settings)
        m.addAction("About Yandex.Disk GUI…", self.open_about)
        m.addAction("Account / authorize…", self.open_auth)
        m.addSeparator()
        m.addAction("Quit", QtWidgets.QApplication.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda r: self.show_window() if r == QtWidgets.QSystemTrayIcon.Trigger else None)
        self.tray.setToolTip("Yandex Disk")
        self.tray.show()
        self._tray_menu = m

    # ---- status ----

    def refresh_status(self):
        cfg = E.load_cfg()
        mounted = E.is_mounted(cfg)
        healthy, why = E.mount_healthy(cfg) if mounted else (False, "")
        if healthy:
            self.lbl_state.setText("● Mounted — shadow files active")
            self.lbl_state.setStyleSheet("color:#2ea043")
        elif mounted:
            self.lbl_state.setText("⚠ Mounted, but Yandex denies file access — use Account…")
            self.lbl_state.setStyleSheet("color:#e5534b")
            self.status.showMessage(why, 15000)
        else:
            self.lbl_state.setText("○ Not mounted")
            self.lbl_state.setStyleSheet("color:#d29922")
        self.act_mount.setEnabled(not mounted)
        self.act_unmount.setEnabled(mounted)
        if self.tray:
            self.tray_status.setText("Mounted" if mounted else "Not mounted")
            self.tray_mount.setText("Unmount" if mounted else "Mount")

        kept = len(E.pins(cfg))
        kept_bytes = 0
        if mounted:
            for rel in E.local_files():
                if E.is_pinned(rel, cfg):
                    kept_bytes += E.local_bytes(rel)
        kept_txt = (f"{kept} kept offline · {E.human_bytes(kept_bytes)}"
                    if kept else "nothing kept offline")
        if self.tray:
            self.tray.setToolTip(f"Yandex Disk — {'mounted' if mounted else 'not mounted'}\n"
                                 f"{kept_txt}")

    # ---- scanning ----

    def reload(self):
        """Scan the current folder only. A recursive walk of a 9.9 TB disk never
        finishes, which is why the table used to stay empty while the header
        happily said "mounted"."""
        if not E.is_mounted():
            self.table.setRowCount(0)
            self._data = []
            self._rows = 0
            self.lbl_counts.setText("mount to see files")
            return
        if self.scanner and self.scanner.isRunning():
            self.scanner.cancel()
            self.scanner.wait(2000)
        self.table.setRowCount(0)
        self._data = []
        self._rows = 0
        self.lbl_counts.setText("scanning…")
        deep = self.act_deep.isChecked()
        self.lbl_path.setText("/" + self.root if self.root else "/ (disk root)")
        self.scanner = ScanWorker(self.root, recursive=deep)
        self.scanner.batch.connect(self._add_rows)
        self.scanner.finished_scan.connect(self._scan_done)
        self.scanner.start()

    def navigate(self, rel: str):
        rel = (rel or "").strip("/")
        if rel == self.root:
            return
        self.root = rel
        self.filter.clear()
        self.reload()

    # column -> sort key; folders are grouped above files in every column
    _SORT_KEYS = {
        0: lambda e: (e["name"].casefold()),
        1: lambda e: e["size"],
        2: lambda e: e["state"],
        3: lambda e: e["mtime"],
    }

    def _sort_by(self, col: int):
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        self.table.horizontalHeader().setSortIndicator(
            col, Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder)
        self._rebuild()

    def _rebuild(self):
        key = self._SORT_KEYS.get(self._sort_col, self._SORT_KEYS[0])
        dirs = [e for e in self._data if e.get("dir_flag")]
        files = [e for e in self._data if not e.get("dir_flag")]
        dirs.sort(key=key, reverse=self._sort_desc)
        files.sort(key=key, reverse=self._sort_desc)
        self._fill(dirs + files)
        self._apply_filter()

    def _add_rows(self, rows):
        self._data.extend(rows)
        self._rebuild()
        self.lbl_counts.setText(f"{len(self._data)} files scanned…")

    def _fill(self, rows):
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for r, e in enumerate(rows):
            is_dir = bool(e.get("dir_flag"))
            name = QtWidgets.QTableWidgetItem(e["name"] + ("/" if is_dir else ""))
            name.setData(Qt.UserRole, e["rel"])
            name.setToolTip(e["rel"])
            if is_dir:
                f = name.font()
                f.setBold(True)
                name.setFont(f)
            size = QtWidgets.QTableWidgetItem(E.human_bytes(e["size"]))
            size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            label, color = STATE_STYLE.get(e["state"], (e["state"], "#8b949e"))
            st = QtWidgets.QTableWidgetItem(label)
            st.setForeground(QtGui.QColor(color))
            st.setData(Qt.UserRole + 1, e["state"])
            if E.is_pinned(e["rel"], self.cfg):
                st.setText(label + "  📌")
            mod = QtWidgets.QTableWidgetItem(time.strftime("%Y-%m-%d %H:%M",
                                                           time.localtime(e["mtime"])))
            for w in (name, size, st, mod):
                w.setFlags(w.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, size)
            self.table.setItem(r, 2, st)
            self.table.setItem(r, 3, mod)
        self.table.setUpdatesEnabled(True)
        self._rows = len(rows)

    def _scan_done(self, loc, cloud, lbytes):
        kept = sum(1 for r in E.local_files() if E.is_pinned(r, E.load_cfg()))
        self.lbl_counts.setText(f"{cloud} in the cloud · {kept} kept on this device "
                                f"({E.human_bytes(lbytes)})")

    # ---- actions ----

    def selected_rels(self) -> list[str]:
        rels = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            if it:
                rels.append(it.data(Qt.UserRole))
        return rels

    def _open_selected(self):
        rels = self.selected_rels()
        if rels:
            self.open_path(rels[0])

    def _activate_row(self, index):
        it = self.table.item(index.row(), 0)
        if not it:
            return
        rel = it.data(Qt.UserRole)
        if os.path.isdir(E.mount_path(rel)):
            self.navigate(rel)
        else:
            self.open_path(rel)

    def open_path(self, rel: str):
        p = E.mount_path(rel) if rel else E.mountpoint()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(p))

    def reveal(self, rel: str):
        import subprocess
        p = E.mount_path(rel)
        subprocess.Popen(["dolphin", "--select", p], start_new_session=True)

    def run_op(self, kind: str, rels=None):
        rels = self.selected_rels() if rels is None else rels
        if not rels:
            self.status.showMessage("select a file first", 4000)
            return
        if kind == "evict":
            from PySide6.QtWidgets import QMessageBox
            n = len(rels)
            if QMessageBox.question(self, "Available online only",
                                    f"Remove {n} item(s) from this PC?\n\n"
                                    "They stay visible in the folder and are downloaded "
                                    "again the next time you open them. Anything in the "
                                    "selection stops being kept offline.") != QMessageBox.Yes:
                return
        dlg = QtWidgets.QProgressDialog("working…", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(400)
        w = OpWorker(kind, rels, self)
        w.progress.connect(lambda d, t: (dlg.setMaximum(max(t, 1)), dlg.setValue(d)))
        dlg.canceled.connect(w.cancel)

        def fin(msg):
            dlg.reset()
            self.status.showMessage(msg, 8000)
            E.notify("Yandex Disk", msg)
            self.refresh_status()
            self.reload()

        w.done.connect(fin)
        w.failed.connect(lambda m: (dlg.reset(), self.status.showMessage(f"failed: {m}", 8000)))
        self.worker = w
        w.start()

    def free_all_unpinned(self):
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "Free up space",
                                "Remove every local copy that is not kept offline?\n\n"
                                "Files you marked \"keep on this device\" stay. Everything else "
                                "stays visible and re-downloads when opened.") != QMessageBox.Yes:
            return
        cfg = E.load_cfg()
        targets = [r for r in E.local_files() if not E.is_pinned(r, cfg)]
        if not targets:
            self.status.showMessage("nothing local to free", 4000)
            return
        self.run_op("evict", targets)

    def toggle_mount(self):
        cfg = E.load_cfg()
        try:
            if E.is_mounted(cfg):
                E.unmount(cfg)
            else:
                E.mount(cfg)
        except Exception as e:                                   # noqa: BLE001
            E.notify("Yandex Disk", f"mount failed: {e}")
            self.status.showMessage(f"mount failed: {e}", 10000)
        self.refresh_status()
        QtCore.QTimer.singleShot(700, self.reload)

    def do_unmount(self):
        E.unmount()
        self.refresh_status()
        self.table.setRowCount(0)
        self._data = []
        self._rows = 0

    def open_auth(self):
        dlg = AuthDialog(self)
        dlg.authorized.connect(self._after_auth)
        dlg.exec()

    def _after_auth(self):
        """A new token only takes effect after rclone restarts the mount."""
        cfg = E.load_cfg()
        try:
            if E.is_mounted(cfg):
                E.unmount(cfg)
        except Exception as ex:                               # noqa: BLE001
            print(f"unmount: {ex}", file=sys.stderr)
        self.status.showMessage("reconnecting with the new token …")
        try:
            E.mount(cfg)
        except Exception as ex:                               # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Yandex Disk", f"mount failed: {ex}")
        self.refresh_status()
        self.reload()

    def open_about(self):
        AboutDialog(self).exec()

    def open_settings(self):
        if SettingsDialog(self).exec():
            self.refresh_status()
            self.reload()

    def _apply_filter(self):
        needle = self.filter.text().strip().lower()
        only_cloud = self.cb_hide_local.isChecked()
        only_local = self.cb_hide_cloud.isChecked()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            st = self.table.item(r, 2)
            if not it or not st:
                continue
            state = st.data(Qt.UserRole + 1)
            show = not needle or needle in it.text().lower()
            if only_cloud and state == "local":
                show = False
            if only_local and state != "local":
                show = False
            self.table.setRowHidden(r, not show)

    def _menu(self, pos):
        rels = self.selected_rels()
        if not rels:
            return
        m = QtWidgets.QMenu(self)
        m.addAction(QtGui.QIcon.fromTheme("cloud-download"), "Keep on this device",
                    lambda: self.run_op("pin"))
        m.addAction(QtGui.QIcon.fromTheme("cloud-check"), "Available online only",
                    lambda: self.run_op("evict"))
        m.addSeparator()
        m.addAction(QtGui.QIcon.fromTheme("document-open"), "Open",
                    lambda: self.open_path(rels[0]))
        m.addAction(QtGui.QIcon.fromTheme("system-file-manager"), "Show in Dolphin",
                    lambda: self.reveal(rels[0]))
        m.addAction(QtGui.QIcon.fromTheme("edit-copy"), "Copy path",
                    lambda: QtWidgets.QApplication.clipboard().setText(E.mount_path(rels[0])))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def shutdown(self):
        """Stop worker threads before Qt destroys them (otherwise exit aborts)."""
        for w in (self.scanner, self.worker):
            try:
                if w and w.isRunning():
                    w.cancel()
                    w.wait(3000)
            except RuntimeError:
                pass

    def closeEvent(self, ev):
        if self.tray and self.tray.isVisible():
            ev.ignore()
            self.hide()
            self.tray.showMessage("Yandex Disk", "Still running in the tray",
                                  QtWidgets.QSystemTrayIcon.Information, 2500)
        else:
            self.shutdown()
            ev.accept()


def main(tray_only: bool = False) -> int:
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("yadisk")
    app.setApplicationDisplayName("Yandex Disk")
    app.setDesktopFileName("yadisk")
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    app.aboutToQuit.connect(win.shutdown)
    if not tray_only:
        win.show()
    elif not win.tray:
        win.show()
    return app.exec()

"""verbatim.tray — a system-tray icon to start/stop recording and open the UI.

Optional desktop extra (needs PySide6). Left-click (or right-click) opens the
menu. Auto-pops the floating overlay when recording starts, whoever started it
(tray, CLI or web).  Launch:  `verbatim tray`
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime

from . import core

# Pick the Qt platform plugin BEFORE PySide6 initialises it. Under a Wayland
# session Qt otherwise falls back to xcb, which segfaults inside libxcb on this
# KDE/Plasma 6 setup (SIGSEGV in xcb_wait_for_event). The "wayland;xcb" form is
# a Qt fallback list, so an X11 session still works.
if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    _HAVE_QT = True
except Exception:  # noqa: BLE001
    _HAVE_QT = False


def _icon(color: str):
    """Draw the Verbatim mark: a microphone capsule on a stand.

    Painted rather than loaded from a file so the glyph can take the state
    colour (idle grey / recording pink) and stay crisp at any tray size, with
    no asset path to resolve at runtime.
    """
    pix = QtGui.QPixmap(64, 64)
    pix.fill(QtCore.Qt.transparent)
    qp = QtGui.QPainter(pix)
    qp.setRenderHint(QtGui.QPainter.Antialiasing)
    c = QtGui.QColor(color)

    # Capsule (the mic body).
    qp.setPen(QtCore.Qt.NoPen)
    qp.setBrush(c)
    qp.drawRoundedRect(QtCore.QRectF(24, 8, 16, 30), 8, 8)

    # Cradle: an arc under the capsule, plus stem and base.
    pen = QtGui.QPen(c, 5, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap)
    qp.setPen(pen)
    qp.setBrush(QtCore.Qt.NoBrush)
    qp.drawArc(QtCore.QRectF(16, 20, 32, 30), 200 * 16, 140 * 16)
    qp.drawLine(32, 48, 32, 55)
    qp.drawLine(23, 56, 41, 56)
    qp.end()
    return QtGui.QIcon(pix)


if _HAVE_QT:

    class Tray(QtCore.QObject):
        _state = QtCore.Signal(object)      # marshals worker-thread result to UI

        def __init__(self, app):
            super().__init__()
            self.app = app
            self.rec = None
            self._state.connect(self._apply)
            # Parent both to self so Qt-side lifetimes follow the Python object.
            self.tray = QtWidgets.QSystemTrayIcon(_icon("#8a929e"), self)
            self.menu = QtWidgets.QMenu()
            self.act_toggle = self.menu.addAction("● Start recording", self._toggle)
            self.act_overlay = self.menu.addAction("Show overlay", self._overlay)
            self.menu.addAction("Open Verbatim (web)", self._web)
            self.menu.addSeparator()
            self.menu.addAction("Quit", self.app.quit)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self._activated)
            self.tray.setToolTip("Verbatim")
            self.tray.show()

            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._poll_async)
            self.timer.start(3000)
            self._poll_async()

        def _activated(self, reason):
            if reason == QtWidgets.QSystemTrayIcon.Trigger:      # left click
                self.menu.popup(QtGui.QCursor.pos())

        def _poll_async(self):
            def work():
                try:
                    mid = core.is_recording()
                except Exception:
                    mid = None
                self._state.emit(mid)
            threading.Thread(target=work, daemon=True).start()

        def _apply(self, mid):
            was = bool(self.rec)
            self.rec = mid
            if mid:
                self.tray.setIcon(_icon("#e0577b"))
                self.act_toggle.setText("■ Stop & save")
                self.tray.setToolTip("Verbatim — recording")
                if not was:                     # just started → pop the overlay
                    self._overlay()
            else:
                self.tray.setIcon(_icon("#8a929e"))
                self.act_toggle.setText("● Start recording")
                self.tray.setToolTip("Verbatim — idle")

        def _toggle(self):
            rec = bool(self.rec)

            def work():
                try:
                    if rec:
                        core.stop_meeting()
                        m = core.get_meeting("latest")
                        if m:
                            core.build_note(m.id, engine="none")
                    else:
                        core.start_meeting(
                            "Meeting " + datetime.now().strftime("%Y-%m-%d %H:%M"),
                            wait=True)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def _overlay(self):
            subprocess.Popen([sys.executable, "-m", "verbatim", "overlay"],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def _web(self):
            webbrowser.open("http://127.0.0.1:8777/")


def run() -> int:
    if not _HAVE_QT:
        print("The tray needs PySide6 (pip install PySide6).", file=sys.stderr)
        return 1
    # Platform is pinned at import time (see top of module); only fall back to
    # xcb when there is no Wayland session at all. Forcing xcb unconditionally
    # runs the X11 path under Plasma Wayland for no reason.
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available on this desktop.", file=sys.stderr)
        return 1
    # Keep a strong reference. `Tray(app)` alone is collected by Python while Qt
    # still owns the QSystemTrayIcon and QTimer and keeps posting queued signals
    # at it, so the next _state.emit() lands in qt_metacall on a dead wrapper
    # and segfaults inside PyObject_GetAttrString. Parenting to `app` also ties
    # the C++ lifetime to the application.
    tray = Tray(app)
    tray.setParent(app)
    app._verbatim_tray = tray  # belt and braces against GC
    return app.exec()

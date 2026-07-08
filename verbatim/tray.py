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

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    _HAVE_QT = True
except Exception:  # noqa: BLE001
    _HAVE_QT = False


def _icon(color: str):
    pix = QtGui.QPixmap(64, 64)
    pix.fill(QtCore.Qt.transparent)
    qp = QtGui.QPainter(pix)
    qp.setRenderHint(QtGui.QPainter.Antialiasing)
    qp.setPen(QtCore.Qt.NoPen)
    qp.setBrush(QtGui.QColor(color))
    qp.drawEllipse(14, 14, 36, 36)
    qp.end()
    return QtGui.QIcon(pix)


if _HAVE_QT:

    class Tray(QtCore.QObject):
        def __init__(self, app):
            super().__init__()
            self.app = app
            self.rec = None
            self.tray = QtWidgets.QSystemTrayIcon(_icon("#8a929e"))
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
                QtCore.QMetaObject.invokeMethod(
                    self, "_apply", QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG(object, mid))
            threading.Thread(target=work, daemon=True).start()

        @QtCore.Slot(object)
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
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray available on this desktop.", file=sys.stderr)
        return 1
    Tray(app)
    return app.exec()

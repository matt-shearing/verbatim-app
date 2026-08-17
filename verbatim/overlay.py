"""verbatim.overlay — a small always-on-top "recording" overlay + mini controller.

Optional desktop extra (needs PySide6). Shows at a glance whether Verbatim is
recording, with an elapsed timer and animated level bars, and lets you
start/stop without opening the web UI. Frameless, translucent, draggable, and
it remembers where you put it.  Launch:  `verbatim overlay`
"""
from __future__ import annotations

import json
import math
import os
import random
import sys
import threading
import time
from datetime import datetime

from . import core

_POS_FILE = core._VERBATIM_STATE / "overlay_pos.json"
_LOCK_FILE = core._VERBATIM_STATE / "overlay.lock"

# Same reason as tray.py: Qt's xcb fallback segfaults under this Wayland
# session, so pin the platform before PySide6 initialises it.
if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("QT_QPA_PLATFORM"):
    os.environ["QT_QPA_PLATFORM"] = "wayland;xcb"

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    _HAVE_QT = True
except Exception:  # noqa: BLE001
    _HAVE_QT = False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _single_instance() -> bool:
    """Best-effort guard so auto-launch doesn't stack overlays. True if we may run."""
    try:
        if _LOCK_FILE.exists():
            pid = int(_LOCK_FILE.read_text() or "0")
            if pid and _pid_alive(pid):
                return False
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception:
        return True


if _HAVE_QT:

    class _Poller(QtCore.QThread):
        """Polls voxtype for recording state off the UI thread (it can block)."""
        state = QtCore.Signal(object, object)  # (mid|None, started_ts|None)

        def __init__(self):
            super().__init__()
            self._run = True

        def run(self):
            last = object()
            while self._run:
                mid, started = None, None
                try:
                    mid = core.is_recording()
                    if mid:
                        m = core.get_meeting(mid)
                        started = m.started_at if m else None
                except Exception:
                    mid = None
                if (mid, started) != last:
                    last = (mid, started)
                    self.state.emit(mid, started)
                for _ in range(20):          # ~2s, but responsive to stop()
                    if not self._run:
                        break
                    time.sleep(0.1)

        def stop(self):
            self._run = False

    class Overlay(QtWidgets.QWidget):
        W, H = 246, 58

        def __init__(self):
            super().__init__(
                None,
                QtCore.Qt.FramelessWindowHint | QtCore.Qt.WindowStaysOnTopHint |
                QtCore.Qt.Tool)
            self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
            self.setFixedSize(self.W, self.H)
            self._rec = None
            self._since = None
            self._drag = None
            self._phase = 0.0
            self._bars = [0.25] * 14

            self.btn = QtWidgets.QPushButton(self)
            self.btn.setGeometry(self.W - 46, 12, 34, 34)
            self.btn.setCursor(QtCore.Qt.PointingHandCursor)
            self.btn.clicked.connect(self._toggle)
            self.closeBtn = QtWidgets.QPushButton("×", self)
            self.closeBtn.setGeometry(self.W - 20, 2, 18, 18)
            self.closeBtn.setCursor(QtCore.Qt.PointingHandCursor)
            self.closeBtn.clicked.connect(self.close)
            self._sync_button()

            self._restore_pos()
            self.anim = QtCore.QTimer(self)
            self.anim.timeout.connect(self._tick)
            self.anim.start(90)
            self.poller = _Poller()
            self.poller.state.connect(self._on_state)
            self.poller.start()

        # ── state ──
        def _on_state(self, mid, started):
            self._rec = mid
            self._since = started or (time.time() if mid else None)
            self.btn.setEnabled(True)
            self._sync_button()
            self.update()

        def _sync_button(self):
            rec = bool(self._rec)
            self.btn.setText("■" if rec else "●")
            self.btn.setToolTip("Stop & save" if rec else "Start recording")
            accent = "#e0577b" if rec else "#57c98a"
            self.btn.setStyleSheet(
                "QPushButton{background:%s;color:#fff;border:none;border-radius:17px;"
                "font-size:15px;font-weight:bold}QPushButton:disabled{background:#555}"
                % accent)
            self.closeBtn.setStyleSheet(
                "QPushButton{background:transparent;color:#98a1b2;border:none;"
                "font-size:14px}QPushButton:hover{color:#e0577b}")

        def _toggle(self):
            self.btn.setEnabled(False)
            self.btn.setText("…")
            rec = bool(self._rec)

            def work():
                try:
                    if rec:
                        core.stop_meeting()
                        try:
                            m = core.get_meeting("latest")
                            if m:
                                core.build_note(m.id, engine="none")
                        except Exception:
                            pass
                    else:
                        core.start_meeting(
                            "Meeting " + datetime.now().strftime("%Y-%m-%d %H:%M"),
                            wait=True)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

        # ── painting ──
        def _tick(self):
            self._phase += 0.35
            if self._rec:
                self._bars = self._bars[1:] + [random.uniform(0.15, 1.0)]
            else:
                self._bars = [max(0.12, b * 0.85) for b in self._bars]
            self.update()

        def paintEvent(self, e):
            qp = QtGui.QPainter(self)
            qp.setRenderHint(QtGui.QPainter.Antialiasing)
            rec = bool(self._rec)
            qp.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 26), 1))
            qp.setBrush(QtGui.QColor(24, 26, 32, 236))
            qp.drawRoundedRect(QtCore.QRectF(1, 1, self.W - 2, self.H - 2), 14, 14)

            cx, cy = 20, self.H / 2
            qp.setPen(QtCore.Qt.NoPen)
            if rec:
                pulse = (math.sin(self._phase) + 1) / 2
                qp.setBrush(QtGui.QColor(224, 87, 123, int(70 * pulse)))
                qp.drawEllipse(QtCore.QPointF(cx, cy), 11 - 3 * pulse, 11 - 3 * pulse)
                qp.setBrush(QtGui.QColor(224, 87, 123))
            else:
                qp.setBrush(QtGui.QColor(120, 128, 140))
            qp.drawEllipse(QtCore.QPointF(cx, cy), 5, 5)

            qp.setPen(QtGui.QColor(231, 233, 238))
            f = qp.font(); f.setPixelSize(12); f.setBold(True); qp.setFont(f)
            qp.drawText(QtCore.QRectF(34, 8, 80, 16),
                        QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft,
                        "REC" if rec else "Verbatim")
            f.setPixelSize(13); f.setBold(False); qp.setFont(f)
            qp.setPen(QtGui.QColor(152, 161, 178))
            qp.drawText(QtCore.QRectF(34, 30, 80, 16),
                        QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self._elapsed())

            x0, w, gap = 118, 3, 2
            base = self.H / 2
            qp.setBrush(QtGui.QColor(224, 87, 123) if rec else QtGui.QColor(90, 96, 108))
            for i, b in enumerate(self._bars):
                x = x0 + i * (w + gap)
                if x > self.W - 54:
                    break
                h = 6 + b * 26
                qp.drawRoundedRect(QtCore.QRectF(x, base - h / 2, w, h), 1.5, 1.5)

        def _elapsed(self) -> str:
            if not self._rec or not self._since:
                return "idle"
            s = max(0, int(time.time() - self._since))
            return f"{s // 60:02d}:{s % 60:02d}"

        # ── drag + persistence ──
        def mousePressEvent(self, e):
            if e.button() == QtCore.Qt.LeftButton:
                self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

        def mouseMoveEvent(self, e):
            if self._drag is not None:
                self.move(e.globalPosition().toPoint() - self._drag)

        def mouseReleaseEvent(self, e):
            self._drag = None
            self._save_pos()

        def _restore_pos(self):
            try:
                d = json.loads(_POS_FILE.read_text())
                self.move(int(d["x"]), int(d["y"]))
                return
            except Exception:
                pass
            scr = QtWidgets.QApplication.primaryScreen().availableGeometry()
            self.move(scr.right() - self.W - 24, scr.bottom() - self.H - 40)

        def _save_pos(self):
            try:
                _POS_FILE.parent.mkdir(parents=True, exist_ok=True)
                _POS_FILE.write_text(json.dumps({"x": self.x(), "y": self.y()}))
            except Exception:
                pass

        def closeEvent(self, e):
            try:
                self.poller.stop(); self.poller.wait(600)
            except Exception:
                pass
            try:
                _LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            e.accept()


def run(force: bool = False) -> int:
    if not _HAVE_QT:
        print("The overlay needs PySide6 (pip install PySide6).", file=sys.stderr)
        return 1
    if not force and not _single_instance():
        print("Overlay already running.", file=sys.stderr)
        return 0
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")  # XWayland: reliable move + on-top
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    w = Overlay()
    w.show()
    return app.exec()

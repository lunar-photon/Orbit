#!/usr/bin/env python3
"""Orbit - the computer-side companion for the Orbit phone app.

Tray icon + menu, a dashboard window (Overview / Files / Send), the phone file
browser, the laser-pointer overlay (UDP 127.0.0.1:1763) and a single-instance
lock. Talks only to the local Orbit service at http://127.0.0.1:1760.
"""
import argparse
import fcntl
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from urllib.parse import quote

from PyQt6.QtCore import (Qt, QTimer, QThread, QObject, pyqtSignal, QSize,
                          QRectF, QPointF, QByteArray)
from PyQt6.QtGui import (QActionGroup, QIcon, QFont, QColor, QPainter, QPixmap, QPainterPath,
                         QPainterPathStroker, QTransform, QFontDatabase, QPen,
                         QRadialGradient, QShortcut, QKeySequence, QDragEnterEvent,
                         QDropEvent, QGuiApplication)
from PyQt6.QtWidgets import (QApplication, QSystemTrayIcon, QMenu, QMainWindow, QWidget,
                             QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QLabel,
                             QLineEdit, QFileDialog, QMessageBox, QHeaderView, QListWidget,
                             QListWidgetItem, QAbstractItemView, QInputDialog, QProgressBar,
                             QFrame, QStackedWidget, QTreeWidget, QTreeWidgetItem,
                             QScrollArea, QButtonGroup, QComboBox, QDialog, QSizePolicy)
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtNetwork import QLocalServer

APP_NAME = "Orbit"
APP_VERSION = "2.0"
DAEMON_URL = "http://127.0.0.1:1760"
LASER_PORT = 1763
PHONE_ROOT = "/storage/emulated/0"
PHONE_DOWNLOADS = "/storage/emulated/0/Download"

UID = os.getuid()
RUN_DIR = f"/run/user/{UID}" if os.path.isdir(f"/run/user/{UID}") else os.path.expanduser("~/.config/orbit")
LOCK_FILE = os.path.join(RUN_DIR, "orbit-tray.lock")
SOCK_PATH = os.path.join(RUN_DIR, "orbit-tray.sock")
CONFIG_DIRS = [os.path.expanduser("~/.config/orbit"), os.path.expanduser("~/.config/pc-authenticator")]


def _unique(seq):
    out = []
    for s in seq:
        if s not in out:
            out.append(s)
    return out


SCRIPT_DIRS = _unique([os.path.dirname(os.path.abspath(__file__)),
                       os.path.dirname(os.path.realpath(__file__))])


# ==========================================
# Local service access
# ==========================================

# The service only trusts local tools that present this user's key (a 0600 file it
# rewrites on each start). Read it fresh per request and attach it only to requests
# for the local service, never to the phone or anything else.
def local_token():
    for d in CONFIG_DIRS:
        try:
            with open(os.path.join(d, "local_token")) as f:
                tok = f.read().strip()
            if tok:
                return tok
        except OSError:
            continue
    return ""


class _LocalTokenHandler(urllib.request.BaseHandler):
    def http_request(self, req):
        if req.host.split(':')[0] in ('127.0.0.1', 'localhost'):
            req.add_header('X-Local-Token', local_token())
        return req


urllib.request.install_opener(urllib.request.build_opener(_LocalTokenHandler))


def describe_error(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = json.loads(e.read().decode('utf-8', 'replace'))
            msg = body.get('message') or body.get('error')
            if msg:
                return f"{msg} ({e.code})"
        except Exception:
            pass
        return f"HTTP {e.code} {e.reason}"
    if isinstance(e, urllib.error.URLError):
        return f"Orbit service not reachable ({e.reason})"
    return str(e)


def fetch_json(endpoint, timeout=3):
    try:
        req = urllib.request.Request(f"{DAEMON_URL}{endpoint}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"error": describe_error(e)}


def post_json(endpoint, data=None, timeout=5):
    try:
        body = json.dumps(data or {}).encode('utf-8')
        req = urllib.request.Request(f"{DAEMON_URL}{endpoint}", data=body,
                                     headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"error": describe_error(e)}


def find_tool(names):
    for n in names:
        for d in [os.path.expanduser('~/.local/bin')] + SCRIPT_DIRS:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        p = shutil.which(n)
        if p:
            return p
    return None


SEND_TOOLS = ('orbit-send', 'pc-connect-send')

_tray_lock_fd = None


def acquire_single_instance_lock():
    global _tray_lock_fd
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        _tray_lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(_tray_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _tray_lock_fd.write(str(os.getpid()))
        _tray_lock_fd.flush()
        return True
    except (IOError, OSError):
        return False


def forward_to_running(cmd):
    """Hand a request (show / browse / send) to the instance that holds the lock."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(SOCK_PATH)
        s.sendall((json.dumps(cmd) + "\n").encode())
        s.close()
        return True
    except OSError:
        return False


def format_size(n):
    if not n:
        return "-"
    n = float(n)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024.0:
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def format_time(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def ago(ts):
    if not ts:
        return "Never"
    d = int(time.time() - ts)
    if d < 0:
        d = 0
    if d < 60:
        return "Just now" if d < 5 else f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


_notify_icon = None


def notify(title, body, icon=None):
    global _notify_icon
    if icon is None:
        if _notify_icon is None:
            _notify_icon = 'smartphone'
            if QIcon.hasThemeIcon('orbit'):
                _notify_icon = 'orbit'
            else:
                for d in SCRIPT_DIRS:
                    p = os.path.join(d, 'icons', 'orbit.svg')
                    if os.path.isfile(p):
                        _notify_icon = p
                        break
        icon = _notify_icon
    try:
        subprocess.Popen(['notify-send', '-a', APP_NAME, '-i', icon, title, body],
                         stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except Exception:
        pass


# ==========================================
# Main-thread helpers
# ==========================================

class _Invoker(QObject):
    sig = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.sig.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, fn):
        try:
            fn()
        except Exception:
            traceback.print_exc()


_INVOKER = None


def on_main(fn):
    _INVOKER.sig.emit(fn)


def run_async(work, done=None):
    def target():
        try:
            res = work()
        except Exception as e:
            res = {"error": describe_error(e)}
        if done is not None:
            on_main(lambda: done(res))
    threading.Thread(target=target, daemon=True).start()


# ==========================================
# Design tokens, fonts, stylesheet
# ==========================================

class C:
    GROUND = "#0D0F12"
    SURFACE = "#15181D"
    RAISED = "#1D2128"
    PRESSED = "#242932"
    HAIRLINE = "#262B33"
    STROKE = "#2E343E"
    T1 = "#ECEEF1"
    T2 = "#A6AEBB"
    T3 = "#7D8593"
    ACCENT = "#8EA8FF"
    ACCENT_HOVER = "#A3B8FF"
    ACCENT_PRESSED = "#7A93EA"
    ONLINE = "#46D39A"
    WARN = "#F2C14E"
    DANGER = "#FF7A7A"
    FILES = "#7DD3FC"
    CLIP = "#C4B5FD"
    RING = "#FDBA74"
    MEDIA = "#F9A8D4"
    TERM = "#86EFAC"


UI_FONT = "Sans Serif"
MONO_FONT = "monospace"


def load_fonts():
    global UI_FONT, MONO_FONT
    dirs = [os.path.expanduser('~/.local/share/fonts/orbit')]
    dirs += [os.path.normpath(os.path.join(d, '..', 'android', 'res', 'font')) for d in SCRIPT_DIRS]
    dirs += SCRIPT_DIRS + [os.path.join(d, 'fonts') for d in SCRIPT_DIRS]
    for name in ('geist_regular.ttf', 'geist_medium.ttf', 'geist_semibold.ttf',
                 'geist_mono_regular.ttf', 'geist_mono_medium.ttf'):
        for d in dirs:
            p = os.path.join(d, name)
            if os.path.isfile(p) and QFontDatabase.addApplicationFont(p) != -1:
                break
    fams = QFontDatabase.families()
    UI_FONT = 'Geist' if 'Geist' in fams else QApplication.font().family()
    MONO_FONT = 'Geist Mono' if 'Geist Mono' in fams else \
        QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()


QSS = """
* { font-family: "@UI"; font-size: 13px; color: @T1; }
QMainWindow, QDialog, QMessageBox, QInputDialog { background: @GROUND; }
QWidget#Root, QWidget#Page { background: @GROUND; }
QWidget#Sidebar { background: @GROUND; border-right: 1px solid @HAIRLINE; }
QLabel { background: transparent; }
QToolTip { background: @RAISED; color: @T1; border: 1px solid @STROKE; padding: 6px 8px; border-radius: 8px; }

QFrame#Card { background: @SURFACE; border: 1px solid @HAIRLINE; border-radius: 18px; }
QFrame#Chip { background: @RAISED; border: 1px solid @HAIRLINE; border-radius: 15px; }
QFrame#Rule { background: @HAIRLINE; border: none; max-height: 1px; min-height: 1px; }
QFrame#Row { background: transparent; border: none; }
QFrame#DropZone { background: @SURFACE; border: 1.5px dashed @STROKE; border-radius: 18px; }
QFrame#DropZone[active="true"] { background: #1A1F2C; border: 1.5px dashed @ACCENT; }

QLabel[role="brand"] { font-size: 17px; font-weight: 600; letter-spacing: 0.2px; }
QLabel[role="h1"] { font-size: 19px; font-weight: 600; }
QLabel[role="title"] { font-size: 14px; font-weight: 600; }
QLabel[role="muted"] { color: @T2; }
QLabel[role="faint"] { color: @T3; font-size: 12px; }
QLabel[role="section"] { color: @T3; font-size: 12px; font-weight: 500; padding-left: 4px; }
QLabel[role="mono"] { font-family: "@MONO"; color: @T2; font-size: 12px; }
QLabel[role="value"] { color: @T1; font-weight: 500; }

QPushButton { background: @RAISED; border: 1px solid @STROKE; border-radius: 12px; padding: 0 14px;
              min-height: 38px; color: @T1; font-weight: 500; }
QPushButton:hover { background: @PRESSED; border-color: #3A414D; }
QPushButton:pressed { background: @SURFACE; }
QPushButton:disabled { color: @T3; background: @SURFACE; border-color: @HAIRLINE; }
QPushButton[kind="primary"] { background: @ACCENT; color: @GROUND; border: 1px solid @ACCENT; font-weight: 600; }
QPushButton[kind="primary"]:hover { background: @ACCENT_HOVER; border-color: @ACCENT_HOVER; }
QPushButton[kind="primary"]:pressed { background: @ACCENT_PRESSED; }
QPushButton[kind="primary"]:disabled { background: @RAISED; color: @T3; border-color: @HAIRLINE; }
QPushButton[kind="danger"] { background: #2E1A1C; color: @DANGER; border: 1px solid #4A2629; }
QPushButton[kind="danger"]:hover { background: #3A1F22; border-color: #6A3236; }
QPushButton[kind="ghost"] { background: transparent; border: 1px solid transparent; color: @T2; }
QPushButton[kind="ghost"]:hover { background: @RAISED; color: @T1; }
QPushButton[kind="icon"] { min-width: 38px; max-width: 38px; padding: 0; }
QPushButton[size="small"] { min-height: 32px; border-radius: 10px; padding: 0 12px; font-size: 12px; }
QPushButton[size="small"][kind="icon"] { min-width: 32px; max-width: 32px; padding: 0; }
QPushButton#Nav { text-align: left; padding-left: 12px; background: transparent; border: 1px solid transparent;
                  color: @T2; border-radius: 12px; min-height: 40px; }
QPushButton#Nav:hover { background: @SURFACE; color: @T1; }
QPushButton#Nav:checked { background: @RAISED; color: @T1; border-color: @HAIRLINE; }
QPushButton#Tile { background: @SURFACE; border: 1px solid @HAIRLINE; border-radius: 16px; padding: 0;
                   min-height: 74px; text-align: left; }
QPushButton#Tile:hover { background: #1A1E24; border-color: @STROKE; }
QPushButton#Tile:pressed { background: @RAISED; }
QPushButton#Tile:disabled { background: @GROUND; border-color: @HAIRLINE; }
QPushButton#Photo { background: @SURFACE; border: 1px solid @HAIRLINE; border-radius: 14px; padding: 0;
                    text-align: left; min-width: 204px; max-width: 204px; min-height: 196px; max-height: 196px; }
QPushButton#Photo:hover { background: #1A1E24; border-color: @STROKE; }

QLineEdit, QComboBox { background: @SURFACE; border: 1px solid @STROKE; border-radius: 12px; padding: 0 12px;
                       min-height: 38px; selection-background-color: @ACCENT; selection-color: @GROUND; }
QLineEdit:focus, QComboBox:focus { border-color: @ACCENT; }
QLineEdit#Path { font-family: "@MONO"; font-size: 12px; color: @T2; }
QLineEdit#Path:focus { color: @T1; }
QComboBox::drop-down { border: none; width: 30px; }
QComboBox::down-arrow { image: url(@CHEVRON); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background: @RAISED; border: 1px solid @STROKE; outline: none; padding: 4px;
                              selection-background-color: @PRESSED; selection-color: @T1; }

QTreeWidget#Files { background: @SURFACE; border: 1px solid @HAIRLINE; border-radius: 16px; padding: 4px 6px;
                    outline: none; }
QTreeWidget#Files[dropping="true"] { border: 1.5px dashed @ACCENT; background: #171C27; }
QTreeWidget#Files::item { border: none; padding-left: 4px; color: @T1; }
QTreeWidget#Files::item:hover { background: @RAISED; }
QTreeWidget#Files::item:selected { background: #1F2740; color: @T1; }
QHeaderView { background: transparent; border: none; }
QHeaderView::section { background: @SURFACE; color: @T3; border: none; border-bottom: 1px solid @HAIRLINE;
                       padding: 10px 8px; font-size: 12px; font-weight: 500; }

QListWidget#Places, QListWidget#Recent { background: transparent; border: none; outline: none; }
QListWidget#Places::item { min-height: 38px; padding-left: 8px; border-radius: 10px; color: @T2; }
QListWidget#Places::item:hover { background: @RAISED; color: @T1; }
QListWidget#Places::item:selected { background: @RAISED; color: @T1; }
QListWidget#Recent::item { border: none; }
QListWidget#Recent::item:selected, QListWidget#Recent::item:hover { background: transparent; }

QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget#ScrollBody { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 6px 2px; }
QScrollBar::handle:vertical { background: @STROKE; border-radius: 3px; min-height: 32px; }
QScrollBar::handle:vertical:hover { background: #3A414D; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px 6px; }
QScrollBar::handle:horizontal { background: @STROKE; border-radius: 3px; min-width: 32px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: none; background: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QMenu { background: @RAISED; border: 1px solid @STROKE; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 8px 18px 8px 12px; border-radius: 8px; color: @T1; background: transparent; }
QMenu::item:selected { background: @PRESSED; }
QMenu::item:disabled { color: @T3; }
QMenu::icon { padding-left: 10px; }
QMenu::separator { height: 1px; background: @HAIRLINE; margin: 6px 8px; }

QProgressBar { background: @RAISED; border: none; border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: @ACCENT; border-radius: 3px; }
QMessageBox QLabel { color: @T1; }
"""


def chevron_file():
    d = os.path.join(tempfile.gettempdir(), f'orbit-ui-{UID}')
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, 'chevron-down.svg')
    with open(p, 'w') as f:
        f.write(icon_svg('chevron-down', C.T2))
    return p


def build_qss():
    s = QSS
    tokens = {
        '@UI': UI_FONT, '@MONO': MONO_FONT, '@CHEVRON': chevron_file(),
        '@GROUND': C.GROUND, '@SURFACE': C.SURFACE, '@RAISED': C.RAISED, '@PRESSED': C.PRESSED,
        '@HAIRLINE': C.HAIRLINE, '@STROKE': C.STROKE, '@T1': C.T1, '@T2': C.T2, '@T3': C.T3,
        '@ACCENT_HOVER': C.ACCENT_HOVER, '@ACCENT_PRESSED': C.ACCENT_PRESSED, '@ACCENT': C.ACCENT,
        '@DANGER': C.DANGER,
    }
    for k in sorted(tokens, key=len, reverse=True):
        s = s.replace(k, tokens[k])
    return s


# ==========================================
# Icons (Lucide-style line icons, 24px grid, 1.75 stroke)
# ==========================================

_FOLDER = '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>'
_FILE = '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>'
ICONS = {
    'smartphone': '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    'monitor': '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
    'folder': _FOLDER,
    'folder-plus': _FOLDER + '<path d="M12 10v6"/><path d="M9 13h6"/>',
    'file': _FILE,
    'file-text': _FILE + '<path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    'image': '<rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    'film': '<path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.87a.5.5 0 0 0-.752-.432L16 10.5"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    'music': '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
    'archive': '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    'package': '<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/><path d="m3.3 7 8.7 5 8.7-5"/>',
    'code': '<path d="m16 18 6-6-6-6"/><path d="m8 6-6 6 6 6"/>',
    'download': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    'upload': '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    'trash': '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    'refresh': '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    'arrow-left': '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'arrow-up': '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    'bell': '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    'bell-off': '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742"/><path d="m2 2 20 20"/><path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05"/>',
    'clipboard': '<rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>',
    'send': '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
    'lock': '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    'globe': '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    'plus': '<path d="M5 12h14"/><path d="M12 5v14"/>',
    'link': '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    'unlink': '<path d="m18.84 12.25 1.72-1.71h-.02a5.004 5.004 0 0 0-.12-7.07 5.006 5.006 0 0 0-6.95 0l-1.72 1.71"/><path d="m5.17 11.75-1.71 1.71a5.004 5.004 0 0 0 .12 7.07 5.006 5.006 0 0 0 6.95 0l1.71-1.71"/><line x1="8" x2="8" y1="2" y2="5"/><line x1="2" x2="5" y1="8" y2="8"/><line x1="16" x2="16" y1="19" y2="22"/><line x1="19" x2="22" y1="16" y2="16"/>',
    'battery': '<rect width="16" height="10" x="2" y="7" rx="2" ry="2"/><line x1="22" x2="22" y1="11" y2="13"/>',
    'battery-charging': '<path d="M15 7h1a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-2"/><path d="M6 7H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h1"/><path d="m11 7-3 5h4l-3 5"/><line x1="22" x2="22" y1="11" y2="13"/>',
    'wifi': '<path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/>',
    'hard-drive': '<line x1="22" x2="2" y1="12" y2="12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" x2="6.01" y1="16" y2="16"/><line x1="10" x2="10.01" y1="16" y2="16"/>',
    'camera': '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    'grid': '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
    'eye': '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    'clock': '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'shield': '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-.76c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 4.03 17 5.24 19 5.24a1 1 0 0 1 1 1z"/>',
    'copy': '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    'check': '<path d="M20 6 9 17l-5-5"/>',
    'x': '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    'alert': '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y1="16" y2="16"/>',
    'map-pin': '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
    'radio': '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/><circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
    'activity': '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    'scan-face': '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><path d="M9 9h.01"/><path d="M15 9h.01"/>',
    'home': '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
}


def icon_svg(name, color, stroke=1.75):
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
            f'fill="none" stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round">{ICONS.get(name, ICONS["file"])}</svg>')


def render_svg(svg, w, h, dpr=1.0):
    r = QSvgRenderer(QByteArray(svg.encode('utf-8')))
    pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, pm.width(), pm.height()))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


_icon_cache = {}


def line_pixmap(name, color, size=20, dpr=None):
    if dpr is None:
        app = QGuiApplication.instance()
        dpr = app.devicePixelRatio() if app else 1.0
    return render_svg(icon_svg(name, color), size, size, max(1.0, dpr))


def line_icon(name, color=C.T2, size=20):
    key = (name, color, size)
    ic = _icon_cache.get(key)
    if ic is None:
        ic = QIcon()
        for scale in (1.0, 2.0):
            ic.addPixmap(line_pixmap(name, color, size, scale), QIcon.Mode.Normal)
            ic.addPixmap(line_pixmap(name, C.T3, size, scale), QIcon.Mode.Disabled)
        _icon_cache[key] = ic
    return ic


# The Orbit mark: a planet, its circular orbit, and a satellite on it (a gap in the orbit
# around the satellite shows it moving along)
APP_ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200"><rect width="200" height="200" rx="46" fill="#15181D"/><path d="M73.00,100.00a27.00,27.00 0 1,0 54.00,0a27.00,27.00 0 1,0 -54.00,0" fill="#ECEEF1"/><path d="M158.97,80.84A62.00,62.00 0 1,1 125.22,43.36" fill="none" stroke="#8EA8FF" stroke-width="9.0" stroke-linecap="round"/><path d="M134.07,58.51a12.00,12.00 0 1,0 24.00,0a12.00,12.00 0 1,0 -24.00,0" fill="#8EA8FF"/></svg>')

TRAY_MARK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 200 200"><path d="M67.06,100.00a32.94,32.94 0 1,0 65.88,0a32.94,32.94 0 1,0 -65.88,0" fill="COLOR"/><path d="M171.94,76.63A75.64,75.64 0 1,1 130.77,30.90" fill="none" stroke="COLOR" stroke-width="14.6" stroke-linecap="round"/><path d="M137.91,49.39a18.30,18.30 0 1,0 36.60,0a18.30,18.30 0 1,0 -36.60,0" fill="COLOR"/></svg>')


def app_icon():
    ic = QIcon()
    for s in (16, 22, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(render_svg(APP_ICON_SVG, s, s))
    return ic


def tray_icon_mono(color=C.T1):
    svg = TRAY_MARK_SVG.replace('COLOR', QColor(color).name())
    ic = QIcon()
    for s in (16, 22, 24, 32, 44, 48, 64, 128):
        ic.addPixmap(render_svg(svg, s, s))
    return ic


# ==========================================
# Small widgets
# ==========================================

def label(text="", role=None, wrap=False):
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    if wrap:
        lb.setWordWrap(True)
    return lb


def button(text="", icon=None, kind=None, size=None, color=None, tip=None):
    b = QPushButton(text)
    if kind:
        b.setProperty("kind", kind)
    if size:
        b.setProperty("size", size)
    if icon:
        if color is None:
            color = C.GROUND if kind == "primary" else (C.DANGER if kind == "danger" else C.T1)
        b.setIcon(line_icon(icon, color, 18))
        b.setIconSize(QSize(18, 18) if size != "small" else QSize(16, 16))
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def rule():
    f = QFrame()
    f.setObjectName("Rule")
    return f


class Card(QFrame):
    def __init__(self, parent=None, margins=16, spacing=12):
        super().__init__(parent)
        self.setObjectName("Card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(margins, margins, margins, margins)
        self.lay.setSpacing(spacing)


class IconTile(QWidget):
    """Rounded square tinted at ~12% with the line icon in the full tint."""

    def __init__(self, icon, tint=C.ACCENT, size=40, parent=None):
        super().__init__(parent)
        self._icon, self._tint, self._size = icon, tint, size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_icon(self, icon, tint=None):
        self._icon = icon
        if tint:
            self._tint = tint
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg = QColor(self._tint)
        bg.setAlphaF(0.13)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        s = self._size
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.3, s * 0.3)
        isz = int(s * 0.5)
        pm = line_pixmap(self._icon, self._tint, isz, self.devicePixelRatioF())
        off = (s - isz) / 2
        p.drawPixmap(QPointF(off, off), pm)


class Dot(QWidget):
    def __init__(self, color=C.T3, size=8, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(size + 6, size + 6)
        self._d = size

    def set_color(self, color):
        self._color = color
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QColor(self._color)
        halo = QColor(c)
        halo.setAlphaF(0.22)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        p.setBrush(c)
        o = (self.width() - self._d) / 2
        p.drawEllipse(QRectF(o, o, self._d, self._d))


class Chip(QFrame):
    """Pill: optional dot or icon + word."""

    def __init__(self, text="", icon=None, dot=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Chip")
        self.setFixedHeight(30)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 12, 0)
        lay.setSpacing(6)
        self.dot = Dot(dot or C.T3, 7) if dot is not None else None
        self.icon_lbl = None
        if self.dot:
            lay.addWidget(self.dot)
        if icon:
            self.icon_lbl = QLabel()
            lay.addWidget(self.icon_lbl)
            self.set_icon(icon, C.T2)
        self.text = label(text)
        self.text.setStyleSheet("font-size: 12px; font-weight: 500;")
        lay.addWidget(self.text)

    def set_icon(self, icon, color):
        if self.icon_lbl:
            self.icon_lbl.setPixmap(line_pixmap(icon, color, 16))

    def set(self, text, color=None):
        self.text.setText(text)
        if color and self.dot:
            self.dot.set_color(color)


class ActionTile(QPushButton):
    def __init__(self, icon, tint, title, subtitle, parent=None):
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(74)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(12)
        self.tile = IconTile(icon, tint, 40)
        lay.addWidget(self.tile)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addStretch()
        self.title = label(title, "value")
        self.sub = label(subtitle, "faint")
        for w in (self.title, self.sub):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            col.addWidget(w)
        col.addStretch()
        lay.addLayout(col, 1)

    def set_text(self, title, subtitle=None, icon=None):
        self.title.setText(title)
        if subtitle is not None:
            self.sub.setText(subtitle)
        if icon:
            self.tile.set_icon(icon)


class DropZone(QFrame):
    filesDropped = pyqtSignal(list)
    chooseClicked = pyqtSignal()

    def __init__(self, title, hint, button_text="Choose files", parent=None):
        super().__init__(parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 28, 24, 28)
        lay.setSpacing(10)
        lay.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        self.tile = IconTile('upload', C.FILES, 52)
        row.addWidget(self.tile)
        row.addStretch()
        lay.addLayout(row)
        self.title = label(title, "title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint = label(hint, "muted", wrap=True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.hint)
        brow = QHBoxLayout()
        brow.addStretch()
        self.btn = button(button_text, 'plus', 'primary')
        self.btn.clicked.connect(self.chooseClicked.emit)
        brow.addWidget(self.btn)
        brow.addStretch()
        lay.addSpacing(4)
        lay.addLayout(brow)
        lay.addStretch()

    def _set_active(self, on):
        self.setProperty("active", "true" if on else "false")
        repolish(self)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, e):
        self._set_active(False)

    def dropEvent(self, e: QDropEvent):
        self._set_active(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if os.path.isfile(u.toLocalFile())]
        if paths:
            e.acceptProposedAction()
            self.filesDropped.emit(paths)


def scroll_page(body_layout_margins=(0, 0, 0, 0)):
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setObjectName("ScrollBody")
    lay = QVBoxLayout(body)
    lay.setContentsMargins(*body_layout_margins)
    lay.setSpacing(12)
    sa.setWidget(body)
    sa.viewport().setAutoFillBackground(False)
    return sa, lay


# ==========================================
# Phone state model
# ==========================================

def summarize(st):
    """Normalise /api/phone/status into what the UI shows."""
    out = {"service": False, "connected": False, "name": "Phone", "ip": None, "port": 1761,
           "battery": None, "charging": False, "model": "", "android": "", "last_seen": 0,
           "storage_free": None, "storage_total": None, "devices": []}
    if not isinstance(st, dict) or "phone" not in st:
        return out
    ph = st.get("phone") or {}
    out.update({
        "service": True,
        "connected": bool(st.get("connected")),
        "name": ph.get("client_name") or "Android phone",
        "ip": ph.get("ip"),
        "port": ph.get("port") or 1761,
        "battery": ph.get("battery_level"),
        "charging": bool(ph.get("is_charging")),
        "model": ph.get("model") or "",
        "android": ph.get("android_version") or "",
        "last_seen": ph.get("last_seen") or 0,
        "storage_free": ph.get("storage_free"),
        "storage_total": ph.get("storage_total"),
        "devices": st.get("devices") or [],
    })
    return out


class PhoneState(QObject):
    changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.raw = {}
        self.s = summarize({})
        self._busy = False

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        run_async(lambda: fetch_json("/api/phone/status"), self._done)

    def refresh_sync(self):
        self._done(fetch_json("/api/phone/status"))

    def _done(self, st):
        self._busy = False
        self.raw = st if isinstance(st, dict) else {}
        self.s = summarize(self.raw)
        self.changed.emit(self.s)


# ==========================================
# Transfers (downloads, previews, uploads) on one worker thread
# ==========================================

class _ProgressReader:
    def __init__(self, f, total, cb):
        self.f, self.total, self.cb, self.done = f, total, cb, 0

    def read(self, n=-1):
        chunk = self.f.read(65536 if (n is None or n < 0) else n)
        self.done += len(chunk)
        self.cb(self.done, self.total)
        return chunk


class Transfers(QObject):
    progress = pyqtSignal(dict, float)   # job, fraction (-1 = waiting on phone)
    finished = pyqtSignal(dict)          # job with ok / error / dest
    idle = pyqtSignal(str)               # origin that went idle

    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.pending = {}
        self.thread = None

    def add(self, job):
        with self.lock:
            self.pending[job['origin']] = self.pending.get(job['origin'], 0) + 1
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()
        self.q.put(job)

    def busy(self, origin):
        with self.lock:
            return self.pending.get(origin, 0) > 0

    def _work(self):
        while True:
            job = self.q.get()
            try:
                self._run(job)
                job['ok'] = True
            except Exception as e:
                job['ok'] = False
                job['error'] = describe_error(e)
            self.finished.emit(job)
            with self.lock:
                self.pending[job['origin']] -= 1
                went_idle = self.pending[job['origin']] == 0
            if went_idle:
                self.idle.emit(job['origin'])

    def _emit_progress(self, job, done, total, state):
        if not total:
            return
        frac = done / total
        now = time.time()
        if frac >= 1.0 or frac - state[0] >= 0.01 or now - state[1] > 0.25:
            state[0], state[1] = frac, now
            self.progress.emit(job, frac)

    def _run(self, job):
        kind = job['kind']
        if kind in ('download', 'preview'):
            f = job['file']
            fn = f.get('name') or 'file'
            if kind == 'preview':
                d = os.path.join(tempfile.gettempdir(), 'orbit-preview')
                os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, fn)
                url = f"{DAEMON_URL}/api/phone/files/download?path={quote(f.get('path', ''))}&preview=1"
            else:
                dl_dir = job.get('dest_dir') or os.path.expanduser('~/Downloads')
                os.makedirs(dl_dir, exist_ok=True)
                dest = os.path.join(dl_dir, fn)
                base, ext = os.path.splitext(fn)
                c = 1
                while os.path.exists(dest):
                    dest = os.path.join(dl_dir, f"{base} ({c}){ext}")
                    c += 1
                url = f"{DAEMON_URL}/api/phone/files/download?path={quote(f.get('path', ''))}"
            self.progress.emit(job, 0.0)
            state = [0.0, 0.0]
            tmp = dest + '.part'
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
                total = int(resp.headers.get('Content-Length') or 0) or int(f.get('size') or 0)
                done = 0
                with open(tmp, 'wb') as out:
                    while chunk := resp.read(65536):
                        out.write(chunk)
                        done += len(chunk)
                        self._emit_progress(job, done, total, state)
            os.replace(tmp, dest)
            job['dest'] = dest
            if kind == 'preview':
                subprocess.Popen(["xdg-open", dest], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif kind == 'upload':
            path = job['path']
            fn = os.path.basename(path)
            size = os.path.getsize(path)
            url = f"{DAEMON_URL}/api/phone/files/upload?path={quote(job.get('remote_dir') or PHONE_DOWNLOADS)}"
            state = [0.0, 0.0]

            def cb(done, total):
                self._emit_progress(job, done, total, state)
                if done >= total:
                    self.progress.emit(job, -1.0)
            self.progress.emit(job, 0.0)
            with open(path, 'rb') as fh:
                req = urllib.request.Request(
                    url, data=_ProgressReader(fh, size, cb),
                    headers={'Content-Type': 'application/octet-stream',
                             'Content-Length': str(size), 'X-Filename': quote(fn)},
                    method='POST')
                with urllib.request.urlopen(req, timeout=60) as resp:
                    resp.read()
        else:
            raise ValueError(f"unknown job {kind}")


# ==========================================
# Files page (phone file browser)
# ==========================================

EXT_KIND = {}
for _k, _exts in {
    'image': 'jpg jpeg png gif webp heic heif bmp svg avif',
    'video': 'mp4 mkv webm mov avi 3gp m4v',
    'audio': 'mp3 m4a aac flac ogg opus wav amr mid',
    'archive': 'zip rar 7z tar gz tgz xz bz2 zst',
    'package': 'apk apks xapk aab',
    'doc': 'pdf doc docx odt txt md rtf csv xls xlsx ods ppt pptx odp json xml log epub',
    'code': 'py js ts kt java c cpp h sh html css rs go',
}.items():
    for _e in _exts.split():
        EXT_KIND[_e] = _k

KIND_STYLE = {
    'folder': ('folder', C.FILES), 'image': ('image', C.MEDIA), 'video': ('film', C.MEDIA),
    'audio': ('music', C.CLIP), 'archive': ('archive', C.RING), 'package': ('package', C.TERM),
    'doc': ('file-text', C.T2), 'code': ('code', C.TERM), 'file': ('file', C.T3),
}


def file_kind(f):
    if f.get('is_dir'):
        return 'folder'
    ext = os.path.splitext(f.get('name', ''))[1].lower().lstrip('.')
    return EXT_KIND.get(ext, 'file')


PLACES = [
    ("Internal storage", PHONE_ROOT, 'smartphone', C.ACCENT),
    ("Camera", "/storage/emulated/0/DCIM", 'camera', C.MEDIA),
    ("Downloads", "/storage/emulated/0/Download", 'download', C.FILES),
    ("Documents", "/storage/emulated/0/Documents", 'file-text', C.CLIP),
    ("Pictures", "/storage/emulated/0/Pictures", 'image', C.MEDIA),
    ("Music", "/storage/emulated/0/Music", 'music', C.CLIP),
    ("Movies", "/storage/emulated/0/Movies", 'film', C.RING),
]


class FilesPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = ctl
        self.setAcceptDrops(True)
        self.current_path = PHONE_ROOT
        self.history = [PHONE_ROOT]
        self.history_idx = 0
        self._req = 0
        self.loaded_count = None
        self._build()
        ctl.transfers.progress.connect(self._on_progress)
        ctl.transfers.finished.connect(self._on_finished)
        ctl.transfers.idle.connect(self._on_idle)

    # ---- layout ----
    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        places = Card(margins=10, spacing=4)
        places.setFixedWidth(208)
        places.lay.addWidget(label("Places", "section"))
        self.places = QListWidget()
        self.places.setObjectName("Places")
        self.places.setIconSize(QSize(18, 18))
        self.places.setCursor(Qt.CursorShape.PointingHandCursor)
        for name, path, icon, tint in PLACES:
            it = QListWidgetItem(line_icon(icon, tint, 18), name)
            it.setData(Qt.ItemDataRole.UserRole, path)
            it.setSizeHint(QSize(0, 38))
            self.places.addItem(it)
        self.places.itemClicked.connect(lambda it: self.load_directory(it.data(Qt.ItemDataRole.UserRole)))
        places.lay.addWidget(self.places, 1)
        places.lay.addWidget(rule())
        hint = label("Drop files anywhere on this page to upload them into the open folder.", "faint", wrap=True)
        hint.setContentsMargins(6, 4, 6, 4)
        places.lay.addWidget(hint)
        root.addWidget(places)

        main = QVBoxLayout()
        main.setSpacing(12)
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_back = button(icon='arrow-left', kind='icon', tip="Back (Alt+Left)")
        self.btn_back.clicked.connect(self.navigate_back)
        self.btn_up = button(icon='arrow-up', kind='icon', tip="Up one folder (Alt+Up)")
        self.btn_up.clicked.connect(self.navigate_up)
        self.btn_refresh = button(icon='refresh', kind='icon', tip="Refresh (F5)")
        self.btn_refresh.clicked.connect(lambda: self.load_directory(self.current_path, record_history=False))
        self.path_edit = QLineEdit(self.current_path)
        self.path_edit.setObjectName("Path")
        self.path_edit.returnPressed.connect(self.on_path_entered)
        for w in (self.btn_back, self.btn_up, self.btn_refresh):
            bar.addWidget(w)
        bar.addWidget(self.path_edit, 1)
        main.addLayout(bar)
        self.btn_mkdir = button("New folder", 'folder-plus')
        self.btn_mkdir.clicked.connect(self.create_folder)
        self.btn_upload = button("Upload", 'upload')
        self.btn_upload.clicked.connect(self.upload_files)
        self.btn_download = button("Download", 'download', 'primary')
        self.btn_download.clicked.connect(self.download_selected)

        self.stack = QStackedWidget()
        self.tree = QTreeWidget()
        self.tree.setObjectName("Files")
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Name", "Size", "Modified"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(1, 96)
        hdr.resizeSection(2, 150)
        hdr.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.tree.itemDoubleClicked.connect(self.on_item_activated)
        self.tree.itemActivated.connect(self.on_item_activated)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_context_menu)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.stack.addWidget(self.tree)

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch()
        r = QHBoxLayout()
        r.addStretch()
        self.empty_tile = IconTile('folder', C.FILES, 52)
        r.addWidget(self.empty_tile)
        r.addStretch()
        el.addLayout(r)
        self.empty_title = label("This folder is empty", "title")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_msg = label("", "muted", wrap=True)
        self.empty_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_title)
        el.addWidget(self.empty_msg)
        rr = QHBoxLayout()
        rr.addStretch()
        self.empty_btn = button("Try again", 'refresh')
        self.empty_btn.clicked.connect(lambda: self.load_directory(self.current_path, record_history=False))
        rr.addWidget(self.empty_btn)
        rr.addStretch()
        el.addSpacing(6)
        el.addLayout(rr)
        el.addStretch()
        empty_card = Card()
        empty_card.lay.addWidget(empty)
        self.stack.addWidget(empty_card)
        main.addWidget(self.stack, 1)

        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.status = label("", "faint")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(180)
        self.progress.hide()
        self.status.setToolTip("Double-click to open or preview. Right-click for more.")
        foot.addWidget(self.status, 1)
        foot.addWidget(self.progress)
        for w in (self.btn_mkdir, self.btn_upload, self.btn_download):
            foot.addWidget(w)
        main.addLayout(foot)
        root.addLayout(main, 1)

        for keys, fn in ((QKeySequence.StandardKey.Refresh, lambda: self.load_directory(self.current_path, record_history=False)),
                         ("Alt+Left", self.navigate_back), ("Backspace", self.navigate_back),
                         ("Alt+Up", self.navigate_up), (QKeySequence.StandardKey.Delete, self.delete_selected),
                         ("Ctrl+L", lambda: (self.path_edit.setFocus(), self.path_edit.selectAll()))):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
        self._update_buttons()

    def _update_buttons(self):
        sel = self.selected_files()
        self.btn_download.setEnabled(any(not f.get('is_dir') for f in sel))
        self.btn_back.setEnabled(self.history_idx > 0)
        self.btn_up.setEnabled(self.current_path.rstrip('/') not in ('', PHONE_ROOT))

    def set_status(self, text, color=None):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};" if color else "")

    # ---- navigation ----
    def on_path_entered(self):
        p = self.path_edit.text().strip()
        if p:
            self.load_directory(p)

    def navigate_back(self):
        if self.history_idx > 0:
            self.history_idx -= 1
            self.load_directory(self.history[self.history_idx], record_history=False)

    def navigate_up(self):
        if self.current_path.rstrip('/') in ('', PHONE_ROOT):
            return
        parent = os.path.dirname(self.current_path.rstrip('/'))
        if parent:
            self.load_directory(parent)

    def load_directory(self, path, record_history=True):
        path = path.rstrip('/') or '/'
        self.current_path = path
        self.path_edit.setText(path)
        if record_history and self.history[self.history_idx] != path:
            self.history = self.history[:self.history_idx + 1] + [path]
            self.history_idx = len(self.history) - 1
        for i in range(self.places.count()):
            it = self.places.item(i)
            it.setSelected(it.data(Qt.ItemDataRole.UserRole) == path)
        self._update_buttons()
        self.set_status(f"Loading {path}")
        self._req += 1
        req = self._req
        run_async(lambda: fetch_json(f"/api/phone/files/list?path={quote(path)}", timeout=10),
                  lambda data: self._listed(req, path, data))

    def load_directory_sync(self, path):
        self._req += 1
        self._listed(self._req, path, fetch_json(f"/api/phone/files/list?path={quote(path)}", timeout=10))

    def _listed(self, req, path, data):
        if req != self._req:
            return
        self.tree.clear()
        if isinstance(data, dict) and "error" in data:
            self.loaded_count = None
            msg = data.get('message') or data['error']
            self.set_status(f"Couldn't open {path}", C.DANGER)
            self.empty_tile.set_icon('alert', C.DANGER)
            self.empty_title.setText("Couldn't reach the phone")
            self.empty_msg.setText(f"{msg}\nMake sure Orbit is open on the phone and both are on the same network.")
            self.empty_btn.show()
            self.stack.setCurrentIndex(1)
            return
        files = data if isinstance(data, list) else []
        files.sort(key=lambda f: (not f.get('is_dir'), f.get('name', '').lower()))
        self.loaded_count = len(files)
        for f in files:
            name = f.get('name', '')
            kind = file_kind(f)
            icon, tint = KIND_STYLE[kind]
            it = QTreeWidgetItem([name, "-" if f.get('is_dir') else format_size(f.get('size', 0)),
                                  format_time(f.get('mtime', 0))])
            it.setIcon(0, line_icon(icon, tint, 20))
            it.setData(0, Qt.ItemDataRole.UserRole, f)
            it.setSizeHint(0, QSize(0, 40))
            it.setForeground(1, QColor(C.T2))
            it.setForeground(2, QColor(C.T3))
            it.setToolTip(0, f.get('path', name))
            self.tree.addTopLevelItem(it)
        if files:
            self.stack.setCurrentIndex(0)
        else:
            self.empty_tile.set_icon('folder', C.FILES)
            self.empty_title.setText("This folder is empty")
            self.empty_msg.setText("Drop files here to upload them to the phone.")
            self.empty_btn.hide()
            self.stack.setCurrentIndex(1)
        n_dirs = sum(1 for f in files if f.get('is_dir'))
        self.set_status(f"{len(files)} items  ·  {n_dirs} folders, {len(files) - n_dirs} files")
        self._update_buttons()

    # ---- selection / actions ----
    def selected_files(self):
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self.tree.selectedItems()]

    def on_item_activated(self, item, _col=0):
        f = item.data(0, Qt.ItemDataRole.UserRole)
        if not f:
            return
        if f.get('is_dir'):
            self.load_directory(f.get('path'))
        else:
            self.preview_file(f)

    def show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction(line_icon('folder-plus', C.T2, 18), "New folder", self.create_folder)
            menu.addAction(line_icon('upload', C.T2, 18), "Upload files", self.upload_files)
            menu.addAction(line_icon('refresh', C.T2, 18), "Refresh",
                           lambda: self.load_directory(self.current_path, record_history=False))
        else:
            if not item.isSelected():
                self.tree.clearSelection()
                item.setSelected(True)
            f = item.data(0, Qt.ItemDataRole.UserRole)
            multi = len(self.tree.selectedItems()) > 1
            if f.get('is_dir') and not multi:
                menu.addAction(line_icon('folder', C.FILES, 18), "Open", lambda: self.load_directory(f.get('path')))
                menu.addSeparator()
            elif not f.get('is_dir'):
                menu.addAction(line_icon('download', C.T2, 18), "Download to computer (~/Downloads)",
                               lambda: self.download_selected() if multi else self.download_file(f))
                menu.addAction(line_icon('eye', C.T2, 18), "Preview (open temporarily)", lambda: self.preview_file(f))
                menu.addSeparator()
            menu.addAction(line_icon('trash', C.DANGER, 18), "Delete", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def preview_file(self, f):
        self.ctl.transfers.add({'kind': 'preview', 'origin': 'files', 'file': f})

    def download_selected(self):
        for f in self.selected_files():
            if not f.get('is_dir'):
                self.download_file(f)

    def download_file(self, f):
        self.ctl.transfers.add({'kind': 'download', 'origin': 'files', 'file': f})

    def upload_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files to upload to the phone")
        self.upload_paths(paths)

    def upload_paths(self, paths):
        for p in paths:
            if os.path.isfile(p):
                self.ctl.transfers.add({'kind': 'upload', 'origin': 'files', 'path': p,
                                        'remote_dir': self.current_path})

    def create_folder(self):
        name, ok = QInputDialog.getText(self, "New folder", f"Folder name (inside {self.current_path})")
        if ok and name.strip():
            path = self.current_path
            run_async(lambda: post_json("/api/phone/files/mkdir", {"path": path, "name": name.strip()}),
                      lambda res: self._after_op(res, "Couldn't create the folder"))

    def delete_selected(self):
        files = self.selected_files()
        if not files:
            return
        what = files[0].get('name') if len(files) == 1 else f"{len(files)} items"
        ans = QMessageBox.question(self, "Delete from phone",
                                   f"Delete {what} from the phone?\n\nThis can't be undone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        paths = [f.get('path') for f in files]

        def work():
            errs = []
            for p in paths:
                r = post_json("/api/phone/files/delete", {"path": p})
                if isinstance(r, dict) and r.get('error'):
                    errs.append(r.get('message') or r['error'])
            return {"error": errs[0]} if errs else {"status": "ok"}
        self.set_status(f"Deleting {what}")
        run_async(work, lambda res: self._after_op(res, "Couldn't delete"))

    def _after_op(self, res, fail_text):
        if isinstance(res, dict) and res.get('error'):
            self.set_status(f"{fail_text}: {res.get('message') or res['error']}", C.DANGER)
        self.load_directory(self.current_path, record_history=False)

    # ---- transfer feedback ----
    def _on_progress(self, job, frac):
        if job.get('origin') != 'files':
            return
        name = job.get('file', {}).get('name') if 'file' in job else os.path.basename(job.get('path', ''))
        verb = {'download': "Downloading", 'preview': "Opening", 'upload': "Uploading"}[job['kind']]
        self.progress.show()
        if frac < 0:
            self.progress.setRange(0, 0)
            self.set_status(f"Saving {name} on the phone")
        else:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(frac * 1000))
            self.set_status(f"{verb} {name}  {int(frac * 100)}%")

    def _on_finished(self, job):
        if job.get('origin') != 'files':
            return
        name = job.get('file', {}).get('name') if 'file' in job else os.path.basename(job.get('path', ''))
        if job['ok']:
            if job['kind'] == 'download':
                self.set_status(f"Saved to {job['dest']}", C.ONLINE)
                notify("File downloaded", f"Saved to {job['dest']}")
            elif job['kind'] == 'preview':
                self.set_status(f"Opened {name} in the default app", C.ONLINE)
            else:
                self.set_status(f"Uploaded {name}", C.ONLINE)
        else:
            self.set_status(f"{name}: {job['error']}", C.DANGER)
            if job['kind'] == 'download':
                QMessageBox.critical(self, "Download failed", f"{name}\n\n{job['error']}")

    def _on_idle(self, origin):
        if origin != 'files':
            return
        self.progress.hide()
        self.progress.setRange(0, 1000)
        self.load_directory(self.current_path, record_history=False)

    # ---- drag and drop ----
    def _set_dropping(self, on):
        self.tree.setProperty("dropping", "true" if on else "false")
        repolish(self.tree)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_dropping(True)
            self.set_status(f"Drop to upload into {self.current_path}", C.ACCENT)

    def dragLeaveEvent(self, e):
        self._set_dropping(False)
        self.set_status("")

    def dropEvent(self, e: QDropEvent):
        self._set_dropping(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if os.path.isfile(u.toLocalFile())]
        if paths:
            e.acceptProposedAction()
            self.upload_paths(paths)


# ==========================================
# Send page
# ==========================================

class SendPage(QWidget):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = ctl
        self.rows = {}
        self._build()
        ctl.state.changed.connect(self._on_state)
        ctl.transfers.progress.connect(self._on_progress)
        ctl.transfers.finished.connect(self._on_finished)

    def _build(self):
        sa, lay = scroll_page()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sa)

        self.drop = DropZone("Drop files to send to your phone",
                             "They land in the phone's Download folder.")
        self.drop.filesDropped.connect(self.send_paths)
        self.drop.chooseClicked.connect(self.choose)
        lay.addWidget(self.drop)

        target = Card()
        tr = QHBoxLayout()
        tr.setSpacing(12)
        tr.addWidget(IconTile('smartphone', C.ACCENT, 40))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label("Send to", "value"))
        self.target_hint = label("Your connected phone", "faint")
        col.addWidget(self.target_hint)
        tr.addLayout(col, 1)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(260)
        tr.addWidget(self.combo)
        target.lay.addLayout(tr)
        lay.addWidget(target)

        clip = Card()
        cr = QHBoxLayout()
        cr.setSpacing(12)
        cr.addWidget(IconTile('clipboard', C.CLIP, 40))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label("Clipboard", "value"))
        col.addWidget(label("Send what you copied on this computer to the phone.", "faint"))
        cr.addLayout(col, 1)
        b = button("Send clipboard", 'send')
        b.clicked.connect(self.ctl.sync_clipboard)
        cr.addWidget(b)
        clip.lay.addLayout(cr)
        lay.addWidget(clip)

        lay.addWidget(label("Recent", "section"))
        self.recent_card = Card(margins=8, spacing=0)
        self.recent = QListWidget()
        self.recent.setObjectName("Recent")
        self.recent.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.recent.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.recent.setMinimumHeight(64)
        self.recent_empty = label("Nothing sent yet.", "faint")
        self.recent_empty.setContentsMargins(10, 10, 10, 10)
        self.recent_card.lay.addWidget(self.recent_empty)
        self.recent_card.lay.addWidget(self.recent)
        self.recent.hide()
        lay.addWidget(self.recent_card)
        lay.addStretch()

    def _on_state(self, s):
        devs = s.get('devices') or []
        cur = self.combo.currentData()
        self.combo.blockSignals(True)
        self.combo.clear()
        for d in devs:
            state = "connected" if d.get('connected') else "idle"
            self.combo.addItem(line_icon('smartphone', C.ONLINE if d.get('connected') else C.T3, 16),
                               f"{d.get('client_name', 'Android phone')}  ({state})", d)
        if not devs:
            self.combo.addItem("No paired phone", None)
        idx = 0
        for i in range(self.combo.count()):
            d = self.combo.itemData(i)
            if cur and d and d.get('client_id') == cur.get('client_id'):
                idx = i
                break
            if not cur and d and d.get('connected'):
                idx = i
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)
        self.combo.setEnabled(len(devs) > 1)
        self.target_hint.setText("Pick which paired phone gets the files" if len(devs) > 1
                                 else (f"Files go to {s['name']}" if s['service'] else "Orbit service not running"))

    def choose(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files to send to the phone")
        self.send_paths(paths)

    def send_paths(self, paths):
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return
        dev = self.combo.currentData()
        devs = self.ctl.state.s.get('devices') or []
        # The service relays uploads to the active phone; another paired phone goes via the send helper.
        direct = dev is None or dev.get('connected') or len(devs) <= 1
        for p in paths:
            self._add_row(p)
            if direct:
                self.ctl.transfers.add({'kind': 'upload', 'origin': 'send', 'path': p,
                                        'remote_dir': PHONE_DOWNLOADS, 'device': dev})
            else:
                ok = self.ctl.run_send_helper([p], dev)
                self._set_row(p, "Handed to sender" if ok else "No send helper found",
                              C.T2 if ok else C.DANGER)

    def _add_row(self, path):
        self.recent_empty.hide()
        self.recent.show()
        w = QFrame()
        w.setObjectName("Row")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(12)
        icon, tint = KIND_STYLE[file_kind({'name': path})]
        lay.addWidget(IconTile(icon, tint, 34))
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(label(os.path.basename(path), "value"))
        try:
            sz = format_size(os.path.getsize(path))
        except OSError:
            sz = ""
        col.addWidget(label(f"{sz}  ·  {time.strftime('%H:%M')}", "faint"))
        lay.addLayout(col, 1)
        st = label("Queued", "faint")
        lay.addWidget(st)
        it = QListWidgetItem()
        it.setSizeHint(QSize(0, 54))
        self.recent.insertItem(0, it)
        self.recent.setItemWidget(it, w)
        self.rows[path] = st
        while self.recent.count() > 20:
            self.recent.takeItem(self.recent.count() - 1)
        self.recent.setFixedHeight(min(6, self.recent.count()) * 56 + 4)

    def _set_row(self, path, text, color=C.T3):
        lb = self.rows.get(path)
        if lb is not None:
            try:
                lb.setText(text)
                lb.setStyleSheet(f"color: {color}; font-size: 12px;")
            except RuntimeError:
                pass

    def _on_progress(self, job, frac):
        if job.get('origin') == 'send':
            self._set_row(job['path'], "Saving on phone" if frac < 0 else f"Sending {int(frac * 100)}%", C.ACCENT)

    def _on_finished(self, job):
        if job.get('origin') != 'send':
            return
        p = job['path']
        if job['ok']:
            self._set_row(p, "Sent", C.ONLINE)
            notify("Sent to phone", f"{os.path.basename(p)} is in Download")
        else:
            # Same fallback chain as before: the helper retries directly / via a staged link.
            ok = self.ctl.run_send_helper([p], job.get('device'))
            self._set_row(p, "Retrying via sender" if ok else "Failed", C.WARN if ok else C.DANGER)
            if not ok:
                self.ctl.window_status(f"{os.path.basename(p)}: {job['error']}")


# ==========================================
# Overview page
# ==========================================

class OverviewPage(QWidget):
    def __init__(self, ctl, win, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl, self.win = ctl, win
        self._dev_sig = None
        self._build()
        ctl.state.changed.connect(self.update_state)
        ctl.transfers.finished.connect(self._on_transfer)

    def _build(self):
        sa, lay = scroll_page()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sa)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Status card
        st = Card()
        h = QHBoxLayout()
        h.addWidget(label("Status", "title"))
        h.addStretch()
        rb = button(icon='refresh', kind='ghost', size='small', tip="Refresh status")
        rb.setProperty("kind", "icon")
        rb.clicked.connect(self.ctl.state.refresh)
        h.addWidget(rb)
        st.lay.addLayout(h)

        def meter(icon, tint, name):
            box = QVBoxLayout()
            box.setSpacing(8)
            r = QHBoxLayout()
            r.setSpacing(8)
            ic = QLabel()
            ic.setPixmap(line_pixmap(icon, tint, 18))
            r.addWidget(ic)
            r.addWidget(label(name, "muted"))
            r.addStretch()
            val = label("--", "value")
            r.addWidget(val)
            box.addLayout(r)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            box.addWidget(bar)
            w = QWidget()
            w.setLayout(box)
            return w, ic, val, bar
        self.bat_box, self.bat_icon, self.bat_val, self.bat_bar = meter('battery', C.ONLINE, "Battery")
        self.sto_box, _, self.sto_val, self.sto_bar = meter('hard-drive', C.FILES, "Storage")
        st.lay.addWidget(self.bat_box)
        st.lay.addWidget(self.sto_box)
        st.lay.addWidget(rule())

        self.details = {}
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(12)
        grid2.setVerticalSpacing(10)
        for i, (key, icon, name) in enumerate((('addr', 'wifi', "Address"), ('seen', 'clock', "Last active"),
                                               ('model', 'smartphone', "Model"), ('android', 'package', "Android"),
                                               ('sec', 'shield', "Security"))):
            ic = QLabel()
            ic.setPixmap(line_pixmap(icon, C.T3, 16))
            k = label(name, "muted")
            v = label("--", "mono" if key == 'addr' else "value")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid2.addWidget(ic, i, 0)
            grid2.addWidget(k, i, 1)
            grid2.addWidget(v, i, 2, Qt.AlignmentFlag.AlignRight)
            self.details[key] = (ic, k, v)
        grid2.setColumnStretch(1, 1)
        st.lay.addLayout(grid2)
        self.details['sec'][2].setText("HMAC-SHA256 signed")
        row.addWidget(st, 1)

        # Paired phones card
        pc = Card()
        h = QHBoxLayout()
        h.addWidget(label("Paired phones", "title"))
        self.dev_count = label("", "faint")
        h.addWidget(self.dev_count)
        h.addStretch()
        pb = button("Pair a phone", 'plus', size='small')
        pb.clicked.connect(self.ctl.pair_new_device)
        h.addWidget(pb)
        pc.lay.addLayout(h)
        self.dev_box = QVBoxLayout()
        self.dev_box.setSpacing(4)
        pc.lay.addLayout(self.dev_box)
        pc.lay.addStretch()
        row.addWidget(pc, 1)
        lay.addLayout(row)

        # Recent transfers, both directions, from any page
        rc = Card()
        h = QHBoxLayout()
        h.addWidget(label("Recent transfers", "title"))
        h.addStretch()
        h.addWidget(label("Drop files anywhere in this window to send them", "faint"))
        rc.lay.addLayout(h)
        self.recent_empty = label("Files you send to the phone or save from it show up here.", "muted", wrap=True)
        rc.lay.addWidget(self.recent_empty)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        rc.lay.addLayout(self.recent_box)
        lay.addWidget(rc)
        lay.addStretch()

    def _on_transfer(self, job):
        if job.get('kind') == 'preview':
            return
        upload = job.get('kind') == 'upload'
        name = os.path.basename(job.get('path') or '') if upload else (job.get('file') or {}).get('name', 'file')
        if not name:
            return
        self.recent_empty.hide()
        w = QFrame()
        w.setObjectName("Row")
        r = QHBoxLayout(w)
        r.setContentsMargins(0, 6, 0, 6)
        r.setSpacing(12)
        icon, tint = KIND_STYLE[file_kind({'name': name})]
        r.addWidget(IconTile(icon, tint, 34))
        col = QVBoxLayout()
        col.setSpacing(1)
        col.addWidget(label(name, "value"))
        where = "Sent to phone" if upload else "Saved to " + os.path.dirname(job.get('dest') or '~/Downloads').replace(os.path.expanduser('~'), '~')
        col.addWidget(label(f"{where}  ·  {time.strftime('%H:%M')}", "faint"))
        r.addLayout(col, 1)
        ok = job.get('ok')
        st = label("Done" if ok else (job.get('error') or "Failed"), "faint")
        st.setStyleSheet(f"color: {C.ONLINE if ok else C.DANGER}; font-size: 12px;")
        r.addWidget(st)
        self.recent_box.insertWidget(0, w)
        while self.recent_box.count() > 6:
            it = self.recent_box.takeAt(self.recent_box.count() - 1)
            if it.widget():
                it.widget().deleteLater()

    def update_state(self, s):
        bat = s.get('battery')
        if bat is not None:
            bat = int(bat)
            col = C.ACCENT if s['charging'] else (C.ONLINE if bat > 30 else (C.WARN if bat > 15 else C.DANGER))
            self.bat_val.setText(f"{bat}%" + ("  ·  charging" if s['charging'] else ""))
            self.bat_bar.setValue(bat)
            self.bat_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {col}; border-radius: 3px; }}")
            self.bat_icon.setPixmap(line_pixmap('battery-charging' if s['charging'] else 'battery', col, 18))
        else:
            self.bat_val.setText("Not reported")
            self.bat_bar.setValue(0)
        tot, free = s.get('storage_total'), s.get('storage_free')
        if tot and free is not None and tot > 0:
            self.sto_val.setText(f"{free / 1024 ** 3:.1f} GB free of {tot / 1024 ** 3:.1f} GB")
            self.sto_bar.setValue(int((tot - free) / tot * 100))
            self.sto_box.show()
        else:
            self.sto_box.hide()
        if s['service']:
            self.details['addr'][2].setText(f"{s['ip']}:{s['port']}" if s['ip'] else "Searching")
            self.details['seen'][2].setText(ago(s['last_seen']))
        else:
            self.details['addr'][2].setText("--")
            self.details['seen'][2].setText("--")
        for key in ('model', 'android'):
            val = s.get(key)
            for w in self.details[key]:
                w.setVisible(bool(val))
            if val:
                self.details[key][2].setText(str(val))

        devs = s.get('devices') or []
        sig = json.dumps([(d.get('client_id'), d.get('client_name'), d.get('ip'), d.get('connected')) for d in devs]
                         + [s['service']])
        if sig == self._dev_sig:
            return
        self._dev_sig = sig
        self.dev_count.setText(str(len(devs)) if devs else "")
        while self.dev_box.count():
            it = self.dev_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not devs:
            msg = "No phone paired yet. Open Orbit on your phone and add this computer." if s['service'] \
                else "The Orbit service on this computer isn't responding."
            self.dev_box.addWidget(label(msg, "muted", wrap=True))
            return
        for i, d in enumerate(devs):
            if i:
                self.dev_box.addWidget(rule())
            w = QFrame()
            w.setObjectName("Row")
            r = QHBoxLayout(w)
            r.setContentsMargins(0, 6, 0, 6)
            r.setSpacing(10)
            conn = bool(d.get('connected'))
            r.addWidget(IconTile('smartphone', C.ONLINE if conn else C.T3, 36))
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(label(d.get('client_name', 'Android phone'), "value"))
            sub = QHBoxLayout()
            sub.setSpacing(4)
            sub.addWidget(Dot(C.ONLINE if conn else C.T3, 6))
            word = label("Connected" if conn else "Idle", "faint")
            word.setStyleSheet(f"color: {C.ONLINE if conn else C.T3}; font-size: 12px;")
            sub.addWidget(word)
            sub.addWidget(label(f"  {d.get('ip') or 'unknown address'}", "mono"))
            sub.addStretch()
            col.addLayout(sub)
            r.addLayout(col, 1)
            sb = button(icon='upload', kind='icon', size='small', tip="Send files to this phone")
            sb.clicked.connect(lambda _=False, dev=d: self.ctl.send_file_to_device(dev))
            r.addWidget(sb)
            ub = button("Unpair", kind='danger', size='small')
            ub.clicked.connect(lambda _=False, dev=d: self.ctl.confirm_unpair_device(dev, self.win))
            r.addWidget(ub)
            self.dev_box.addWidget(w)


# ==========================================
# Find my page: who tried to get in, the phone's Find my buttons, this computer's settings
# ==========================================

def fetch_bytes(endpoint, timeout=8):
    try:
        with urllib.request.urlopen(f"{DAEMON_URL}{endpoint}", timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return b''


def cover_pixmap(data, w, h, radius=10):
    """The photo cropped to fill w x h, with rounded corners."""
    src = QPixmap()
    if not data or not src.loadFromData(data):
        return None
    app = QGuiApplication.instance()
    dpr = max(1.0, app.devicePixelRatio() if app else 1.0)
    pw, ph = int(w * dpr), int(h * dpr)
    scaled = src.scaled(pw, ph, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    crop = scaled.copy((scaled.width() - pw) // 2, (scaled.height() - ph) // 2, pw, ph)
    out = QPixmap(pw, ph)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pw, ph), radius * dpr, radius * dpr)
    p.setClipPath(path)
    p.drawPixmap(0, 0, crop)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


# kind -> (what happened, icon, tint)
FINDMY_WORDS = {
    'laptop_wrong_password': ("Wrong password on this computer", 'camera', C.DANGER),
    'wrong_password': ("Wrong password on this computer", 'camera', C.DANGER),
    'wrong_unlock': ("Wrong unlock attempt on the phone", 'smartphone', C.DANGER),
    'lost_report': ("Lost mode report from this computer", 'globe', C.DANGER),
    'sim_changed': ("The phone's SIM card changed", 'alert', C.WARN),
    'sim_removed': ("The phone's SIM card was removed", 'alert', C.WARN),
    'low_battery': ("Phone battery nearly flat, its last place saved", 'battery', C.WARN),
    'walk_away_lock': ("Locked when you walked away", 'lock', C.ACCENT),
}


def photo_caption(ph):
    return {'laptop_wrong_password': "Wrong password here",
            'wrong_unlock': "Wrong unlock on the phone",
            'lost_report': "Lost mode" + (f", {ph['city']}" if ph.get('city') else "")}.get(ph.get('kind'), "Photo")


class Toggle(QWidget):
    """A small on/off switch."""
    toggled = pyqtSignal(bool)

    def __init__(self, on=False, parent=None):
        super().__init__(parent)
        self._on = bool(on)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self):
        return self._on

    def setChecked(self, on):
        self._on = bool(on)
        self.update()

    def mouseReleaseEvent(self, e):
        if self.isEnabled() and self.rect().contains(e.position().toPoint()):
            self.setChecked(not self._on)
            self.toggled.emit(self._on)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        track = QColor(C.ACCENT if self._on else C.STROKE)
        if not self.isEnabled():
            track.setAlphaF(0.4)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 11, 11)
        p.setBrush(QColor(C.GROUND if self._on else C.T2))
        p.drawEllipse(QRectF(self.width() - 19 if self._on else 3, 3, 16, 16))


class PhotoTile(QPushButton):
    def __init__(self, photo, parent=None):
        super().__init__(parent)
        self.setObjectName("Photo")
        self.photo = photo
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(204, 196)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 8)
        lay.setSpacing(4)
        self.img = QLabel()
        self.img.setFixedSize(190, 142)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setStyleSheet(f"background: {C.RAISED}; border-radius: 10px;")
        self.img.setPixmap(line_pixmap('image', C.T3, 28))
        lay.addWidget(self.img)
        for w in (label(photo_caption(photo), "value"),
                  label(f"{ago(photo['t'])}  ·  {time.strftime('%d %b, %H:%M', time.localtime(photo['t']))}", "faint")):
            w.setContentsMargins(4, 0, 4, 0)
            w.setText(w.fontMetrics().elidedText(w.text(), Qt.TextElideMode.ElideRight, 180))
            w.setToolTip(photo_caption(photo))
            lay.addWidget(w)
        for w in self.findChildren(QLabel):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_photo(self, data):
        pm = cover_pixmap(data, 190, 142)
        if pm is not None:
            self.img.setStyleSheet("")
            self.img.setPixmap(pm)


class PhotoDialog(QDialog):
    def __init__(self, photo, data, page, parent=None):
        super().__init__(parent)
        self.photo, self.data, self.page = photo, data, page
        self.setWindowTitle(photo_caption(photo))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        lay.addWidget(label(FINDMY_WORDS.get(photo['kind'], (photo_caption(photo),))[0], "h1"))
        lay.addWidget(label(time.strftime('%A %d %B %Y, %H:%M:%S', time.localtime(photo['t'])), "muted"))
        img = QLabel()
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = QPixmap()
        if data and pm.loadFromData(data):
            img.setPixmap(pm.scaled(760, 520, Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation))
        else:
            img.setText("This photo couldn't be loaded.")
        lay.addWidget(img, 1)
        if photo.get('networks'):
            nets = label("Wi-Fi around it: " + ", ".join(n for n in photo['networks'] if n), "faint", wrap=True)
            lay.addWidget(nets)
        row = QHBoxLayout()
        row.setSpacing(8)
        dl = button("Delete", 'trash', kind='danger')
        dl.clicked.connect(self.delete)
        row.addWidget(dl)
        row.addStretch()
        sv = button("Save to Downloads", 'download')
        sv.clicked.connect(self.save)
        sv.setEnabled(bool(data))
        row.addWidget(sv)
        cl = button("Close", kind='primary')
        cl.clicked.connect(self.accept)
        row.addWidget(cl)
        lay.addLayout(row)

    def save(self):
        dest = os.path.join(os.path.expanduser('~/Downloads'), 'orbit-' + self.photo['name'])
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, 'wb') as f:
                f.write(self.data)
            notify("Photo saved", dest.replace(os.path.expanduser('~'), '~'))
        except OSError as e:
            QMessageBox.warning(self, "Couldn't save the photo", str(e))

    def delete(self):
        ret = QMessageBox.question(self, "Delete photo", "Delete this photo from this computer?\n\n"
                                   "A copy your phone already has stays on the phone.",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        res = post_json("/api/findmy/photo/delete", {"name": self.photo['name']})
        if res.get('status') != 'ok':
            QMessageBox.warning(self, "Couldn't delete the photo", str(res.get('message') or res.get('error') or res))
            return
        self.page.forget_photo(self.photo['name'])
        self.accept()


class FindMyPage(QWidget):
    TILE_STEP = 214              # a photo card and the gap after it

    def __init__(self, ctl, win, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl, self.win = ctl, win
        self._photos_sig = None
        self._thumbs = {}            # name -> jpeg bytes
        self._tiles = {}
        self._cols_now = 0
        self._busy = False
        self._nearby_until = 0.0
        self._build()
        ctl.state.changed.connect(self._on_state)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(15000)

    def _build(self):
        sa, lay = scroll_page()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sa)

        # Who tried to get in
        pc = Card()
        h = QHBoxLayout()
        h.addWidget(label("Who tried to get in", "title"))
        self.photo_count = label("", "faint")
        h.addWidget(self.photo_count)
        h.addStretch()
        rb = button(icon='refresh', kind='icon', size='small', tip="Refresh")
        rb.clicked.connect(self.refresh)
        h.addWidget(rb)
        pc.lay.addLayout(h)
        self.photos_empty = label("No photos. When someone types a wrong password on this computer, or a wrong "
                                  "unlock on your phone, their photo shows up here.", "muted", wrap=True)
        pc.lay.addWidget(self.photos_empty)
        self.photo_grid = QGridLayout()
        self.photo_grid.setHorizontalSpacing(10)
        self.photo_grid.setVerticalSpacing(10)
        self.photo_grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        pc.lay.addLayout(self.photo_grid)
        lay.addWidget(pc)

        row = QHBoxLayout()
        row.setSpacing(12)
        row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Your phone: the phone app's Find my buttons
        ph = Card()
        ph.lay.addWidget(label("Your phone", "title"))
        g = QGridLayout()
        g.setSpacing(10)
        self.t_map = ActionTile('map-pin', C.ACCENT, "Show on map", "Where it is now")
        self.t_map.clicked.connect(self.ctl.open_find_my)
        self.t_ring = ActionTile('bell', C.RING, "Play sound", "Even on silent")
        self.t_ring.clicked.connect(self.win.toggle_ring)
        self.t_near = ActionTile('radio', C.ONLINE, "Nearby", "Same place as you?")
        self.t_near.clicked.connect(self.toggle_nearby)
        self.t_lab = ActionTile('activity', C.CLIP, "Wi-Fi Lab", "Signals, movement")
        self.t_lab.clicked.connect(self.open_wifilab)
        for i, t in enumerate((self.t_map, self.t_ring, self.t_near, self.t_lab)):
            g.addWidget(t, i // 2, i % 2)
        ph.lay.addLayout(g)
        ph.lay.addStretch()
        row.addWidget(ph, 1)

        # This computer: the settings the phone app has for it, and lost mode
        cc = Card()
        cc.lay.addWidget(label("This computer", "title"))

        def setting(icon, tint, title, desc):
            w = QHBoxLayout()
            w.setSpacing(12)
            w.addWidget(IconTile(icon, tint, 36))
            col = QVBoxLayout()
            col.setSpacing(1)
            t = label(title, "value")
            d = label(desc, "faint", wrap=True)
            col.addWidget(t)
            col.addWidget(d)
            w.addLayout(col, 1)
            return w, t, d
        r, _, _ = setting('lock', C.ACCENT, "Lock when I walk away", "When your phone leaves, this computer locks itself")
        self.sw_walk = Toggle()
        self.sw_walk.toggled.connect(lambda on: self.set_setting('walk_away_lock', on, self.sw_walk))
        r.addWidget(self.sw_walk)
        cc.lay.addLayout(r)
        r, _, _ = setting('camera', C.DANGER, "Photo on wrong password",
                          "The webcam takes a photo of whoever types a wrong password")
        self.sw_photo = Toggle()
        self.sw_photo.toggled.connect(lambda on: self.set_setting('wrong_password_photo', on, self.sw_photo))
        r.addWidget(self.sw_photo)
        cc.lay.addLayout(r)
        cc.lay.addWidget(rule())
        r, self.lost_title, self.lost_desc = setting('alert', C.T3, "Not marked as lost",
                                                     "If it goes missing, mark it lost from your phone")
        self.lost_tile = r.itemAt(0).widget()
        cc.lay.addLayout(r)
        cc.lay.addStretch()
        lb = button("Lock now", 'lock')
        lb.clicked.connect(self.lock_now)
        cc.lay.addWidget(lb, 0, Qt.AlignmentFlag.AlignLeft)
        row.addWidget(cc, 1)
        lay.addLayout(row)

        # Recent
        rc = Card()
        rc.lay.addWidget(label("Recent", "title"))
        self.recent_empty = label("Nothing unusual. All quiet in orbit.", "muted")
        rc.lay.addWidget(self.recent_empty)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        rc.lay.addLayout(self.recent_box)
        lay.addWidget(rc)
        lay.addStretch()
        self._on_state(self.ctl.state.s)

    # ---- data ----
    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def work():
            return {'photos': fetch_json("/api/findmy/photos", timeout=5),
                    'laptop': fetch_json("/api/findmy/laptop", timeout=8),
                    'phone': fetch_json("/api/findmy/phone", timeout=5)}
        run_async(work, self._loaded)

    def _tick(self):
        if self.isVisible():
            self.refresh()

    def _loaded(self, res):
        self._busy = False
        if not isinstance(res, dict):
            return
        photos = (res.get('photos') or {}).get('photos')
        if photos is not None:
            self._show_photos(photos)
        lap = res.get('laptop') or {}
        st = lap.get('settings')
        if st:
            self.sw_walk.setChecked(st.get('walk_away_lock', True))
            self.sw_photo.setChecked(st.get('wrong_password_photo', True))
        lost = lap.get('lost') or {}
        if lost.get('lost'):
            since = f"Since {ago(lost['since']).lower()}. " if lost.get('since') else ""
            self.lost_title.setText("Marked as lost")
            self.lost_desc.setText(since + "It sends a photo, the Wi-Fi around it and its city every 10 minutes. "
                                   "Mark it found from your phone.")
            self.lost_tile.set_icon('alert', C.DANGER)
        else:
            self.lost_title.setText("Not marked as lost")
            self.lost_desc.setText("If it goes missing, mark it lost from your phone")
            self.lost_tile.set_icon('alert', C.T3)
        phone = res.get('phone') or {}
        near = phone.get('nearby') or {}
        if time.time() < self._nearby_until:
            self.t_near.set_text("Nearby: " + (near.get('word') or "comparing Wi-Fi…"), "Click to stop")
        events = [e for e in (phone.get('events') or [])]
        events += [dict(a, kind='laptop_wrong_password') for a in (lap.get('alerts') or [])
                   if a.get('kind') == 'wrong_password']
        events.sort(key=lambda e: e.get('t') or 0, reverse=True)
        self._show_recent(events[:12])

    def _show_photos(self, photos):
        sig = [p['name'] for p in photos]
        if sig == self._photos_sig:
            return
        self._photos_sig = sig
        while self.photo_grid.count():
            it = self.photo_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._tiles = {}
        self._cols_now = self._cols()
        self.photos_empty.setVisible(not photos)
        self.photo_count.setText(str(len(photos)) if photos else "")
        for i, ph in enumerate(photos):
            t = PhotoTile(ph)
            t.clicked.connect(lambda _=False, p=ph: self.open_photo(p))
            self.photo_grid.addWidget(t, i // self._cols_now, i % self._cols_now)
            self._tiles[ph['name']] = t
            if ph['name'] in self._thumbs:
                t.set_photo(self._thumbs[ph['name']])
            else:
                run_async(lambda n=ph['name']: fetch_bytes("/api/findmy/photo?name=" + quote(n)),
                          lambda data, n=ph['name']: self._got_thumb(n, data))

    def _cols(self):
        # the page less the card's padding and the scrollbar
        return max(1, (self.width() - 46 + 10) // self.TILE_STEP)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cols = self._cols()
        if self._tiles and cols != self._cols_now:
            self._cols_now = cols
            tiles = [self._tiles[n] for n in (self._photos_sig or []) if n in self._tiles]
            for t in tiles:
                self.photo_grid.removeWidget(t)
            for i, t in enumerate(tiles):
                self.photo_grid.addWidget(t, i // cols, i % cols)

    def _got_thumb(self, name, data):
        if not data:
            return
        self._thumbs[name] = data
        t = self._tiles.get(name)
        if t is not None:
            t.set_photo(data)

    def forget_photo(self, name):
        self._thumbs.pop(name, None)
        self._photos_sig = None
        self.refresh()

    def _show_recent(self, events):
        while self.recent_box.count():
            it = self.recent_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.recent_empty.setVisible(not events)
        for e in events:
            kind = e.get('kind') or ''
            what, icon, tint = FINDMY_WORDS.get(kind, (kind.replace('_', ' ').capitalize(), 'clock', C.T3))
            if kind == 'sim_changed' and e.get('operator'):
                what += f" ({e['operator']})"
            if kind == 'lost_report' and e.get('city'):
                what += f", {e['city']}"
            w = QFrame()
            w.setObjectName("Row")
            r = QHBoxLayout(w)
            r.setContentsMargins(0, 5, 0, 5)
            r.setSpacing(12)
            r.addWidget(IconTile(icon, tint, 32))
            r.addWidget(label(what, "value"), 1)
            r.addWidget(label(f"{ago(e.get('t'))}  ·  {time.strftime('%d %b, %H:%M', time.localtime(e.get('t') or 0))}",
                              "faint"))
            self.recent_box.addWidget(w)

    # ---- actions ----
    def open_photo(self, ph):
        data = self._thumbs.get(ph['name']) or fetch_bytes("/api/findmy/photo?name=" + quote(ph['name']))
        PhotoDialog(ph, data, self, self.win).exec()

    def set_setting(self, key, on, sw):
        def done(res):
            if not isinstance(res, dict) or res.get('status') != 'ok':
                sw.setChecked(not on)
                QMessageBox.warning(self.win, "Couldn't change the setting",
                                    str((res or {}).get('message') or (res or {}).get('error') or "No answer"))
        run_async(lambda: post_json("/api/findmy/settings", {key: on}), done)

    def toggle_nearby(self):
        on = time.time() >= self._nearby_until
        self._nearby_until = time.time() + 180 if on else 0.0
        self.t_near.set_text("Nearby: comparing Wi-Fi…" if on else "Nearby",
                             "Click to stop" if on else "Same place as you?")
        run_async(lambda: post_json("/api/findmy/phone/nearby", {"on": on}))
        if on:
            for s in (4, 9, 15, 25, 40, 60):
                QTimer.singleShot(s * 1000, self.refresh)
            QTimer.singleShot(181000, self._nearby_done)

    def _nearby_done(self):
        if time.time() >= self._nearby_until:
            self.t_near.set_text("Nearby", "Same place as you?")

    def open_wifilab(self):
        subprocess.Popen(["xdg-open", f"{DAEMON_URL}/wifilab"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def lock_now(self):
        subprocess.Popen(["loginctl", "lock-session"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def set_ringing(self, on):
        self.t_ring.set_text("Stop sound" if on else "Play sound",
                             "Ringing now" if on else "Even on silent")

    def _on_state(self, s):
        conn = bool(s.get('connected'))
        for t in (self.t_ring, self.t_near):
            t.setEnabled(conn)
        if not conn and time.time() < self._nearby_until:
            self._nearby_until = 0.0
            self._nearby_done()


# ==========================================
# Main window
# ==========================================

class OrbitWindow(QMainWindow):
    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.ringing = False
        self.setWindowTitle("Orbit")
        self.setWindowIcon(ctl.app_icon)
        self.resize(1080, 720)
        self.setMinimumSize(860, 580)
        self.setAcceptDrops(True)
        self._build()
        ctl.state.changed.connect(self.update_header)
        self.update_header(ctl.state.s)
        self.overview.update_state(ctl.state.s)
        self.send._on_state(ctl.state.s)

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(228)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 20, 16, 16)
        sl.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(self.ctl.app_icon.pixmap(QSize(30, 30)))
        brand.addWidget(logo)
        brand.addWidget(label("Orbit", "brand"))
        brand.addStretch()
        sl.addLayout(brand)
        sl.addSpacing(20)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        for i, (text, icon) in enumerate((("Overview", 'grid'), ("Files", 'folder'), ("Send", 'send'),
                                          ("Find my", 'shield'))):
            b = QPushButton(f"  {text}")
            b.setObjectName("Nav")
            b.setCheckable(True)
            b.setIcon(line_icon(icon, C.T2, 18))
            b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.nav.addButton(b, i)
            sl.addWidget(b)
        self.nav.idClicked.connect(self.show_page)
        sl.addStretch()

        comp = Card(margins=12, spacing=6)
        r = QHBoxLayout()
        r.setSpacing(10)
        r.addWidget(IconTile('monitor', C.ACCENT, 32))
        col = QVBoxLayout()
        col.setSpacing(0)
        col.addWidget(label("This computer", "faint"))
        host = label(socket.gethostname(), "mono")
        host.setStyleSheet(f"color: {C.T1};")
        col.addWidget(host)
        r.addLayout(col, 1)
        comp.lay.addLayout(r)
        sl.addWidget(comp)
        h.addWidget(side)

        content = QWidget()
        content.setObjectName("Page")
        cl = QVBoxLayout(content)
        cl.setContentsMargins(24, 20, 24, 16)
        cl.setSpacing(16)

        head = QHBoxLayout()
        head.setSpacing(14)
        self.h_tile = IconTile('smartphone', C.ACCENT, 46)
        head.addWidget(self.h_tile)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.h_name = label("Phone", "h1")
        self.h_sub = label("", "mono")
        col.addWidget(self.h_name)
        col.addWidget(self.h_sub)
        head.addLayout(col, 1)
        self.h_battery = Chip("--", icon='battery')
        head.addWidget(self.h_battery)
        self.h_status = Chip("Checking", dot=C.T3)
        head.addWidget(self.h_status)
        self.h_ring = button("Ring", 'bell', color=C.RING)
        self.h_ring.clicked.connect(self.toggle_ring)
        head.addWidget(self.h_ring)
        cl.addLayout(head)

        self.stack = QStackedWidget()
        self.overview = OverviewPage(self.ctl, self)
        self.files = FilesPage(self.ctl)
        self.send = SendPage(self.ctl)
        self.findmy = FindMyPage(self.ctl, self)
        for p in (self.overview, self.files, self.send, self.findmy):
            self.stack.addWidget(p)
        cl.addWidget(self.stack, 1)
        h.addWidget(content, 1)

        self.nav.button(0).setChecked(True)
        self._files_loaded = False

    def show_page(self, idx):
        self.nav.button(idx).setChecked(True)
        self.stack.setCurrentIndex(idx)
        if idx == 1 and not self._files_loaded:
            self._files_loaded = True
            self.files.load_directory(self.files.current_path, record_history=False)
        if idx == 3:
            self.findmy.refresh()

    def present(self, page=None):
        if page is not None:
            self.show_page(page)
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def toggle_ring(self):
        self.set_ringing(not self.ringing)
        run_async(lambda: post_json("/api/ring" if self.ringing else "/api/unring"))

    def set_ringing(self, on):
        self.ringing = on
        self.h_ring.setText("Stop" if on else "Ring")
        self.h_ring.setIcon(line_icon('bell-off' if on else 'bell', C.DANGER if on else C.RING, 18))
        self.h_ring.setProperty("kind", "danger" if on else None)
        repolish(self.h_ring)
        self.findmy.set_ringing(on)

    def update_header(self, s):
        if not s['service']:
            self.h_name.setText("Orbit service not running")
            self.h_sub.setText("Start it with: systemctl --user start orbit")
            self.h_status.set("Offline", C.DANGER)
            self.h_tile.set_icon('alert', C.DANGER)
            self.h_battery.hide()
            return
        self.h_tile.set_icon('smartphone', C.ONLINE if s['connected'] else C.ACCENT)
        self.h_name.setText(s['name'])
        bits = [f"{s['ip']}:{s['port']}" if s['ip'] else "Looking for the phone"]
        if s['android']:
            bits.append(f"Android {s['android']}")
        if s['model'] and s['model'].lower() not in s['name'].lower():
            bits.append(s['model'])
        self.h_sub.setText("  ·  ".join(bits))
        self.h_status.set("Connected" if s['connected'] else "Not connected",
                          C.ONLINE if s['connected'] else C.T3)
        bat = s.get('battery')
        if bat is not None:
            bat = int(bat)
            col = C.ACCENT if s['charging'] else (C.ONLINE if bat > 30 else (C.WARN if bat > 15 else C.DANGER))
            self.h_battery.set(f"{bat}%")
            self.h_battery.set_icon('battery-charging' if s['charging'] else 'battery', col)
            self.h_battery.show()
        else:
            self.h_battery.hide()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if os.path.isfile(u.toLocalFile())]
        if not paths:
            return
        e.acceptProposedAction()
        if self.stack.currentIndex() == 1:
            self.files.upload_paths(paths)
        else:
            self.show_page(2)
            self.send.send_paths(paths)


# ==========================================
# Pair dialog
# ==========================================

class PairDialog(QDialog):
    def __init__(self, ctl, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair a new phone")
        self.setWindowIcon(ctl.app_icon)
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 20)
        lay.setSpacing(16)
        h = QHBoxLayout()
        h.setSpacing(14)
        h.addWidget(IconTile('link', C.ACCENT, 46))
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(label("Pair a new phone", "h1"))
        col.addWidget(label("Pairing starts on the phone and is confirmed with a PIN.", "muted"))
        h.addLayout(col, 1)
        lay.addLayout(h)

        steps = Card()
        for i, text in enumerate((
                "Open Orbit on your phone. Keep it on the same Wi-Fi as this computer.",
                "Tap Add computer and pick this computer, or type the address below.",
                "A 6-digit PIN appears here as a notification. Type it on the phone.")):
            r = QHBoxLayout()
            r.setSpacing(12)
            n = QLabel(str(i + 1))
            n.setFixedSize(26, 26)
            n.setAlignment(Qt.AlignmentFlag.AlignCenter)
            n.setStyleSheet(f"background: #1F2740; color: {C.ACCENT}; border-radius: 13px; font-weight: 600;")
            r.addWidget(n, 0, Qt.AlignmentFlag.AlignTop)
            r.addWidget(label(text, wrap=True), 1)
            steps.lay.addLayout(r)
        lay.addWidget(steps)

        addr = Card()
        r = QHBoxLayout()
        r.setSpacing(12)
        r.addWidget(IconTile('monitor', C.TERM, 40))
        col = QVBoxLayout()
        col.setSpacing(2)
        self.host = label(socket.gethostname(), "value")
        self.url = label("Looking up address", "mono")
        self.url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        col.addWidget(self.host)
        col.addWidget(self.url)
        r.addLayout(col, 1)
        self.copy = button("Copy", 'copy', size='small')
        self.copy.clicked.connect(self._copy)
        self.copy.setEnabled(False)
        r.addWidget(self.copy)
        addr.lay.addLayout(r)
        lay.addWidget(addr)

        b = QHBoxLayout()
        b.addStretch()
        done = button("Done", kind='primary')
        done.clicked.connect(self.accept)
        b.addWidget(done)
        lay.addLayout(b)
        run_async(lambda: fetch_json("/api/info"), self._info)

    def _info(self, info):
        try:
            if not isinstance(info, dict) or info.get('error'):
                self.url.setText("Orbit service isn't responding")
                self.url.setStyleSheet(f"color: {C.DANGER};")
                return
            self.host.setText(info.get('hostname') or socket.gethostname())
            self.url.setText(info.get('local_url') or "")
            self.copy.setEnabled(bool(info.get('local_url')))
        except RuntimeError:
            pass

    def _copy(self):
        QApplication.clipboard().setText(self.url.text())
        self.copy.setText("Copied")


# ==========================================
# Laser pointer overlay (driven by UDP packets on 127.0.0.1:1763)
# ==========================================

class LaserPointerOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet("background: transparent;")
        self.x_pos = 0.5
        self.y_pos = 0.5
        self.fade_timer = QTimer(self)
        self.fade_timer.setSingleShot(True)
        self.fade_timer.setInterval(800)
        self.fade_timer.timeout.connect(self.hide_overlay)

    def handle_packet(self, data):
        if not isinstance(data, dict):
            return
        if data.get('stop') or (data.get('laser') is False and 'dx' not in data):
            self.hide_overlay()
            return
        if not self.isVisible():
            self.x_pos = 0.5
            self.y_pos = 0.5
            self.show_overlay()
        if 'dx' in data or 'dy' in data:
            dx = float(data.get('dx', 0))
            dy = float(data.get('dy', 0))
            screen = self.screen() or QApplication.primaryScreen()
            ratio = 16.0 / 9.0
            if screen and screen.size().height() > 0:
                ratio = float(screen.size().width()) / float(screen.size().height())
            elif self.height() > 0:
                ratio = float(self.width()) / float(self.height())
            self.x_pos = min(0.995, max(0.005, self.x_pos + dx))
            self.y_pos = min(0.995, max(0.005, self.y_pos + dy * ratio))
            self.update()
        self.fade_timer.start(1000)

    def show_overlay(self):
        screen = QApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.raise_()
        self.update()

    def hide_overlay(self):
        self.fade_timer.stop()
        self.hide()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx = int(self.x_pos * self.width())
        cy = int(self.y_pos * self.height())
        grad = QRadialGradient(cx, cy, 26)
        grad.setColorAt(0.0, QColor(255, 255, 255, 255))
        grad.setColorAt(0.2, QColor(255, 50, 50, 245))
        grad.setColorAt(0.45, QColor(255, 10, 10, 200))
        grad.setColorAt(0.75, QColor(255, 0, 0, 70))
        grad.setColorAt(1.0, QColor(255, 0, 0, 0))
        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - 26, cy - 26, 52, 52)
        p.setBrush(QColor(255, 255, 255, 245))
        p.drawEllipse(cx - 3, cy - 3, 6, 6)


class LaserListenerThread(QThread):
    laser_packet = pyqtSignal(dict)

    def run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', LASER_PORT))
            while True:
                data, _ = sock.recvfrom(2048)
                try:
                    payload = json.loads(data.decode('utf-8'))
                    if isinstance(payload, dict):
                        self.laser_packet.emit(payload)
                except Exception:
                    pass
        except Exception:
            pass


# ==========================================
# Tray application
# ==========================================

class OrbitApp:
    def __init__(self, app, args, selftest=False):
        self.app = app
        self.args = args
        self.app_icon = app_icon()
        self.app.setWindowIcon(self.app_icon)
        self.state = PhoneState()
        self.transfers = Transfers()
        self.window = None
        self._dev_sig = None
        self._status_text = ""

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(tray_icon_mono())
        self.tray.setToolTip("Orbit")
        self.tray.activated.connect(self.on_tray_activated)
        self.menu = QMenu()
        self.build_menu()
        self.tray.setContextMenu(self.menu)
        if not selftest:
            self.tray.setVisible(True)

        self.state.changed.connect(self.update_status)
        self.laser_overlay = LaserPointerOverlay()
        self.laser_thread = None
        self.server = None
        if not selftest:
            self.laser_thread = LaserListenerThread()
            self.laser_thread.laser_packet.connect(self.laser_overlay.handle_packet)
            self.laser_thread.start()
            self._start_ipc()

        self.timer = QTimer()
        self.timer.timeout.connect(self.state.refresh)
        if not selftest:
            self.timer.start(4000)
            self.state.refresh()

    # ---- IPC for second launches (orbit-tray --browse while running) ----
    def _start_ipc(self):
        try:
            QLocalServer.removeServer(SOCK_PATH)
            self.server = QLocalServer()
            self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if self.server.listen(SOCK_PATH):
                self.server.newConnection.connect(self._on_ipc)
            else:
                self.server = None
        except Exception:
            self.server = None

    def _on_ipc(self):
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()

            def ready(c=conn):
                while c.canReadLine():
                    try:
                        self.handle_command(json.loads(bytes(c.readLine()).decode()))
                    except Exception:
                        traceback.print_exc()
            conn.readyRead.connect(ready)
            conn.disconnected.connect(conn.deleteLater)
            if conn.bytesAvailable():
                ready()

    def handle_command(self, cmd):
        c = cmd.get('cmd')
        if c == 'browse':
            self.open_explorer()
        elif c == 'show':
            self.show_phone_info()
        elif c == 'findmy':
            self.open_find_my_page()
        elif c == 'send':
            self.ensure_window().present(2)
            self.window.send.send_paths(cmd.get('files') or [])

    # ---- windows ----
    def ensure_window(self):
        if self.window is None:
            self.window = OrbitWindow(self)
        return self.window

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.window is not None and self.window.isVisible() and self.window.isActiveWindow() \
                    and reason == QSystemTrayIcon.ActivationReason.Trigger:
                self.window.hide()
            else:
                self.show_phone_info()

    def show_phone_info(self):
        self.state.refresh()
        self.ensure_window().present(0)

    def open_explorer(self):
        self.ensure_window().present(1)

    def open_find_my_page(self):
        self.ensure_window().present(3)

    def open_find_my(self):
        """The Find my phone map, in the browser (the key becomes a local-only cookie there)."""
        from urllib.parse import quote
        tok = local_token()
        if not tok:
            notify("Find my phone", "The Orbit service isn't running.")
            return
        subprocess.Popen(["xdg-open", f"{DAEMON_URL}/findmy?ltok={quote(tok)}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def window_status(self, text):
        if self.window is not None:
            self.window.files.set_status(text, C.DANGER)

    # ---- menu ----
    def build_menu(self):
        # Two quiet lines say how the phone is; below them, the things you'd do from here.
        # (Phone details, pairing and unpairing live in the window.)
        self.menu.clear()
        self.act_status_header = self.menu.addAction("Orbit")
        self.act_status_conn = self.menu.addAction("Checking…")
        for a in (self.act_status_header, self.act_status_conn):
            a.setEnabled(False)
        f = self.act_status_header.font()
        f.setBold(True)
        self.act_status_header.setFont(f)
        self.menu.addSeparator()
        self.menu.addAction("Open Orbit", self.show_phone_info)
        self.act_send = self.menu.addAction("Send files to phone…", self.send_file_dialog)
        self.act_browse = self.menu.addAction("Browse phone files", self.open_explorer)
        self.act_clip = self.menu.addAction("Send clipboard to phone", self.sync_clipboard)
        self.act_ring = self.menu.addAction("Ring phone", self._toggle_ring)
        self.menu.addAction("Find my phone", self.open_find_my)
        self.menu.addAction("Who tried to get in", self.open_find_my_page)
        # Orbit Cam's format, one at a time: Sharp suits current apps; Raw and Compatible are for
        # an app that doesn't see the camera or shows a broken picture
        self.cam_menu = self.menu.addMenu("Orbit Cam")
        self.cam_group = QActionGroup(self.cam_menu)
        self.cam_actions = {}
        for mode, label in (("sharp", "Sharp · 1080p"), ("raw", "Raw · 720p"),
                            ("compatible", "Compatible · for older apps")):
            a = self.cam_menu.addAction(label)
            a.setCheckable(True)
            self.cam_group.addAction(a)
            a.triggered.connect(lambda checked, m=mode: self._switch_cam_mode(m))
            self.cam_actions[mode] = a
        self.menu.aboutToShow.connect(self._refresh_cam_format)
        self.menu.addSeparator()
        self.act_pair = self.menu.addAction("Pair a phone…", self.pair_new_device)
        self.menu.addAction("Quit Orbit", self.app.quit)

    _ringing = False

    def _refresh_cam_format(self):
        res = fetch_json('/api/webcam/format', timeout=1)
        self._cam_mode = res.get('mode') if res.get('status') == 'ok' else None
        for mode, a in self.cam_actions.items():
            a.setChecked(mode == self._cam_mode)
        self.cam_menu.setEnabled(self._cam_mode is not None)

    def _switch_cam_mode(self, mode):
        res = post_json('/api/webcam/format', {"mode": mode})
        if res.get('status') == 'ok':
            self.tray.showMessage("Orbit Cam", {"sharp": "Sharp: 1080p, as real webcams send it.",
                                                "raw": "Raw: 720p, uncompressed.",
                                                "compatible": "Compatible: 720p for older apps."}[mode],
                                  QSystemTrayIcon.MessageIcon.Information, 4000)
        else:
            self._refresh_cam_format()          # back to the mode it still has
            self.tray.showMessage("Orbit Cam", res.get('message') or res.get('error') or "Couldn't switch the format",
                                  QSystemTrayIcon.MessageIcon.Warning, 6000)

    def _toggle_ring(self):
        if self._ringing:
            self._ringing = False
            self.unring()
        else:
            self._ringing = True
            self.ring()
            QTimer.singleShot(60000, lambda: setattr(self, '_ringing', False) or self._ring_label())
        self._ring_label()

    def _ring_label(self):
        self.act_ring.setText("Stop ringing" if self._ringing else "Ring phone")

    def update_status(self, s):
        devs = s.get('devices') or []
        self.act_pair.setText("Pair another phone…" if devs else "Pair a phone…")
        if s['service']:
            name, conn, bat = s['name'], s['connected'], s.get('battery')
            self.act_status_header.setText(name)
            line = "Connected" if conn else "Not connected"
            if bat is not None:
                line += f" · battery {bat}%" + (", charging" if s['charging'] else "")
            self.act_status_conn.setText(line)
            for a in (self.act_send, self.act_browse, self.act_clip, self.act_ring):
                a.setEnabled(bool(conn))          # nothing to send to while it's away
            bat_str = f", {bat}%" if bat is not None else ""
            tip = f"{name}: {'connected' if conn else 'not connected'}{bat_str}"
            self.tray.setToolTip(f"Orbit\n{tip}\nClick to open")
            self.tray.setIcon(self._tray_icon_for(conn))
        else:
            self.act_status_header.setText("Orbit")
            self.act_status_conn.setText("The Orbit service isn't running")
            for a in (self.act_send, self.act_browse, self.act_clip, self.act_ring):
                a.setEnabled(False)
            self.tray.setToolTip("Orbit\nService not running")
            self.tray.setIcon(self._tray_icon_for(False))

    _tray_icons = {}

    def _tray_icon_for(self, connected):
        # Full-strength mark when the phone is connected, dimmed when it isn't.
        key = bool(connected)
        if key not in self._tray_icons:
            self._tray_icons[key] = tray_icon_mono(C.T1 if connected else C.T3)
        return self._tray_icons[key]

    # ---- actions ----
    def ring(self):
        run_async(lambda: post_json("/api/ring"))
        if self.window:
            self.window.set_ringing(True)

    def unring(self):
        run_async(lambda: post_json("/api/unring"))
        if self.window:
            self.window.set_ringing(False)

    def sync_clipboard(self):
        def done(res):
            if isinstance(res, dict) and res.get('error'):
                notify("Clipboard not sent", res.get('message') or res['error'])
            else:
                notify("Clipboard sent", "Computer clipboard sent to the phone")
        run_async(lambda: post_json("/api/clipboard"), done)

    def pair_new_device(self):
        dlg = PairDialog(self, self.window if (self.window and self.window.isVisible()) else None)
        dlg.exec()
        self.state.refresh()

    def run_send_helper(self, files, device=None):
        tool = find_tool(SEND_TOOLS)
        if not tool:
            return False
        cmd = [tool]
        if device:
            cid = device.get('client_id') or device.get('token')
            if cid:
                cmd += ["--device", cid]
        subprocess.Popen(cmd + list(files))
        return True

    def send_file_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Send files to phone")
        if files and not self.run_send_helper(files):
            for p in files:
                self.transfers.add({'kind': 'upload', 'origin': 'tray', 'path': p, 'remote_dir': PHONE_DOWNLOADS})
            notify("Sending to phone", f"{len(files)} file(s) on the way to Download")

    def send_file_to_device(self, device):
        files, _ = QFileDialog.getOpenFileNames(None, f"Send files to {device.get('client_name', 'phone')}")
        if files and not self.run_send_helper(files, device):
            for p in files:
                self.transfers.add({'kind': 'upload', 'origin': 'tray', 'path': p, 'remote_dir': PHONE_DOWNLOADS})

    def confirm_unpair_device(self, device, parent=None):
        name = device.get('client_name', 'this phone')
        ret = QMessageBox.question(
            parent, "Unpair phone",
            f"Unpair {name}?\n\nIt will be disconnected and its pairing keys revoked. "
            "You can pair it again later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        res = post_json("/api/devices/unpair", {"token": device.get('token'), "client_id": device.get('client_id')})
        self.state.refresh()
        if isinstance(res, dict) and (res.get('error') or res.get('status') == 'error'):
            QMessageBox.warning(parent, "Couldn't unpair", str(res.get('message') or res.get('error')))
        else:
            QMessageBox.information(parent, "Phone unpaired", f"{name} has been unpaired.")

    def run(self):
        if self.args.browse:
            self.open_explorer()
        elif self.args.show:
            self.show_phone_info()
        elif self.args.findmy:
            self.open_find_my_page()
        if self.args.send:
            self.handle_command({'cmd': 'send', 'files': [os.path.abspath(p) for p in self.args.send]})
        return self.app.exec()


# ==========================================
# Entry points
# ==========================================

def make_qapp():
    app = QApplication(sys.argv)
    app.setApplicationName("Orbit")
    app.setApplicationDisplayName("Orbit")
    app.setDesktopFileName("orbit-tray")
    app.setQuitOnLastWindowClosed(False)
    global _INVOKER
    _INVOKER = _Invoker()
    load_fonts()
    f = QFont(UI_FONT)
    f.setPixelSize(13)
    app.setFont(f)
    app.setStyleSheet(build_qss())
    return app


def selftest(args):
    """Build every window offscreen, load live data once, report, exit."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = make_qapp()
    ctl = OrbitApp(app, args, selftest=True)
    report = {"ui_font": UI_FONT, "mono_font": MONO_FONT, "token_present": bool(local_token())}
    ctl.state.refresh_sync()
    s = ctl.state.s
    report["service"] = s["service"]
    report["phone"] = {k: s[k] for k in ("name", "connected", "battery", "ip")}
    report["devices"] = len(s["devices"])
    win = ctl.ensure_window()
    win.show()
    for i in range(4):
        win.show_page(i)
        app.processEvents()
    win.files.load_directory_sync(PHONE_ROOT)
    report["files_in_root"] = win.files.loaded_count
    report["files_status"] = win.files.status.text()
    win.show_page(0)
    report["tray_menu"] = [a.text() for a in ctl.menu.actions() if a.text()]
    dlg = PairDialog(ctl)
    dlg.show()
    app.processEvents()
    dlg.close()
    if args.screenshot:
        base, ext = os.path.splitext(args.screenshot)
        ext = ext or ".png"
        for i, name in enumerate(("overview", "files", "send", "findmy")):
            win.show_page(i)
            deadline = time.time() + (3.0 if name == "findmy" else 0)
            for _ in range(5):
                app.processEvents()
            while time.time() < deadline:          # Find my loads its photos in the background
                app.processEvents()
                time.sleep(0.02)
            win.grab().save(f"{base}-{name}{ext}")
        dlg = PairDialog(ctl)
        dlg.show()
        deadline = time.time() + 1.5
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.02)
        dlg.grab().save(f"{base}-pair{ext}")
        ctl.tray.icon().pixmap(64, 64).save(f"{base}-tray{ext}")
        app_icon().pixmap(128, 128).save(f"{base}-icon{ext}")
        report["screenshots"] = f"{base}-*{ext}"
    print(json.dumps(report, indent=2))
    return 0


def parse_args():
    p = argparse.ArgumentParser(prog="orbit-tray",
                                description="Orbit: tray app and dashboard for your phone.")
    p.add_argument("--browse", action="store_true", help="open the phone file browser")
    p.add_argument("--show", action="store_true", help="open the overview window")
    p.add_argument("--findmy", action="store_true", help="open Find my: who tried to get in, and its settings")
    p.add_argument("--send", nargs="+", metavar="FILE", help="send files to the phone")
    p.add_argument("--selftest", action="store_true",
                   help="build all windows offscreen, load live status once, print a report and exit")
    p.add_argument("--screenshot", metavar="PATH", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"Orbit {APP_VERSION}")
    args, _unknown = p.parse_known_args()   # Qt may add its own -platform etc.
    return args


def main():
    args = parse_args()
    if args.selftest:
        sys.exit(selftest(args))
    if not acquire_single_instance_lock():
        cmd = None
        if args.send:
            cmd = {'cmd': 'send', 'files': [os.path.abspath(f) for f in args.send]}
        elif args.browse:
            cmd = {'cmd': 'browse'}
        elif args.show:
            cmd = {'cmd': 'show'}
        elif args.findmy:
            cmd = {'cmd': 'findmy'}
        if cmd and forward_to_running(cmd):
            print("[INFO] Orbit is already running; passed the request to it.")
        else:
            print("[INFO] Another instance of Orbit is already running. Exiting.")
        sys.exit(0)
    app = make_qapp()
    ctl = OrbitApp(app, args)
    sys.exit(ctl.run())


if __name__ == '__main__':
    main()

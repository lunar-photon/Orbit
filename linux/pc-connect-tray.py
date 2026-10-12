#!/usr/bin/env python3
import sys
import os
import json
import urllib.request
import urllib.error
from urllib.parse import quote, unquote
import subprocess
import time
import tempfile
import threading
import fcntl

from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QLineEdit, QFileDialog, QMessageBox,
    QHeaderView, QListWidget, QListWidgetItem, QAbstractItemView,
    QInputDialog, QProgressBar
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QIcon, QAction, QFont, QColor, QDragEnterEvent, QDropEvent, QCursor


# The daemon only trusts local tools that present this user's key (a 0600 file it
# rewrites on each start). Attach it to requests for the local daemon, never others.
def local_token():
    try:
        with open(os.path.expanduser('~/.config/pc-authenticator/local_token')) as f:
            return f.read().strip()
    except OSError:
        return ''

class _LocalTokenHandler(urllib.request.BaseHandler):
    def http_request(self, req):
        if req.host.split(':')[0] in ('127.0.0.1', 'localhost'):
            req.add_header('X-Local-Token', local_token())
        return req

urllib.request.install_opener(urllib.request.build_opener(_LocalTokenHandler))

LOCK_FILE = f"/run/user/{os.getuid()}/pc-connect-tray.lock" if os.path.exists(f"/run/user/{os.getuid()}") else os.path.expanduser("~/.config/pc-authenticator/tray.lock")
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

DAEMON_URL = "http://127.0.0.1:1760"

def fetch_json(endpoint, timeout=3):
    try:
        req = urllib.request.Request(f"{DAEMON_URL}{endpoint}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}

def post_json(endpoint, data=None, timeout=5):
    try:
        body = json.dumps(data or {}).encode('utf-8')
        req = urllib.request.Request(f"{DAEMON_URL}{endpoint}", data=body, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"error": str(e)}

def format_size(bytes_num):
    if not bytes_num:
        return "-"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.1f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} PB"

def format_time(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

# ==========================================
# Phone File Explorer Window
# ==========================================

class PhoneExplorerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PC Connect - Phone File Explorer")
        self.resize(950, 600)
        self.setAcceptDrops(True)

        self.current_path = "/storage/emulated/0"
        self.history = [self.current_path]
        self.history_idx = 0

        self.setup_ui()
        self.load_directory(self.current_path)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 1. Left Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet("background-color: #1e222d; border-right: 1px solid #2e3440;")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(10, 15, 10, 15)

        title_lbl = QLabel("📱 PHONE STORAGE")
        title_lbl.setStyleSheet("color: #88c0d0; font-weight: bold; font-size: 11px;")
        sidebar_layout.addWidget(title_lbl)

        self.bookmark_list = QListWidget()
        self.bookmark_list.setStyleSheet("""
            QListWidget { background: transparent; border: none; outline: none; }
            QListWidget::item { padding: 8px 12px; color: #eceff4; border-radius: 6px; }
            QListWidget::item:hover { background-color: #2e3440; }
            QListWidget::item:selected { background-color: #3b4252; color: #88c0d0; font-weight: bold; }
        """)

        bookmarks = [
            ("📱 Internal Storage", "/storage/emulated/0"),
            ("📷 Camera & DCIM", "/storage/emulated/0/DCIM"),
            ("📥 Downloads", "/storage/emulated/0/Download"),
            ("📄 Documents", "/storage/emulated/0/Documents"),
            ("🖼️ Pictures", "/storage/emulated/0/Pictures"),
            ("🎵 Music", "/storage/emulated/0/Music"),
            ("🎬 Movies", "/storage/emulated/0/Movies")
        ]
        for label, path in bookmarks:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.bookmark_list.addItem(item)
        self.bookmark_list.itemClicked.connect(self.on_bookmark_clicked)
        sidebar_layout.addWidget(self.bookmark_list)
        sidebar_layout.addStretch()

        # Ring & Stop Phone button in sidebar
        btn_ring = QPushButton("🔔 Ring Phone")
        btn_ring.setStyleSheet("background-color: #3b4252; color: #eceff4; border-radius: 6px; padding: 8px;")
        btn_ring.clicked.connect(lambda: post_json("/api/ring"))
        sidebar_layout.addWidget(btn_ring)

        btn_unring = QPushButton("🛑 Stop Ringing")
        btn_unring.setStyleSheet("background-color: #4c1d24; color: #ff8080; border-radius: 6px; padding: 6px;")
        btn_unring.clicked.connect(lambda: post_json("/api/unring"))
        sidebar_layout.addWidget(btn_unring)

        main_layout.addWidget(sidebar)

        # 2. Right Content Area
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(15, 15, 15, 15)
        content_layout.setSpacing(10)

        # Navigation Bar
        nav_layout = QHBoxLayout()
        self.btn_back = QPushButton("◀")
        self.btn_back.setFixedWidth(36)
        self.btn_back.clicked.connect(self.navigate_back)
        self.btn_up = QPushButton("⬆ Up")
        self.btn_up.clicked.connect(self.navigate_up)
        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.clicked.connect(lambda: self.load_directory(self.current_path))

        self.path_edit = QLineEdit(self.current_path)
        self.path_edit.returnPressed.connect(self.on_path_entered)
        self.path_edit.setStyleSheet("padding: 6px 10px; border-radius: 6px; background: #2e3440; color: #eceff4; border: 1px solid #434c5e;")

        nav_layout.addWidget(self.btn_back)
        nav_layout.addWidget(self.btn_up)
        nav_layout.addWidget(self.btn_refresh)
        nav_layout.addWidget(self.path_edit)
        content_layout.addLayout(nav_layout)

        # Drop Zone & Progress Indicator
        self.status_bar = QLabel("💡 Tip: Double-click a file to preview it temporarily. Right-click to download to PC.")
        self.status_bar.setStyleSheet("color: #88c0d0; font-size: 12px; padding: 4px;")
        content_layout.addWidget(self.status_bar)

        # File Table
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet("""
            QTableWidget { background-color: #242933; color: #eceff4; border: 1px solid #2e3440; border-radius: 8px; }
            QHeaderView::section { background-color: #1e222d; color: #88c0d0; padding: 6px; border: none; font-weight: bold; }
            QTableWidget::item { padding: 6px; }
            QTableWidget::item:selected { background-color: #3b4252; color: #88c0d0; }
        """)
        self.table.cellDoubleClicked.connect(self.on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        content_layout.addWidget(self.table)

        # Action Buttons Bottom
        btn_layout = QHBoxLayout()
        self.btn_download = QPushButton("📥 Download to PC")
        self.btn_download.setStyleSheet("background-color: #88c0d0; color: #1e222d; font-weight: bold; padding: 8px 16px; border-radius: 6px;")
        self.btn_download.clicked.connect(self.download_selected)

        self.btn_upload = QPushButton("📤 Upload Files...")
        self.btn_upload.setStyleSheet("background-color: #434c5e; color: #eceff4; padding: 8px 16px; border-radius: 6px;")
        self.btn_upload.clicked.connect(self.upload_files)

        self.btn_mkdir = QPushButton("📁 New Folder")
        self.btn_mkdir.setStyleSheet("background-color: #434c5e; color: #eceff4; padding: 8px 16px; border-radius: 6px;")
        self.btn_mkdir.clicked.connect(self.create_folder)

        self.btn_delete = QPushButton("🗑️ Delete")
        self.btn_delete.setStyleSheet("background-color: #bf616a; color: #eceff4; padding: 8px 16px; border-radius: 6px;")
        self.btn_delete.clicked.connect(self.delete_selected)

        btn_layout.addWidget(self.btn_download)
        btn_layout.addWidget(self.btn_upload)
        btn_layout.addWidget(self.btn_mkdir)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_delete)
        content_layout.addLayout(btn_layout)

        main_layout.addWidget(content)

    def on_bookmark_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if path:
            self.load_directory(path)

    def on_path_entered(self):
        path = self.path_edit.text().strip()
        if path:
            self.load_directory(path)

    def navigate_back(self):
        if self.history_idx > 0:
            self.history_idx -= 1
            self.load_directory(self.history[self.history_idx], record_history=False)

    def navigate_up(self):
        if self.current_path in ("/", "/storage/emulated/0"):
            return
        parent = os.path.dirname(self.current_path.rstrip('/'))
        if parent:
            self.load_directory(parent)

    def load_directory(self, path, record_history=True):
        self.current_path = path
        self.path_edit.setText(path)
        if record_history:
            if not self.history or self.history[-1] != path:
                self.history.append(path)
                self.history_idx = len(self.history) - 1

        self.status_bar.setText(f"Loading {path}...")
        QApplication.processEvents()

        data = fetch_json(f"/api/phone/files/list?path={quote(path)}")
        if isinstance(data, dict) and "error" in data:
            self.status_bar.setText(f"❌ Error: {data.get('message', data['error'])}")
            self.table.setRowCount(0)
            return

        self.table.setRowCount(0)
        files = data if isinstance(data, list) else []
        self.status_bar.setText(f"📁 {len(files)} items in {path}")

        for f in files:
            row = self.table.rowCount()
            self.table.insertRow(row)

            is_dir = f.get('is_dir', False)
            icon = "📁" if is_dir else "📄"
            name = f.get('name', '')
            size = format_size(f.get('size', 0)) if not is_dir else "-"
            mtime = format_time(f.get('mtime', 0))

            item_name = QTableWidgetItem(f"{icon}  {name}")
            item_name.setData(Qt.ItemDataRole.UserRole, f)
            item_size = QTableWidgetItem(size)
            item_mtime = QTableWidgetItem(mtime)

            self.table.setItem(row, 0, item_name)
            self.table.setItem(row, 1, item_size)
            self.table.setItem(row, 2, item_mtime)

    def on_cell_double_clicked(self, row, col):
        item = self.table.item(row, 0)
        if not item:
            return
        f = item.data(Qt.ItemDataRole.UserRole)
        if f.get('is_dir'):
            self.load_directory(f.get('path'))
        else:
            self.preview_file(f)

    def show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item:
            return
        row = item.row()
        f_item = self.table.item(row, 0)
        f = f_item.data(Qt.ItemDataRole.UserRole)

        menu = QMenu(self)
        if not f.get('is_dir'):
            act_dl = menu.addAction("📥 Download to PC (~/Downloads)")
            act_dl.triggered.connect(lambda: self.download_selected() if len(self.table.selectedIndexes()) > 1 else self.download_file(f))
            act_prev = menu.addAction("👁️ Preview (Open Temporarily)")
            act_prev.triggered.connect(lambda: self.preview_file(f))
            menu.addSeparator()
        act_del = menu.addAction("🗑️ Delete")
        act_del.triggered.connect(lambda: self.delete_file(f))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def preview_file(self, f):
        fn = f.get('name', 'file')
        temp_dir = os.path.join(tempfile.gettempdir(), 'pc-connect-preview')
        os.makedirs(temp_dir, exist_ok=True)
        dest = os.path.join(temp_dir, fn)

        self.status_bar.setText(f"⏳ Downloading temporary preview for {fn}...")

        def fetch_and_open():
            try:
                url = f"{DAEMON_URL}/api/phone/files/download?path={quote(f.get('path'))}&preview=1"
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    with open(dest, 'wb') as out_f:
                        while chunk := resp.read(65536):
                            out_f.write(chunk)
                subprocess.Popen(["xdg-open", dest])
                QTimer.singleShot(0, lambda: self.status_bar.setText(f"👁️ Previewing {fn} in default viewer"))
            except Exception as e:
                err_msg = str(e)
                QTimer.singleShot(0, lambda: self.status_bar.setText(f"❌ Preview failed: {err_msg}"))

        threading.Thread(target=fetch_and_open, daemon=True).start()

    def download_selected(self):
        rows = set(index.row() for index in self.table.selectedIndexes())
        for row in rows:
            f = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if not f.get('is_dir'):
                self.download_file(f)

    def download_file(self, f):
        fn = f.get('name', 'file')
        dl_dir = os.path.expanduser('~/Downloads')
        dest = os.path.join(dl_dir, fn)
        base, ext = os.path.splitext(fn)
        c = 1
        while os.path.exists(dest):
            dest = os.path.join(dl_dir, f"{base} ({c}){ext}")
            c += 1

        self.status_bar.setText(f"⏳ Downloading {fn}...")
        QApplication.processEvents()

        try:
            url = f"{DAEMON_URL}/api/phone/files/download?path={quote(f.get('path'))}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(dest, 'wb') as out_f:
                    while chunk := resp.read(65536):
                        out_f.write(chunk)
            self.status_bar.setText(f"✅ Downloaded to {dest}")
            subprocess.Popen(['notify-send', '-i', 'document-save', '-a', 'PC Connect', 'File Downloaded', f"Saved to {dest}"])
        except Exception as e:
            self.status_bar.setText(f"❌ Download failed: {e}")
            QMessageBox.critical(self, "Download Error", str(e))

    def upload_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Files to Upload to Phone")
        if not paths:
            return
        for p in paths:
            self.do_upload(p)
        self.load_directory(self.current_path)

    def do_upload(self, filepath):
        fn = os.path.basename(filepath)
        self.status_bar.setText(f"⏳ Uploading {fn} to phone...")
        QApplication.processEvents()
        try:
            url = f"{DAEMON_URL}/api/phone/files/upload?path={quote(self.current_path)}"
            with open(filepath, 'rb') as f:
                data = f.read()
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/octet-stream', 'X-Filename': quote(fn)},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.status_bar.setText(f"✅ Uploaded {fn}")
        except Exception as e:
            self.status_bar.setText(f"❌ Upload error: {e}")

    def create_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Enter folder name:")
        if ok and name.strip():
            post_json("/api/phone/files/mkdir", {"path": self.current_path, "name": name.strip()})
            self.load_directory(self.current_path)

    def delete_selected(self):
        rows = set(index.row() for index in self.table.selectedIndexes())
        for row in rows:
            f = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            self.delete_file(f)

    def delete_file(self, f):
        fn = f.get('name')
        ans = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete:\n{fn}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            post_json("/api/phone/files/delete", {"path": f.get('path')})
            self.load_directory(self.current_path)

    # Drag and Drop support
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        for url in urls:
            path = url.toLocalFile()
            if os.path.isfile(path):
                self.do_upload(path)
        self.load_directory(self.current_path)

# ==========================================
# Laser Pointer Overlay (KDE Connect Style)
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
        from PyQt6.QtGui import QPainter, QRadialGradient, QColor
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        cx = int(self.x_pos * w)
        cy = int(self.y_pos * h)

        # Draw glowing red laser dot (KDE Connect style)
        grad = QRadialGradient(cx, cy, 26)
        grad.setColorAt(0.0, QColor(255, 255, 255, 255))      # Intense white core
        grad.setColorAt(0.2, QColor(255, 50, 50, 245))        # Bright red core
        grad.setColorAt(0.45, QColor(255, 10, 10, 200))       # Vivid laser red
        grad.setColorAt(0.75, QColor(255, 0, 0, 70))          # Glowing halo
        grad.setColorAt(1.0, QColor(255, 0, 0, 0))            # Soft edge

        p.setBrush(grad)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(cx - 26, cy - 26, 52, 52)

        # Pinpoint white laser center
        p.setBrush(QColor(255, 255, 255, 245))
        p.drawEllipse(cx - 3, cy - 3, 6, 6)


class LaserListenerThread(QThread):
    laser_packet = pyqtSignal(dict)

    def run(self):
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', 1763))
            while True:
                data, _ = sock.recvfrom(2048)
                try:
                    payload = json.loads(data.decode('utf-8'))
                    self.laser_packet.emit(payload)
                except Exception:
                    pass
        except Exception:
            pass

# ==========================================
# Phone Status & Info Window (KDE Connect style)
# ==========================================

class PhoneInfoWindow(QWidget):
    def __init__(self, parent_tray=None):
        super().__init__()
        self.parent_tray = parent_tray
        self.setWindowTitle("PC Connect - Device Status")
        self.setWindowIcon(QIcon.fromTheme("smartphone", QIcon.fromTheme("phone")))
        self.resize(460, 520)
        self.is_ringing = False

        self.setup_ui()
        self.refresh_data()

        # Auto-refresh while open
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_data)
        self.poll_timer.start(3000)

    def setup_ui(self):
        self.setStyleSheet("""
            QWidget {
                background-color: #1e222d;
                color: #eceff4;
                font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            }
            QPushButton {
                background-color: #3b4252;
                color: #eceff4;
                border: 1px solid #434c5e;
                border-radius: 6px;
                padding: 8px 12px;
                font-weight: 500;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #434c5e;
                border-color: #88c0d0;
            }
            QPushButton:pressed {
                background-color: #4c566a;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # 1. Device Header Card
        header_card = QWidget()
        header_card.setStyleSheet("background-color: #242933; border: 1px solid #3b4252; border-radius: 10px;")
        h_layout = QHBoxLayout(header_card)
        h_layout.setContentsMargins(14, 12, 14, 12)

        icon_lbl = QLabel("📱")
        icon_lbl.setStyleSheet("font-size: 34px; background: transparent; border: none;")
        h_layout.addWidget(icon_lbl)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(2)
        self.device_name_lbl = QLabel("Android Phone")
        self.device_name_lbl.setStyleSheet("font-size: 17px; font-weight: bold; color: #eceff4; background: transparent; border: none;")
        self.device_sub_lbl = QLabel("Checking connection...")
        self.device_sub_lbl.setStyleSheet("font-size: 12px; color: #88c0d0; background: transparent; border: none;")
        title_vbox.addWidget(self.device_name_lbl)
        title_vbox.addWidget(self.device_sub_lbl)
        h_layout.addLayout(title_vbox)
        h_layout.addStretch()

        self.status_badge = QLabel("Connected")
        self.status_badge.setStyleSheet("background-color: #2e4338; color: #a3be8c; border: 1px solid #3e684a; border-radius: 12px; padding: 4px 10px; font-weight: bold; font-size: 11px;")
        h_layout.addWidget(self.status_badge)
        layout.addWidget(header_card)

        # 2. Battery & Hardware Card
        metrics_card = QWidget()
        metrics_card.setStyleSheet("background-color: #242933; border: 1px solid #3b4252; border-radius: 10px;")
        m_layout = QVBoxLayout(metrics_card)
        m_layout.setContentsMargins(16, 14, 16, 14)
        m_layout.setSpacing(12)

        # Battery
        bat_header = QHBoxLayout()
        self.bat_label = QLabel("🔋 Battery Level")
        self.bat_label.setStyleSheet("font-weight: bold; color: #eceff4; font-size: 13px; background: transparent; border: none;")
        self.bat_val = QLabel("--%")
        self.bat_val.setStyleSheet("color: #88c0d0; font-weight: bold; font-size: 13px; background: transparent; border: none;")
        bat_header.addWidget(self.bat_label)
        bat_header.addStretch()
        bat_header.addWidget(self.bat_val)
        m_layout.addLayout(bat_header)

        self.bat_bar = QProgressBar()
        self.bat_bar.setRange(0, 100)
        self.bat_bar.setValue(0)
        self.bat_bar.setTextVisible(True)
        self.bat_bar.setFixedHeight(22)
        m_layout.addWidget(self.bat_bar)

        # Storage
        storage_header = QHBoxLayout()
        self.storage_label = QLabel("💾 Internal Storage")
        self.storage_label.setStyleSheet("font-weight: bold; color: #eceff4; font-size: 13px; background: transparent; border: none;")
        self.storage_val = QLabel("--")
        self.storage_val.setStyleSheet("color: #88c0d0; font-size: 12px; background: transparent; border: none;")
        storage_header.addWidget(self.storage_label)
        storage_header.addStretch()
        storage_header.addWidget(self.storage_val)
        m_layout.addLayout(storage_header)

        self.storage_bar = QProgressBar()
        self.storage_bar.setRange(0, 100)
        self.storage_bar.setValue(0)
        self.storage_bar.setTextVisible(True)
        self.storage_bar.setFixedHeight(22)
        self.storage_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #3b4252;
                border-radius: 6px;
                text-align: center;
                background-color: #2e3440;
                color: #eceff4;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #81a1c1;
                border-radius: 5px;
            }
        """)
        m_layout.addWidget(self.storage_bar)

        # Network details
        self.ip_detail = QLabel("🌐 IP: Waiting for connection...")
        self.ip_detail.setStyleSheet("color: #d8dee9; font-size: 12px; background: transparent; border: none; padding-top: 4px;")
        m_layout.addWidget(self.ip_detail)

        self.sync_detail = QLabel("🔄 Status: Polling...")
        self.sync_detail.setStyleSheet("color: #d8dee9; font-size: 12px; background: transparent; border: none;")
        m_layout.addWidget(self.sync_detail)

        layout.addWidget(metrics_card)

        # 3. Quick Actions
        actions_label = QLabel("⚡ QUICK ACTIONS")
        actions_label.setStyleSheet("font-size: 11px; font-weight: bold; color: #88c0d0; padding-left: 2px;")
        layout.addWidget(actions_label)

        btn_grid1 = QHBoxLayout()
        btn_grid1.setSpacing(8)

        self.btn_browse = QPushButton("📂 Browse Files")
        self.btn_browse.setStyleSheet("background-color: #88c0d0; color: #1e222d; font-weight: bold; border-radius: 6px; padding: 9px;")
        self.btn_browse.clicked.connect(self.on_browse)
        btn_grid1.addWidget(self.btn_browse)

        self.btn_send = QPushButton("📤 Send File")
        self.btn_send.clicked.connect(self.on_send)
        btn_grid1.addWidget(self.btn_send)

        self.btn_ring = QPushButton("🔔 Ring Phone")
        self.btn_ring.clicked.connect(self.toggle_ring)
        btn_grid1.addWidget(self.btn_ring)
        layout.addLayout(btn_grid1)

        btn_grid2 = QHBoxLayout()
        btn_grid2.setSpacing(8)

        self.btn_clip = QPushButton("📋 Sync Clipboard")
        self.btn_clip.clicked.connect(self.on_clip)
        btn_grid2.addWidget(self.btn_clip)

        self.btn_refresh = QPushButton("🔄 Refresh Status")
        self.btn_refresh.clicked.connect(self.refresh_data)
        btn_grid2.addWidget(self.btn_refresh)
        layout.addLayout(btn_grid2)

        # 4. Paired Devices Section
        devices_header = QLabel("📱 PAIRED DEVICES")
        devices_header.setStyleSheet("font-size: 11px; font-weight: bold; color: #88c0d0; padding-left: 2px; padding-top: 6px;")
        layout.addWidget(devices_header)

        self.paired_card = QWidget()
        self.paired_card.setStyleSheet("background-color: #242933; border: 1px solid #3b4252; border-radius: 10px;")
        self.paired_card_layout = QVBoxLayout(self.paired_card)
        self.paired_card_layout.setContentsMargins(14, 12, 14, 12)
        self.paired_card_layout.setSpacing(10)
        layout.addWidget(self.paired_card)

        layout.addStretch()

    def refresh_data(self):
        st = fetch_json("/api/phone/status")
        if not isinstance(st, dict) or "phone" not in st:
            self.status_badge.setText("Daemon Offline")
            self.status_badge.setStyleSheet("background-color: #4c1d24; color: #ff8080; border: 1px solid #73232c; border-radius: 12px; padding: 4px 10px; font-weight: bold; font-size: 11px;")
            self.device_sub_lbl.setText("PC Authenticator daemon is not responding")
            return

        phone = st["phone"]
        connected = st.get("connected", False)
        name = phone.get("client_name", "Android Device")
        model = phone.get("model") or ""
        android_ver = phone.get("android_version") or ""
        sub_info = []
        if android_ver:
            sub_info.append(f"Android {android_ver}")
        if model and model.lower() not in name.lower():
            sub_info.append(f"Model: {model}")
        if not sub_info:
            sub_info.append("Paired Phone")

        self.device_name_lbl.setText(name)
        self.device_sub_lbl.setText(" • ".join(sub_info))

        if connected:
            self.status_badge.setText("🟢 Connected")
            self.status_badge.setStyleSheet("background-color: #2e4338; color: #a3be8c; border: 1px solid #3e684a; border-radius: 12px; padding: 4px 10px; font-weight: bold; font-size: 11px;")
        else:
            self.status_badge.setText("⚪ Idle / Offline")
            self.status_badge.setStyleSheet("background-color: #3b4252; color: #d8dee9; border: 1px solid #4c566a; border-radius: 12px; padding: 4px 10px; font-weight: bold; font-size: 11px;")

        # Battery
        bat = phone.get("battery_level")
        charging = phone.get("is_charging", False)
        if bat is not None:
            self.bat_bar.setValue(int(bat))
            self.bat_val.setText(f"{bat}%{' (Charging ⚡)' if charging else ' (Discharging)'}")
            chunk_color = "#88c0d0" if charging else ("#a3be8c" if bat > 30 else ("#ebcb8b" if bat > 15 else "#bf616a"))
            self.bat_bar.setStyleSheet(f"""
                QProgressBar {{
                    border: 1px solid #3b4252;
                    border-radius: 6px;
                    text-align: center;
                    background-color: #2e3440;
                    color: #eceff4;
                    font-weight: bold;
                }}
                QProgressBar::chunk {{
                    background-color: {chunk_color};
                    border-radius: 5px;
                }}
            """)
        else:
            self.bat_val.setText("Not reported")
            self.bat_bar.setValue(0)

        # Storage
        storage_free = phone.get("storage_free")
        storage_total = phone.get("storage_total")
        if storage_total and storage_total > 0:
            free_gb = storage_free / (1024**3)
            total_gb = storage_total / (1024**3)
            used_pct = int(((storage_total - storage_free) / storage_total) * 100)
            self.storage_val.setText(f"{free_gb:.1f} GB Free / {total_gb:.1f} GB Total")
            self.storage_bar.setValue(used_pct)
            self.storage_bar.setVisible(True)
            self.storage_label.setVisible(True)
            self.storage_val.setVisible(True)
        else:
            self.storage_bar.setVisible(False)
            self.storage_label.setVisible(False)
            self.storage_val.setVisible(False)

        # Network details
        ip = phone.get("ip") or "Searching..."
        port = phone.get("port", 1761)
        self.ip_detail.setText(f"🌐 Phone IP: {ip}:{port}")
        last_seen = phone.get("last_seen", 0)
        diff = int(time.time() - last_seen) if last_seen else -1
        if diff >= 0 and diff < 60:
            seen_str = f"{diff}s ago"
        elif diff >= 60:
            seen_str = f"{diff // 60}m ago"
        else:
            seen_str = "Never"
        self.sync_detail.setText(f"🕒 Last Active: {seen_str} • 🔐 Encryption: HMAC-SHA256")

        # Populate Paired Devices
        while self.paired_card_layout.count():
            item = self.paired_card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                sub = item.layout()
                while sub.count():
                    si = sub.takeAt(0)
                    if si.widget():
                        si.widget().deleteLater()

        devices = st.get("devices", [])
        if devices:
            for dev in devices:
                d_name = dev.get("client_name", "Android Phone")
                d_ip = dev.get("ip", "Unknown IP")
                is_conn = dev.get("connected", False)

                dev_row = QHBoxLayout()
                dev_lbl = QLabel(f"📱 {d_name} ({d_ip})")
                dev_lbl.setStyleSheet("color: #eceff4; font-size: 13px; font-weight: bold; background: transparent; border: none;")
                dev_row.addWidget(dev_lbl)

                badge = QLabel("🟢 Connected" if is_conn else "⚪ Idle")
                badge_style = "background-color: #2e4338; color: #a3be8c;" if is_conn else "background-color: #3b4252; color: #d8dee9;"
                badge.setStyleSheet(f"{badge_style} border-radius: 8px; padding: 2px 8px; font-size: 11px; font-weight: bold;")
                dev_row.addWidget(badge)
                dev_row.addStretch()

                btn_unpair = QPushButton("🗑️ Unpair")
                btn_unpair.setStyleSheet("background-color: #4c1d24; color: #ff8080; border: 1px solid #73232c; border-radius: 6px; padding: 4px 10px; font-weight: bold;")
                btn_unpair.clicked.connect(lambda checked=False, d=dev: self.unpair_device_action(d))
                dev_row.addWidget(btn_unpair)

                self.paired_card_layout.addLayout(dev_row)
        else:
            no_lbl = QLabel("No paired devices found")
            no_lbl.setStyleSheet("color: #d8dee9; font-size: 12px; background: transparent; border: none;")
            self.paired_card_layout.addWidget(no_lbl)

        btn_add_pair = QPushButton("➕ Pair New Device...")
        btn_add_pair.setStyleSheet("background-color: #3b4252; color: #88c0d0; border-radius: 6px; padding: 6px; font-weight: bold; margin-top: 4px;")
        btn_add_pair.clicked.connect(lambda: subprocess.Popen(["pc-auth", "--pair"]))
        self.paired_card_layout.addWidget(btn_add_pair)

    def unpair_device_action(self, dev):
        name = dev.get('client_name', 'Device')
        ret = QMessageBox.question(
            self,
            "Unpair Device",
            f"Are you sure you want to unpair {name}?\n\nThis will disconnect the device and revoke its pairing credentials.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ret == QMessageBox.StandardButton.Yes:
            post_json("/api/devices/unpair", {"token": dev.get('token'), "client_id": dev.get('client_id')})
            self.refresh_data()
            if self.parent_tray:
                self.parent_tray.build_menu()
                self.parent_tray.update_status()
            QMessageBox.information(self, "Unpaired", f"{name} was unpaired successfully.")

    def on_browse(self):
        if self.parent_tray:
            self.parent_tray.open_explorer()

    def on_send(self):
        if self.parent_tray:
            self.parent_tray.send_file_dialog()

    def on_clip(self):
        if self.parent_tray:
            self.parent_tray.sync_clipboard()

    def toggle_ring(self):
        if not self.is_ringing:
            post_json("/api/ring")
            self.btn_ring.setText("🛑 Stop Ringing")
            self.btn_ring.setStyleSheet("background-color: #bf616a; color: #eceff4; font-weight: bold; border-radius: 6px; padding: 9px;")
            self.is_ringing = True
        else:
            post_json("/api/unring")
            self.btn_ring.setText("🔔 Ring Phone")
            self.btn_ring.setStyleSheet("background-color: #3b4252; color: #eceff4; border-radius: 6px; padding: 9px;")
            self.is_ringing = False

# ==========================================
# System Tray Application
# ==========================================

class PCConnectTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # System Tray Icon
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon.fromTheme("smartphone", QIcon.fromTheme("phone")))
        self.tray.setVisible(True)

        self.info_window = None
        self.explorer_window = None

        # Left-click on tray icon opens Phone Info
        self.tray.activated.connect(self.on_tray_activated)

        # Laser pointer overlay listener
        self.laser_overlay = LaserPointerOverlay()
        self.laser_thread = LaserListenerThread()
        self.laser_thread.laser_packet.connect(self.laser_overlay.handle_packet)
        self.laser_thread.start()

        self.menu = QMenu()
        self.build_menu()
        self.tray.setContextMenu(self.menu)

        # Polling Timer for status
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_status)
        self.timer.start(4000)
        self.update_status()

    def on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_phone_info()

    def show_phone_info(self):
        if not self.info_window:
            self.info_window = PhoneInfoWindow(self)
        self.info_window.refresh_data()
        self.info_window.show()
        self.info_window.raise_()
        self.info_window.activateWindow()

    def build_menu(self):
        self.menu.clear()

        # Phone Header in Menu
        self.act_status_header = self.menu.addAction("📱 Android Phone")
        self.act_status_header.triggered.connect(self.show_phone_info)

        self.act_status_bat = self.menu.addAction("🔋 Battery: --%")
        self.act_status_bat.triggered.connect(self.show_phone_info)

        self.act_status_ip = self.menu.addAction("🌐 IP: --")
        self.act_status_ip.triggered.connect(self.show_phone_info)

        self.act_status_conn = self.menu.addAction("📶 Checking Status...")
        self.act_status_conn.triggered.connect(self.show_phone_info)

        self.menu.addSeparator()

        act_view_info = self.menu.addAction("📱 View Phone Details & Status...")
        act_view_info.triggered.connect(self.show_phone_info)

        # Paired Devices Submenu
        st_dev = fetch_json("/api/phone/status")
        dev_list = st_dev.get('devices', []) if isinstance(st_dev, dict) else []

        menu_paired = self.menu.addMenu(f"📱 Paired Devices ({len(dev_list)})")
        if dev_list:
            for dev in dev_list:
                d_name = dev.get('client_name', 'Android Phone')
                d_ip = dev.get('ip', 'Unknown IP')
                d_conn = "🟢" if dev.get('connected') else "⚪"
                
                dev_submenu = menu_paired.addMenu(f"{d_conn} {d_name} ({d_ip})")
                act_dev_info = dev_submenu.addAction(f"ℹ️ View {d_name} Details...")
                act_dev_info.triggered.connect(self.show_phone_info)
                
                act_dev_send = dev_submenu.addAction("📤 Send File to Device...")
                act_dev_send.triggered.connect(lambda checked=False, d=dev: self.send_file_to_device(d))
                
                act_dev_unpair = dev_submenu.addAction("🗑️ Unpair Device...")
                act_dev_unpair.triggered.connect(lambda checked=False, d=dev: self.confirm_unpair_device(d))
        else:
            act_no = menu_paired.addAction("No paired devices found")
            act_no.setEnabled(False)

        menu_paired.addSeparator()
        act_sub_pair = menu_paired.addAction("➕ Pair New Device...")
        act_sub_pair.triggered.connect(lambda: subprocess.Popen(["pc-auth", "--pair"]))

        self.menu.addSeparator()

        act_browse = self.menu.addAction("📂 Browse Phone Files...")
        act_browse.triggered.connect(self.open_explorer)

        act_send = self.menu.addAction("📤 Send File to Phone...")
        act_send.triggered.connect(self.send_file_dialog)

        act_clip = self.menu.addAction("📋 Copy PC Clipboard to Phone")
        act_clip.triggered.connect(self.sync_clipboard)

        act_ring = self.menu.addAction("🔔 Find My Phone (Ring)")
        act_ring.triggered.connect(lambda: post_json("/api/ring"))

        act_unring = self.menu.addAction("🛑 Stop Ringing Phone")
        act_unring.triggered.connect(lambda: post_json("/api/unring"))

        act_lock = self.menu.addAction("🔒 Lock PC Screen")
        act_lock.triggered.connect(lambda: post_json("/api/action", {"action": "lock"}))

        self.menu.addSeparator()

        act_web = self.menu.addAction("🌐 Open Web Explorer")
        act_web.triggered.connect(lambda: subprocess.Popen(["xdg-open", f"{DAEMON_URL}/browse?ltok={local_token()}"]))

        act_pair = self.menu.addAction("➕ Pair New Device...")
        act_pair.triggered.connect(lambda: subprocess.Popen(["pc-auth", "--pair"]))

        self.menu.addSeparator()

        act_quit = self.menu.addAction("🚪 Exit PC Connect")
        act_quit.triggered.connect(self.app.quit)

    def confirm_unpair_device(self, device):
        name = device.get('client_name', 'this device')
        ret = QMessageBox.question(
            None,
            "Unpair Device",
            f"Are you sure you want to unpair {name}?\n\nThis will disconnect the device and revoke its security credentials.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ret == QMessageBox.StandardButton.Yes:
            post_json("/api/devices/unpair", {"token": device.get('token'), "client_id": device.get('client_id')})
            self.build_menu()
            self.update_status()
            if self.info_window:
                self.info_window.refresh_data()
            QMessageBox.information(None, "Device Unpaired", f"{name} has been unpaired.")

    def send_file_to_device(self, device):
        files, _ = QFileDialog.getOpenFileNames(None, f"Send Files to {device.get('client_name')}")
        if files:
            send_bin = os.path.expanduser("~/.local/bin/pc-connect-send")
            cid = device.get('client_id') or device.get('token')
            subprocess.Popen([send_bin, "--device", cid] + files)

    def update_status(self):
        st = fetch_json("/api/phone/status")
        if isinstance(st, dict) and "phone" in st:
            phone = st["phone"]
            connected = st.get("connected", False)
            name = phone.get("client_name", "Android Phone")
            bat = phone.get("battery_level")
            charging = phone.get("is_charging", False)
            ip = phone.get("ip") or "Searching..."

            bat_str = f" • {bat}%{' ⚡' if charging else ''}" if bat is not None else ""
            status_text = f"🟢 {name}{bat_str}" if connected else f"⚪ {name} (Idle)"

            self.act_status_header.setText(f"📱 {name}")
            self.act_status_conn.setText(f"📶 {'Connected' if connected else 'Disconnected'}")
            if bat is not None:
                self.act_status_bat.setText(f"🔋 Battery: {bat}%{' (Charging ⚡)' if charging else ''}")
                self.act_status_bat.setVisible(True)
            else:
                self.act_status_bat.setVisible(False)
            self.act_status_ip.setText(f"🌐 IP: {ip}")

            self.tray.setToolTip(f"PC Connect: {status_text}\nClick icon to view phone details")
        else:
            self.act_status_header.setText("⚠️ Daemon Offline")
            self.act_status_conn.setText("📶 Disconnected")
            self.act_status_bat.setVisible(False)
            self.act_status_ip.setText("🌐 IP: 127.0.0.1")
            self.tray.setToolTip("PC Connect: Daemon Offline")

    def open_explorer(self):
        if not self.explorer_window:
            self.explorer_window = PhoneExplorerWindow()
        self.explorer_window.show()
        self.explorer_window.raise_()
        self.explorer_window.activateWindow()

    def send_file_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Send Files to Phone")
        if files:
            send_bin = os.path.expanduser("~/.local/bin/pc-connect-send")
            subprocess.Popen([send_bin] + files)

    def sync_clipboard(self):
        post_json("/api/clipboard")
        subprocess.Popen(['notify-send', '-i', 'edit-paste', '-a', 'PC Connect', 'Clipboard Synced', 'PC clipboard sent to phone'])

    def run(self):
        if "--browse" in sys.argv:
            self.open_explorer()
        return self.app.exec()

def main():
    if not acquire_single_instance_lock():
        print("[INFO] Another instance of PC Connect Tray is already running. Exiting.")
        sys.exit(0)
    app = PCConnectTrayApp()
    sys.exit(app.run())

if __name__ == '__main__':
    main()

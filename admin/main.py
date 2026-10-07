import sys
import json
import socket
import struct
import threading
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLineEdit, QLabel, QPushButton, QDialog, QFormLayout, QMessageBox, QCheckBox,
    QComboBox, QStackedWidget, QButtonGroup, QSizePolicy
)
from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from config import DISCOVERY_PORT, PIN_CODE
from discovery import listen
from viewer import Viewer
from auth import UserStore
from user_management import UserManagementDialog
from recordings import RecordingsPage


def recv_exact(sock, size):
    """Read exactly `size` bytes - see the matching helper in viewer.py for
    why a plain sock.recv(n) isn't safe to use for length-prefixed
    protocol data."""
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class LoginDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.user = None
        self.setWindowTitle("RemoteDesk Login")
        form = QFormLayout(self)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox("Remember this device for 8 hours")
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        form.addRow("", self.remember)
        login = QPushButton("Sign in")
        login.clicked.connect(self.try_login)
        form.addRow(login)
        self.password.returnPressed.connect(self.try_login)

    def try_login(self):
        self.user = self.store.authenticate(self.username.text().strip(), self.password.text())
        if self.user:
            if self.remember.isChecked():
                self.store.save_session(self.username.text().strip(), self.password.text())
            else:
                self.store.clear_session()
            self.accept()
        else:
            QMessageBox.warning(self, "Sign-in failed", "Invalid username or password.")


def create_first_manager(store):
    dialog = QDialog()
    dialog.setWindowTitle("Create manager account")
    form = QFormLayout(dialog)
    username = QLineEdit()
    password = QLineEdit()
    password.setEchoMode(QLineEdit.EchoMode.Password)
    form.addRow("Manager username", username)
    form.addRow("Manager password", password)
    submit = QPushButton("Create")
    submit.clicked.connect(dialog.accept)
    form.addRow(submit)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    if not username.text().strip() or not password.text():
        QMessageBox.warning(None, "Unable to create manager", "Username and password are required.")
        return False
    try:
        store.add_user(username.text(), password.text(), "manager")
        return True
    except ValueError as error:
        QMessageBox.warning(None, "Unable to create manager", str(error))
        return False


class AgentRowWidget(QWidget):
    """One row in the agent list: identity, online dot, USB access chip,
    and (for managers) Allow/Block controls plus a View button."""

    view_requested = pyqtSignal(str)
    access_requested = pyqtSignal(str, bool)  # dev_id, blocked

    def __init__(self, dev_id, dev, can_manage_access, parent=None):
        super().__init__(parent)
        self.dev_id = dev_id

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)

        text_block = QVBoxLayout()
        text_block.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setObjectName("rowTitle")
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("rowSubtitle")
        text_block.addWidget(self.title_label)
        text_block.addWidget(self.subtitle_label)
        layout.addLayout(text_block, 1)

        self.access_chip = QLabel()
        self.access_chip.setObjectName("accessChip")
        layout.addWidget(self.access_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        self.allow_button = None
        self.block_button = None
        if can_manage_access:
            self.allow_button = QPushButton("Allow")
            self.allow_button.setObjectName("allowButton")
            self.allow_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.allow_button.clicked.connect(lambda: self.access_requested.emit(self.dev_id, False))
            layout.addWidget(self.allow_button)

            self.block_button = QPushButton("Block")
            self.block_button.setObjectName("blockButton")
            self.block_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.block_button.clicked.connect(lambda: self.access_requested.emit(self.dev_id, True))
            layout.addWidget(self.block_button)

        self.view_button = QPushButton("View")
        self.view_button.setObjectName("viewButton")
        self.view_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_button.clicked.connect(lambda: self.view_requested.emit(self.dev_id))
        layout.addWidget(self.view_button)

        self.update_data(dev)

    def update_data(self, dev):
        username = dev.get("username", "")
        hostname = dev.get("hostname", "")
        ip = dev.get("ip", "?")
        port = dev.get("port", "?")
        control_port = dev.get("control_port", "?")
        online = dev.get("online", True)

        title = f"{username}@{hostname}" if username else (hostname or "Unknown device")
        self.title_label.setText(title)
        self.subtitle_label.setText(f"{ip}:{port}  •  control channel :{control_port}")

        self.dot.setStyleSheet(
            "background:#22c55e; border-radius:5px;" if online
            else "background:#9ca3af; border-radius:5px;"
        )
        self.dot.setToolTip("Online" if online else "Not seen recently")

        # USB port access (blocking USB storage devices on the remote
        # machine) - a separate, OS-level policy from keyboard/mouse
        # "Interact" control, which stays a per-session toggle inside the
        # live viewer and isn't shown here.
        blocked = dev.get("usb_blocked")
        if blocked is None:
            self.access_chip.setText("USB: Unknown")
            self.access_chip.setStyleSheet(
                "background:#eef1f6; color:#64748b; border-radius:9px; padding:3px 10px; font-weight:600;"
            )
        elif blocked:
            self.access_chip.setText("USB: Blocked")
            self.access_chip.setStyleSheet(
                "background:#fde8e8; color:#b91c1c; border-radius:9px; padding:3px 10px; font-weight:600;"
            )
        else:
            self.access_chip.setText("USB: Allowed")
            self.access_chip.setStyleSheet(
                "background:#e6f7ee; color:#15803d; border-radius:9px; padding:3px 10px; font-weight:600;"
            )

        if self.allow_button is not None:
            self.allow_button.setEnabled(blocked is not False)
            self.block_button.setEnabled(blocked is not True)


class AdminApp(QWidget):
    refresh_signal = pyqtSignal()

    def __init__(self, current_user):
        super().__init__()
        self.store = UserStore()
        self.current_user = current_user
        self.viewers = []
        self.setWindowTitle("RemoteDesk - Admin")
        self.resize(1180, 720)
        self.setMinimumSize(860, 560)

        self.setStyleSheet(self._build_stylesheet())

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        content = QWidget()
        content.setObjectName("content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(28, 24, 28, 20)
        content_layout.setSpacing(14)
        content_layout.addLayout(self._build_header())

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_agents_page())
        self.pages.addWidget(self._build_users_page())
        self.recordings_page = RecordingsPage()
        self.pages.addWidget(self.recordings_page)
        self.pages.addWidget(self._build_placeholder_page(
            "Logs", "Activity logs aren't available yet.", "📄"))
        self.pages.addWidget(self._build_placeholder_page(
            "Settings", "Settings aren't available yet.", "⚙"))
        content_layout.addWidget(self.pages, 1)

        root.addWidget(content, 1)

        self.nav_group.button(0).setChecked(True)

        self.refresh_signal.connect(self.refresh)

        # Start discovery listener thread safely
        threading.Thread(
            target=listen,
            args=(self.refresh_signal.emit,),
            daemon=True
        ).start()

        # holds current discovered agents keyed by device_id
        self.agents = {}
        self.device_ids = []  # preserve insertion order
        self.row_widgets = {}
        self.row_items = {}

        timer = QTimer(self)
        timer.timeout.connect(self.refresh)
        timer.start(3000)  # every 3 seconds
        self.session_timer = QTimer(self)
        self.session_timer.setSingleShot(True)
        self.session_timer.timeout.connect(self.logout)
        self.session_timer.start(8 * 60 * 60 * 1000)

        self.update_list_display()

    # ---------------------------------------------------------------- UI

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(250)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        logo = QLabel("🖥")
        logo.setObjectName("logoBadge")
        logo.setFixedSize(40, 40)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel("RemoteDesk")
        name.setObjectName("brandName")
        sub = QLabel("Console")
        sub.setObjectName("brandSub")
        brand_text.addWidget(name)
        brand_text.addWidget(sub)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(18)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        nav_items = [
            ("👤", "Users"),
            ("💻", "Agents"),
            ("🎬", "Recordings"),
            ("📄", "Logs"),
            ("⚙", "Settings"),
        ]
        RECORDINGS_TAB_INDEX = 2
        for index, (icon, label) in enumerate(nav_items):
            button = QPushButton(f"  {icon}   {label}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, i=index: self._on_nav_clicked(i))
            self.nav_group.addButton(button, index)
            layout.addWidget(button)
        self._recordings_tab_index = RECORDINGS_TAB_INDEX

        layout.addStretch(1)

        server_box = QWidget()
        server_box.setObjectName("serverBox")
        server_layout = QVBoxLayout(server_box)
        server_layout.setContentsMargins(12, 10, 12, 10)
        server_layout.setSpacing(4)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        dot = QLabel()
        dot.setFixedSize(9, 9)
        dot.setStyleSheet("background:#22c55e; border-radius:4px;")
        status_row.addWidget(dot)
        status_row.addWidget(QLabel("Server Running"))
        status_row.addStretch(1)
        server_layout.addLayout(status_row)
        addr_label = QLabel(f"0.0.0.0:{DISCOVERY_PORT}")
        addr_label.setObjectName("serverAddr")
        server_layout.addWidget(addr_label)
        layout.addWidget(server_box)

        return sidebar

    def _build_header(self):
        header = QHBoxLayout()
        header.setSpacing(12)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.title_label = QLabel("RemoteDesk Console")
        self.title_label.setObjectName("titleLabel")
        title_block.addWidget(self.title_label)
        self.subtitle_label = QLabel("Discover active agents on your network and connect instantly.")
        self.subtitle_label.setObjectName("subtitleLabel")
        title_block.addWidget(self.subtitle_label)
        header.addLayout(title_block, 1)

        self.session_label = QLabel(f"👤  {self.current_user['username']}   |   {self.current_user['role'].title()}")
        self.session_label.setObjectName("userChip")
        header.addWidget(self.session_label)

        self.logout_button = QPushButton("↩  Sign out")
        self.logout_button.setObjectName("logoutButton")
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout_button.clicked.connect(self.logout)
        header.addWidget(self.logout_button)
        return header

    def _build_agents_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("🔍  Search by username, hostname, IP, or port...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.update_list_display)
        search_row.addWidget(self.search_input, 1)

        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("filterCombo")
        self.filter_combo.addItems(["Online", "All"])
        self.filter_combo.currentTextChanged.connect(self.update_list_display)
        search_row.addWidget(self.filter_combo)

        self.refresh_button = QPushButton("⟳")
        self.refresh_button.setObjectName("refreshButton")
        self.refresh_button.setFixedSize(38, 38)
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh)
        search_row.addWidget(self.refresh_button)
        layout.addLayout(search_row)

        panel = QWidget()
        panel.setObjectName("panelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        panel_layout.setSpacing(12)

        panel_header = QHBoxLayout()
        panel_icon = QLabel("💻")
        panel_icon.setObjectName("panelIcon")
        panel_icon.setFixedSize(44, 44)
        panel_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_header.addWidget(panel_icon)
        panel_text = QVBoxLayout()
        panel_text.setSpacing(0)
        panel_title = QLabel("Manage Agents")
        panel_title.setObjectName("panelTitle")
        panel_sub = QLabel("View and manage active agents on your network.")
        panel_sub.setObjectName("panelSubtitle")
        panel_text.addWidget(panel_title)
        panel_text.addWidget(panel_sub)
        panel_header.addLayout(panel_text)
        panel_header.addStretch(1)
        self.count_label = QLabel("0 visible")
        self.count_label.setObjectName("countBadge")
        panel_header.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignTop)
        panel_layout.addLayout(panel_header)

        self.list = QListWidget()
        self.list.setObjectName("agentList")
        self.list.itemDoubleClicked.connect(self.connect_to_agent)
        panel_layout.addWidget(self.list, 1)

        self.empty_state = self._build_empty_state()
        panel_layout.addWidget(self.empty_state)

        layout.addWidget(panel, 1)

        self.hint_label = QLabel("ℹ️  Tip: Double-click a device to open the live viewer.")
        self.hint_label.setObjectName("hintLabel")
        layout.addWidget(self.hint_label)

        return page

    def _build_empty_state(self):
        box = QWidget()
        box.setObjectName("emptyState")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(20, 40, 20, 40)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🖥")
        icon.setObjectName("emptyIcon")
        icon.setFixedSize(90, 90)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("No agents found")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        sub = QLabel("No active agents are currently online on your network.")
        sub.setObjectName("emptySubtitle")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sub)

        refresh_again = QPushButton("⟳  Refresh")
        refresh_again.setObjectName("emptyRefreshButton")
        refresh_again.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_again.clicked.connect(self.refresh)
        layout.addWidget(refresh_again, 0, Qt.AlignmentFlag.AlignHCenter)

        return box

    def _build_users_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        panel = QWidget()
        panel.setObjectName("panelCard")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        panel_layout.setSpacing(14)

        panel_header = QHBoxLayout()
        panel_icon = QLabel("👤")
        panel_icon.setObjectName("panelIcon")
        panel_icon.setFixedSize(44, 44)
        panel_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        panel_header.addWidget(panel_icon)
        panel_text = QVBoxLayout()
        panel_text.setSpacing(0)
        panel_title = QLabel("Manage Users")
        panel_title.setObjectName("panelTitle")
        panel_sub = QLabel("Create and manage admin console accounts.")
        panel_sub.setObjectName("panelSubtitle")
        panel_text.addWidget(panel_title)
        panel_text.addWidget(panel_sub)
        panel_header.addLayout(panel_text)
        panel_header.addStretch(1)
        panel_layout.addLayout(panel_header)

        if self.current_user.get("role") == "manager":
            open_button = QPushButton("Open user management")
            open_button.clicked.connect(self.manage_users)
            panel_layout.addWidget(open_button, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            notice = QLabel("Manager access is required to manage users.")
            notice.setObjectName("emptySubtitle")
            panel_layout.addWidget(notice)

        panel_layout.addStretch(1)
        layout.addWidget(panel, 1)
        return page

    def _build_placeholder_page(self, title, message, icon):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(6)

        icon_label = QLabel(icon)
        icon_label.setObjectName("emptyIcon")
        icon_label.setFixedSize(90, 90)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignHCenter)

        title_label = QLabel(title)
        title_label.setObjectName("emptyTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        sub_label = QLabel(message)
        sub_label.setObjectName("emptySubtitle")
        sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(sub_label)

        return page

    # ----------------------------------------------------------- actions

    def _on_nav_clicked(self, index):
        self.pages.setCurrentIndex(index)
        if index == self._recordings_tab_index:
            self.recordings_page.refresh()

    def logout(self):
        self.store.clear_session()
        for viewer in getattr(self, "viewers", []):
            viewer.close()
        self.close()

    def manage_users(self):
        if self.current_user["role"] != "manager":
            return
        UserManagementDialog(self.store, self).exec()

    def refresh(self):
        """Refresh device list - agents that stop broadcasting are marked
        offline (rather than deleted) so the "All" filter can still show
        recently-seen devices; their own Viewer window (if open) already
        has its own reconnect logic and isn't affected by this."""
        from discovery import get_devices

        devices = get_devices()
        devices_dict = {d['device_id']: d for d in devices}

        for dev_id, dev in devices_dict.items():
            dev["online"] = True
            if dev_id not in self.agents:
                ip = dev.get("ip")
                port = dev.get("port")
                if ip and port and dev_id:
                    self.agents[dev_id] = dev
                    self.device_ids.append(dev_id)
                    print(f"[+] Added device: {dev_id}")
            else:
                self.agents[dev_id].update(dev)

        for dev_id in self.device_ids:
            if dev_id not in devices_dict and dev_id in self.agents:
                if self.agents[dev_id].get("online", True):
                    self.agents[dev_id]["online"] = False
                    print(f"[-] Device went offline: {dev_id}")

        self.update_list_display()

    def connect_to_agent(self, item):
        dev_id = item.data(Qt.ItemDataRole.UserRole)
        self.connect_to_agent_by_id(dev_id)

    def connect_to_agent_by_id(self, dev_id):
        # If this admin console already has a viewer open for this device,
        # just bring it to front instead of opening a second connection.
        # A viewer that was already closed stays in self.viewers until Qt
        # actually deletes its C++ object - closing a window alone doesn't
        # do that unless WA_DeleteOnClose is set, so without this check we
        # could resurrect a dead, already-closed window (sockets
        # permanently None) instead of opening a fresh one whenever the
        # same device was reconnected to.
        for existing in list(self.viewers):
            if getattr(existing, "device_id", None) != dev_id:
                continue
            if getattr(existing, "_stop_requested", False):
                self.viewers.remove(existing)
                continue
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        dev = self.agents.get(dev_id, {})
        ip = dev.get("ip")
        port = dev.get("port")
        if not ip or not port:
            return
        control_port = dev.get("control_port", port + 1)
        username = dev.get("username", "")  # agent's OS username, display only
        viewer = Viewer(
            ip, port, username, control_port,
            role=self.current_user.get("role"),
            admin_id=self.current_user.get("username"),
        )
        viewer.device_id = dev_id
        self.viewers.append(viewer)
        viewer.destroyed.connect(lambda: self.viewers.remove(viewer) if viewer in self.viewers else None)
        viewer.show()

    def set_agent_access(self, dev_id, blocked):
        if self.current_user.get("role") != "manager":
            return
        dev = self.agents.get(dev_id)
        if not dev:
            return
        # Optimistic local update. If the command doesn't actually reach
        # the agent, the next presence broadcast (every ~3s) carries the
        # agent's real usb_blocked value and will self-correct this.
        dev["usb_blocked"] = blocked
        self.update_list_display()
        self._send_usb_access_command(dev, blocked)

    def _send_usb_access_command(self, dev, blocked):
        ip = dev.get("ip")
        control_port = dev.get("control_port")
        if not ip or not control_port:
            return
        admin_id = self.current_user.get("username")
        role = self.current_user.get("role")

        def _worker():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((ip, control_port))

                handshake = json.dumps({"pin": PIN_CODE, "admin_id": admin_id, "role": role}).encode()
                sock.sendall(struct.pack(">I", len(handshake)) + handshake)

                size_data = recv_exact(sock, 4)
                if not size_data:
                    raise ConnectionError("no handshake reply")
                length = struct.unpack(">I", size_data)[0]
                payload = recv_exact(sock, length)
                if not payload:
                    raise ConnectionError("handshake reply truncated")
                reply = json.loads(payload.decode())
                if reply.get("status") != "ok":
                    raise ConnectionError(reply.get("reason", "rejected"))

                cmd = json.dumps({
                    "action": "set_usb_access",
                    "blocked": blocked,
                    "admin_id": admin_id,
                }).encode()
                sock.sendall(struct.pack(">I", len(cmd)) + cmd)
            except Exception as e:
                print(f"[!] Failed to send access command to {ip}:{control_port}: {e}")
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

        threading.Thread(target=_worker, daemon=True).start()

    def update_list_display(self):
        search = self.search_input.text().lower().strip()
        filter_mode = self.filter_combo.currentText()
        can_manage_access = self.current_user.get("role") == "manager"

        visible_ids = []
        for dev_id in self.device_ids:
            dev = self.agents.get(dev_id)
            if not dev:
                continue
            if filter_mode == "Online" and not dev.get("online", True):
                continue
            username = dev.get("username", "")
            hostname = dev.get("hostname", "")
            ip = dev.get("ip", "")
            port = dev.get("port", "")
            haystack = f"{username}@{hostname} ({ip}:{port})".lower()
            if search and search not in haystack:
                continue
            visible_ids.append(dev_id)

        # drop rows that are no longer visible
        for dev_id in list(self.row_widgets.keys()):
            if dev_id not in visible_ids:
                item = self.row_items.pop(dev_id, None)
                if item is not None:
                    self.list.takeItem(self.list.row(item))
                self.row_widgets.pop(dev_id, None)

        # add new rows / refresh existing ones in place
        for dev_id in visible_ids:
            dev = self.agents[dev_id]
            if dev_id in self.row_widgets:
                self.row_widgets[dev_id].update_data(dev)
            else:
                row_widget = AgentRowWidget(dev_id, dev, can_manage_access)
                row_widget.view_requested.connect(self.connect_to_agent_by_id)
                row_widget.access_requested.connect(self.set_agent_access)
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, dev_id)
                item.setSizeHint(row_widget.sizeHint())
                self.list.addItem(item)
                self.list.setItemWidget(item, row_widget)
                self.row_widgets[dev_id] = row_widget
                self.row_items[dev_id] = item

        visible = len(visible_ids)
        total = len(self.device_ids)
        self.count_label.setText(f"{visible} visible")
        self.count_label.setToolTip(f"{total} discovered this session")

        self.empty_state.setVisible(visible == 0)
        self.list.setVisible(visible > 0)

    # -------------------------------------------------------- styling

    def _build_stylesheet(self):
        return """
            QWidget {
                font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
                font-size: 13px;
                color: #172433;
            }
            #content {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f1f5fc,
                    stop:0.55 #eef4fb,
                    stop:1 #eaf1fb
                );
            }
            #sidebar {
                background: qlineargradient(
                    x1:0, y1:0, x2:0, y2:1,
                    stop:0 #0c1a33,
                    stop:1 #0a1730
                );
            }
            #sidebar QLabel { color: #c7d3ea; }
            #logoBadge {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2f6fed, stop:1 #5b8def);
                border-radius: 10px;
                font-size: 18px;
            }
            #brandName { color: #ffffff; font-weight: 700; font-size: 15px; }
            #brandSub { color: #6ea1f5; font-size: 12px; }
            QPushButton#navButton {
                text-align: left;
                background: transparent;
                color: #9fb3d1;
                border: 0;
                border-radius: 8px;
                padding: 10px 8px;
                font-weight: 600;
            }
            QPushButton#navButton:hover {
                background: rgba(255, 255, 255, 0.06);
                color: #ffffff;
            }
            QPushButton#navButton:checked {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2f6fed, stop:1 #2657c4);
                color: #ffffff;
            }
            #serverBox {
                background: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
            }
            #serverBox QLabel { color: #dfe8f9; font-weight: 600; font-size: 12px; }
            #serverAddr { color: #6ea1f5; font-family: Consolas, monospace; font-weight: 500; }

            QLabel#titleLabel { font-size: 24px; font-weight: 700; color: #0e1b33; }
            QLabel#subtitleLabel { font-size: 13px; color: #5b6b85; }
            QLabel#userChip {
                font-size: 12px;
                font-weight: 600;
                color: #2d4a66;
                background: #ffffff;
                border: 1px solid #dbe4f3;
                border-radius: 16px;
                padding: 8px 14px;
            }
            QPushButton#logoutButton {
                background: #ffffff;
                color: #2f6fed;
                border: 1px solid #dbe4f3;
                border-radius: 16px;
                padding: 9px 16px;
                font-weight: 600;
            }
            QPushButton#logoutButton:hover { background: #f3f7ff; }

            QLineEdit#searchInput {
                background: #ffffff;
                border: 1px solid #d7e0ee;
                border-radius: 18px;
                padding: 10px 16px;
                selection-background-color: #6ca9e9;
            }
            QLineEdit#searchInput:focus { border: 1px solid #2f6fed; }
            QComboBox#filterCombo {
                background: #e6f7ee;
                color: #15803d;
                border: 1px solid #b9e6cc;
                border-radius: 18px;
                padding: 8px 14px;
                font-weight: 600;
            }
            QPushButton#refreshButton {
                background: #ffffff;
                border: 1px solid #d7e0ee;
                border-radius: 19px;
                font-size: 15px;
                color: #2f6fed;
                font-weight: 700;
            }
            QPushButton#refreshButton:hover { background: #f3f7ff; }

            #panelCard {
                background: rgba(255, 255, 255, 0.96);
                border: 1px solid #dfe6f2;
                border-radius: 16px;
            }
            #panelIcon {
                background: #e7effe;
                border-radius: 12px;
                font-size: 20px;
            }
            QLabel#panelTitle { font-size: 16px; font-weight: 700; color: #0e1b33; }
            QLabel#panelSubtitle { font-size: 12px; color: #64748b; }

            QScrollArea#recordingsScroll { border: 0; background: transparent; }
            #thumbFrame {
                background: #eef2f9;
                border: 1px solid #e2e8f4;
                border-radius: 10px;
            }
            QLabel#thumbLabel { color: #9aa7bd; font-size: 12px; border-radius: 10px; }
            #recordingCard {
                background: #ffffff;
                border: 1px solid #e7ecf5;
                border-radius: 12px;
            }
            #recordingCard:hover { border-color: #c9def7; background: #f6f9ff; }
            QLabel#countBadge {
                font-size: 12px;
                font-weight: 700;
                color: #15803d;
                background: #e6f7ee;
                border: 1px solid #b9e6cc;
                border-radius: 10px;
                padding: 4px 12px;
            }

            QListWidget#agentList {
                border: 0;
                background: transparent;
                outline: none;
            }
            QListWidget#agentList::item {
                border: 1px solid #e7ecf5;
                border-radius: 10px;
                margin: 3px 0;
                background: #ffffff;
            }
            QListWidget#agentList::item:hover { border-color: #c9def7; background: #f6f9ff; }
            QListWidget#agentList::item:selected { border-color: #2f6fed; background: #eef4ff; }

            QLabel#rowTitle { font-weight: 700; color: #0e1b33; font-size: 13px; }
            QLabel#rowSubtitle { color: #7c8aa0; font-size: 11px; }
            QPushButton#allowButton, QPushButton#blockButton, QPushButton#viewButton {
                border-radius: 8px;
                padding: 6px 12px;
                font-weight: 600;
                font-size: 12px;
                border: 1px solid transparent;
            }
            QPushButton#allowButton { background: #e6f7ee; color: #15803d; }
            QPushButton#allowButton:disabled { background: #f2f5f4; color: #a8b3ac; }
            QPushButton#allowButton:hover:!disabled { background: #d3f0e0; }
            QPushButton#blockButton { background: #fde8e8; color: #b91c1c; }
            QPushButton#blockButton:disabled { background: #f6f0f0; color: #c3a8a8; }
            QPushButton#blockButton:hover:!disabled { background: #fbd4d4; }
            QPushButton#viewButton { background: #2f6fed; color: #ffffff; }
            QPushButton#viewButton:hover { background: #2657c4; }

            #emptyState { background: transparent; }
            #emptyIcon {
                background: #e7effe;
                border-radius: 45px;
                font-size: 34px;
            }
            QLabel#emptyTitle { font-size: 17px; font-weight: 700; color: #0e1b33; }
            QLabel#emptySubtitle { font-size: 12px; color: #7c8aa0; }
            QPushButton#emptyRefreshButton {
                background: #e7effe;
                color: #2f6fed;
                border: 0;
                border-radius: 8px;
                padding: 9px 18px;
                font-weight: 600;
                margin-top: 6px;
            }
            QPushButton#emptyRefreshButton:hover { background: #d7e3fd; }

            QLabel#hintLabel { color: #7c8aa0; font-size: 12px; }
        """


if __name__ == "__main__":
    app = QApplication(sys.argv)
    store = UserStore()
    while not store.users():
        if not create_first_manager(store):
            sys.exit(0)
    remembered_user = store.remembered_user()
    if remembered_user:
        current_user = remembered_user
    else:
        login = LoginDialog(store)
        if login.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)
        current_user = login.user
    window = AdminApp(current_user)
    window.show()
    sys.exit(app.exec())

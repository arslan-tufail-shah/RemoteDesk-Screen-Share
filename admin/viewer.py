import socket
import struct
import threading
import json
import time
from random import randint
import cv2
import numpy as np
from datetime import datetime
import os
from PyQt6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout, QPushButton, QHBoxLayout, QMessageBox
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtCore import Qt, QEvent, QTimer, pyqtSignal
from config import PIN_CODE, TCP_PORT, CONTROL_PORT


def recv_exact(sock, size):
    """Read exactly `size` bytes from a stream socket.

    A plain sock.recv(n) is only guaranteed to return "up to" n bytes -
    under network fragmentation/congestion it can return less, which
    silently desyncs any length-prefixed framing built on top of it.
    """
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class Viewer(QWidget):
    control_status_changed = pyqtSignal(str)
    stream_status_changed = pyqtSignal(str)
    viewer_rejected = pyqtSignal(str)
    viewer_kicked = pyqtSignal(str)
    capture_pause_changed = pyqtSignal(bool)
    usb_status_changed = pyqtSignal(bool)

    # Must match agent.py's STREAM_MSG_* constants.
    STREAM_MSG_FRAME = 1
    STREAM_MSG_NOTICE = 2

    def __init__(self, ip, port, username=None, control_port=None, role=None, admin_id=None):
        super().__init__()
        title = f"Viewing {ip}"
        if username:
            title = f"{username} @ {ip}"
        self.setWindowTitle(title)
        self.resize(1150, 760)
        self.setMinimumSize(760, 520)
        # Make closing this window actually delete its C++ object, so the
        # "destroyed" signal (which AdminApp uses to prune self.viewers)
        # fires promptly instead of only when Python happens to garbage
        # collect this object later. _safe_emit() already tolerates a
        # background thread emitting after this happens.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.ip = ip
        self.port = port
        self.control_port = control_port or CONTROL_PORT
        # self.username is the *agent's* OS username (for display only -
        # window title, "already viewing <agent>" messages, etc).
        self.username = username or "unknown"
        self.role = role or "supervisor"
        # self.admin_id is this *admin's own* stable identity, used in the
        # exclusivity/takeover handshake so the agent can tell different
        # admins apart. This used to be derived from self.username (the
        # agent's name) plus a random per-window uuid - which meant every
        # admin connecting to a given agent looked identical to the
        # protocol (since self.username is the same for everyone viewing
        # that agent), and could cause a spurious "already viewing" / kick
        # against yourself. Callers should always pass the real logged-in
        # admin's username here.
        self.admin_id = admin_id or self.username
        self.interact_enabled = False
        # USB port access status as last reported by the agent - purely
        # informational display here, never gates Interact.
        self.usb_blocked = False
        self.current_pin = None
        self.control_sock = None
        self.lock_active = False

        self.setStyleSheet("""
            QWidget {
                font-family: "Segoe UI", "Noto Sans", sans-serif;
                color: #152235;
            }
            Viewer {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #f6fbff,
                    stop:0.5 #eef6ff,
                    stop:1 #edf9f3
                );
            }
            QLabel#titleLabel {
                font-size: 22px;
                font-weight: 700;
                color: #0f2c47;
            }
            QLabel#metaLabel {
                color: #4f667f;
                font-size: 12px;
            }
            QLabel#streamLabel {
                border: 1px solid #becfe2;
                border-radius: 16px;
                background-color: rgba(9, 20, 30, 0.93);
            }
            QLabel#statusPill {
                border: 1px solid #c0d6ec;
                border-radius: 11px;
                background-color: rgba(255, 255, 255, 0.86);
                color: #2e4f70;
                font-size: 12px;
                padding: 4px 10px;
            }
            QPushButton {
                border: 1px solid #b7cde4;
                border-radius: 12px;
                background-color: rgba(255, 255, 255, 0.92);
                color: #16314d;
                font-weight: 600;
                padding: 9px 14px;
                min-height: 18px;
            }
            QPushButton:hover {
                background-color: #f0f7ff;
                border-color: #88b4de;
            }
            QPushButton:pressed {
                background-color: #e4f1ff;
            }
        """)

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(18, 16, 18, 16)
        main_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        title_block = QVBoxLayout()
        title_block.setSpacing(2)

        self.title_label = QLabel(f"Live Session - {self.username}")
        self.title_label.setObjectName("titleLabel")
        title_block.addWidget(self.title_label)

        self.meta_label = QLabel(f"Target: {self.ip}:{self.port} | Control: {self.control_port}")
        self.meta_label.setObjectName("metaLabel")
        title_block.addWidget(self.meta_label)

        header_layout.addLayout(title_block, 1)

        self.record_status_label = QLabel("Recording: Off")
        self.record_status_label.setObjectName("statusPill")
        header_layout.addWidget(self.record_status_label)

        main_layout.addLayout(header_layout)

        # Video display label
        self.label = QLabel()
        self.label.setObjectName("streamLabel")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setText("Waiting for stream...")
        self.label.setScaledContents(True)
        self.label.setMouseTracking(True)
        self.label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.label.installEventFilter(self)
        main_layout.addWidget(self.label, 1)

        # Control buttons layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        self.record_button = QPushButton("Start Recording")
        self.record_button.clicked.connect(self.toggle_recording)
        button_layout.addWidget(self.record_button)

        self.interact_button = QPushButton("Interact: Off")
        self.interact_button.clicked.connect(self.toggle_interact)
        button_layout.addWidget(self.interact_button)

        self.lock_button = QPushButton("Lock View")
        self.lock_button.clicked.connect(self.toggle_lock)
        button_layout.addWidget(self.lock_button)

        self.pin_label = QLabel("PIN: N/A")
        self.pin_label.setObjectName("statusPill")
        button_layout.addWidget(self.pin_label)
        
        # Control channel status
        self.control_status_label = QLabel("Control: Disconnected")
        self.control_status_label.setObjectName("statusPill")
        button_layout.addWidget(self.control_status_label)

        # USB access status - informational only. This is a separate,
        # OS-level policy (blocking USB storage devices on the remote
        # machine) and must never affect the Interact (keyboard/mouse)
        # button above.
        self.usb_status_label = QLabel("USB: Unknown")
        self.usb_status_label.setObjectName("statusPill")
        button_layout.addWidget(self.usb_status_label)

        button_layout.addStretch(1)

        main_layout.addLayout(button_layout)
        self.setLayout(main_layout)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFocus()

        self.control_status_changed.connect(self.control_status_label.setText)
        self.usb_status_changed.connect(self._on_usb_status_changed)
        self.stream_status_changed.connect(self._on_stream_status_changed)
        self.viewer_rejected.connect(self._on_viewer_rejected)
        self.viewer_kicked.connect(self._on_viewer_kicked)
        self.capture_pause_changed.connect(self._on_capture_pause_changed)

        # Single-slot "latest frame" buffer. The network thread only ever
        # overwrites this - it never queues frames - and a timer on the GUI
        # thread paints whatever is currently in it at a capped rate. This
        # is what stops a GUI thread that's momentarily slower than the
        # incoming stream from piling up an unbounded backlog of decoded
        # QPixmaps in Qt's event queue (each one several MB at full
        # resolution), which is what was causing this app to run out of
        # memory and crash during longer sessions.
        self._frame_lock = threading.Lock()
        self._latest_pixmap = None
        self._capture_paused = False
        self._paint_timer = QTimer(self)
        self._paint_timer.timeout.connect(self._paint_latest_frame)
        self._paint_timer.start(33)  # ~30 fps cap on GUI-side painting

        # Recording setup
        self.is_recording = False
        self.video_writer = None
        self.frame_count = 0
        self.last_frame_shape = None
        self.recording_fps = 30.0
        self.recording_start_time = None
        self.recording_written_frames = 0
        self.recording_last_frame = None

        # Stream socket - connected (and reconnected) by connect_stream()
        self.sock = None
        self._stop_requested = False
        self._rejected = False

        self.connect_stream()
        self.connect_control_channel()

    def _paint_latest_frame(self):
        if self._capture_paused:
            # Remote session is locked - leave the "locked" placeholder
            # text in place rather than painting over it with whatever
            # might land in the buffer (there shouldn't be anything, since
            # the agent stops sending real frames while paused, but this
            # keeps the two states from ever visually racing).
            return
        with self._frame_lock:
            pixmap = self._latest_pixmap
            self._latest_pixmap = None
        if pixmap is not None:
            self.label.setPixmap(pixmap)

    def _on_stream_status_changed(self, text):
        # Only fall back to a text placeholder while there's no picture yet
        # (or the connection dropped) - once frames are flowing, the
        # painted pixmap should stay on screen instead of being overwritten.
        if text != "Stream: Connected":
            self.label.setText(text)

    def _on_viewer_rejected(self, other_admin_id):
        message = (
            f"{other_admin_id} is already viewing {self.username}.\n\n"
            "This window will close - if that's you in another window, "
            "just reopen this device from the list once you're done there."
        )
        QMessageBox.warning(self, "Agent already in use", message)
        self._rejected = True
        self.close()

    def _on_viewer_kicked(self, by_admin_id):
        message = (
            f"{by_admin_id} took over this session, so you were disconnected.\n\n"
            "You can reopen this device from the list at any time."
        )
        QMessageBox.information(self, "Session taken over", message)
        self._stop_requested = True
        self.close()

    def _on_capture_pause_changed(self, paused):
        self._capture_paused = paused
        if paused:
            # Discard any frame that was already buffered right before the
            # lock happened, so the paint timer can't flash a stale frame
            # on top of this placeholder.
            with self._frame_lock:
                self._latest_pixmap = None
            self.label.setText("Remote session is locked.\nWaiting for it to unlock...")
        # When resuming, just let the next real frame repaint the label
        # naturally via _paint_latest_frame - nothing to clear here.

    def _safe_emit(self, signal_name, *args):
        """Emit a Qt signal by name, tolerating the underlying C++ widget
        having already been torn down.

        Background threads (the stream/control reconnect loops) can still
        be alive and mid-iteration for a brief window after this window
        has been closed. If that happens, PyQt raises 'RuntimeError:
        wrapped C/C++ object of type Viewer has been deleted' - and since
        that can happen *inside* an except block's own cleanup code, it
        was an uncaught exception that silently killed the whole
        reconnect thread, with no further retries possible until the
        entire admin app was restarted.

        Crucially, this takes the signal's *name* (a string), not the
        signal object itself: `self._safe_emit(self.some_signal, ...)`
        looks harmless, but Python evaluates `self.some_signal` - a bound
        attribute lookup on a sip-wrapped QObject - as part of building
        the argument list for this very call, which happens in the
        *caller's* frame before this function's own try/except ever runs.
        That lookup is exactly where the RuntimeError actually fires once
        the widget is deleted, so wrapping only the emit() call here never
        protected anything. Doing `getattr(self, signal_name)` inside the
        try block below is what actually catches it.
        """
        try:
            signal = getattr(self, signal_name)
            signal.emit(*args)
        except RuntimeError:
            self._stop_requested = True

    def _safe_ui(self, fn):
        """Run a direct widget-mutating call (e.g. self.some_button.setText(...))
        tolerating the widget having already been deleted.

        Unlike _safe_emit, calls wrapped here don't go through Qt's
        signal/slot queuing, so they also aren't automatically marshalled
        onto the GUI thread - this should only be used for calls that are
        either already on the GUI thread, or that are safe/idempotent
        enough not to matter if they interleave. Its real job is just to
        swallow the "wrapped C/C++ object has been deleted" RuntimeError
        that fires once this window has been closed, since there's nothing
        left to update at that point.
        """
        try:
            fn()
        except RuntimeError:
            pass

    def connect_stream(self):
        """Connect the video stream socket, retrying with backoff on
        network failures. Mirrors connect_control_channel()'s retry loop
        so a dropped stream socket recovers on its own instead of leaving
        the viewer permanently blank until the user reopens it - unlike
        the control channel, this previously had no reconnect logic at
        all.
        """

        def _connect_loop():
            backoff_seconds = 2
            while not self._stop_requested:
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    except OSError:
                        pass
                    sock.settimeout(5)
                    print(f"[*] Connecting to {self.ip}:{self.port}")
                    sock.connect((self.ip, self.port))

                    handshake = json.dumps({
                        "pin": PIN_CODE,
                        "admin_id": self.admin_id,
                        "role": self.role,
                    }).encode()
                    sock.sendall(struct.pack(">I", len(handshake)) + handshake)

                    reply = self._read_handshake_reply(sock)
                    if reply is None:
                        raise ConnectionError("no handshake reply from agent")

                    if reply.get("status") != "ok":
                        reason = reply.get("reason", "connection rejected by agent")
                        print(f"[!] Stream connection rejected: {reason}")
                        sock.close()
                        self._safe_emit("viewer_rejected", reason)
                        return  # rejection isn't a network failure - don't retry

                    # A read timeout here doubles as dead-connection
                    # detection: frames arrive continuously, so if none
                    # show up for this long the link is almost certainly
                    # dead even without a clean TCP close.
                    sock.settimeout(10)
                    self.sock = sock
                    # A fresh connection (e.g. after a Fast User Switch
                    # handoff to a different session's agent instance)
                    # always starts unpaused - the new instance will send
                    # its own "capture_paused" notice if it turns out the
                    # active session is locked too.
                    self._capture_paused = False
                    self._safe_emit("stream_status_changed", "Stream: Connected")
                    print("[*] Stream connected")
                    backoff_seconds = 2

                    self.receive_stream()  # blocks until the connection drops

                    self.sock = None
                    if self._stop_requested or self._rejected:
                        return
                    self._safe_emit("stream_status_changed", "Stream: Reconnecting...")
                except Exception as error:
                    print(f"[!] Stream connection failed: {error}")
                    self._safe_emit("stream_status_changed", "Stream: Disconnected")
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass
                    self.sock = None

                if self._stop_requested:
                    return
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

        threading.Thread(target=_connect_loop, daemon=True).start()

    def _read_handshake_reply(self, sock):
        size_data = recv_exact(sock, 4)
        if not size_data:
            return None
        try:
            length = struct.unpack(">I", size_data)[0]
        except struct.error:
            return None
        if length <= 0 or length > 8192:
            return None
        payload = recv_exact(sock, length)
        if not payload:
            return None
        try:
            return json.loads(payload.decode())
        except json.JSONDecodeError:
            return None

    def connect_control_channel(self):
        """Connect the control socket, authenticate with the PIN (this
        channel previously accepted mouse/key commands from anyone who
        could open the port at all), and reconnect with backoff if it
        drops - mirroring connect_stream()'s reconnect logic, since this
        channel previously only ever tried to connect once and never
        recovered from a mid-session drop.
        """

        def _connect_loop():
            backoff_seconds = 2
            while not self._stop_requested:
                sock = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    except OSError:
                        pass
                    sock.settimeout(5)
                    print(f"[*] Connecting control channel to {self.ip}:{self.control_port}")
                    sock.connect((self.ip, self.control_port))

                    handshake = json.dumps({
                        "pin": PIN_CODE,
                        "admin_id": self.admin_id,
                        "role": self.role,
                    }).encode()
                    sock.sendall(struct.pack(">I", len(handshake)) + handshake)

                    reply = self._read_handshake_reply(sock)
                    if reply is None or reply.get("status") != "ok":
                        reason = reply.get("reason") if reply else "no handshake reply"
                        raise ConnectionError(reason)

                    self.usb_blocked = bool(reply.get("usb_blocked", False))
                    self._safe_emit("usb_status_changed", self.usb_blocked)

                    # A read timeout here just lets the reader loop below
                    # check _stop_requested periodically - it isn't treated
                    # as a dead connection the way the stream socket's is,
                    # since the agent only sends control messages
                    # occasionally (access-change notices), not continuously.
                    sock.settimeout(5)
                    self.control_sock = sock
                    print("[*] Control channel connected")
                    self._safe_emit("control_status_changed", "Control: Connected")
                    backoff_seconds = 2

                    self._read_control_messages(sock)  # blocks until disconnect

                    self.control_sock = None
                    if self._stop_requested:
                        return
                    self._safe_emit("control_status_changed", "Control: Reconnecting...")
                except Exception as e:
                    print(f"[!] Control channel error: {e}")
                    self.control_sock = None
                    self._safe_emit("control_status_changed", "Control: Disconnected")
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass

                if self._stop_requested:
                    return
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30)

        # run connect attempts in background so UI stays responsive
        threading.Thread(target=_connect_loop, daemon=True).start()

    def _read_control_messages(self, sock):
        """Read agent-initiated control messages (USB access-change
        notices, and periodic watchdog pings that exist purely so the
        agent can detect a dead connection quickly - see agent.py's
        control_watchdog) until the connection drops."""
        while not self._stop_requested:
            try:
                size_data = recv_exact(sock, 4)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[!] control read error: {e}")
                break

            if not size_data:
                print("[!] control channel closed by agent")
                break

            try:
                size = struct.unpack(">I", size_data)[0]
                payload = recv_exact(sock, size)
            except Exception as e:
                print(f"[!] control payload read error: {e}")
                break

            if not payload:
                break

            try:
                message = json.loads(payload.decode())
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            event = message.get("event")
            if event == "usb_access_changed":
                blocked = bool(message.get("blocked", False))
                self.usb_blocked = blocked
                self._safe_emit("usb_status_changed", blocked)
            # "ping" and any other/unknown event are ignored - they exist
            # only so the agent can detect a dead connection by failing to
            # write to it, not to trigger anything on this side.

        try:
            sock.close()
        except Exception:
            pass

    def _on_usb_status_changed(self, blocked):
        # Informational only - USB access is a separate policy from
        # keyboard/mouse Interact and must never enable/disable it.
        if blocked:
            self.usb_status_label.setText("USB: Blocked")
        else:
            self.usb_status_label.setText("USB: Allowed")

    def send_control_command(self, command):
        if not self.control_sock:
            print("[!] Control channel not connected, dropping command:", command.get("action"))
            self._safe_emit("control_status_changed", "Control: Disconnected")
            return
        try:
            data = json.dumps(command).encode()
            self.control_sock.sendall(struct.pack(">I", len(data)) + data)
        except Exception as e:
            print(f"[!] Control send failed: {e}")
            self.control_sock = None
            self._safe_emit("control_status_changed", "Control: Disconnected")

    def toggle_recording(self):
        """Start or stop video recording"""
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def toggle_interact(self):
        self.interact_enabled = not self.interact_enabled
        self.interact_button.setText("Interact: On" if self.interact_enabled else "Interact: Off")
        self.interact_button.setStyleSheet(
            "background-color: #dff3e5; border-color: #8ec7a0;" if self.interact_enabled else ""
        )
        self.send_control_command({
            "action": "set_interact",
            "enabled": self.interact_enabled,
            "admin_id": self.admin_id,
        })

    def toggle_lock(self):
        """Toggle lock state: either lock or unlock"""
        if self.lock_active:
            self.send_unlock_command()
        else:
            self.send_lock_command()

    def send_lock_command(self):
        """Send lock command with random PIN"""
        self.current_pin = str(randint(1000, 9999))
        self.pin_label.setText(f"PIN: {self.current_pin}")
        self.lock_button.setText("Unlock")
        self.lock_button.setStyleSheet("background-color: #fff0e0; border-color: #e3b782;")
        self.lock_active = True
        self.send_control_command({"action": "lock", "pin": self.current_pin})

    def send_unlock_command(self):
        """Send unlock command to release lock"""
        self.lock_button.setText("Lock View")
        self.lock_button.setStyleSheet("")
        self.lock_active = False
        self.pin_label.setText("PIN: N/A")
        self.send_control_command({"action": "unlock"})

    def start_recording(self):
        """Start recording video"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.mp4"

        if not os.path.exists("recordings"):
            os.makedirs("recordings")

        filepath = os.path.join("recordings", filename)
        self.recording_filepath = filepath
        self.is_recording = True
        self.frame_count = 0
        self.recording_start_time = None
        self.recording_written_frames = 0
        self.recording_last_frame = None
        self._safe_ui(lambda: self.record_button.setText("Stop Recording"))
        self._safe_ui(lambda: self.record_button.setStyleSheet(
            "background-color: #ffe7e7; border-color: #e0aaaa;"))
        self._safe_ui(lambda: self.record_status_label.setText("Recording: On"))
        self._safe_ui(lambda: self.record_status_label.setStyleSheet(
            "border: 1px solid #dea7a7; border-radius: 11px; background-color: #ffe8e8; color: #8a2d2d; font-size: 12px; padding: 4px 10px;"
        ))
        print(f"[+] Recording started: {filepath}")

    def stop_recording(self):
        """Stop recording video"""
        if self.video_writer:
            self.video_writer.release()
            self.video_writer = None
        duration = self.recording_written_frames / self.recording_fps if self.recording_written_frames else 0
        self.is_recording = False
        self.recording_start_time = None
        self.recording_last_frame = None
        # This can be called from a background thread (receive_stream()'s
        # cleanup path when the connection drops), unlike start_recording()
        # which is only ever triggered by a button click on the GUI
        # thread. Directly touching a widget from another thread is unsafe
        # in Qt even when the widget still exists, and once this window
        # has been closed (WA_DeleteOnClose deletes it promptly) it's a
        # guaranteed 'wrapped C/C++ object has been deleted' crash - so
        # every widget touch here goes through _safe_ui.
        self._safe_ui(lambda: self.record_button.setText("Start Recording"))
        self._safe_ui(lambda: self.record_button.setStyleSheet(""))
        self._safe_ui(lambda: self.record_status_label.setText("Recording: Off"))
        self._safe_ui(lambda: self.record_status_label.setStyleSheet(""))
        print(f"[-] Recording stopped ({self.recording_written_frames} frames, ~{duration:.2f}s)")

    def _write_smoothed_recording_frame(self, frame):
        if not self.is_recording or frame is None:
            return

        now = time.time()

        if self.video_writer is None:
            h, w = frame.shape[:2]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.video_writer = cv2.VideoWriter(
                self.recording_filepath,
                fourcc,
                self.recording_fps,
                (w, h)
            )
            self.last_frame_shape = frame.shape
            self.recording_start_time = now
            self.recording_written_frames = 0
            self.recording_last_frame = frame.copy()
            print(f"[+] Video writer initialized: {w}x{h} @ {self.recording_fps:.1f}fps")

        if self.video_writer is None or self.recording_start_time is None:
            return

        target_frame_index = int((now - self.recording_start_time) * self.recording_fps)

        # Keep a stable recording resolution for the entire file.
        base_h, base_w = self.last_frame_shape[:2]
        if frame.shape[:2] != (base_h, base_w):
            frame = cv2.resize(frame, (base_w, base_h), interpolation=cv2.INTER_AREA)

        # Fill time gaps with the last frame so playback remains smooth.
        while self.recording_written_frames < target_frame_index and self.recording_last_frame is not None:
            self.video_writer.write(self.recording_last_frame)
            self.recording_written_frames += 1

        self.video_writer.write(frame)
        self.recording_written_frames += 1
        self.recording_last_frame = frame.copy()

    def eventFilter(self, source, event):
        if source == self.label and self.interact_enabled:
            if event.type() in (
                QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseMove,
            ):
                self.handle_mouse_event(event)
                return True
            if event.type() == QEvent.Type.Wheel:
                self.handle_wheel_event(event)
                return True
            if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                self.send_key_event(
                    event,
                    "press" if event.type() == QEvent.Type.KeyPress else "release",
                )
                return True
        return super().eventFilter(source, event)

    def handle_mouse_event(self, event):
        if not self.interact_enabled:
            return

        if event.type() == QEvent.Type.MouseButtonPress:
            self.label.setFocus(Qt.FocusReason.MouseFocusReason)

        label_width = max(1, self.label.width())
        label_height = max(1, self.label.height())
        x = event.position().x()
        y = event.position().y()
        norm_x = min(max(x / label_width, 0.0), 1.0)
        norm_y = min(max(y / label_height, 0.0), 1.0)

        event_type = None
        if event.type() == QEvent.Type.MouseButtonPress:
            event_type = "press"
        elif event.type() == QEvent.Type.MouseButtonRelease:
            event_type = "release"
        elif event.type() == QEvent.Type.MouseMove:
            event_type = "move"

        button = None
        if hasattr(event, 'button'):
            button = self._mouse_button_name(event.button())

        self.send_control_command({
            "action": "mouse",
            "type": event_type,
            "x": norm_x,
            "y": norm_y,
            "button": button,
            "admin_id": self.admin_id,
        })

    def handle_wheel_event(self, event):
        if not self.interact_enabled:
            return

        label_width = max(1, self.label.width())
        label_height = max(1, self.label.height())
        x = event.position().x()
        y = event.position().y()
        norm_x = min(max(x / label_width, 0.0), 1.0)
        norm_y = min(max(y / label_height, 0.0), 1.0)

        delta_y = event.angleDelta().y()
        delta_x = event.angleDelta().x()
        pixel_delta_y = event.pixelDelta().y()
        pixel_delta_x = event.pixelDelta().x()

        self.send_control_command({
            "action": "mouse",
            "type": "wheel",
            "x": norm_x,
            "y": norm_y,
            "delta_y": delta_y,
            "delta_x": delta_x,
            "pixel_delta_y": int(pixel_delta_y),
            "pixel_delta_x": int(pixel_delta_x),
            "admin_id": self.admin_id,
        })

    def _mouse_button_name(self, button):
        if button == Qt.MouseButton.LeftButton:
            return "left"
        if button == Qt.MouseButton.RightButton:
            return "right"
        if button == Qt.MouseButton.MiddleButton:
            return "middle"
        return "unknown"

    def keyPressEvent(self, event):
        if self.interact_enabled:
            self.send_key_event(event, "press")
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if self.interact_enabled:
            self.send_key_event(event, "release")
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _mods_to_int(self, mods):
        try:
            return int(mods)
        except Exception:
            value = getattr(mods, "value", None)
            if value is not None:
                try:
                    return int(value)
                except Exception:
                    pass
        return 0

    def send_key_event(self, event, event_type):
        # Ensure modifiers are serialized as an int across PyQt versions
        modifiers = self._mods_to_int(event.modifiers())

        mods_state = {
            "shift": bool(modifiers & Qt.KeyboardModifier.ShiftModifier.value),
            "ctrl": bool(modifiers & Qt.KeyboardModifier.ControlModifier.value),
            "alt": bool(modifiers & Qt.KeyboardModifier.AltModifier.value),
            "meta": bool(modifiers & Qt.KeyboardModifier.MetaModifier.value),
        }

        command = {
            "action": "key",
            "type": event_type,
            "key": int(event.key()),
            "text": event.text(),
            "modifiers": int(modifiers),
            "mods": mods_state,
            "admin_id": self.admin_id,
        }

        # For release events, include active modifiers after this key event.
        if event_type == "release":
            next_mods = self._mods_to_int(QApplication.keyboardModifiers())
            command["next_modifiers"] = next_mods
            command["next_mods"] = {
                "shift": bool(next_mods & Qt.KeyboardModifier.ShiftModifier.value),
                "ctrl": bool(next_mods & Qt.KeyboardModifier.ControlModifier.value),
                "alt": bool(next_mods & Qt.KeyboardModifier.AltModifier.value),
                "meta": bool(next_mods & Qt.KeyboardModifier.MetaModifier.value),
            }

        self.send_control_command(command)

    def receive_stream(self):
        """Read frames off self.sock until the connection drops or times
        out. Returns (rather than looping forever) so connect_stream()'s
        caller can decide whether to reconnect.
        """
        sock = self.sock
        if sock is None:
            return

        while not self._stop_requested:
            try:
                type_byte = recv_exact(sock, 1)
            except socket.timeout:
                # No message arrived within the read timeout - the agent
                # sends continuously, so this means the link is dead even
                # though no clean TCP close happened (cable pull, dropped
                # Wi-Fi, NAT timeout, etc). Treat it the same as a
                # disconnect so connect_stream() reconnects instead of
                # hanging forever.
                print("[!] viewer: stream read timed out, treating as dead connection")
                break
            except Exception as e:
                print(f"[!] viewer recv error: {e}")
                break

            if not type_byte:
                print("[!] viewer: agent closed connection")
                break
            msg_type = type_byte[0]

            try:
                size_data = recv_exact(sock, 4)
            except socket.timeout:
                print("[!] viewer: stream read timed out mid-message")
                break
            except Exception as e:
                print(f"[!] viewer recv error: {e}")
                break

            if not size_data:
                print("[!] viewer: connection closed mid-message")
                break

            try:
                size = struct.unpack(">I", size_data)[0]
            except struct.error as e:
                print(f"[!] viewer: invalid message size: {e}")
                break

            try:
                data = recv_exact(sock, size)
            except socket.timeout:
                print("[!] viewer: stream read timed out mid-payload")
                break
            except Exception as e:
                print(f"[!] viewer recv packet error: {e}")
                break

            if not data:
                print("[!] viewer: connection closed mid-payload")
                break

            if msg_type == self.STREAM_MSG_NOTICE:
                try:
                    notice = json.loads(data.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    notice = {}
                event = notice.get("event")
                if event == "kicked":
                    by_admin_id = notice.get("by", "another admin")
                    print(f"[!] viewer: session taken over by {by_admin_id}")
                    self._stop_requested = True
                    self._safe_emit("viewer_kicked", by_admin_id)
                    break
                if event == "capture_paused":
                    # The agent's Windows session is locked - it keeps the
                    # connection open and will resume on its own, so just
                    # show a clear placeholder instead of a frozen/blank
                    # frame until "capture_resumed" arrives.
                    print("[*] viewer: remote session locked, capture paused")
                    self._safe_emit("capture_pause_changed", True)
                    continue
                if event == "capture_resumed":
                    print("[*] viewer: remote session unlocked, capture resumed")
                    self._safe_emit("capture_pause_changed", False)
                    continue
                # Unknown notice type - ignore and keep reading.
                continue

            if msg_type != self.STREAM_MSG_FRAME:
                print(f"[!] viewer: unknown stream message type {msg_type}")
                continue

            try:
                pixmap = QPixmap()
                if not pixmap.loadFromData(data):
                    continue

                if self.is_recording:
                    frame = cv2.imdecode(
                        np.frombuffer(data, np.uint8),
                        cv2.IMREAD_COLOR
                    )
                    if frame is None:
                        continue
                    try:
                        self._write_smoothed_recording_frame(frame)
                        self.frame_count += 1
                    except Exception as e:
                        print(f"[!] Error writing frame to video: {e}")

                # Overwrite the single-slot buffer rather than emitting a
                # queued signal per frame - see the comment in __init__ for
                # why. If the GUI thread hasn't painted the previous frame
                # yet, it's simply dropped instead of piling up.
                with self._frame_lock:
                    self._latest_pixmap = pixmap
            except Exception as e:
                print(f"[!] Error processing frame: {e}")
                continue

        self.stop_recording()
        try:
            sock.close()
        except Exception:
            pass
        print("[!] stream connection ended")

    def closeEvent(self, event):
        # Proactively tell the agent to release Interact ownership before
        # tearing anything down, rather than relying purely on the agent
        # noticing this connection died sometime in the next few seconds
        # (via its own read timeout or watchdog ping). This makes a
        # reconnect to the same agent right after closing far less likely
        # to be rejected with "another admin owns control" while that
        # passive cleanup is still catching up.
        if self.interact_enabled and self.control_sock:
            self.send_control_command({
                "action": "set_interact",
                "enabled": False,
                "admin_id": self.admin_id,
            })
        # Don't close self.sock/self.control_sock directly from here: this
        # runs on the GUI thread, while the reconnect-loop background
        # threads may be blocked inside recv() on those exact same socket
        # objects. Closing a socket from a different thread than the one
        # blocked reading it is a classic source of WinError 10038
        # (WSAENOTSOCK) on Windows, and once that happens mid-cleanup here
        # it can leave things in a half-torn-down state. Setting
        # _stop_requested is enough: each background thread checks it and
        # closes its own socket once its next read times out (within
        # ~10s for the stream, ~5s for control), which is a small, bounded
        # delay in exchange for never touching a socket from the wrong
        # thread.
        self._stop_requested = True
        self.stop_recording()
        event.accept()

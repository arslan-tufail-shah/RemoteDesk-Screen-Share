import atexit
import ctypes
import json
import socket
import struct
import threading
import time
import getpass
import tkinter as tk
import cv2
import numpy as np
from PIL import ImageGrab
try:
    import winreg
except ImportError:
    winreg = None  # non-Windows dev environment - USB blocking becomes a no-op
from config import TCP_PORT, CONTROL_PORT, PIN_CODE, FRAME_QUALITY, FPS, MAX_WIDTH, MAX_HEIGHT
from discovery import start_discovery, usb_policy
from session_guard import guard

clients = []
# Message type byte prefixing each length-prefixed payload on the stream
# socket, so the agent can push an out-of-band notice (e.g. "you've been
# taken over") to a viewer without it being mistaken for a JPEG frame.
STREAM_MSG_FRAME = 1
STREAM_MSG_NOTICE = 2
control_state = {
    "enabled": False,
    "owner": None,
    "lock": threading.Lock(),
}
# Tracks which admin currently "owns" the video stream, so a second admin
# can't silently start viewing the same agent while another one is already
# connected.
viewer_lock_state = {
    "conn": None,
    "admin_id": None,
    "lock": threading.Lock(),
}
control_clients = []
control_clients_lock = threading.Lock()


def apply_usb_policy(blocked):
    """Enable/disable the Windows USB mass-storage driver (USBSTOR) via the
    registry. This is the standard lightweight way to block USB drives at
    the OS level; it's a *storage* block, not a way to disable physical USB
    ports themselves, and it requires the agent to run elevated (as
    Administrator) to write to HKLM. A drive that's already plugged in when
    the policy changes may need to be unplugged/replugged (or the machine
    rebooted) before the new setting takes effect - that's a limitation of
    this technique, not a bug.
    """
    if winreg is None:
        print("[!] USB policy change requested but winreg is unavailable (not Windows)")
        return False
    try:
        key_path = r"SYSTEM\CurrentControlSet\Services\USBSTOR"
        value = 4 if blocked else 3  # 4 = disabled, 3 = manual start (enabled)
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "Start", 0, winreg.REG_DWORD, value)
        winreg.CloseKey(key)
        print(f"[*] USB storage policy applied: {'blocked' if blocked else 'allowed'}")
        return True
    except PermissionError:
        print("[!] Failed to apply USB policy: agent needs to run as Administrator")
        return False
    except Exception as e:
        print(f"[!] Failed to apply USB policy: {e}")
        return False

lock_status = {
    "active": False,
    "pin": None,
    "lock": threading.Lock(),
    "unlock_requested": False,
}

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
WHEEL_DELTA = 120
PIXEL_TO_WHEEL_FACTOR = 8

QT_SHIFT_MODIFIER = 0x02000000
QT_CONTROL_MODIFIER = 0x04000000
QT_ALT_MODIFIER = 0x08000000
QT_META_MODIFIER = 0x10000000

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LWIN = 0x5B

QT_TO_VK = {
    0x01000004: 0x0D,
    0x01000005: 0x0D,
    0x01000003: 0x08,
    0x01000001: 0x09,
    0x01000000: 0x1B,
    0x01000007: 0x2E,
    0x01000008: 0x13,
    0x01000009: 0x2C,
    0x01000010: 0x24,
    0x01000011: 0x23,
    0x01000016: 0x21,
    0x01000017: 0x22,
    0x01000012: 0x25,
    0x01000013: 0x26,
    0x01000014: 0x27,
    0x01000015: 0x28,
    0x01000024: 0x14,
    0x01000025: 0x90,
    0x01000026: 0x91,
    0x01000020: 0x10,
    0x01000021: 0x11,
    0x01000023: 0x12,
    0x20: 0x20,
    0x01000030: 0x70,
    0x01000031: 0x71,
    0x01000032: 0x72,
    0x01000033: 0x73,
    0x01000034: 0x74,
    0x01000035: 0x75,
    0x01000036: 0x76,
    0x01000037: 0x77,
    0x01000038: 0x78,
    0x01000039: 0x79,
    0x0100003A: 0x7A,
    0x0100003B: 0x7B,
}

wheel_state = {
    "vertical_remainder": 0.0,
    "horizontal_remainder": 0.0,
}
pressed_vks = set()


def recv_exact(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def send_handshake_reply(conn, obj):
    try:
        data = json.dumps(obj).encode()
        conn.sendall(struct.pack(">I", len(data)) + data)
        return True
    except Exception as e:
        print(f"[!] failed to send handshake reply: {e}")
        return False


def send_typed(conn, msg_type, payload):
    conn.sendall(bytes([msg_type]) + struct.pack(">I", len(payload)) + payload)


def send_mouse_input(command):
    if lock_status["active"]:
        return

    # Move cursor only when a coordinate is explicitly provided.
    # This avoids accidental recentering for wheel-only events.
    if "x" in command and "y" in command:
        norm_x = max(0.0, min(1.0, float(command.get("x", 0.5))))
        norm_y = max(0.0, min(1.0, float(command.get("y", 0.5))))
        x = int(norm_x * 65535)
        y = int(norm_y * 65535)
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, x, y, 0, 0)

    event_type = command.get("type")
    button = command.get("button")
    if event_type == "press":
        if button == "left":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        elif button == "right":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
        elif button == "middle":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_MIDDLEDOWN, 0, 0, 0, 0)
    elif event_type == "release":
        if button == "left":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        elif button == "right":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        elif button == "middle":
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_MIDDLEUP, 0, 0, 0, 0)
    elif event_type == "wheel":
        raw_delta_y = float(command.get("delta_y", command.get("delta", 0)))
        raw_delta_x = float(command.get("delta_x", 0))

        # Use pixel deltas as fallback for high-resolution trackpad scrolling.
        if raw_delta_y == 0:
            raw_delta_y = float(command.get("pixel_delta_y", 0)) * PIXEL_TO_WHEEL_FACTOR
        if raw_delta_x == 0:
            raw_delta_x = float(command.get("pixel_delta_x", 0)) * PIXEL_TO_WHEEL_FACTOR

        wheel_state["vertical_remainder"] += raw_delta_y
        wheel_state["horizontal_remainder"] += raw_delta_x

        vertical_delta = int(wheel_state["vertical_remainder"])
        horizontal_delta = int(wheel_state["horizontal_remainder"])

        if vertical_delta:
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_WHEEL,
                0,
                0,
                vertical_delta,
                0,
            )
            wheel_state["vertical_remainder"] -= vertical_delta

        if horizontal_delta:
            ctypes.windll.user32.mouse_event(
                MOUSEEVENTF_HWHEEL,
                0,
                0,
                horizontal_delta,
                0,
            )
            wheel_state["horizontal_remainder"] -= horizontal_delta


def _qt_key_to_vk(key, text=""):
    if key is None:
        return None

    # A-Z and 0-9 map directly to VK codes.
    if 0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A:
        return key

    if text and len(text) == 1 and text.isalnum():
        return ord(text.upper())

    return QT_TO_VK.get(key)


def _modifier_vks_from_state(state):
    vks = []
    if state.get("shift"):
        vks.append(VK_SHIFT)
    if state.get("ctrl"):
        vks.append(VK_CONTROL)
    if state.get("alt"):
        vks.append(VK_MENU)
    if state.get("meta"):
        vks.append(VK_LWIN)
    return vks


def _modifier_state_from_command(command, key_name):
    state = command.get(key_name)
    if isinstance(state, dict):
        return {
            "shift": bool(state.get("shift", False)),
            "ctrl": bool(state.get("ctrl", False)),
            "alt": bool(state.get("alt", False)),
            "meta": bool(state.get("meta", False)),
        }

    mask = int(command.get("modifiers" if key_name == "mods" else "next_modifiers", 0))
    return {
        "shift": bool(mask & QT_SHIFT_MODIFIER),
        "ctrl": bool(mask & QT_CONTROL_MODIFIER),
        "alt": bool(mask & QT_ALT_MODIFIER),
        "meta": bool(mask & QT_META_MODIFIER),
    }


def _set_vk_state(vk, pressed):
    if vk is None:
        return

    if pressed:
        if vk in pressed_vks:
            return
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        pressed_vks.add(vk)
        return

    if vk not in pressed_vks:
        return
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    pressed_vks.remove(vk)


def send_key_input(command):
    if lock_status["active"]:
        return

    current_mods_state = _modifier_state_from_command(command, "mods")
    requested_modifiers = _modifier_vks_from_state(current_mods_state)

    for mod_vk in requested_modifiers:
        _set_vk_state(mod_vk, True)

    text = command.get("text", "")
    is_press = command.get("type") == "press"
    vk = _qt_key_to_vk(command.get("key"), text)

    # Use Unicode input only for plain text entry (no shortcuts/modifier chords).
    has_shortcut_mod = current_mods_state["ctrl"] or current_mods_state["alt"] or current_mods_state["meta"]
    use_unicode = bool(text) and not has_shortcut_mod and vk is None

    if use_unicode:
        for ch in text:
            if is_press:
                ctypes.windll.user32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE, 0)
            else:
                ctypes.windll.user32.keybd_event(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0)
    else:
        _set_vk_state(vk, is_press)

    if not is_press:
        next_mods_state = _modifier_state_from_command(command, "next_mods")
        keep_modifiers = set(_modifier_vks_from_state(next_mods_state))
        for mod_vk in requested_modifiers:
            if mod_vk not in keep_modifiers:
                _set_vk_state(mod_vk, False)


def show_lock_window(pin):
    root = tk.Tk()
    root.title("Locked by Admin")
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    root.bind("<Escape>", lambda event: "break")
    root.bind("<Alt-F4>", lambda event: "break")

    # Periodic check for unlock request from admin
    def check_unlock_flag():
        if lock_status["unlock_requested"]:
            with lock_status["lock"]:
                lock_status["unlock_requested"] = False
            root.destroy()
        else:
            root.after(100, check_unlock_flag)

    root.after(100, check_unlock_flag)

    root.configure(background="#222")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    frame = tk.Frame(root, bg="#222")
    frame.grid(sticky="nsew")

    label = tk.Label(
        frame,
        text="Your system has been locked by the admin. Ask your admin to unlock.",
        fg="white",
        bg="#222",
        font=("Arial", 28),
        wraplength=1000,
        justify="center"
    )
    label.pack(pady=(100, 30))

    lock_info = tk.Label(
        frame,
        text="Enter the PIN provided by your admin to unlock:",
        fg="white",
        bg="#222",
        font=("Arial", 22)
    )
    lock_info.pack(pady=(0, 20))

    entry = tk.Entry(frame, show="*", font=("Arial", 28), justify="center")
    entry.pack(ipadx=20, ipady=10)
    entry.focus_set()

    status_label = tk.Label(
        frame,
        text="",
        fg="yellow",
        bg="#222",
        font=("Arial", 18)
    )
    status_label.pack(pady=(20, 0))

    def try_unlock():
        value = entry.get().strip()
        if value == pin:
            lock_status["active"] = False
            root.destroy()
        else:
            status_label.config(text="PIN incorrect, try again.", fg="red")
            entry.delete(0, tk.END)
            entry.focus_set()

    submit = tk.Button(
        frame,
        text="Unlock",
        font=("Arial", 22),
        command=try_unlock,
        bg="#444",
        fg="white",
        activebackground="#666",
        activeforeground="white",
        padx=20,
        pady=10
    )
    submit.pack(pady=(30, 0))

    root.mainloop()


def start_lock_popup(pin):
    with lock_status["lock"]:
        if lock_status["active"]:
            return
        lock_status["active"] = True
        lock_status["pin"] = str(pin)
        threading.Thread(target=show_lock_window, args=(str(pin),), daemon=True).start()


def cleanup_control_conn(conn):
    """Release everything a control connection was holding. Used both by
    the normal disconnect path and by the watchdog below when it detects
    a connection that died without ever generating a read error."""
    with control_state["lock"]:
        if control_state["owner"] is conn:
            control_state["owner"] = None
            control_state["enabled"] = False
    with control_clients_lock:
        if conn in control_clients:
            control_clients.remove(conn)
    try:
        conn.close()
    except Exception:
        pass


def control_watchdog():
    """Periodically probe every open control connection by writing a tiny
    ping to it.

    Without this, a connection that dies ungracefully (crash, cable pull,
    sleep/hibernate, Wi-Fi drop) is never noticed by the read loop in
    handle_control_client(): recv() with a timeout just means "no data
    yet," not "the peer is gone," so that loop can sit there forever.
    That left control_state["owner"] pointing at a dead connection
    indefinitely, which blocked every future Interact request - from any
    admin - until the agent process itself was restarted. A write fails
    fast against a dead socket, so this catches it within one interval
    instead of never.
    """
    while True:
        time.sleep(5)
        with control_clients_lock:
            targets = list(control_clients)
        for c in targets:
            if not send_handshake_reply(c, {"event": "ping"}):
                print("[!] control watchdog: dead connection detected, cleaning up")
                cleanup_control_conn(c)


def broadcast_usb_change(blocked):
    """Push the new USB Allow/Block state to every currently-connected
    control client (i.e. every open Viewer window), so any admin already
    viewing this agent sees the current status without needing to
    reconnect."""
    with control_clients_lock:
        targets = list(control_clients)
    for c in targets:
        send_handshake_reply(c, {"event": "usb_access_changed", "blocked": blocked})


def process_control_message(message, conn):
    action = message.get("action")
    if action == "lock":
        start_lock_popup(message.get("pin"))
    elif action == "unlock":
        with lock_status["lock"]:
            lock_status["active"] = False
            lock_status["unlock_requested"] = True
        print("[*] Unlock requested by admin")
    elif action == "set_usb_access":
        # USB port access (storage devices) - this is a completely
        # separate policy from keyboard/mouse "Interact" control below.
        # Blocking USB must never affect an in-progress Interact session,
        # and vice versa.
        blocked = bool(message.get("blocked", False))
        with usb_policy["lock"]:
            usb_policy["blocked"] = blocked
        apply_usb_policy(blocked)
        print(f"[*] USB access {'blocked' if blocked else 'allowed'} (requested by {message.get('admin_id', 'admin')})")
        broadcast_usb_change(blocked)
    elif action == "set_interact":
        # Keyboard/mouse control ("Interact"). Independent of USB policy.
        enabled = bool(message.get("enabled", False))
        with control_state["lock"]:
            if enabled:
                if control_state["owner"] not in (None, conn):
                    print("[!] control request rejected: another admin owns control")
                    return
                control_state["owner"] = conn
                control_state["enabled"] = True
            elif control_state["owner"] is conn:
                control_state["owner"] = None
                control_state["enabled"] = False
    elif action == "mouse":
        with control_state["lock"]:
            permitted = control_state["enabled"] and control_state["owner"] is conn
        # Don't inject input while the workstation is locked - the lock
        # screen runs on a separate secure desktop synthetic input can't
        # (and shouldn't) reach, and capture is already paused for the
        # same reason.
        if permitted and guard.is_unlocked:
            send_mouse_input(message)
    elif action == "key":
        with control_state["lock"]:
            permitted = control_state["enabled"] and control_state["owner"] is conn
        if permitted and guard.is_unlocked:
            send_key_input(message)


def handle_control_client(conn, addr):
    print(f"[*] control handler started for {addr}")
    registered = False
    try:
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass
        # Handshake first: previously this channel accepted mouse/key
        # commands from anyone who could open the TCP port at all, with no
        # PIN check whatsoever (the PIN was only ever checked on the video
        # stream socket). Require the same PIN here before processing any
        # command, and only after registering this connection do we allow
        # process_control_message() to be called for it.
        conn.settimeout(5)
        size_data = recv_exact(conn, 4)
        if not size_data:
            print(f"[!] control handshake failed (no size) from {addr}")
            conn.close()
            return
        length = struct.unpack(">I", size_data)[0]
        if length <= 0 or length > 8192:
            print(f"[!] control handshake failed (bad length) from {addr}")
            conn.close()
            return
        payload = recv_exact(conn, length)
        if not payload:
            print(f"[!] control handshake failed (no payload) from {addr}")
            conn.close()
            return

        try:
            handshake = json.loads(payload.decode())
        except json.JSONDecodeError:
            print(f"[!] control handshake failed (bad json) from {addr}")
            conn.close()
            return

        pin = str(handshake.get("pin", ""))
        if pin != PIN_CODE:
            print(f"[!] control handshake: invalid pin from {addr}")
            send_handshake_reply(conn, {"status": "rejected", "reason": "invalid pin"})
            conn.close()
            return

        with usb_policy["lock"]:
            usb_blocked = usb_policy["blocked"]
        if not send_handshake_reply(conn, {"status": "ok", "usb_blocked": usb_blocked}):
            conn.close()
            return

        with control_clients_lock:
            control_clients.append(conn)
        registered = True

        conn.settimeout(1)
        while True:
            try:
                size_data = recv_exact(conn, 4)
            except socket.timeout:
                # no data yet, keep waiting
                continue

            if not size_data:
                # connection closed by client
                break

            length = struct.unpack(">I", size_data)[0]

            try:
                payload = recv_exact(conn, length)
            except socket.timeout:
                # partial read timed out, continue to wait for remaining bytes
                continue

            if not payload:
                break

            try:
                message = json.loads(payload.decode())
                print(f"[*] control message from {addr}: {message}")
                process_control_message(message, conn)
            except json.JSONDecodeError as e:
                print(f"[!] invalid control message from {addr}: {e}")
    except Exception as e:
        print(f"[!] control handler exception {addr}: {e}")
    finally:
        cleanup_control_conn(conn)
        print(f"[-] Control disconnected: {addr}")


def handle_client(conn, addr):
    print(f"[*] handler started for {addr}")
    admin_id = None
    registered = False
    try:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

        conn.settimeout(10)

        size_data = recv_exact(conn, 4)
        if not size_data:
            print(f"[!] handshake failed (no size) from {addr}")
            conn.close()
            return
        length = struct.unpack(">I", size_data)[0]
        if length <= 0 or length > 8192:
            print(f"[!] handshake failed (bad length) from {addr}")
            conn.close()
            return
        payload = recv_exact(conn, length)
        if not payload:
            print(f"[!] handshake failed (no payload) from {addr}")
            conn.close()
            return

        try:
            handshake = json.loads(payload.decode())
        except json.JSONDecodeError:
            print(f"[!] handshake failed (bad json) from {addr}")
            conn.close()
            return

        pin = str(handshake.get("pin", ""))
        admin_id = str(handshake.get("admin_id") or addr[0])
        role = str(handshake.get("role") or "supervisor")

        if pin != PIN_CODE:
            print(f"[!] invalid pin from {addr}")
            conn.close()
            return

        with viewer_lock_state["lock"]:
            current = viewer_lock_state["admin_id"]
            if current is not None and current != admin_id:
                if role == "manager":
                    # Managers can take over an in-progress session instead
                    # of being blocked by it. Notify the admin being bumped
                    # so they see why they were disconnected, rather than
                    # it looking like a network drop.
                    old_conn = viewer_lock_state["conn"]
                    print(f"[!] manager '{admin_id}' taking over stream from '{current}' for {addr}")
                    try:
                        notice = json.dumps({"event": "kicked", "by": admin_id}).encode()
                        send_typed(old_conn, STREAM_MSG_NOTICE, notice)
                    except Exception:
                        pass
                    try:
                        old_conn.close()
                    except Exception:
                        pass
                else:
                    print(f"[!] rejecting viewer '{admin_id}' from {addr}: '{current}' already viewing")
                    send_handshake_reply(conn, {"status": "rejected", "reason": current})
                    conn.close()
                    return
            viewer_lock_state["conn"] = conn
            viewer_lock_state["admin_id"] = admin_id
            registered = True

        if not send_handshake_reply(conn, {"status": "ok"}):
            conn.close()
            return

        conn.settimeout(0.5)

        user = getpass.getuser()
        print(f"[+] Authorized ({user}): {addr} as viewer '{admin_id}'")
        clients.append(conn)

        frame_count = 0
        last_log = 0
        session_locked = False

        while True:
            if not guard.owns_network:
                # Another session has become the active console session
                # (Fast User Switching away from this one) - this process
                # is no longer the one that should be serving the network
                # at all. Close this connection so the viewer disconnects
                # and reconnects, picking up whichever session's agent
                # instance takes over ownership next.
                print(f"[*] network ownership lost mid-session, closing {addr}")
                break

            if not guard.is_unlocked:
                # The workstation is locked. Unlike an ownership change,
                # this doesn't hand off to a different process - just
                # pause capture and hold the connection open so it resumes
                # instantly on unlock instead of making the admin sit
                # through a reconnect for something this transient.
                if not session_locked:
                    session_locked = True
                    print(f"[*] session locked, pausing capture for {addr}")
                    try:
                        notice = json.dumps({"event": "capture_paused", "reason": "locked"}).encode()
                        send_typed(conn, STREAM_MSG_NOTICE, notice)
                    except Exception as e:
                        print(f"[!] failed to notify {addr} of lock: {e}")
                        break
                time.sleep(0.5)
                continue
            elif session_locked:
                session_locked = False
                print(f"[*] session unlocked, resuming capture for {addr}")
                try:
                    notice = json.dumps({"event": "capture_resumed"}).encode()
                    send_typed(conn, STREAM_MSG_NOTICE, notice)
                except Exception as e:
                    print(f"[!] failed to notify {addr} of unlock: {e}")
                    break

            try:
                screenshot = ImageGrab.grab()
                frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

                h, w = frame.shape[:2]
                if MAX_WIDTH > 0 and MAX_HEIGHT > 0:
                    if w > MAX_WIDTH or h > MAX_HEIGHT:
                        scale = min(MAX_WIDTH / w, MAX_HEIGHT / h)
                        new_w = int(w * scale)
                        new_h = int(h * scale)
                        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

                _, buffer = cv2.imencode(
                    ".jpg", frame,
                    [cv2.IMWRITE_JPEG_QUALITY, FRAME_QUALITY]
                )
                data = buffer.tobytes()
            except Exception as e:
                print(f"[!] capture/encode error for {addr}: {e}")
                time.sleep(1 / FPS)
                continue

            try:
                conn.sendall(bytes([STREAM_MSG_FRAME]) + struct.pack(">I", len(data)) + data)
                frame_count += 1
                if frame_count % 30 == 0:
                    now = time.time()
                    if now - last_log > 5:
                        print(f"[*] sent {frame_count} frames to {addr}")
                        last_log = now
            except socket.timeout:
                # sendall() may have written part of the length-prefixed
                # frame before timing out. The stream is now desynced from
                # the receiver's point of view, so this connection can't be
                # trusted anymore - close it and let the viewer reconnect
                # cleanly rather than silently corrupting every frame after.
                print(f"[!] send timeout to {addr}, closing connection")
                break
            except (BrokenPipeError, ConnectionResetError) as e:
                print(f"[!] client {addr} disconnected: {e}")
                break
            except Exception as e:
                print(f"[!] send error to {addr}, closing connection: {e}")
                break

            time.sleep(1 / FPS)

    except socket.timeout:
        print(f"[!] socket timeout during handshake from {addr}")
    except Exception as e:
        print(f"[!] connection handler exception {addr}: {e}")
    finally:
        print(f"[-] Disconnected: {addr}")
        if conn in clients:
            clients.remove(conn)
        if registered:
            with viewer_lock_state["lock"]:
                if viewer_lock_state["conn"] is conn:
                    viewer_lock_state["conn"] = None
                    viewer_lock_state["admin_id"] = None
        try:
            conn.close()
        except:
            pass


def _close_all(conns):
    for c in list(conns):
        try:
            c.close()
        except Exception:
            pass


def run_stream_server_when_owner():
    """Only bind/serve the video stream port while this process holds
    network ownership (session_guard.guard). When ownership is lost - a
    different session became the active one - the listening socket is
    closed and every currently-connected client is dropped so admins
    reconnect promptly to whichever instance takes over, instead of
    multiple per-session processes all trying to bind the same port at
    once (which is what let an inactive session's black-frame capture
    reach the LAN admin in the first place).
    """
    while True:
        if not guard.owns_network:
            time.sleep(1)
            continue

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("", TCP_PORT))
        except OSError as e:
            print(f"[!] Failed to bind stream port {TCP_PORT}: {e}")
            time.sleep(2)
            continue
        server.listen(5)
        server.settimeout(1.0)
        print(f"[+] Agent listening on port {TCP_PORT}")

        while guard.owns_network:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=handle_client,
                args=(conn, addr),
                daemon=True
            ).start()

        print(f"[*] Lost network ownership - stopping stream server on port {TCP_PORT}")
        try:
            server.close()
        except Exception:
            pass
        _close_all(clients)


def run_control_server_when_owner():
    """Same ownership-gated lifecycle as run_stream_server_when_owner(),
    for the control/Interact port."""
    while True:
        if not guard.owns_network:
            time.sleep(1)
            continue

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("", CONTROL_PORT))
        except OSError as e:
            print(f"[!] Failed to bind control port {CONTROL_PORT}: {e}")
            time.sleep(2)
            continue
        server.listen(5)
        server.settimeout(1.0)
        print(f"[+] Agent control listening on port {CONTROL_PORT}")

        while guard.owns_network:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=handle_control_client,
                args=(conn, addr),
                daemon=True
            ).start()

        print(f"[*] Lost network ownership - stopping control server on port {CONTROL_PORT}")
        try:
            server.close()
        except Exception:
            pass
        _close_all(control_clients)
        with control_state["lock"]:
            control_state["owner"] = None
            control_state["enabled"] = False


def _release_ownership_on_exit():
    # Best-effort only: a hard logout or crash just kills this process,
    # and Windows frees an abandoned mutex for the next instance on its
    # own (see NetworkOwnership.try_acquire's WAIT_ABANDONED handling).
    # This just makes a clean shutdown (e.g. Ctrl+C) hand ownership over
    # immediately instead of waiting out that path.
    if guard.owns_network:
        guard.ownership.release()
        guard.owns_network = False


if __name__ == "__main__":
    atexit.register(_release_ownership_on_exit)
    guard.start()
    start_discovery()
    threading.Thread(target=run_control_server_when_owner, daemon=True).start()
    threading.Thread(target=control_watchdog, daemon=True).start()
    run_stream_server_when_owner()

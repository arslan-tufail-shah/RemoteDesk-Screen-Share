"""
Makes the agent aware of Windows Fast User Switching and session locking.

Background: when the agent is configured to start for every Windows user,
each logged-in session (Fast User Switching keeps earlier sessions running
in the background) launches its own agent process. Every one of those
processes used to bind the same TCP_PORT/CONTROL_PORT with SO_REUSEADDR,
which on Windows allows more than one process to successfully bind the
same port - so incoming connections could land on an inactive session's
listener, whose screen capture returns black/blank frames (an inactive
session isn't the one being composited to the physical display), and
having several of these capture loops running at once is also what was
driving the machine to crawl/freeze after a few minutes.

This module provides:
  - is_active_console_session(): is *this* process's session the one
    currently attached to the physical console (i.e. the session a user
    would actually see on screen right now)?
  - is_input_desktop_accessible(): is the current session's desktop
    actually reachable right now (False while the workstation is locked)?
  - NetworkOwnership: a cross-session named mutex so that, of all the
    per-session agent processes running on one machine, only the one
    whose session is currently active ever binds the network ports.
  - SessionGuard: polls the above and exposes `owns_network` /
    `is_unlocked`, acquiring or releasing NetworkOwnership automatically
    as sessions switch, with optional callbacks for when ownership
    changes hands.

On non-Windows platforms (there only for local development/testing of the
rest of the codebase - the agent itself is Windows-only), every check
fails open (always "active", always "unlocked", ownership always
uncontested) so the rest of the code behaves normally without a real
Windows session to inspect.
"""

import platform
import re
import threading
import time

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    # use_last_error=True is what makes ctypes.get_last_error() report
    # anything meaningful after a failed call below - without it, Windows
    # API failures here would be entirely silent, which is exactly the
    # kind of thing that makes "one session's agent never shows up" hard
    # to diagnose from the agent's own console output.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    # Explicit signatures instead of relying on ctypes' default (signed
    # c_int) return type, which would misinterpret large DWORD values -
    # notably INVALID_SESSION_ID (0xFFFFFFFF) comparisons, and generally
    # any value ctypes would otherwise guess wrong.
    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = wintypes.DWORD

    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

    kernel32.WTSGetActiveConsoleSessionId.argtypes = []
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD

    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL

    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE

    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL

    DESKTOP_SWITCHDESKTOP = 0x0100
    WAIT_OBJECT_0 = 0x00000000
    WAIT_ABANDONED = 0x00000080
    WAIT_FAILED = 0xFFFFFFFF
    INVALID_SESSION_ID = 0xFFFFFFFF
    ERROR_ACCESS_DENIED = 5
else:
    kernel32 = None
    user32 = None


def _sanitize_for_object_name(name):
    """Windows kernel-object names can't contain backslashes and are
    happiest kept to a simple character set - strip anything else out of
    the hostname before using it to build a mutex name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "unknown-host")


def get_machine_id():
    """A stable identifier for this specific physical machine, independent
    of the hostname and of which user is currently logged in.

    This matters because device identity (built in discovery.py) has to
    be both: (a) stable across a Fast User Switch, which rules out
    including the username, and (b) unique across *different* physical
    machines, which hostname alone does not guarantee - cloned/imaged
    machines that were never individually renamed can easily share the
    same hostname. Using hostname as the sole device identity collapses
    those different machines into a single, flickering entry in the
    admin's device list, each one's broadcast overwriting the last.

    Prefers the Windows installation's MachineGuid (set once at OS
    install/sysprep time, stable across reboots, logins, and network
    changes). Falls back to a MAC-address-derived id if that can't be
    read, and finally to a fixed placeholder if neither works.
    """
    if IS_WINDOWS:
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            winreg.CloseKey(key)
            if value:
                return str(value)
        except Exception as e:
            print(f"[!] session guard: could not read MachineGuid ({e}), falling back to a MAC-based id")
    try:
        import uuid as _uuid
        return format(_uuid.getnode(), "x")
    except Exception:
        return "unknown-machine"


def get_current_session_id():
    """This process's own Windows session ID, or None if it can't be
    determined (always None on non-Windows)."""
    if not IS_WINDOWS:
        return None
    pid = kernel32.GetCurrentProcessId()
    session_id = wintypes.DWORD()
    if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
        return session_id.value
    error_code = ctypes.get_last_error()
    print(f"[!] session guard: ProcessIdToSessionId failed for pid {pid} (error {error_code})")
    return None


def get_active_console_session_id():
    """The session ID currently attached to the physical console (the
    session whose desktop is actually being displayed), or None if that
    can't be determined (always None on non-Windows)."""
    if not IS_WINDOWS:
        return None
    session_id = kernel32.WTSGetActiveConsoleSessionId()
    if session_id == INVALID_SESSION_ID:
        return None
    return session_id


def is_active_console_session():
    """Is *this* process's session the one currently attached to the
    physical console? This is the Fast-User-Switching check: when another
    user switches in, their session becomes the active console session
    and this one no longer is, even though this session (and this agent
    process) keeps running in the background.

    Fails open (returns True) if session info can't be read, and always
    True on non-Windows, so a detection failure degrades to "behave like
    before" rather than silently going dark.
    """
    if not IS_WINDOWS:
        return True
    current = get_current_session_id()
    active = get_active_console_session_id()
    if current is None or active is None:
        return True
    return current == active


def is_input_desktop_accessible():
    """Is the current session's interactive desktop reachable right now?
    Returns False while the workstation is locked (the lock screen runs
    on a separate, secure desktop that OpenInputDesktop can't reach),
    True otherwise. Always True on non-Windows.
    """
    if not IS_WINDOWS:
        return True
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if hdesk:
        user32.CloseDesktop(hdesk)
        return True
    return False


class NetworkOwnership:
    """A named, cross-session mutex used purely as a lock: whichever
    per-session agent process holds it is the one allowed to bind
    TCP_PORT/CONTROL_PORT and broadcast discovery. "Global\\" makes the
    mutex visible across different users' sessions on the same machine,
    which is what lets the Fast-User-Switching handoff work at all -
    without it, each session would only ever see its own local mutex and
    every instance would think it was uncontested.
    """

    def __init__(self, name):
        self.name = name
        self._handle = None
        self._owns = False
        self._handle_failed = False

    def _ensure_handle(self):
        if self._handle is not None or self._handle_failed:
            return self._handle
        if not IS_WINDOWS:
            return None
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            error_code = ctypes.get_last_error()
            print(f"[!] session guard: CreateMutexW('{self.name}') failed (error {error_code})")
            if error_code == ERROR_ACCESS_DENIED:
                # Standard users are normally allowed to create Global\
                # namespace mutexes for plain synchronization like this,
                # but some locked-down environments (restrictive Group
                # Policy, certain Terminal Server configurations) can
                # deny it. Falling back to a session-local mutex at least
                # keeps this instance from silently never starting - it
                # just means it can no longer coordinate with other
                # sessions on the same machine, so more than one session
                # could end up serving at once again. That's a real
                # degradation, which is why it's logged loudly rather
                # than silently swallowed.
                local_name = self.name.split("\\")[-1]
                print(f"[!] session guard: falling back to a session-local mutex ('{local_name}') "
                      "- cross-session coordination will NOT work correctly until this is resolved")
                handle = kernel32.CreateMutexW(None, False, local_name)
                if not handle:
                    print(f"[!] session guard: local mutex fallback also failed (error {ctypes.get_last_error()})")
            self._handle_failed = handle is None
        self._handle = handle
        return self._handle

    def try_acquire(self):
        if self._owns:
            return True
        if not IS_WINDOWS:
            self._owns = True
            return True
        handle = self._ensure_handle()
        if not handle:
            return False
        result = kernel32.WaitForSingleObject(handle, 0)
        # WAIT_ABANDONED means the previous owner's process ended (e.g. a
        # hard logout or crash) without releasing the mutex - Windows
        # still hands it to us, it's just flagging that the prior holder
        # didn't clean up after itself. Treat that the same as a normal
        # successful acquire rather than leaving network ownership
        # permanently stuck with a process that no longer exists.
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self._owns = True
            return True
        if result == WAIT_FAILED:
            print(f"[!] session guard: WaitForSingleObject failed (error {ctypes.get_last_error()})")
        return False

    def release(self):
        if self._owns and IS_WINDOWS and self._handle:
            try:
                kernel32.ReleaseMutex(self._handle)
            except Exception:
                pass
        self._owns = False

    def owns(self):
        return self._owns


class SessionGuard:
    """Polls Windows session state and arbitrates network ownership
    between however many per-session agent processes are currently
    running on this machine.

    `owns_network` - True only for the single instance (across every
    logged-in session on this PC) that should bind the network ports and
    broadcast discovery right now. Flips to False the moment this
    session stops being the active console session (Fast User
    Switching away), and back to True if the user switches back.

    `is_unlocked` - True unless the *currently active* session's
    workstation is locked. This is independent of ownership: a locked
    session keeps network ownership (the agent and the admin's
    connection both stay alive) but should pause actual frame capture
    until unlocked, rather than tearing the connection down.
    """

    def __init__(self, mutex_name, poll_interval=1.0, heartbeat_every=15):
        self.ownership = NetworkOwnership(mutex_name)
        self.poll_interval = poll_interval
        # How often (in ticks) to log current state even when nothing
        # changed. Transition-only logging looks identical whether the
        # guard is working correctly and simply staying inactive, or
        # whether its polling thread has silently stopped altogether -
        # a periodic heartbeat line is what tells those two apart from
        # the agent's own console output.
        self.heartbeat_every = heartbeat_every
        self._tick_count = 0
        self.owns_network = False
        self.is_unlocked = True
        self.on_ownership_gained = None
        self.on_ownership_lost = None

    def start(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self):
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[!] session guard error: {e}")
            time.sleep(self.poll_interval)

    def _tick(self):
        active = is_active_console_session()
        unlocked = is_input_desktop_accessible()
        self.is_unlocked = unlocked

        self._tick_count += 1
        if self._tick_count % self.heartbeat_every == 0:
            print(
                f"[*] Session guard heartbeat: session={get_current_session_id()} "
                f"active_console={get_active_console_session_id()} "
                f"is_active={active} is_unlocked={unlocked} owns_network={self.owns_network}"
            )

        if active and not self.owns_network:
            if self.ownership.try_acquire():
                self.owns_network = True
                print("[*] Session guard: this session is now active - acquired network ownership")
                if self.on_ownership_gained:
                    try:
                        self.on_ownership_gained()
                    except Exception as e:
                        print(f"[!] on_ownership_gained callback error: {e}")
        elif not active and self.owns_network:
            self.ownership.release()
            self.owns_network = False
            print("[*] Session guard: another session became active - released network ownership")
            if self.on_ownership_lost:
                try:
                    self.on_ownership_lost()
                except Exception as e:
                    print(f"[!] on_ownership_lost callback error: {e}")


_hostname = platform.node() or "unknown-host"
_mutex_name = f"Global\\RemoteDeskAgent_NetOwner_{_sanitize_for_object_name(_hostname)}"

# Shared singleton - both agent.py and discovery.py import this so they
# always agree on the current ownership/lock state.
guard = SessionGuard(_mutex_name)

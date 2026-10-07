# RemoteDesk

A lightweight LAN remote-desktop and screen-monitoring tool for Windows, built with Python and PyQt6. One **Admin Console** discovers and connects to many **Agents** running on other PCs on the same local network — live screen viewing, optional keyboard/mouse control, session recording, and basic device policy, with no internet dependency.

> This project is under active development. Read the [Security Considerations](#security-considerations) section before deploying it on any network you care about.

## Features

- **Auto-discovery** — agents broadcast their presence over UDP; the admin console finds them automatically, no manual IP entry required.
- **Live screen viewing** — JPEG-over-TCP streaming with configurable quality/resolution/frame rate.
- **Remote keyboard & mouse ("Interact")** — opt-in, per-session control that any admin can enable from the viewer.
- **Manager takeover** — a `manager`-role admin can take over a session another admin is already viewing, with the displaced admin notified.
- **USB port policy** — managers can Allow/Block USB storage devices on an agent from the device list, independent of remote-control access.
- **Session recording** — record a live view to `.mp4`, then browse, rename, trim, and play back recordings from a built-in gallery.
- **Role-based accounts** — `manager` and `supervisor` roles, local credential store protected with Windows DPAPI.
- **Windows session awareness** — correctly handles Fast User Switching and workstation lock/unlock so only the active, unlocked desktop is ever captured (see [How session awareness works](#how-session-awareness-works)).
- **Resilient networking** — automatic reconnect with backoff, dead-connection detection, and clean recovery from locked/backgrounded sessions.

## Architecture

```
┌─────────────────┐        UDP broadcast (discovery)        ┌─────────────────┐
│                 │ ───────────────────────────────────────▶│                 │
│  Admin Console  │        TCP  (video stream)               │      Agent      │
│   (any OS*)     │ ◀───────────────────────────────────────▶│  (Windows PC)   │
│                 │        TCP  (control: input + policy)    │                 │
└─────────────────┘ ◀───────────────────────────────────────▶└─────────────────┘
```

| Component | Role |
|---|---|
| `agent/` | Runs on the PC being monitored/controlled. Captures the screen, serves the video and control TCP sockets, and broadcasts discovery packets. **Windows only.** |
| `admin/` | The console used to discover, view, and manage agents. Built with PyQt6. |

Default ports (see [Configuration](#configuration)):

| Port | Purpose |
|---|---|
| UDP 9999 | Agent discovery broadcast |
| TCP 5050 | Video stream |
| TCP 5051 | Control channel (keyboard/mouse, lock, USB policy) |

\* The admin console itself is plain PyQt6/OpenCV and runs cross-platform, but the credential store uses Windows DPAPI for encryption, so Windows is the only officially supported platform for it today.

## Requirements

- Python 3.9+ (tested with recent CPython 3.x)
- **Agent**: Windows 10/11
- **Admin console**: Windows 10/11 (see note above)

Python packages:

```
pip install PyQt6 opencv-python numpy pillow
```

- `PyQt6` — GUI, video widget, and media playback for recordings
- `opencv-python` — video encoding/decoding and recording to `.mp4`
- `numpy` — frame buffer handling
- `pillow` — screen capture (`ImageGrab`), agent only

## Getting started

### 1. Run the admin console

```bash
cd admin
python main.py
```

On first launch, you'll be prompted to create the initial `manager` account. After that, sign in normally — the console will discover agents on the LAN automatically and list them as they come online.

### 2. Run the agent

```bash
cd agent
python agent.py
```

Run this on the PC you want to monitor/control. For real deployments, configure it to launch automatically at login (Startup folder, Scheduled Task, or similar) — see [How session awareness works](#how-session-awareness-works) for why starting it per-user is the correct approach even on shared/multi-user PCs.

The agent needs **no interaction** to run — it starts capturing and broadcasting silently once launched. It does require **Administrator privileges** if you want the USB-blocking feature to work (it writes to `HKLM` to toggle the USB mass-storage driver).

### 3. Connect

From the admin console, double-click any discovered device to open a live viewer. Use the **Interact** button to request keyboard/mouse control, and **Start Recording** to save the session.

## Building standalone executables

```bash
pip install pyinstaller

cd agent
pyinstaller --onefile --noconsole --name RemoteDesk-Agent agent.py

cd ../admin
pyinstaller --onefile --noconsole --name RemoteDesk-Admin main.py
```

Resulting `.exe` files are in each folder's `dist/`. If PyQt6 plugins aren't found at runtime, rebuild with:

```bash
pyinstaller --onefile --noconsole --collect-all=PyQt6 --name RemoteDesk-Admin main.py
```

## Configuration

Settings live in `agent/config.py` and `admin/config.py` (both must match for an agent and admin console to talk to each other):

| Setting | Description |
|---|---|
| `DISCOVERY_PORT` | UDP port for presence broadcasts |
| `TCP_PORT` | Video stream port |
| `CONTROL_PORT` | Control channel port |
| `PIN_CODE` | Shared secret both sides must present before any connection is accepted |
| `FRAME_QUALITY` | JPEG quality, 1–100 (agent only) |
| `FPS` | Capture/send rate (agent only) |
| `MAX_WIDTH` / `MAX_HEIGHT` | Downscale captured frames above this size; `0` disables scaling (agent only, also overridable via environment variables of the same name) |

**Change `PIN_CODE` from the default before deploying anywhere.** See below.

## How session awareness works

Windows allows multiple users to be logged in at once via Fast User Switching, each with their own desktop session — only one of which is actually visible on the physical display at a time. Because of this, the agent is designed to start **once per user session** rather than as a single machine-wide service, and the instances coordinate so that only one of them is ever actually active:

- Each agent instance checks whether its own session is the one currently attached to the console (`WTSGetActiveConsoleSessionId`) and whether the workstation is unlocked (`OpenInputDesktop`).
- A named, cross-session mutex ensures only the instance belonging to the **active** session binds the network ports and broadcasts discovery — inactive sessions stay completely silent on the network.
- Switching users hands this "network ownership" over automatically: the previously-active instance releases it and closes its connections; the newly-active instance's broadcast lets the admin console reconnect within a few seconds, picking up the new session's screen.
- Locking the workstation **pauses** capture in place (the connection stays open) rather than tearing anything down, so unlocking resumes instantly. The viewer shows a clear "session locked" placeholder in the meantime.
- Device identity is derived from the machine's stable Windows installation ID, not the hostname or username, so the same physical PC is recognized as one consistent entry across switches — even for cloned/imaged machines that happen to share a hostname.

This is all handled by `agent/session_guard.py`.

## Security considerations

This project prioritizes LAN convenience over hardened security. Before using it on any network beyond a personal/trusted lab:

- **Shared PIN, not per-user auth.** The agent-side PIN in `config.py` is a single shared secret, stored in plaintext in source. Anyone who can read the source or sniff LAN traffic during the handshake has it. Change the default, and don't rely on it as your only line of defense.
- **No transport encryption.** Video, control commands, and the PIN handshake are sent unencrypted over TCP. Don't run this across untrusted networks or the open internet.
- **Admin role trust model.** Role checks (`manager` vs `supervisor`) for sensitive actions (USB policy, session takeover) are enforced on the wire by the agent, but a client asserting its own role during the handshake is inherently only as trustworthy as your network. Treat this as LAN-only, behind your own firewall.
- **USB blocking requires elevation** and toggles a machine-wide registry setting (`HKLM\...\USBSTOR`) — understand what it does before enabling it in production.
- **`admin/users.json` in this repository is sample/test data** (hashed, not plaintext, but still real-looking usernames) left over from development — the app's actual runtime credential store lives outside the repo, DPAPI-encrypted under `%LOCALAPPDATA%\RemoteDesk`. Remove or replace `admin/users.json` before publishing your own fork if you don't want it included.

## Known limitations

- Agent is Windows-only (screen capture, session APIs, DPAPI-related features).
- Discovery relies on UDP broadcast, so the admin console and agents must be on the same broadcast domain (no VLAN/router-spanning discovery out of the box).
- No built-in TLS — see [Security Considerations](#security-considerations).
- Session-awareness logic depends on Windows Terminal Services APIs; it has fallback behavior for non-Windows development but isn't meaningful outside Windows.

## Project structure

```
RemoteDesk/
├── admin/
│   ├── main.py            # Admin console entry point (PyQt6 UI)
│   ├── viewer.py           # Live viewer window: stream, control, recording
│   ├── discovery.py        # UDP discovery listener
│   ├── auth.py              # DPAPI-backed local account store
│   ├── user_management.py  # Manager-only user administration dialog
│   ├── recordings.py        # Recordings gallery / playback
│   └── config.py
└── agent/
    ├── agent.py             # Agent entry point: capture, streaming, control
    ├── discovery.py         # UDP discovery broadcaster
    ├── session_guard.py     # Windows session/lock awareness, ownership arbitration
    └── config.py
```

## Contributing

Issues and pull requests are welcome. Please avoid committing real credentials, PINs, or network details in examples.

## License

MIT © 2026 Arslan Tufail Shah, Axorya — see [LICENSE](LICENSE) for the full text. Free to use, modify, and sell, including commercially, as long as the copyright notice is kept intact.

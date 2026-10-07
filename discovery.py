import socket
import json
import time
import threading
import platform
import getpass
import subprocess
import re
from config import DISCOVERY_PORT, TCP_PORT, CONTROL_PORT
from session_guard import guard, get_machine_id

# Whether USB ports are currently blocked on this agent (a DLP-style
# policy, unrelated to keyboard/mouse remote control - that's the
# separate "Interact" feature). Read by broadcast_presence() below so
# every admin console's device list picks up the current Allow/Block
# status automatically, and written by agent.py when an admin toggles it.
usb_policy = {"blocked": False, "lock": threading.Lock()}

def get_broadcast_addresses():
    """Get all broadcast addresses for active network interfaces using ipconfig."""
    addresses = set()
    
    try:
        # Use ipconfig to get all network interfaces on Windows
        result = subprocess.run(['ipconfig'], capture_output=True, text=True)
        lines = result.stdout.split('\n')
        
        current_ip = None
        current_subnet = None
        
        for line in lines:
            # Look for "IPv4 Address"
            if 'IPv4 Address' in line and ':' in line:
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    current_ip = match.group(1)
                    if current_ip != '127.0.0.1':
                        # Add all-broadcast address
                        parts = current_ip.split('.')
                        broadcast = '.'.join(parts[:3]) + '.255'
                        addresses.add(broadcast)
                        print(f"[+] Found interface IP: {current_ip} -> broadcast: {broadcast}")
            
    except Exception as e:
        print(f"[!] error enumerating interfaces with ipconfig: {e}")
    
    # Always include fallback broadcast addresses
    addresses.add('255.255.255.255')  # standard broadcast
    addresses.add('<broadcast>')  # special broadcast address
    
    return list(addresses)

def broadcast_presence():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except:
        pass

    hostname = platform.node()
    # A short, stable, per-machine suffix - see get_machine_id() for why
    # hostname alone isn't a safe device identity (cloned/imaged machines
    # can share a hostname, which would otherwise make two different
    # physical PCs collide into a single, flickering entry in the admin's
    # device list). Computed once here rather than every broadcast tick,
    # since it never changes for the lifetime of this process.
    machine_suffix = get_machine_id().replace("-", "")[-8:]
    broadcast_addrs = get_broadcast_addresses()
    print(f"[+] Agent broadcasting on host: {hostname} (machine id suffix: {machine_suffix})")
    print(f"[+] Broadcast addresses: {', '.join(broadcast_addrs)}")

    while True:
        # Only the single per-session instance that currently holds
        # network ownership (see session_guard.py) should be visible on
        # the network at all. An agent running in a session that Fast
        # User Switching has pushed into the background stays silent
        # here instead of also announcing itself - this is what makes
        # "one PC" look like one discoverable device no matter which of
        # its logged-in sessions happens to be active right now.
        if not guard.owns_network:
            time.sleep(1)
            continue

        username = getpass.getuser()
        # Device identity is per-machine, not per-session: the same
        # device_id must keep being broadcast across a Fast User Switch
        # handoff so the admin console's existing device entry (and any
        # open viewer pointed at it) just reconnects to whichever session
        # is now active, rather than looking like a different device. The
        # machine_suffix is what keeps two different physical PCs from
        # colliding into one entry if they happen to share a hostname.
        dev_id = f"{hostname}-{machine_suffix}"
        with usb_policy["lock"]:
            usb_blocked = usb_policy["blocked"]

        message = json.dumps({
            "device_id": dev_id,
            "hostname": hostname,
            "username": username,
            "port": TCP_PORT,
            "control_port": CONTROL_PORT,
            "usb_blocked": usb_blocked
        })
        
        # Send to all local network broadcast addresses
        for broadcast_addr in broadcast_addrs:
            try:
                if broadcast_addr == '<broadcast>':
                    continue  # skip placeholder
                sock.sendto(message.encode(), (broadcast_addr, DISCOVERY_PORT))
            except Exception as e:
                print(f"[!] broadcast error to {broadcast_addr}: {e}")
        
        print(f"[*] Broadcasting presence: {dev_id} (active user: {username})")
        time.sleep(3)


def start_discovery():
    threading.Thread(target=broadcast_presence, daemon=True).start()
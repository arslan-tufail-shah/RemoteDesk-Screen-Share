import socket
import json
import threading
import time
from config import DISCOVERY_PORT

devices = {}
lock = threading.Lock()

def listen(callback):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
        print(f"[+] Admin listening for agents on port {DISCOVERY_PORT}")
    except Exception as e:
        print(f"[!] Failed to bind to port {DISCOVERY_PORT}: {e}")
        return

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            try:
                device = json.loads(data.decode())
                device["ip"] = addr[0]
                device["last_seen"] = time.time()

                with lock:
                    devices[device["device_id"]] = device

                print(f"[+] Discovered: {device.get('username')}@{device.get('hostname')} ({addr[0]}:{device.get('port')})")
                callback()
            except json.JSONDecodeError as e:
                print(f"[!] Invalid JSON from {addr}: {e}")
            except Exception as e:
                print(f"[!] Error processing discovery packet: {e}")
        except Exception as e:
            print(f"[!] Socket receive error: {e}")
            time.sleep(1)

def get_devices():
    now = time.time()
    with lock:
        return [
            d for d in devices.values()
            if now - d["last_seen"] < 10
        ]
import os

# network settings
TCP_PORT = 5050
DISCOVERY_PORT = 9999
CONTROL_PORT = 5051
PIN_CODE = "1234"

# encoding quality (1-100); higher gives better image but uses more bandwidth
FRAME_QUALITY = 70
# frames per second to capture/send
FPS = 20

# maximum dimensions for transmission; larger screenshots are scaled down
# set both values to 0 (or leave unset via environment) to disable resizing and
# transmit full‑resolution frames from the capture device.  You can also set
# these values in the environment before starting the agent, e.g.:
#
#   set MAX_WIDTH=1920
#   set MAX_HEIGHT=1080
#
# which makes it easy to tweak resolution without changing the source.

# default fallbacks (0 disables scaling)
try:
    MAX_WIDTH = int(os.getenv("MAX_WIDTH", "0"))
except ValueError:
    MAX_WIDTH = 0

try:
    MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "0"))
except ValueError:
    MAX_HEIGHT = 0

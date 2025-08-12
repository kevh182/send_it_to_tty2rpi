import time
import win32gui
import win32process
import configparser
import os
import paramiko
import io
import logging
import psutil

# =====================================================================================
# Logging Configuration
# =====================================================================================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# =====================================================================================
# Constants
# =====================================================================================
CONFIG_PATH = "tty2rpi_sender.ini"
CMDCOR_DATA = "CMDCOR§PARAM§"
CHECK_INTERVAL = 0.5

# =====================================================================================
# Process -> Emulator Mapping
# =====================================================================================
PROCESS_TO_EMU = {
    "mame.exe": "mame",
    "flycast.exe": "flycast",
    "duckstation-qt-x64-releaseltcg.exe": "duckstation",
    "teknoparrotui.exe": "teknoparrot",
    "pcsx2.exe": "pcsx2",
    "pcsx2-qt.exe": "pcsx2",
    "pcsx2-qtx64-avx2.exe": "pcsx2",
    "dolphin.exe": "dolphin",
    "dolphinqt.exe": "dolphin",
    "dolphinqt2.exe": "dolphin",
}

# =====================================================================================
# Transient (ignore) titles per emulator
# =====================================================================================
IGNORE_SUBSTRINGS = {
    "duckstation": [
        "duckstation-qt-x64-releaseltcg",
        "select disc image",
        "automatic updater",
        "about duckstation",
        "about qt",
        "padtest",
        "memory scanner",
        "download covers",
        "memory card editor",
        "iso browser",
        "select search directory",
        "duckstation settings",
        "error",
        "select save state file",
        "duckstation controller presets",
        "select background image",
    ],
    "pcsx2": [
        "pcsx2-qt",
        "ps2 bios (usa)",
        "select iso image",
        "open iso",
        "about pcsx2",
        "about qt",
        "automatic updater",
        "select location to save block dump",
        "show advanced settings",
        "select search directory",
        "select save state file",
    ],
    "dolphin": [
        "dolphin-emu",
        "confirm",
        "open file",
    ],
}

def is_transient_title(emulator: str, low_title: str) -> bool:
    """
    Returns True if the given lowercase window title matches any transient/ignored
    substrings for the specified emulator.
    """
    for needle in IGNORE_SUBSTRINGS.get(emulator, []):
        if needle in low_title:
            return True
    return False

# =====================================================================================
# Load Settings from INI
# =====================================================================================
config = configparser.ConfigParser()
if not os.path.exists(CONFIG_PATH):
    logging.error(f"INI file not found: {CONFIG_PATH}")
    raise SystemExit(1)

config.read(CONFIG_PATH)

try:
    remote_ip = config['tty2rpi']['remote_ip']
    username = config['tty2rpi']['username']
    password = config['tty2rpi']['password']
except Exception as e:
    logging.error(f"Error reading INI file: {e}")
    raise SystemExit(1)

# =====================================================================================
# Runtime State
# =====================================================================================
tracked_windows = {}
last_sent_titles = {}
hwnd_emulator = {}
last_sent_payload = {}
last_global_payload = None

# =====================================================================================
# Helper: get_hwnd_process_name
# =====================================================================================
def get_hwnd_process_name(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""

# =====================================================================================
# Window Discovery
# =====================================================================================
def find_and_add_matching_windows():
    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            proc_name = get_hwnd_process_name(hwnd)
            emu = PROCESS_TO_EMU.get(proc_name)
            if not emu:
                return
            if hwnd not in tracked_windows:
                tracked_windows[hwnd] = title
                hwnd_emulator[hwnd] = emu
                logging.info(f"[NEW WINDOW] HWND={hwnd} | emu={emu} | Title='{title}'")
                parse_and_send(hwnd, title)
                last_sent_titles[hwnd] = title
        except Exception as e:
            logging.debug(f"[ENUM ERR] hwnd={hwnd}: {e}")

    win32gui.EnumWindows(callback, None)

# =====================================================================================
# SSH Sender
# =====================================================================================
def update_tty2rpi_marquee(marquee_data):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(remote_ip, username=username, password=password, timeout=5.0)
        try:
            transport = ssh.get_transport()
            if transport:
                transport.set_keepalive(15)
        except Exception:
            pass
        sftp = ssh.open_sftp()
        file_like = io.StringIO(marquee_data)
        with sftp.file("/dev/shm/tty2rpi.socket", 'w') as remote_file:
            remote_file.write(file_like.read())
            remote_file.flush()
        logging.info(f"[SEND] -> tty2rpi.socket: {marquee_data}")
        sftp.close()
        ssh.close()
    except Exception as e:
        logging.error(f"[ERROR][SSH] {e}")

# =====================================================================================
# Parser & Dispatcher
# =====================================================================================
def parse_and_send(hwnd, new_title):
    emulator = hwnd_emulator.get(hwnd)
    if not emulator:
        logging.debug(f"[SKIP] Unknown emulator for HWND {hwnd} | '{new_title}'")
        return
    logging.info(f"[INFO] HWND: {hwnd} | Emu: {emulator} | Title bar data: {new_title}")
    loaded_rom = None
    low = new_title.lower().strip()

    if emulator == "mame":
        start = new_title.find("[")
        end = new_title.find("]", start)
        if start != -1 and end != -1:
            loaded_rom = new_title[start + 1:end]
            if loaded_rom == "___empty":
                loaded_rom = "MAME-MENU"

    elif emulator == "flycast":
        if low.startswith("flycast"):
            dash = new_title.find("- ")
            if dash != -1:
                loaded_rom = new_title[dash + 2:].strip()
            else:
                loaded_rom = "DCEMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "duckstation":
        if is_transient_title(emulator, low):
            logging.info(f"[SKIP] DuckStation transient window: {new_title}")
            return
        if low.startswith("duckstation"):
            loaded_rom = "PS1EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "teknoparrot":
        if low.startswith("teknoparrot"):
            loaded_rom = "TPEMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "pcsx2":
        if is_transient_title(emulator, low):
            logging.info(f"[SKIP] PCSX2 transient window: {new_title}")
            return
        if low.startswith("pcsx2"):
            loaded_rom = "PS2EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "dolphin":
        if is_transient_title(emulator, low):
            logging.info(f"[SKIP] Dolphin transient window: {new_title}")
            return
        if low.startswith("dolphin"):
            loaded_rom = "DOLPHIN-MENU"
        else:
            loaded_rom = new_title.strip()

    if not loaded_rom:
        logging.debug(f"[SKIP] Parsed empty from '{new_title}' ({emulator})")
        return

    payload = CMDCOR_DATA + loaded_rom
    global last_global_payload
    if last_sent_payload.get(hwnd) == payload or last_global_payload == payload:
        logging.debug(f"[SKIP DUP] HWND={hwnd} payload unchanged globally: {payload}")
        return

    update_tty2rpi_marquee(payload)
    last_sent_payload[hwnd] = payload
    last_global_payload = payload
    logging.info(f"[INFO] {emulator} title: {loaded_rom}")

# =====================================================================================
# Main Loop
# =====================================================================================
def main_loop():
    logging.info("tty2rpi sender...")
    logging.info("Press Ctrl+C to stop.\n")
    try:
        while True:
            find_and_add_matching_windows()
            for hwnd in list(tracked_windows.keys()):
                if not win32gui.IsWindow(hwnd):
                    old_title = tracked_windows.get(hwnd, "")
                    logging.info(f"[CLOSE] HWND={hwnd} | '{old_title}'")
                    tracked_windows.pop(hwnd, None)
                    last_sent_titles.pop(hwnd, None)
                    hwnd_emulator.pop(hwnd, None)
                    last_sent_payload.pop(hwnd, None)
                    continue
                current_title = win32gui.GetWindowText(hwnd)
                prev = tracked_windows.get(hwnd)
                if current_title == prev:
                    continue
                logging.info(f"[TITLE CHANGED] HWND={hwnd} | '{prev}' -> '{current_title}'")
                tracked_windows[hwnd] = current_title
                parse_and_send(hwnd, current_title)
                last_sent_titles[hwnd] = current_title
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Stopped monitoring.")

# =====================================================================================
# Entry Point
# =====================================================================================
if __name__ == "__main__":
    main_loop()

import time
import win32gui
import win32process
import configparser
import os
import paramiko
import io
import logging
import psutil

# =========================
# Logging Configuration
# =========================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# =========================
# Constants
# =========================
CONFIG_PATH = "tty2rpi_sender.ini"   # INI file for SSH only
CMDCOR_DATA = "CMDCOR§PARAM§"        # Prefix tty2rpi expects
CHECK_INTERVAL = 0.5                 # How often to scan for window changes (seconds)

# Known emulator process names -> canonical emulator key (use LOWERCASE keys)
PROCESS_TO_EMU = {
    "mame.exe": "mame",
    "flycast.exe": "flycast",
    "duckstation-qt-x64-releaseltcg.exe": "duckstation",
    "teknoparrotui.exe": "teknoparrot",
    # --- PCSX2 common binaries ---
    "pcsx2.exe": "pcsx2",
    "pcsx2-qt.exe": "pcsx2",
    "pcsx2-qtx64-avx2.exe": "pcsx2",
    # --- Dolphin common binaries ---
    "dolphin.exe": "dolphin",
    "dolphinqt.exe": "dolphin",
    "dolphinqt2.exe": "dolphin",
}

# =========================
# Load Settings from INI
# =========================
config = configparser.ConfigParser()
if not os.path.exists(CONFIG_PATH):
    logging.error(f"INI file not found: {CONFIG_PATH}")
    raise SystemExit(1)

config.read(CONFIG_PATH)

try:
    # SSH connection details
    remote_ip = config['tty2rpi']['remote_ip']
    username = config['tty2rpi']['username']
    password = config['tty2rpi']['password']
except Exception as e:
    logging.error(f"Error reading INI file: {e}")
    raise SystemExit(1)

# =========================
# Data Structures for Tracking Windows
# =========================
tracked_windows = {}      # {hwnd: last_seen_title}
last_sent_titles = {}     # {hwnd: last_title_we_triggered_on}
hwnd_emulator = {}        # {hwnd: "mame"|"flycast"|"duckstation"|"teknoparrot"|"pcsx2"|"dolphin"}
last_sent_payload = {}    # {hwnd: last_payload_string_sent}  # Debounce by payload

# =========================
# Helpers
# =========================
def get_hwnd_process_name(hwnd):
    """
    Returns lowercase process name for a given window handle, or '' on failure.
    """
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return ""
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""

# =========================
# Window Scanning Function
# =========================
def find_and_add_matching_windows():
    """
    Enumerates all visible top-level windows and adds those that
    belong to a known emulator process (per PROCESS_TO_EMU).
    """
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
                # Send initial state immediately, and record it to avoid a second send on first loop
                parse_and_send(hwnd, title)
                last_sent_titles[hwnd] = title
        except Exception as e:
            logging.debug(f"[ENUM ERR] hwnd={hwnd}: {e}")

    win32gui.EnumWindows(callback, None)

# =========================
# Send Data to tty2rpi via SSH
# =========================
def update_tty2rpi_marquee(marquee_data):
    """
    Opens an SSH connection and writes the given marquee_data
    to /dev/shm/tty2rpi.socket on the Raspberry Pi.
    """
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(remote_ip, username=username, password=password, timeout=5.0)
        # Keepalive for flaky networks
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

# =========================
# Emulator Title Parsing & Sending
# =========================
def parse_and_send(hwnd, new_title):
    """
    Uses the remembered emulator for this hwnd, logs the title,
    extracts the game/menu label, and sends it to the Raspberry Pi via SSH.
    Debounced by payload so we don't send duplicate CMDCOR strings.
    """
    emulator = hwnd_emulator.get(hwnd)
    if not emulator:
        logging.debug(f"[SKIP] Unknown emulator for HWND {hwnd} | '{new_title}'")
        return

    logging.info(f"[INFO] HWND: {hwnd} | Emu: {emulator} | Title bar data: {new_title}")

    loaded_rom = None
    low = new_title.lower().strip()

    if emulator == "mame":
        # MAME window title typically contains [rom_name]
        start = new_title.find("[")
        end = new_title.find("]", start)
        if start != -1 and end != -1:
            loaded_rom = new_title[start + 1:end]
            if loaded_rom == "___empty":
                loaded_rom = "MAME-MENU"

    elif emulator == "flycast":
        # MENU: "Flycast" (or with version); GAME: "Flycast - Game Name" or sometimes just the game name
        if low.startswith("flycast"):
            dash = new_title.find("- ")
            if dash != -1:
                loaded_rom = new_title[dash + 2:].strip()
            else:
                loaded_rom = "DCEMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "duckstation":
        # Ignore transient/launcher/file dialog windows
        if ("duckstation-qt-x64-releaseltcg" in low) or ("select disc image" in low):
            logging.info(f"[SKIP] DuckStation transient window: {new_title}")
            return
        # MENU: "DuckStation" or "DuckStation <version...>"
        # GAME: title is just the game name (e.g., "1Xtreme (USA)")
        if low.startswith("duckstation"):
            loaded_rom = "PS1EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "teknoparrot":
        # MENU: "TeknoParrot" (or with version); GAME: often just game name
        if low.startswith("teknoparrot"):
            loaded_rom = "TPEMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "pcsx2":
        # Ignore transient/launcher/file dialog windows for PCSX2 (more forgiving variants)
        if ("pcsx2-qt" in low) or ("select iso image" in low) or ("open iso" in low):
            logging.info(f"[SKIP] PCSX2 transient window: {new_title}")
            return
        # MENU: "PCSX2 ..." (menu or version info); GAME: window title becomes just the game name
        if low.startswith("pcsx2"):
            loaded_rom = "PS2EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    elif emulator == "dolphin":
        # Ignore transient/launcher/file dialog windows for Dolphin (more forgiving)
        if ("dolphin-emu" in low) or ("confirm" in low) or ("open" in low and "file" in low):
            logging.info(f"[SKIP] Dolphin transient window: {new_title}")
            return
        # MENU: "Dolphin ..." (menu or version info); GAME: window title becomes just the game name
        if low.startswith("dolphin"):
            loaded_rom = "DOLPHIN-MENU"
        else:
            loaded_rom = new_title.strip()

    # If we couldn't parse anything meaningful, don't send
    if not loaded_rom:
        logging.debug(f"[SKIP] Parsed empty from '{new_title}' ({emulator})")
        return

    # Build payload once and debounce by payload
    payload = CMDCOR_DATA + loaded_rom
    if last_sent_payload.get(hwnd) == payload:
        logging.debug(f"[SKIP DUP] HWND={hwnd} payload unchanged: {payload}")
        return

    update_tty2rpi_marquee(payload)
    last_sent_payload[hwnd] = payload
    logging.info(f"[INFO] {emulator} title: {loaded_rom}")

# =========================
# Main Real-Time Loop
# =========================
def main_loop():
    """
    Continuously scans for emulator windows and sends updated
    titles to the Raspberry Pi in real time when they change.
    """
    logging.info("tty2rpi sender...")
    logging.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            # Discover any new matching windows
            find_and_add_matching_windows()

            # Check existing tracked windows for changes
            for hwnd in list(tracked_windows.keys()):
                if not win32gui.IsWindow(hwnd):
                    # Window closed: cleanup
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
                    # No change — skip quickly
                    continue

                logging.info(f"[TITLE CHANGED] HWND={hwnd} | '{prev}' -> '{current_title}'")
                tracked_windows[hwnd] = current_title
                parse_and_send(hwnd, current_title)
                last_sent_titles[hwnd] = current_title

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logging.info("Stopped monitoring.")

# =========================
# Script Entry Point
# =========================
if __name__ == "__main__":
    main_loop()
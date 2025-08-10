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
# -------------------------------------------------------------------------------------
# We configure Python's built-in logging module to print time-stamped log lines.
# Level=INFO means you'll see general operational messages (startup, sends, changes).
# If you need to debug tricky situations, bump this to logging.DEBUG to see more.
# =====================================================================================
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# =====================================================================================
# Constants
# -------------------------------------------------------------------------------------
# CONFIG_PATH     : INI file storing SSH credentials used to reach the Raspberry Pi.
# CMDCOR_DATA     : The prefix "magic word" expected by tty2rpi on the Pi side.
# CHECK_INTERVAL  : How often (in seconds) we rescan windows and detect title changes.
# =====================================================================================
CONFIG_PATH = "tty2rpi_sender.ini"   # INI file for SSH only
CMDCOR_DATA = "CMDCOR§PARAM§"        # Prefix tty2rpi expects
CHECK_INTERVAL = 0.5                 # Polling interval in seconds (0.5s ~= "real-time")

# =====================================================================================
# Process -> Emulator Mapping
# -------------------------------------------------------------------------------------
# PROCESS_TO_EMU maps lowercased executable names to a canonical emulator key we use
# elsewhere in the script. This is *process-only* detection: we don't scan window titles
# to discover emulators—only the owning process name. If you add new emulators or have a
# different EXE name, extend this dict.
# =====================================================================================
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

# =====================================================================================
# Load Settings from INI
# -------------------------------------------------------------------------------------
# We load SSH connection details from CONFIG_PATH. If the file is missing or malformed,
# we exit with an error. Expected format:
#
#   [tty2rpi]
#   remote_ip = 192.168.x.x
#   username = pi
#   password = raspberry
# =====================================================================================
config = configparser.ConfigParser()
if not os.path.exists(CONFIG_PATH):
    logging.error(f"INI file not found: {CONFIG_PATH}")
    raise SystemExit(1)

config.read(CONFIG_PATH)

try:
    remote_ip = config['tty2rpi']['remote_ip']  # IP of the Raspberry Pi
    username = config['tty2rpi']['username']    # SSH username
    password = config['tty2rpi']['password']    # SSH password
except Exception as e:
    logging.error(f"Error reading INI file: {e}")
    raise SystemExit(1)

# =====================================================================================
# Runtime State (Per-Window Tracking)
# -------------------------------------------------------------------------------------
# tracked_windows     : maps HWND -> last seen window title (string).
# last_sent_titles    : maps HWND -> last title we triggered on (string), useful for logs.
# hwnd_emulator       : maps HWND -> emulator key ("mame", "duckstation", etc.) so we
#                       don't depend on title contents on subsequent changes.
# last_sent_payload   : maps HWND -> last CMDCOR payload string sent (per-window debounce).
# last_global_payload : last payload we sent across ALL windows (global debounce).
#                       Prevents duplicates when a different HWND produces the same marquee,
#                       e.g., opening DuckStation Settings after we already sent PS1EMU-MENU.
# =====================================================================================
tracked_windows = {}
last_sent_titles = {}
hwnd_emulator = {}
last_sent_payload = {}
last_global_payload = None  # <-- NEW: cross-window/global payload debounce

# =====================================================================================
# Helper: get_hwnd_process_name(hwnd)
# -------------------------------------------------------------------------------------
# Given a window handle (HWND), find its owning process ID, look up the process with
# psutil, and return the *lowercased* executable name. Returns empty string on error.
# =====================================================================================
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
        # Any failure (no pid, process ended, access denied) -> empty string
        return ""

# =====================================================================================
# Discovery: find_and_add_matching_windows()
# -------------------------------------------------------------------------------------
# We enumerate *all* visible top-level windows (EnumWindows). For each window:
#   - Ignore if not visible or has empty title.
#   - Resolve its owning process name.
#   - If the process name is in PROCESS_TO_EMU, we start tracking it:
#       * Record the title and emulator mapping.
#       * Immediately call parse_and_send() once to send initial state.
#       * Cache last_sent_titles to avoid double-sends on the first loop.
# The whole callback is wrapped in try/except so one bad window won't break the sweep.
# =====================================================================================
def find_and_add_matching_windows():
    """
    Enumerates all visible top-level windows and adds those that
    belong to a known emulator process (per PROCESS_TO_EMU).
    """
    def callback(hwnd, _):
        try:
            # Skip non-visible windows (minimized still returns True if the window is visible)
            if not win32gui.IsWindowVisible(hwnd):
                return

            # Title fetch: empty titles are common for utility windows—ignore those
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return

            # Determine owning process name and map to emulator (if known)
            proc_name = get_hwnd_process_name(hwnd)
            emu = PROCESS_TO_EMU.get(proc_name)
            if not emu:
                return  # Not one of our emulators

            # New window discovered: start tracking and send initial state
            if hwnd not in tracked_windows:
                tracked_windows[hwnd] = title
                hwnd_emulator[hwnd] = emu
                logging.info(f"[NEW WINDOW] HWND={hwnd} | emu={emu} | Title='{title}'")

                # Initial send (menu or title) so marquee updates immediately
                parse_and_send(hwnd, title)

                # Mark that we've already sent for this initial title
                last_sent_titles[hwnd] = title

        except Exception as e:
            # If anything goes sideways on this HWND, log and keep scanning
            logging.debug(f"[ENUM ERR] hwnd={hwnd}: {e}")

    # Perform the enumeration; callback is called for every top-level window
    win32gui.EnumWindows(callback, None)

# =====================================================================================
# Sender: update_tty2rpi_marquee(marquee_data)
# -------------------------------------------------------------------------------------
# Establish an SSH session to the Raspberry Pi and write the payload to
# /dev/shm/tty2rpi.socket via SFTP. We open a new connection for each send for
# simplicity and robustness. Keepalive helps avoid NAT/idles killing sockets while
# the send is in progress (harmless if connect() is fresh).
# =====================================================================================
def update_tty2rpi_marquee(marquee_data):
    """
    Opens an SSH connection and writes the given marquee_data
    to /dev/shm/tty2rpi.socket on the Raspberry Pi.
    """
    try:
        # Create a fresh SSH client for each send (simplifies recovery on errors)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Connect to the Raspberry Pi using credentials from the INI
        ssh.connect(remote_ip, username=username, password=password, timeout=5.0)

        # Keepalive reduces chance of mid-transfer socket drops on noisy networks
        try:
            transport = ssh.get_transport()
            if transport:
                transport.set_keepalive(15)  # Send a keepalive packet every 15 seconds
        except Exception:
            # If keepalive setup fails, it's non-fatal—proceed anyway
            pass

        # Open SFTP and write the payload to the socket file on the Pi
        sftp = ssh.open_sftp()
        file_like = io.StringIO(marquee_data)

        # Open the "socket" path in write mode and flush to ensure delivery
        with sftp.file("/dev/shm/tty2rpi.socket", 'w') as remote_file:
            remote_file.write(file_like.read())
            remote_file.flush()

        logging.info(f"[SEND] -> tty2rpi.socket: {marquee_data}")

        # Clean shutdown of SFTP and SSH session
        sftp.close()
        ssh.close()

    except Exception as e:
        # Any connection/auth/file error is logged here (won't crash the script loop)
        logging.error(f"[ERROR][SSH] {e}")

# =====================================================================================
# Parser & Dispatcher: parse_and_send(hwnd, new_title)
# -------------------------------------------------------------------------------------
# Given a window and its most recent title bar text, we:
#   - Look up which emulator owns this HWND (from initial discovery).
#   - Apply emulator-specific parsing rules to extract the "loaded_rom" label.
#   - Build the CMDCOR payload and debounce sending (don't repeat identical strings).
#
# Emulator rules summary:
#   * MAME:       [rom_name] is in square brackets. ___empty -> MAME-MENU
#   * Flycast:    "Flycast - Game Name" -> Game Name; "Flycast" alone -> DCEMU-MENU
#   * DuckStation: if title startswith "DuckStation" -> PS1EMU-MENU; else title is the game
#                  Ignore transient: "duckstation-qt-x64-ReleaseLTCG", "Select Disc Image",
#                  "Automatic Updater"
#   * TeknoParrot: if title startswith "TeknoParrot" -> TPEMU-MENU; else title is the game
#   * PCSX2:       if title startswith "PCSX2" -> PS2EMU-MENU; else title is the game
#                  Ignore transient: "pcsx2-qt", "Select ISO Image", "Open ISO"
#   * Dolphin:     if title startswith "Dolphin" -> DOLPHIN-MENU; else title is the game
#                  Ignore transient: "dolphin-emu", "Confirm", and generic "Open File"
# =====================================================================================
def parse_and_send(hwnd, new_title):
    """
    Uses the remembered emulator for this hwnd, logs the title,
    extracts the game/menu label, and sends it to the Raspberry Pi via SSH.
    Debounced by payload so we don't send duplicate CMDCOR strings.
    """
    # Figure out which emulator this window belongs to (set during discovery)
    emulator = hwnd_emulator.get(hwnd)
    if not emulator:
        # Should not happen under normal flow; skip safely
        logging.debug(f"[SKIP] Unknown emulator for HWND {hwnd} | '{new_title}'")
        return

    # Always log raw title bar data for traceability
    logging.info(f"[INFO] HWND: {hwnd} | Emu: {emulator} | Title bar data: {new_title}")

    loaded_rom = None
    low = new_title.lower().strip()  # Normalized title for stable comparisons

    # ------------------------
    # MAME
    # ------------------------
    if emulator == "mame":
        # MAME includes the ROM name in brackets, e.g. "MAME: [outrun]"
        start = new_title.find("[")
        end = new_title.find("]", start)
        if start != -1 and end != -1:
            loaded_rom = new_title[start + 1:end]
            if loaded_rom == "___empty":
                loaded_rom = "MAME-MENU"

    # ------------------------
    # Flycast (Dreamcast)
    # ------------------------
    elif emulator == "flycast":
        # "Flycast - Game Name" -> extract after the hyphen
        # "Flycast" or "Flycast <version>" -> menu
        if low.startswith("flycast"):
            dash = new_title.find("- ")
            if dash != -1:
                loaded_rom = new_title[dash + 2:].strip()
            else:
                loaded_rom = "DCEMU-MENU"
        else:
            # Some builds may use a bare game window title
            loaded_rom = new_title.strip()

    # ------------------------
    # DuckStation (PS1)
    # ------------------------
    elif emulator == "duckstation":
        # Ignore transient launchers/dialogs that don't represent a game or menu state
        if ("duckstation-qt-x64-releaseltcg" in low) \
                or ("select disc image" in low) \
                or ("automatic updater" in low) \
                or ("about duckstation" in low) \
                or ("about qt" in low)\
                or ("padtest" in low):
            logging.info(f"[SKIP] DuckStation transient window: {new_title}")
            return
        # Menu if the title starts with "DuckStation", otherwise it's the game title itself
        if low.startswith("duckstation"):
            loaded_rom = "PS1EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    # ------------------------
    # TeknoParrot (Arcade)
    # ------------------------
    elif emulator == "teknoparrot":
        # Menu view shows "TeknoParrot", a running game usually shows just the game name
        if low.startswith("teknoparrot"):
            loaded_rom = "TPEMU-MENU"
        else:
            loaded_rom = new_title.strip()

    # ------------------------
    # PCSX2 (PS2)
    # ------------------------
    elif emulator == "pcsx2":
        # Ignore transient windows: Qt launcher, and common "open/select ISO" dialogs
        if ("pcsx2-qt" in low) \
                or ("ps2 bios (usa)" in low) \
                or ("select iso image" in low) \
                or ("open iso" in low) \
                or ("about pcsx2" in low) \
                or ("about qt" in low) \
                or ("automatic updater" in low) \
                or ("select location to save block dump" in low) \
                or ("show advanced settings" in low) \
                or ("select search directory" in low) \
                or ("select save state file" in low):
            logging.info(f"[SKIP] PCSX2 transient window: {new_title}")
            return
        # Menu if starting with "PCSX2", else treat as the running game's title
        if low.startswith("pcsx2"):
            loaded_rom = "PS2EMU-MENU"
        else:
            loaded_rom = new_title.strip()

    # ------------------------
    # Dolphin (GC/Wii)
    # ------------------------
    elif emulator == "dolphin":
        # Ignore transient windows: "dolphin-emu" helper, "Confirm" prompts, and generic "Open File"
        if ("dolphin-emu" in low) or ("confirm" in low) or ("open" in low and "file" in low):
            logging.info(f"[SKIP] Dolphin transient window: {new_title}")
            return
        # Menu if starting with "Dolphin", else treat as the running game's title
        if low.startswith("dolphin"):
            loaded_rom = "DOLPHIN-MENU"
        else:
            loaded_rom = new_title.strip()

    # If parsing yielded nothing (unexpected format), skip quietly but log at DEBUG
    if not loaded_rom:
        logging.debug(f"[SKIP] Parsed empty from '{new_title}' ({emulator})")
        return

    # Build the outgoing message once, and debounce by payload
    payload = CMDCOR_DATA + loaded_rom

    # Debounce: per-HWND and GLOBAL (covers new child windows like DuckStation Settings)
    global last_global_payload
    if last_sent_payload.get(hwnd) == payload or last_global_payload == payload:
        logging.debug(f"[SKIP DUP] HWND={hwnd} payload unchanged globally: {payload}")
        return

    # Ship it to the Pi and remember what we sent (per-window + global)
    update_tty2rpi_marquee(payload)
    last_sent_payload[hwnd] = payload
    last_global_payload = payload
    logging.info(f"[INFO] {emulator} title: {loaded_rom}")

# =====================================================================================
# Main Loop: main_loop()
# -------------------------------------------------------------------------------------
# The heartbeat of the script:
#   - Continuously discover new emulator windows (based on owning process).
#   - For tracked windows, compare current titles to the last observed titles.
#   - When a title changes, call parse_and_send() immediately to update the marquee.
#   - Clean up tracking when windows close.
# The loop sleeps for CHECK_INTERVAL seconds between sweeps.
# =====================================================================================
def main_loop():
    """
    Continuously scans for emulator windows and sends updated
    titles to the Raspberry Pi in real time when they change.
    """
    logging.info("tty2rpi sender...")
    logging.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            # STEP 1: Discover any new emulator windows (process-based detection)
            find_and_add_matching_windows()

            # STEP 2: Iterate over a *snapshot* of keys because we'll mutate the dict
            for hwnd in list(tracked_windows.keys()):
                # If the window no longer exists, clean up our state and move on
                if not win32gui.IsWindow(hwnd):
                    old_title = tracked_windows.get(hwnd, "")
                    logging.info(f"[CLOSE] HWND={hwnd} | '{old_title}'")
                    tracked_windows.pop(hwnd, None)
                    last_sent_titles.pop(hwnd, None)
                    hwnd_emulator.pop(hwnd, None)
                    last_sent_payload.pop(hwnd, None)
                    continue

                # Read the current title for this window
                current_title = win32gui.GetWindowText(hwnd)
                prev = tracked_windows.get(hwnd)

                # If nothing changed since last sweep, skip work quickly
                if current_title == prev:
                    continue

                # Otherwise, we detected a title change—log it and act
                logging.info(f"[TITLE CHANGED] HWND={hwnd} | '{prev}' -> '{current_title}'")
                tracked_windows[hwnd] = current_title

                # Parse and send updated info to the Pi (debounced)
                parse_and_send(hwnd, current_title)
                last_sent_titles[hwnd] = current_title

            # STEP 3: Take a tiny nap before the next sweep
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        # Graceful shutdown on Ctrl+C
        logging.info("Stopped monitoring.")

# =====================================================================================
# Entry Point
# =====================================================================================
if __name__ == "__main__":
    main_loop()
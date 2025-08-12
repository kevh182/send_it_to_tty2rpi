import requests
import configparser
import io
import paramiko
import csv
import time
import sys
import logging
from pathlib import Path

# ---------------- Config ----------------
config = configparser.ConfigParser()
config.read('tty2rpi_sender.ini')

MCP2_IP = config['MemCardPro']['mcp2_ip']
MCP_GC_IP = config['MemCardPro']['mcp_gc_ip']

CHECK_INTERVAL = 5   # seconds
HTTP_TIMEOUT = 3     # seconds for HTTP requests
CMDCOR_DATA = "CMDCOR§PARAM§"

remote_ip = config['tty2rpi']['remote_ip']
username = config['tty2rpi']['username']
password = config['tty2rpi']['password']

default_ps1_mc = config['MemCardPro']['default_memory_card_ps1']
default_ps2_mc = config['MemCardPro']['default_memory_card_ps2']
default_gc_mc  = config['MemCardPro']['default_memory_card_gc']

remote_file_path = "/dev/shm/tty2rpi.socket"
GAME_DB_PATH = '../Game_DB.csv'

# ---------------- Logging (INI controlled) ----------------
def _resolve_log_level(name: str) -> int:
    name = (name or "").strip().upper()
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(name, logging.INFO)

logging_enabled = config.getboolean('logging', 'enabled', fallback=True)
logging_level_name = config.get('logging', 'level', fallback='INFO')
log_level = _resolve_log_level(logging_level_name)
if not logging_enabled:
    log_level = logging.CRITICAL  # silence all but critical

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------- State ----------------
last_game_name = None
last_mode = None
GAME_DB = {}  # cached lookup {game_id: (title, serial)}

# ---------------- Helpers ----------------
def _norm(s: str) -> str:
    """Normalize strings for case/space-insensitive comparisons."""
    return (s or "").strip().upper()

# Store normalized defaults
default_ps1_mc_norm = _norm(default_ps1_mc)
default_ps2_mc_norm = _norm(default_ps2_mc)
default_gc_mc_norm  = _norm(default_gc_mc)

def load_game_db(csv_path: str):
    """Load the game database into a dict {game_id: (title, serial)} with raw & normalized keys."""
    db = {}
    p = Path(csv_path)
    if not p.exists():
        logging.warning("DB file not found: %s", csv_path)
        return db
    with p.open(newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = row.get('title') or ''
            serial = row.get('serial') or ''
            for v in row.values():
                if not v:
                    continue
                db.setdefault(v, (title, serial))        # raw key
                db.setdefault(_norm(v), (title, serial)) # normalized key
    logging.info("Loaded %d entries from %s", len(db), csv_path)
    return db

def update_tty2rpi_marquee(marquee_data):
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(remote_ip, username=username, password=password)
        sftp = ssh.open_sftp()
        file_like = io.StringIO(marquee_data)
        with sftp.file(remote_file_path, 'w') as remote_file:
            remote_file.write(file_like.read())
            remote_file.flush()
        logging.info("Wrote to %s", remote_file_path)
        logging.debug("Data sent to tty2rpi.socket: %s", marquee_data)
        sftp.close()
        ssh.close()
    except Exception as e:
        logging.error("Error writing to tty2rpi: %s", e)

def do_ps1_mode(state: dict):
    logging.debug("PS1 mode logic (ch=%s, size=%s, rssi=%s)",
                  state.get('currentChannel'), state.get('currentSize'), state.get('rssi'))

def do_ps2_mode(state: dict):
    logging.debug("PS2 mode logic (ch=%s, size=%s, rssi=%s)",
                  state.get('currentChannel'), state.get('currentSize'), state.get('rssi'))

def do_gc_mode(state: dict):
    logging.debug("GC mode logic (ch=%s, size=%s, rssi=%s)",
                  state.get('currentChannel'), state.get('currentSize'), state.get('rssi'))

def get_state(host):
    """Return JSON state from /api/currentState, or None if request fails."""
    try:
        r = requests.get(f"http://{host}/api/currentState", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None

def find_memcard_ip():
    """Return the first responding IP among the configured devices, or None."""
    for candidate in (MCP2_IP, MCP_GC_IP):
        if candidate and get_state(candidate):
            return candidate
    return None

# ---------------- Main logic ----------------
def get_game_id(host):
    global last_game_name, last_mode
    data = get_state(host)
    if not data:
        return False  # treat as offline

    # --- Mode detection ---
    reported_mode = (data.get("currentMode") or "").upper()
    inferred_mode = "GC" if host == MCP_GC_IP else None
    mode = (reported_mode or inferred_mode or "").upper()

    if mode and mode != last_mode:
        logging.info("MemCard Pro mode: %s", mode)
        if mode == "PS1":
            do_ps1_mode(data)
        elif mode == "PS2":
            do_ps2_mode(data)
        elif mode == "GC":
            do_gc_mode(data)
        else:
            logging.warning("Unrecognized mode: %s", data.get('currentMode') or 'UNKNOWN')
        last_mode = mode

    # --- Pick correct default memory card token ---
    if mode == "PS1":
        memory_card_token = default_ps1_mc_norm
    elif mode == "PS2":
        memory_card_token = default_ps2_mc_norm
    elif mode == "GC":
        memory_card_token = default_gc_mc_norm
    else:
        memory_card_token = None

    # --- ID & title resolution ---
    game_id = data.get('game_id') or data.get('gameID') or ""
    gid_norm = _norm(game_id)

    game_name = None
    serial = None

    if memory_card_token and gid_norm == memory_card_token:
        game_name = f"{mode}" if mode else "EMU_MENU"
    else:
        game_name, serial = GAME_DB.get(game_id, (None, None))
        if not game_name:
            game_name, serial = GAME_DB.get(gid_norm, (None, None))
        if not game_name:
            return True  # silently skip unknown IDs

    # --- Update marquee only on change ---
    if game_name != last_game_name:
        logging.info("MemCard Pro Data found: %s", game_id)
        if serial:
            logging.info("Database record found: '%s' - '%s'", game_name, serial)
        else:
            logging.info("Game Title: %s", game_name)
        marquee_data = CMDCOR_DATA + game_name
        update_tty2rpi_marquee(marquee_data)
        last_game_name = game_name

    return True

# ---------------- Entry ----------------
if __name__ == "__main__":
    # Load DB once
    GAME_DB = load_game_db(GAME_DB_PATH)

    current_ip = None
    logging.info("Starting MemCard Pro watcher (HTTP probe only).")

    try:
        while True:
            if current_ip is None:
                ip = find_memcard_ip()
                if ip:
                    current_ip = ip
                    logging.info("MemCard Pro detected at %s", current_ip)
                    last_mode = None
                else:
                    logging.debug("No device found; retrying soon…")
                    time.sleep(CHECK_INTERVAL)
                    continue

            if not get_game_id(current_ip):
                logging.warning("Lost connection to MemCard Pro at %s. Searching again…", current_ip)
                current_ip = None
                last_mode = None

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        logging.info("Exiting watcher.")
        sys.exit(0)
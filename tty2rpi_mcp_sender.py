import requests
import configparser
import platform
import subprocess
import re
import io
import paramiko
import csv
import time


# Create a ConfigParser object
config = configparser.ConfigParser()

# INI file
config.read('tty2rpi_sender.ini')

MCP2_IP = config['MemCardPro']['mcp2_ip']
MCP_GC_IP = config['MemCardPro']['mcp_gc_ip']

# How often to check the running processes
CHECK_INTERVAL = 5

# Prefix command - This is the magic that tty2rpi is looking for
CMDCOR_DATA = "CMDCOR§PARAM§"

remote_ip = config['tty2rpi']['remote_ip']
username = config['tty2rpi']['username']
password = config['tty2rpi']['password']

remote_file_path = "/dev/shm/tty2rpi.socket"

last_game_name = None

def update_tty2rpi_marquee(marquee_data):

    try:
        # Set up SSH and SFTP
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(remote_ip, username=username, password=password)

        sftp = ssh.open_sftp()

        # Convert string to file-like object
        file_like = io.StringIO(marquee_data)

        # Open and overwrite remote file
        with sftp.file(remote_file_path, 'w') as remote_file:
            remote_file.write(file_like.read())
            remote_file.flush()  # Ensure all data is written

        print(f"[INFO] Successfully wrote to {remote_file_path}")
        print(f"[INFO] Data to send to tty2rpi.socket: {marquee_data}")

        sftp.close()
        ssh.close()

    except Exception as e:
        print(f"[ERROR] {e}")

def ping(host: str, count: int = 2, timeout: int = 1000):

    system = platform.system()

    if system == "Windows":
        cmd = ["ping", host, "-n", str(count), "-w", str(timeout)]
    else:
        timeout_sec = int(timeout / 1000)
        cmd = ["ping", "-c", str(count), "-W", str(timeout_sec), host]

    try:
        output = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout = output.stdout.lower()

        # Look for a successful reply
        if system == "Windows":
            success_pattern = r"reply from [\d\.]+: bytes="
            unreachable_pattern = r"destination host unreachable|request timed out"
        else:
            success_pattern = r"bytes from [\d\.]+"
            unreachable_pattern = r"destination host unreachable|100% packet loss|no route to host"

        has_reply = re.search(success_pattern, stdout)
        has_error = re.search(unreachable_pattern, stdout)

        return bool(has_reply and not has_error)

    except Exception as e:
        print(f"Ping error: {e}")
        return False


# Determine the working IP
if ping(MCP2_IP):

    # MemcardPro2 is online
    MEMCARDPRO_IP = MCP2_IP

elif ping(MCP_GC_IP):

    # MemcardPro GC is online
    MEMCARDPRO_IP = MCP_GC_IP

else:

    MEMCARDPRO_IP = None

def get_game_id(host):
    global last_game_name

    try:
        url = f"http://{host}/api/currentState"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        game_id = data.get('game_id') or data.get('gameID')

        if game_id == "MemoryCard1":
            game_name = "PS2_EMU"
            serial = None
        else:
            game_name = None
            serial = None

            with open('PS2_DB.csv', newline='', encoding='utf-8-sig') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    if game_id in row.values():
                        game_name = row['title']  # assuming you fixed BOM in CSV header
                        serial = row['serial']
                        break

            if not game_name:
                # Game not found, skip update and print
                if last_game_name != None:
                    print("[WARNING] Game ID not found in DB. Skipping update.")
                return

        if game_name != last_game_name:
            print(f"[INFO] MemCard Pro Data found: {game_id}")
            if serial:
                print(f"[INFO] Database: {game_name}, serial: {serial}")
            else:
                print(f"[INFO] Database: {game_name}")
            marquee_data = CMDCOR_DATA + game_name
            update_tty2rpi_marquee(marquee_data)
            last_game_name = game_name
        else:
            pass

    except requests.exceptions.RequestException as e:
        pass

if __name__ == "__main__":
    while True:
        if MEMCARDPRO_IP:
            get_game_id(MEMCARDPRO_IP)
        time.sleep(CHECK_INTERVAL)
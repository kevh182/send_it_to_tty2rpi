import psutil
import time
import paramiko
import logging
import io
import configparser
import pygetwindow as gw

# Create a ConfigParser object
config = configparser.ConfigParser()

# INI file
config.read('tty2rpi_sender.ini')

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

remote_ip = config['tty2rpi']['remote_ip']
username = config['tty2rpi']['username']
password = config['tty2rpi']['password']

remote_file_path = "/dev/shm/tty2rpi.socket"

# system processes
MAME_PROCESS = "mame.exe"
DC_EMU_PROCESS = "flycast.exe"
PS1_EMU_PROCESS = "duckstation-qt-x64-ReleaseLTCG.exe"

# How often to check the running processes
CHECK_INTERVAL = 5

# Prefix command - This is the magic that tty2rpi is looking for
CMDCOR_DATA = "CMDCOR§PARAM§"

def mame_process_running(MAME_PROCESS):
    for proc in psutil.process_iter(['name', 'pid']):
        if proc.info['name'] == MAME_PROCESS:
            return proc.info['pid']
    return False

def dreamcast_process_running(DC_EMU_PROCESS):
    for proc in psutil.process_iter(['name', 'pid']):
        if proc.info['name'] == DC_EMU_PROCESS:
            return proc.info['pid']
    return False

def psx_process_running(PS1_EMU_PROCESS):
    for proc in psutil.process_iter(['name', 'pid']):
        if proc.info['name'] == PS1_EMU_PROCESS:
            return proc.info['pid']
    return False

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

def monitor_processes():

    last_loaded_rom = None  # Track the last loaded MAME ROM
    last_loaded_dc_rom = None  # Track the last loaded DC ROM
    last_loaded_ps1_rom = None # Track the last loaded PS1 ROM

    while True:

        try:
            # Start MAME process check
            if mame_process_running(MAME_PROCESS):

                # gather all the running tasks
                for window in gw.getAllWindows():

                    # MAME application window
                    if 'MAME' in window.title:

                        current_game = window.title

                        # find the loaded MAME rom in the title bar.  Rom title is located between the brackets
                        # - "[rom name]"
                        start_index = current_game.find("[")
                        end_index = current_game.find("]", start_index)

                        if start_index != -1 and end_index != -1:

                            # extract the rom name sans brackets
                            loaded_rom = current_game[start_index + 1: end_index]

                            # prevent rom name from writing over and over * infinity
                            if loaded_rom != last_loaded_rom:

                                mame_pid = mame_process_running(MAME_PROCESS)

                                print(f"[INFO] Application: {MAME_PROCESS}, PID: {mame_pid}")
                                print(f"[INFO] Original title bar data:: {current_game}")

                                # Update the last loaded game
                                last_loaded_rom = loaded_rom

                                # Default MAME window title "rom name"
                                if loaded_rom == "___empty":

                                    print(f"[INFO] Title bar data: {loaded_rom}")

                                    # change "___empty" to "MAME-MENU" to match tty2rpi marquee image filename
                                    loaded_rom = "MAME-MENU"

                                    # add tty2rpi command "CMDCOR§PARAM§" before "MAME-MENU"
                                    marquee_data = CMDCOR_DATA + loaded_rom

                                    # Update tty2rpi marquee
                                    update_tty2rpi_marquee(marquee_data)


                                else:

                                    print(f"[INFO] Title bar data: {loaded_rom}")

                                    # MAME - add command: "CMDCOR§PARAM§" plus the Loaded Game name
                                    marquee_data = CMDCOR_DATA + loaded_rom

                                    # Update tty2rpi marquee by handing it off to ssh to handle the rest
                                    update_tty2rpi_marquee(marquee_data)

            # Start Dreamcast Emulator process check
            elif dreamcast_process_running(DC_EMU_PROCESS):

                # gather all the running tasks
                for window in gw.getAllWindows():

                    # Dreamcast emulator application window
                    if 'Flycast' in window.title:

                        dc_emu_title = window.title

                        # find the game title in the title bar.  The game title is located after the emulator title
                        # "Flycast - Game Name"
                        start_index = dc_emu_title.find("- ")

                        # prevent rom name from writing over and over * infinity
                        if dc_emu_title != last_loaded_dc_rom:

                            # Update the last loaded game
                            last_loaded_dc_rom = dc_emu_title

                            dc_emu_pid = dreamcast_process_running(DC_EMU_PROCESS)

                            print(f"[INFO] Application: {DC_EMU_PROCESS}, PID: {dc_emu_pid}")
                            print(f"[INFO] Title bar data: {dc_emu_title}")

                            # Default Flycast window title
                            if dc_emu_title == "Flycast":

                                dc_emu_title = "DCEMU-MENU"

                                # add tty2rpi command "CMDCOR§PARAM§" before "MAME-MENU"
                                marquee_data = CMDCOR_DATA + dc_emu_title

                                # Update tty2rpi marquee
                                update_tty2rpi_marquee(marquee_data)

                            else:

                                if start_index != -1:

                                    # extract the rom name
                                    loaded_dc_rom = dc_emu_title[start_index + 2:]

                                    print(f"[INFO] Title bar data: {loaded_dc_rom}")

                                    # Dreamcast emulator - add command: "CMDCOR§PARAM§" plus the loaded game name
                                    marquee_data = CMDCOR_DATA + loaded_dc_rom

                                    # Update tty2rpi marquee by handing it off to ssh to handle the rest
                                    update_tty2rpi_marquee(marquee_data)

            # Start Playstation emulator process check
            elif psx_process_running(PS1_EMU_PROCESS):

                # gather all the running tasks
                for window in gw.getAllWindows():

                    # Find running PS1 emulator application window
                    if "DuckStation" in window.title:

                        raw_ps1_emu_title = window.title

                        # find the game title in the title bar.
                        end_index = raw_ps1_emu_title.find(" ")

                        # extract the 'DuckStation' name in the title bar
                        ps1_emu_title = raw_ps1_emu_title[:end_index]

                        # prevent rom name from writing over and over * infinity
                        if ps1_emu_title != last_loaded_ps1_rom:

                            # Update the last loaded game
                            last_loaded_ps1_rom = ps1_emu_title

                            ps1_emu_pid = psx_process_running(PS1_EMU_PROCESS)

                            print(f"[INFO] Application: {PS1_EMU_PROCESS}, PID: {ps1_emu_pid}")

                            if ps1_emu_title == "DuckStation":

                                print(f"[INFO] Original title bar data: {raw_ps1_emu_title}")
                                print(f"[INFO] Modified title bar data: {ps1_emu_title}")

                                ps1_emu_title = "PS1EMU-MENU"

                                # Playstation emulator - add command: "CMDCOR§PARAM§" plus the loaded game name
                                marquee_data = CMDCOR_DATA + ps1_emu_title

                                # Update tty2rpi marquee by handing it off to ssh to handle the rest
                                update_tty2rpi_marquee(marquee_data)

        except Exception as e:

            print(f"Error checking process: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    monitor_processes()
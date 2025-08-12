### Overview
"send_it_to_tty2rpi" are extension apps for use with [ojaksch's (aka RealLarry) "MiSTer tty2rpi" project](https://github.com/ojaksch/MiSTer_tty2rpi).  More information on this project can be found on the [discussion page for tty2rpi](https://misterfpga.org/viewtopic.php?t=5437) on the [MiSTer FPGA Forum](https://misterfpga.org).

"send_it_to_tty2rpi" assumes you already have a "MiSTer tty2rpi" marquee set up.

### Python
"send_it_to_tty2rpi" requires Python 3.  If you don't have Python installed, you can [follow this guide](https://www.geeksforgeeks.org/python/how-to-install-python-on-windows/) to install it on Windows.  "send_it_to_tty2rpi" was written with Windows in mind because it monitors thr running processes and reads the "window title bar"..  

## "tty2rpi_sender.py"
Monitors specific running process in Windows and reads the window title bar for it's data to send to tty2rpi.  A Windows PC is required.

### Additional Required Python Modules for "tty2rpi_sender.py"

pywin32 → Provides win32gui, win32process
```
pip install pywin32
```
paramiko → For SSH/SFTP communication

```
pip install paramiko
```
psutil → For process name lookups and PID handling

```
pip install psutil
```
## "tty2rpi_mcp_sender.py"

Reads data from  MemCardPro's api and sends it's "current state" to "tty2rpi" to update your marquee.
```
http://<mcp-ip-address>/api/currentstate
```
Examples of MemCardPro 2 api data
```
{ "currentMode": "PS2", "gameName": "MemoryCard1", "gameID": "MemoryCard1", "currentChannel": 1, "rssi": -18, "currentSize": "8MB" }
```
```
{ "currentMode": "PS2", "gameName": "God of War", "gameID": "SCUS-97399", "currentChannel": 1, "rssi": -18, "currentSize": "8MB" }
```

Designed to run on the Raspberry Pi, since the original intention of "tty2rpi" was to also run on a Raspberry Pi.  It "should" work on any Linux OS but Debian is prefered.  I test tty2rpi with an old laptop running Debian and it seems to work just fine.

### Additional Required Python Modules for "tty2rpi_mcp_sender.py"

requests → For HTTP API calls
```
pip install requests
```

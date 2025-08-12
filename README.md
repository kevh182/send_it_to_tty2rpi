### Overview
"send it to tty2rpi" is an extension app for use with [ojaksch's (aka RealLarry) "MiSTer tty2rpi" project](https://github.com/ojaksch/MiSTer_tty2rpi).  More information on this project be found on the [discussion page for tty2rpi](https://misterfpga.org/viewtopic.php?t=5437) on the [MiSTer FPGA Forum](https://misterfpga.org).

### Python
"send_it_to_tty2rpi" requires Python 3.  If you don't have Python installed, you can [follow this guide](https://www.geeksforgeeks.org/python/how-to-install-python-on-windows/) to install it on Windows.  "send_it_to_tty2rpi" was written with Windows in mind.  

"tty2rpi_sender.py" - monitors running process in Windows specfically.  A Windows PC is required.

"tty2rpi_mcp_sender.py" - designed to run on the Raspberry Pi, since the original intention of "MiSTer tty2rpi" was to also run on a Raspberry Pi.  It "should" work on any Linux OS and Debian is prefered.  I test tty2rpi with an old laptop running Debian and it seems to work just fine.

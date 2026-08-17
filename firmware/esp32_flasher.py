#!/usr/bin/env python3
# coding: utf-8

"""
ESP32 Multi-Project Flashing Tool

This script provides a flexible and lightweight solution for flashing firmware
onto ESP32-S3 devices across multiple projects. It is designed to work on
Windows, Linux, and macOS with minimal dependencies.

Key features:
- Supports multiple projects with different firmware files
- Automatic detection of ESP32-S3 devices
- Configurable flashing parameters (tool path, baud rate, timeout)
- Cross-platform compatibility
- Lightweight implementation using Python standard library

Usage:
    python esp32_flasher.py --project PROJECT_NAME [--tool PATH] [--baud-rate RATE] [--timeout SECONDS]

Dependencies:
    - Python 3.x
    - serial
    - esptool (external)

Configuration:
    Projects and their corresponding firmware files are defined in 'project_config.json'.
    Each project specifies the memory addresses and filenames for its firmware components.

Notes:
    - Ensure 'project_config.json' is in the same directory as this script.
    - The script requires appropriate permissions to access serial ports.
    - For Windows users: This script uses 'wmic' to list serial ports.
    - For Linux/macOS users: This script uses 'ls' to list serial devices.

For more information, please refer to the README.md file.
"""

__author__ = "Spencer Yan"
__email__ = "spencer@seeed.cc"
__version__ = "2.0.0"
__license__ = "MIT"
__status__ = "Production"

import subprocess
import os
import time
import json
import argparse
import sys
import platform
import serial.tools.list_ports

def log(message, level="INFO"):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {level} - {message}")

class ESP32Flasher:
    def __init__(self, tool='esptool', baud_rate=921600, timeout=30, project=None):
        self.tool = tool
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.project = project
        self.firmware_files = self.load_project_config()

    def load_project_config(self):
        config_file = 'project_config.json'
        if not os.path.exists(config_file):
            log(f"Configuration file {config_file} not found.", "ERROR")
            sys.exit(1)
        
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        if self.project not in config:
            log(f"Project '{self.project}' not found in configuration.", "ERROR")
            sys.exit(1)
        
        return config[self.project]

    def run_command(self, command, timeout=10):
        try:
            process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            process.kill()
            log(f"Command timed out after {timeout} seconds", "ERROR")
            return -1, b"", b"Timeout"

    def find_device(self):
        ports = list(serial.tools.list_ports.comports())
        for port in ports:
            if port.vid == 0x303A and port.pid == 0x1001:  # Adjust these to your device's VID/PID
                log(f'Found port: {port.device}')
                return port.device
        log('No test board found', "ERROR")
        return None

    def is_target_device(self, port_info):
        return port_info.vid == 0x303A and port_info.pid == 0x1001

    def reset_device(self, port):
        if platform.system() == "Windows":
            os.system(f"MODE {port}:1200,N,8,1")
        else:
            os.system(f"stty -F {port} 1200 cs8 -cstopb -parenb")
        time.sleep(1)

    def write_test(self):
        log(f'Writing {self.project} firmware to test board')
        port = self.find_device()
        if not port:
            return -1

        self.reset_device(port)

        flash_command = f'{self.tool} --chip esp32s3 --port {port} --baud {self.baud_rate} ' \
                        f'--before default_reset --after hard_reset write_flash -z ' \
                        f'--flash_mode dio --flash_freq 80m --flash_size 8MB '

        for address, filename in self.firmware_files.items():
            flash_command += f'{address} {filename} '
        
        print(f"if it runs failed, try copy to run the command below: \n{flash_command}\n")
        
        returncode, stdout, stderr = self.run_command(flash_command, timeout=self.timeout)
        if returncode != 0:
            log(f"Flashing failed: {stderr.decode()}", "ERROR")
        else:
            log(stdout.decode())
        return returncode

def main():
    parser = argparse.ArgumentParser(description='ESP32 Flashing Tool')
    parser.add_argument('--tool', default='esptool', help='Path to esptool')
    parser.add_argument('--baud-rate', type=int, default=921600, help='Baud rate for flashing')
    parser.add_argument('--timeout', type=int, default=30, help='Timeout for flashing')
    parser.add_argument('--project', required=True, help='Project name to flash')
    args = parser.parse_args()

    flasher = ESP32Flasher(tool=args.tool, baud_rate=args.baud_rate, timeout=args.timeout, project=args.project)
    result = flasher.write_test()
    
    if result == 0:
        log("Flashing completed successfully")
    else:
        log(f"Flashing failed with return code {result}", "ERROR")
        sys.exit(1)

if __name__ == "__main__":
    main()
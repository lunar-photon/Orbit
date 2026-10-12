#!/usr/bin/env python3
import os
import sys
import glob
import json
import time

def get_config_file():
    target_user = os.environ.get('SUDO_USER') or os.environ.get('REAL_USER') or os.environ.get('USER')
    if target_user and target_user != 'root':
        for sub in (".config/orbit", ".config/pc-authenticator"):
            user_config = os.path.expanduser(f"~{target_user}/{sub}/config.json")
            if os.path.exists(user_config) or os.path.exists(os.path.dirname(user_config)):
                return user_config
    for path in ('/etc/orbit/config.json', '/etc/pc-auth/config.json'):
        if os.path.exists(path):
            return path
    return os.path.expanduser('~/.config/orbit/config.json')

CONFIG_FILE = get_config_file()

def get_usb_devices():
    devices = {}
    for dev_path in glob.glob('/sys/bus/usb/devices/*'):
        serial_file = os.path.join(dev_path, 'serial')
        if os.path.exists(serial_file):
            try:
                with open(serial_file, 'r', errors='ignore') as f:
                    serial = f.read().strip()
                if not serial:
                    continue

                prod_file = os.path.join(dev_path, 'product')
                product = "Unknown Device"
                if os.path.exists(prod_file):
                    with open(prod_file, 'r', errors='ignore') as f:
                        product = f.read().strip()

                mfg_file = os.path.join(dev_path, 'manufacturer')
                mfg = ""
                if os.path.exists(mfg_file):
                    with open(mfg_file, 'r', errors='ignore') as f:
                        mfg = f.read().strip()

                vendor_file = os.path.join(dev_path, 'idVendor')
                vendor_id = ""
                if os.path.exists(vendor_file):
                    with open(vendor_file, 'r', errors='ignore') as f:
                        vendor_id = f.read().strip()

                devices[serial] = {
                    "serial": serial,
                    "product": product,
                    "manufacturer": mfg,
                    "vendor_id": vendor_id,
                    "path": dev_path
                }
            except Exception:
                pass
    return devices

def save_serial(serial, product=""):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass

    serials = cfg.get("allowed_usb_serials", [])
    if serial not in serials:
        serials.append(serial)
        cfg["allowed_usb_serials"] = serials
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f, indent=2)
        print(f"\n[+] Successfully registered USB Device:")
        print(f"    Product: {product}")
        print(f"    Serial:  {serial}")
        print(f"    Saved to: {CONFIG_FILE}\n")
    else:
        print(f"\n[!] Device is already registered: {serial} ({product})\n")

def remove_serial(serial):
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            serials = cfg.get("allowed_usb_serials", [])
            if serial in serials:
                serials.remove(serial)
                cfg["allowed_usb_serials"] = serials
                with open(CONFIG_FILE, 'w') as f:
                    json.dump(cfg, f, indent=2)
                print(f"Removed USB serial: {serial}")
        except Exception as e:
            print(f"[-] Error: {e}")

def main():
    print("==================================================")
    print("   Orbit - USB cable unlock enrollment")
    print("==================================================")
    
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
        except Exception:
            pass

    enrolled = cfg.get("allowed_usb_serials", [])
    if enrolled:
        print("\nCurrently Enrolled USB Tokens:")
        for s in enrolled:
            print(f"  Serial: {s}")
    else:
        print("\nNo USB tokens currently enrolled.")

    current = get_usb_devices()
    device_list = list(current.items())
    
    print("\nCurrently detected USB devices:")
    if device_list:
        for idx, (serial, d) in enumerate(device_list, 1):
            is_enrolled = " (Already Enrolled)" if serial in enrolled else ""
            print(f"  [{idx}] {d['manufacturer']} {d['product']} [Serial: {serial}]{is_enrolled}")
        print(f"  [W] Wait for newly plugged-in USB phone / device")
        if enrolled:
            print(f"  [C] Clear all enrolled USB devices")
        print(f"  [Q] Quit")

        choice = input("\nSelect device number to enroll or option [1-{}/W/Q]: ".format(len(device_list))).strip()
        if choice.lower() == 'q':
            return
        elif choice.lower() == 'c':
            cfg["allowed_usb_serials"] = []
            with open(CONFIG_FILE, 'w') as f:
                json.dump(cfg, f, indent=2)
            print("Cleared all enrolled USB devices.")
            return
        elif choice.isdigit() and 1 <= int(choice) <= len(device_list):
            chosen_serial, d = device_list[int(choice) - 1]
            prod_name = f"{d['manufacturer']} {d['product']}".strip()
            save_serial(chosen_serial, prod_name)
            return

    print("\nPlug in your phone (or USB token) via cable now...")
    print("(Press Ctrl+C to cancel)\n")
    
    initial_serials = set(current.keys())
    
    try:
        while True:
            time.sleep(0.8)
            now = get_usb_devices()
            new_serials = set(now.keys()) - initial_serials
            if new_serials:
                for s in new_serials:
                    d = now[s]
                    prod_name = f"{d['manufacturer']} {d['product']}".strip()
                    save_serial(s, prod_name)
                break
    except KeyboardInterrupt:
        print("\nEnrollment cancelled.")

if __name__ == '__main__':
    main()

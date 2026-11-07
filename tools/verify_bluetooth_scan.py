#!/usr/bin/env python3
"""Bounded scan on an already-powered adapter, releasing only our discovery session."""
import argparse,os
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true')
if not parser.parse_args().run:parser.print_help();raise SystemExit(0)
os.environ['POU_NO_TRAY']='1';os.environ['GSETTINGS_BACKEND']='memory'
from panasonic_on_ubuntu.dashboard import App
from gi.repository import Gio,GLib
app=App();app.set_flags(Gio.ApplicationFlags.NON_UNIQUE);errors=[];scanned=False
def start():
    controller=app.bluetooth_devices
    if not any(i.get('org.bluez.Adapter1',{}).get('Powered') for i in controller.objects.values()):
        print('SKIP: no powered Bluetooth adapter');app.window.close();return False
    controller.scan();GLib.timeout_add_seconds(4,stop);return False
def stop():
    global scanned
    c=app.bluetooth_devices
    if not c.scanning:errors.append('discovery did not start')
    else:scanned=True;c.stop_scan()
    GLib.timeout_add_seconds(2,finish);return False
def finish():
    c=app.bluetooth_devices
    if c.scanning:errors.append('discovery lease not released')
    if scanned and not errors:print('BLUEZ_REAL_ADAPTER_SCAN_STOP_OK',flush=True)
    app.window.close();return False
GLib.timeout_add_seconds(3,start);app.run(['omp-test'])
if errors:raise SystemExit(str(errors))

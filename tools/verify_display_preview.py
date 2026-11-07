#!/usr/bin/env python3
"""Temporarily change one screen and verify compositor recovery after client exit."""
import argparse,time,json,subprocess,sys
from panasonic_on_ubuntu import display_config as display

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--run',action='store_true')
    if not parser.parse_args().run:parser.print_help();return
    original=display.read()
    if len(original[2])!=1 or len(original[2][0][5])!=1:raise SystemExit('SKIP: one active display required')
    connector=original[2][0][5][0][0]
    monitor=next(m for m in original[1] if m[0][0]==connector)
    mode=next(m for m in monitor[1] if m[6].get('is-current'))
    alternative=next((s for s in mode[5] if abs(s-original[2][0][2])>.00001),None)
    if alternative is None:raise SystemExit('SKIP: no alternate scale')
    configuration=display.target(original,connector,mode[0],alternative)
    preview=display.Preview(original,configuration)
    try:
        # The process exits immediately after requesting the preview.
        subprocess.run([sys.executable,'-c','import json,sys; from panasonic_on_ubuntu.display_config import Preview; s,c=json.load(sys.stdin); Preview(s,c).start()'],input=json.dumps([original,configuration]),text=True,check=True,timeout=8)
        time.sleep(.5)
        assert display.same_layout(display.layout(display.read()),configuration),'preview readback mismatch'
        time.sleep(23)
        assert display.same_layout(display.layout(display.read()),display.layout(original)),'restoration readback mismatch'
        print('DISPLAY_PREVIEW_OK client-exit',flush=True)
    finally:
        if not display.same_layout(display.layout(display.read()),display.layout(original)):preview.cancel()

if __name__=='__main__':main()

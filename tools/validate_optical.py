"""Explicit idle optical transitions, restoring the original power state."""
import json,os,time
from pathlib import Path
from panasonic_on_ubuntu.optical import switch,status

log=Path('/var/log/panasonic-on-ubuntu/optical-cycles.jsonl')
def record(event):
    data={'timestamp':time.time(),'event':event,'status':status()}
    with log.open('a') as f:
        f.write(json.dumps(data)+'\n');f.flush();os.fsync(f.fileno())
    print(event,data['status'],flush=True)
def validate():
    original = status()
    if not original["supported"]:
        raise RuntimeError("No supported optical device")
    try:
        for i in range(3):
            switch('off');s=status()
            assert s['supported'] and s['powered'] is False and not s['driver_bound'] and not s['block_present']
            record('off-'+str(i));time.sleep(2)
            switch('on');s=status()
            assert s['powered'] and s['driver_bound'] and s['block_present']
            record('on-'+str(i));time.sleep(2)
    finally:
        switch('on' if original['powered'] else 'off');record('restored')


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run the live hardware check")
    if parser.parse_args().run:
        validate()
    else:
        parser.print_help()


if __name__ == "__main__":
    from validation_cli import checked
    checked(main)

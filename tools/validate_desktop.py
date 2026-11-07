"""Small reversible audio/backlight round trips in the real user session."""
import json
import subprocess
import time
from panasonic_on_ubuntu.desktop import backlight,set_backlight
from panasonic_on_ubuntu.panel import audio
from panasonic_on_ubuntu.telemetry import audio as read_audio

def pactl(*args):
    return subprocess.check_output(['pactl',*args],text=True,timeout=5)

def validate():
    results=[]
    for kind,target,action,command in [('sinks','@DEFAULT_SINK@','volume','set-sink-volume'),('sources','@DEFAULT_SOURCE@','mic-volume','set-source-volume')]:
        current=next(d for d in read_audio()[kind] if d['default'])
        raw=next(d for d in json.loads(pactl('-f','json','list',kind)) if d['index']==current['id'])
        original=[str(v['value']) for v in raw['volume'].values()]
        requested=max(0,min(100,current['volume']-1))
        try:
            audio(action,str(requested));time.sleep(.2)
            actual=next(d for d in read_audio()[kind] if d['default'])['volume']
            assert actual==requested,(action,requested,actual)
            results.append({'control':action,'requested':requested,'readback':actual})
        finally:pactl(command,str(current['id']),*original)

    serial,monitor=backlight()
    original=monitor['value']
    target=max(monitor['min'],original-max(1,round((monitor['max']-monitor['min'])*.01)))
    try:
        set_backlight(serial,monitor['connector'],target);time.sleep(.3)
        _,actual=backlight()
        assert actual['value']==target,(target,actual)
        results.append({'control':'brightness','requested_raw':target,'readback_raw':actual['value']})
    finally:
        serial,latest=backlight()
        set_backlight(serial,latest['connector'],original)
    print(json.dumps({'result':'pass','controls':results,'restored':True},ensure_ascii=False))


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

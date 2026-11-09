import argparse
import json
from .doctor import collect
from .battery import snapshot, history, monitor_once


def main():
    parser = argparse.ArgumentParser(prog='pou')
    parser.add_argument('action', nargs='?', default='gui', choices=['gui', 'tray', 'tray-autostart', 'hide-tray', 'tray-process', 'telemetry', 'telemetry-stream', 'audio', 'doctor', 'status', 'history', 'monitor-once'])
    parser.add_argument('parameters', nargs='*')
    parser.add_argument('--scope', default='all')
    args = parser.parse_args()
    if args.action != 'telemetry' and args.scope != 'all':
        parser.error('--scope is only valid for telemetry')
    if args.action not in ('audio','gui') and args.parameters:
        parser.error('Unexpected arguments')
    if args.action == 'gui':
        if len(args.parameters)>1 or (args.parameters and args.parameters[0] not in ('overview','battery','cpu','fan','network','bluetooth','audio','display','touchpad','storage','face')):
            parser.error('Unknown panel page')
        from .gui import run
        run(args.parameters[0] if args.parameters else None)
    elif args.action in ('tray','tray-autostart'):
        from .panel import show
        show(autostart=args.action=='tray-autostart')
    elif args.action == 'hide-tray':
        from .panel import hide
        hide()
    elif args.action == 'tray-process':
        from .tray import run
        run()
    elif args.action == 'telemetry':
        from .telemetry import collect as telemetry, SCOPES
        if args.scope != 'all' and args.scope not in SCOPES:parser.error('Unknown telemetry scope')
        print(json.dumps(telemetry(args.scope), ensure_ascii=False))
    elif args.action == 'telemetry-stream':
        from .telemetry import stream
        try:stream()
        except ValueError as exc:parser.error(str(exc))
    elif args.action == 'audio':
        if len(args.parameters) != 2:
            parser.error('audio needs ACTION VALUE')
        from .panel import audio
        audio(*args.parameters)
    elif args.action == 'monitor-once':
        monitor_once()
    else:
        print(json.dumps({'doctor': collect, 'status': snapshot, 'history': history}[args.action](), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

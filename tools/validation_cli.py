"""Report expected live-test failures without creating desktop crash reports."""
import subprocess
import sys


def checked(main):
    try:
        main()
    except KeyboardInterrupt:
        print('VALIDATION_CANCELLED', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, RuntimeError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        print(f'VALIDATION_FAILED: {exc}', file=sys.stderr)
        raise SystemExit(1)

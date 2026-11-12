"""Select a validated GPU driver without changing authentication policy."""
import json
import re
from pathlib import Path
from .hardware import selection

PCI=Path('/sys/bus/pci/devices/0000:00:02.0')
PROFILES={
    ('sv7','0x5917'):'/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl_legacy1.so',
    ('sr3','0x46a8'):'/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so',
}

_TIMING_KEYS = ('preflight', 'models', 'camera', 'templates', 'warmup', 'recognition', 'total')
_TIMING_LINE = re.compile('OMP_FACE_TIMING ' + ' '.join(
    key + r'_ms=(\d{1,5}\.\d{3})' for key in _TIMING_KEYS))


def timing_fields(stderr):
    """Accept only bounded stage durations, without forwarding biometric output."""
    if not isinstance(stderr, str):
        return ''
    for line in stderr.splitlines():
        match = _TIMING_LINE.fullmatch(line)
        if match and all(float(value) <= 60000 for value in match.groups()):
            return ' ' + line.removeprefix('OMP_FACE_TIMING ')
    return ''


def profile():
    try:
        model=selection()[0]
        if (PCI/'vendor').read_text().strip()!='0x8086':return None
        device=(PCI/'device').read_text().strip()
        driver=PROFILES.get((model,device))
        if driver is None or not Path(driver).is_file():return None
        return dict(model=model,device=device,driver=driver)
    except OSError:return None


def provisioned(cache):
    selected=profile()
    if selected is None:return None
    try:
        saved=json.loads((cache/'current/backend.json').read_text())
        return selected if saved==selected else None
    except FileNotFoundError:
        # Existing SV7 installations predate the manifest and use this exact
        # legacy GPU/driver pair. New hardware requires a local compilation.
        return selected if selected['model']=='sv7' else None
    except (OSError,ValueError):return None

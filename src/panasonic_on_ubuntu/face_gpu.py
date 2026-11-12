"""Select a validated GPU driver without changing authentication policy."""
import json
from pathlib import Path
from .hardware import selection

PCI=Path('/sys/bus/pci/devices/0000:00:02.0')
PROFILES={
    ('sv7','0x5917'):'/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl_legacy1.so',
    ('sr3','0x46a8'):'/usr/lib/x86_64-linux-gnu/intel-opencl/libigdrcl.so',
}


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

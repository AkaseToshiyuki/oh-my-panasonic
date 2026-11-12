"""Select the colour preview paired with the laptop's infrared camera."""
from pathlib import Path
import re


def colour_camera(root=Path('/sys/class/video4linux'), devices=Path('/dev')):
    candidates = []
    infrared = set()
    for entry in sorted(root.glob('video*'), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 10000):
        if not re.fullmatch(r'video\d+', entry.name):
            continue
        try:
            if (entry/'index').read_text().strip() != '0':
                continue
            name = (entry/'name').read_text().strip()
            usb = next((p for p in entry.resolve().parents if (p/'idVendor').is_file()), None)
        except OSError:
            continue
        if re.search(r'\bIR\b|infrared', name, re.I):
            if usb is not None:
                infrared.add(usb)
        else:
            candidates.append((devices/entry.name, usb))
    for path, usb in candidates:
        if usb in infrared:
            return path
    if candidates:
        return candidates[0][0]
    raise RuntimeError('未找到可用于预览的摄像头。')

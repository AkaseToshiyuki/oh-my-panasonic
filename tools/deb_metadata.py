"""Generate the payload checksums and size used by dpkg verification."""
import hashlib
from pathlib import Path
import re


def finalize(stage):
    stage = Path(stage)
    # Build-directory privacy must not become installed /usr or /etc permissions.
    # These packages contain only public payloads; private state is created at runtime.
    stage.chmod(0o755)
    for path in stage.rglob('*'):
        if path.is_symlink():
            continue
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    entries = []
    kib = 0
    for path in sorted(stage.rglob('*')):
        relative = path.relative_to(stage)
        if relative.parts[0] == 'DEBIAN' or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        entries.append(f'{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {relative.as_posix()}\n')
        kib += (len(data) + 1023) // 1024
    (stage/'DEBIAN/md5sums').write_text(''.join(entries))
    (stage/'DEBIAN/md5sums').chmod(0o644)
    control = stage/'DEBIAN/control'
    text = re.sub(r'^Installed-Size:.*\n', '', control.read_text(), flags=re.MULTILINE)
    control.write_text(f'Installed-Size: {kib}\n' + text)

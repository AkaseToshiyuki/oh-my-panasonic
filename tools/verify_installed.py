#!/usr/bin/env python3
"""Compare every installed payload file with the exact supplied deb archives."""
import argparse
import hashlib
from pathlib import Path
import stat
import subprocess
import tempfile

def digest(path):
    return hashlib.sha256(path.read_bytes()).digest()

def verify(archive):
    count=0
    with tempfile.TemporaryDirectory(prefix='pou-verify-') as directory:
        root=Path(directory)
        subprocess.run(['dpkg-deb','--extract',str(archive),directory],check=True)
        for source in root.rglob('*'):
            relative=source.relative_to(root)
            target=Path('/')/relative
            if source.is_symlink():
                if not target.is_symlink() or source.readlink()!=target.readlink():
                    raise RuntimeError(f'Installed symlink differs: {target}')
                count+=1
            elif source.is_file():
                if not target.is_file() or digest(source)!=digest(target):
                    raise RuntimeError(f'Installed bytes differ: {target}')
                if stat.S_IMODE(source.stat().st_mode)!=stat.S_IMODE(target.stat().st_mode):
                    raise RuntimeError(f'Installed permissions differ: {target}')
                count+=1
    print(f'PAYLOAD_VERIFIED {archive.name} {count} files',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives',nargs='+',type=Path)
    for archive in parser.parse_args().archives:verify(archive.resolve())

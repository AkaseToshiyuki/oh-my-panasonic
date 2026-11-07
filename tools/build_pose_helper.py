"""Build against a clean archive of the pinned Facelock sources (cargo, libtss2-dev)."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collect_rust_licenses import collect
from rust_build import environment

ROOT = Path(__file__).resolve().parents[1]
REV = '352bbbcb0f8f61cc21e36a1f038442e94fa6e26b'


def main():
    source = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='pou-facelock-') as tmp:
        work = Path(tmp)
        archive = work/'upstream.tar'
        subprocess.run(['git','-C',str(source),'archive','-o',str(archive),REV],check=True)
        with tarfile.open(archive) as files:
            files.extractall(work,filter='data')
        binary = work/'crates/facelock-tpm/src/bin/pou-pose-verify.rs'
        binary.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT/'src/pose-verify.rs',binary)
        env = {**environment(work, 'facelock'),'CARGO_PROFILE_RELEASE_LTO':'false','CARGO_PROFILE_RELEASE_CODEGEN_UNITS':'16'}
        subprocess.run(['cargo','build','-j2','--release','--locked','-p','facelock-tpm','--features','tpm',
                        '--bin','pou-pose-verify'],cwd=work,env=env,check=True)
        target = ROOT/'build/pou-pose-verify'
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(Path(env.get('CARGO_TARGET_DIR',work/'target'))/'release/pou-pose-verify',target)
        notices = ROOT/'build/pose-licenses'
        if notices.exists():
            shutil.rmtree(notices)
        collect(work, 'facelock-tpm', notices, 'tpm')


if __name__ == '__main__':
    main()

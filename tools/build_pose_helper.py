"""Build against a clean archive of the pinned Facelock sources (cargo, libtss2-dev)."""
import argparse
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collect_rust_licenses import collect
from rust_build import environment

ROOT = Path(__file__).resolve().parents[1]
REV = '352bbbcb0f8f61cc21e36a1f038442e94fa6e26b'
TPM_LIBRARIES = ('tss2-sys', 'tss2-esys', 'tss2-mu', 'tss2-tctildr')


def check_dependencies(source):
    if sys.version_info < (3, 12):
        raise RuntimeError('Python 3.12 or newer is required.')
    if not source.is_dir():
        raise RuntimeError(f'Facelock checkout not found: {source}')
    missing = [name for name in ('git', 'cargo', 'rustc', 'pkg-config') if shutil.which(name) is None]
    if missing:
        raise RuntimeError('Missing build tools: ' + ', '.join(missing) +
                           '. Install git, cargo, rustc and pkg-config.')
    version = subprocess.check_output(['rustc', '--version'], text=True)
    match = re.search(r'\brustc (\d+)\.(\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (1, 88, 0):
        raise RuntimeError('Rust 1.88 or newer is required.')
    missing = [name for name in TPM_LIBRARIES if subprocess.run(
        ['pkg-config', '--atleast-version=4.1.3', name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode]
    if missing:
        raise RuntimeError('TPM development libraries >= 4.1.3 are unavailable: ' + ', '.join(missing) +
                           '. On Ubuntu 26.04, run: sudo apt install libtss2-dev pkg-config')
    result = subprocess.run(['git', '-C', str(source), 'cat-file', '-e', REV + '^{commit}'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f'The Facelock checkout does not contain {REV}. Fetch that revision before building.')


def build(source):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Facelock checkout containing the pinned source revision')
    args = parser.parse_args(argv)
    stage = 'Dependency check'
    try:
        source = args.source.resolve()
        check_dependencies(source)
        stage = 'Pose helper build'
        build(source)
    except subprocess.CalledProcessError as error:
        reason = f'command exited with status {error.returncode}' if error.returncode >= 0 else 'command was interrupted'
        print(f'{stage} failed: {reason}. See the command output above.', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, tarfile.TarError) as error:
        print(f'{stage} failed: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Pose helper build cancelled.', file=sys.stderr)
        return 130
    print('Pose helper and dependency licenses are ready in build/.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

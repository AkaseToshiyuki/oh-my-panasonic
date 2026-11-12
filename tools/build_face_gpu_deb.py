#!/usr/bin/env python3
"""Build the optional multi-model GPU verifier using pinned source and Intel wheel."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from deb_metadata import finalize

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'src/face-gpu'
REV = '352bbbcb0f8f61cc21e36a1f038442e94fa6e26b'
WHEEL_SHA = '6b581cf1c3fa759495eb051d1ec5be4b5cb61a4e15265db7b9d56260ea5925d0'
PKG = 'panasonic-on-ubuntu-face-gpu'
VERSION = '0.18.0'
RUNTIME = ('libopenvino.so.2540','libopenvino_intel_gpu_plugin.so','libopenvino_onnx_frontend.so.2540',
           'libopenvino_ir_frontend.so.2540','libtbb.so.12','libtbbmalloc.so.2','libtbbbind_2_5.so.3')

def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)

def compile_helpers(repo, work, intel, dest):
    upstream = work/'source'
    upstream.mkdir()
    archive = work/'source.tar'
    run('git','-C',repo,'archive','-o',archive,REV)
    with tarfile.open(archive) as tar:
        tar.extractall(upstream,filter='data')
    run('git','apply',SOURCE/'facelock.patch',cwd=upstream)
    flags=['g++','-O2','-D_GLIBCXX_USE_CXX11_ABI=0','-I'+str(intel/'include'),
           '-L'+str(intel/'libs')]
    run(*flags,'-fPIC','-shared',SOURCE/'inference.cpp','-l:libopenvino.so.2540','-lcrypto',
        '-Wl,-soname,libpou_ov.so','-Wl,-rpath,$ORIGIN/ov','-o',dest/'libpou_ov.so')
    run(*flags,SOURCE/'compile.cpp','-l:libopenvino.so.2540','-o',dest/'compile')
    env={**os.environ,'LIBRARY_PATH':str(dest)+':'+os.environ.get('LIBRARY_PATH',''),
         'LD_LIBRARY_PATH':str(intel/'libs'),'CARGO_PROFILE_RELEASE_LTO':'false',
         'CARGO_PROFILE_RELEASE_CODEGEN_UNITS':'16','CARGO_TARGET_DIR':str(work/'target')}
    run('cargo','build','--release','--locked','-j2','-p','facelock-cli','--no-default-features',
        '--features','tpm','--bin','pou-face-gpu',env=env,cwd=upstream)
    shutil.copyfile(work/'target/release/pou-face-gpu',dest/'pou-face-gpu')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel',type=Path)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--source',type=Path,help='Git checkout containing the pinned Facelock revision')
    source.add_argument('--validated-binaries',type=Path,help='Package exactly the three on-device tested build artifacts')
    opts=parser.parse_args()
    with opts.wheel.open('rb') as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=WHEEL_SHA:
            raise SystemExit('Unexpected OpenVINO wheel digest')
    with tempfile.TemporaryDirectory(prefix='pou-gpu-package-') as tmp:
        work=Path(tmp)
        intel=work/'intel'
        stage=work/'package'
        dest=stage/'usr/lib'/PKG
        (dest/'ov').mkdir(parents=True)
        docs=stage/'usr/share/doc'/PKG
        docs.mkdir(parents=True)
        with zipfile.ZipFile(opts.wheel) as z:
            for name in z.namelist():
                if name.startswith(('openvino/include/','openvino/libs/')):
                    z.extract(name,intel)
                if '.dist-info/licenses/' in name and not name.endswith('/'):
                    license_file=docs/'openvino'/name.split('.dist-info/licenses/',1)[1]
                    license_file.parent.mkdir(parents=True,exist_ok=True)
                    license_file.write_bytes(z.read(name))
        sources=intel/'openvino'
        for name in RUNTIME:
            shutil.copyfile(sources/'libs'/name,dest/'ov'/name)
        if opts.source:
            compile_helpers(opts.source.resolve(),work,sources,dest)
        else:
            for name in ('compile','libpou_ov.so','pou-face-gpu'):
                shutil.copyfile(opts.validated_binaries/name,dest/name)
        for name in ('compile','pou-face-gpu'):
            (dest/name).chmod(0o755)
        command=stage/'usr/sbin/pou-face-gpu-prepare'
        command.parent.mkdir(parents=True)
        shutil.copyfile(SOURCE/'prepare.py',command)
        command.chmod(0o755)
        shutil.copyfile(ROOT/'vendor/Facelock-LICENSE-MIT',docs/'Facelock-LICENSE-MIT')
        shutil.copyfile(ROOT/'LICENSE',docs/'copyright')
        meta=stage/'DEBIAN'
        meta.mkdir()
        (meta/'control').write_text(f"""Package: {PKG}
Version: {VERSION}
Architecture: amd64
Section: utils
Priority: optional
Maintainer: AkaseToshiyuki <51808852+AkaseToshiyuki@users.noreply.github.com>
Depends: panasonic-on-ubuntu (>= 0.23.4), facelock (= 0.2.0-1~ubuntu26.04.1), ocl-icd-libopencl1, libssl3t64, libstdc++6
Recommends: intel-opencl-icd | intel-opencl-icd-legacy
Description: Oh My Panasonic local FP32 face verification with Intel graphics
 Native GPU inference, with machine-local compilation of installed face models.
""")
        shutil.copyfile(ROOT/'packaging/face-gpu-prerm', meta/'prerm')
        (meta/'prerm').chmod(0o755)
        (meta/'postrm').write_text('#!/bin/sh\nset -e\nif [ "$1" = purge ]; then rm -rf -- /var/cache/pou-face-gpu; fi\n')
        (meta/'postrm').chmod(0o755)
        finalize(stage)
        (ROOT/'dist').mkdir(exist_ok=True)
        run('dpkg-deb','-Zxz','-z3','--threads-max=2','--root-owner-group','--build',stage,
            ROOT/f'dist/{PKG}_{VERSION}_amd64.deb')

if __name__=='__main__':
    main()

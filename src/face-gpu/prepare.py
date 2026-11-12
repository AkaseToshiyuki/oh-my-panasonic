#!/usr/bin/python3 -I
"""Provision machine-local FP32 graphs. This tool never opens a camera or the enrollment store."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib

CACHE = Path('/var/cache/pou-face-gpu')
LIB = Path('/usr/lib/panasonic-on-ubuntu-face-gpu')
MODELS = (
    ('detector', 640, 'scrfd_2.5g_bnkps.onnx', 'bc24bb349491481c3ca793cf89306723162c280cb284c5a5e49df3760bf5c2ce'),
    ('embedder', 112, 'w600k_r50.onnx', '4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43'),
)

def prepare():
    if os.geteuid() != 0:
        raise RuntimeError('需要 root 权限。')
    os.umask(0o077)
    CACHE.mkdir(mode=0o700, exist_ok=True)
    info = CACHE.lstat()
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('缓存目录权限错误。')
    with (CACHE/'prepare.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready = CACHE/'enabled'
        if sys.argv[1:] == ['--disable']:
            ready.unlink(missing_ok=True)
            return
        if sys.argv[1:]:
            raise RuntimeError('只接受 --disable 或无参数。')
        from panasonic_on_ubuntu.face_gpu import profile
        selected=profile()
        if selected is None:
            raise RuntimeError('未找到本机已验证的核显驱动。')
        with open('/etc/facelock/config.toml','rb') as source:
            config = tomllib.load(source)
        models = Path(config.get('daemon',{}).get('model_dir','/var/lib/facelock/models'))
        rec = config.get('recognition',{})
        stage = Path(tempfile.mkdtemp(prefix='graphs-',dir=CACHE))
        published = False
        try:
            for field, side, filename, digest in MODELS:
                if rec.get(field+'_model',filename) != filename or rec.get(field+'_sha256',digest) != digest:
                    raise RuntimeError('当前识别模型不匹配。')
                copy = stage/f'model-{side}.onnx'
                shutil.copyfile(models/filename,copy)
                with copy.open('rb') as source:
                    if hashlib.file_digest(source,'sha256').hexdigest() != digest:
                        raise RuntimeError('原版模型校验失败。')
            subprocess.run([LIB/'compile',stage],check=True,timeout=180,env={
                'PATH':'/usr/bin:/bin','HOME':str(stage),'LC_ALL':'C',
                'LD_LIBRARY_PATH':str(LIB/'ov'),'OCL_ICD_VENDORS':selected['driver']})
            for _,side,_,_ in MODELS:
                (stage/f'model-{side}.onnx').unlink()
                blob=stage/f'model-{side}'
                with blob.open('rb') as data:
                    checksum=hashlib.file_digest(data,'sha256').hexdigest()
                (stage/(blob.name+'.sha256')).write_text(checksum+'\n')
            (stage/'backend.json').write_text(json.dumps(selected))
            current=CACHE/'current'
            previous=current.resolve() if current.is_symlink() else None
            link=CACHE/'current.new'
            link.unlink(missing_ok=True)
            link.symlink_to(stage.name)
            link.replace(current)
            published=True
            ready.touch(mode=0o600)
            if previous and previous.parent==CACHE and previous.name.startswith('graphs-'):
                shutil.rmtree(previous,ignore_errors=True)
        finally:
            if not published:
                shutil.rmtree(stage,ignore_errors=True)

if __name__=='__main__':
    try:
        prepare()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        sys.exit(str(e))

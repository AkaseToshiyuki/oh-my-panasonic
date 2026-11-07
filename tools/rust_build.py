"""Rust build settings that avoid embedding workstation paths in release binaries."""
import os
from pathlib import Path
import shlex


def environment(source, label):
    env = dict(os.environ)
    encoded = env.get('CARGO_ENCODED_RUSTFLAGS')
    flags = encoded.split('\x1f') if encoded else shlex.split(env.get('RUSTFLAGS', ''))
    flags.extend([
        f'--remap-path-prefix={Path.home()}=/build/user',
        f'--remap-path-prefix={Path(env.get("CARGO_HOME", Path.home()/".cargo"))}=/build/cargo',
        f'--remap-path-prefix={Path(source).resolve()}=/build/{label}',
    ])
    env['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(flags)
    env.setdefault('CARGO_BUILD_JOBS', '2')
    return env

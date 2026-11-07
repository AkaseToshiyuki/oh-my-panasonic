"""Collect license notices for the normal and build dependencies of a Rust binary."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess


def collect(repository, package, destination, features=None):
    repository, destination = Path(repository), Path(destination)
    target = 'x86_64-unknown-linux-gnu'
    flags = ['--features', f'{package}/{features}'] if features else []
    metadata = json.loads(subprocess.check_output(
        ['cargo', 'metadata', '--format-version=1', '--locked', '--filter-platform', target, *flags],
        cwd=repository, text=True))
    tree = subprocess.check_output(
        ['cargo', 'tree', '--locked', '--package', package, '--target', target,
         '--edges', 'normal,build', '--prefix', 'none', '--format', '{p}', *flags],
        cwd=repository, text=True)
    selected = set()
    for line in tree.splitlines():
        match = re.match(r'([\w-]+) v([^\s]+)', line)
        if not match:
            raise RuntimeError(f'Unrecognized cargo dependency: {line}')
        selected.add(match.groups())
    records = []
    for name, version in sorted(selected):
        matches = [p for p in metadata['packages'] if (p['name'], p['version']) == (name, version)]
        if len(matches) != 1:
            raise RuntimeError(f'Ambiguous dependency: {name} {version}')
        info = matches[0]
        source = Path(info['manifest_path']).parent
        licenses = []
        for path in source.iterdir():
            if path.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'COPYRIGHT')):
                licenses.extend([path] if path.is_file() else [p for p in path.rglob('*') if p.is_file()])
        if info.get('license_file'):
            licenses.append(source / info['license_file'])
        # Workspace crates may use a single license at the workspace root.
        if not licenses and info['id'] in metadata['workspace_members']:
            workspace = Path(metadata['workspace_root'])
            licenses = [p for p in workspace.iterdir() if p.is_file()
                        and p.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE'))]
        if not licenses:
            raise RuntimeError(f'Missing license text: {name} {version}')
        folder = destination / f'{name}-{version}'
        folder.mkdir(parents=True, exist_ok=True)
        for index, path in enumerate(sorted(set(licenses))):
            shutil.copyfile(path, folder / f'{index:02d}-{path.name}')
        records.append(f'{name} {version}\nLicense: {info.get("license") or "see included license"}\n'
                       f'Source: {info.get("repository") or "https://crates.io/crates/" + name}\n')
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'SOURCES.txt').write_text('\n'.join(records), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('package')
    parser.add_argument('destination', type=Path)
    parser.add_argument('--features')
    args = parser.parse_args()
    collect(args.repository, args.package, args.destination, args.features)

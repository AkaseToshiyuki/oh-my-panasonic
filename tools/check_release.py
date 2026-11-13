"""Validate tracked release files without reading ignored private work."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {'docs/usage.zh-CN.md', 'docs/build.zh-CN.md', 'docs/hardware.zh-CN.md'}
ROOT_FILES = {'.gitattributes', '.gitignore', 'CHANGELOG.md',
              'CONTRIBUTING.md', 'LICENSE', 'README.md', 'SECURITY.md', 'pyproject.toml'}
DIRECTORIES = {'.github', 'assets', 'extension', 'kernel', 'packaging',
               'src', 'tests', 'tools', 'vendor'}


def allowed(path):
    path = Path(path)
    if path.as_posix() in ROOT_FILES | DOCUMENTS:
        return True
    if path.parts[0] not in DIRECTORIES:
        return False
    if any(part in {'.git', '__pycache__', 'target', 'build', 'dist', 'private'} for part in path.parts):
        return False
    return path.suffix.lower() not in {'.deb', '.log', '.pem', '.key', '.onnx', '.blob',
                                       '.sqlite', '.db', '.zip', '.gz', '.exe', '.dll', '.dmp',
                                       '.o', '.ko', '.so', '.a', '.rlib', '.bin', '.pth', '.pt', '.safetensors', '.efivar'}


def main():
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    paths = [p for p in tracked if p]
    failures = []
    for name in paths:
        path = ROOT / name
        if not allowed(name) or not path.is_file() or path.is_symlink():
            failures.append('Unexpected release file: ' + name)
            continue
        content = path.read_bytes()
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', content):
            failures.append('Private key material: ' + name)
        if path.suffix == '.md':
            text = content.decode('utf-8')
            for link in re.findall(r'\]\(([^)]+)\)', text):
                link = link.split('#', 1)[0]
                if link and '://' not in link and not link.startswith('mailto:'):
                    if not (path.parent / link).is_file():
                        failures.append(f'Broken document link: {name}: {link}')
    if failures:
        raise SystemExit('\n'.join(failures))
    print(f'RELEASE_SOURCE_OK {len(paths)} tracked files')


if __name__ == '__main__':
    main()

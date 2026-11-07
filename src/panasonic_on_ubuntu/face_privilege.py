"""One local privilege request, one compositor confirmation, one fresh IR match."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import socket
import stat
import struct
import time

from . import face

PREFS = Path('/var/lib/panasonic-on-ubuntu/face-authorize')
RUN = Path('/run/panasonic-on-ubuntu')
HOOK = 'auth sufficient pam_exec.so seteuid quiet quiet_log stdout /usr/libexec/pou-face-authorize\n'
SERVICES = ('sudo', 'polkit-1')


def preferences(uid):
    try:
        settings = json.loads((PREFS/str(uid)).read_text())
        return [s for s in SERVICES if s in settings]
    except (OSError, ValueError, TypeError):
        return []


def set_option(uid, service, enable):
    if service not in SERVICES:
        raise ValueError('未知认证服务')
    PREFS.mkdir(mode=0o711, parents=True, exist_ok=True)
    PREFS.chmod(0o711)
    selection = set(preferences(uid))
    if enable:
        selection.add(service)
    else:
        selection.discard(service)
    dest = PREFS/str(uid)
    if selection:
        tmp = dest.with_suffix('.new')
        tmp.write_text(json.dumps(sorted(selection)))
        tmp.chmod(0o644)
        tmp.replace(dest)
    else:
        dest.unlink(missing_ok=True)


def edit_hooks(enable, services=SERVICES):
    # Respect vendor PAM files and retain unrelated administrator edits.
    import tempfile
    for service in services:
        dest = Path('/etc/pam.d')/service
        vendor = Path('/usr/lib/pam.d')/service
        source = dest if dest.is_file() else vendor
        if not source.is_file():
            continue
        original = source.read_text()
        clean = ''.join(l for l in original.splitlines(keepends=True) if l != HOOK)
        if enable:
            if clean.count('@include common-auth\n') != 1:
                raise RuntimeError(f'{service} 的 PAM 配置需要手动检查。')
            clean = clean.replace('@include common-auth\n', HOOK + '@include common-auth\n')
        if clean == original:
            continue
        marker = PREFS.parent / ('pam-vendor-' + service)
        if not enable and marker.is_file() and vendor.exists() and vendor.read_text() == clean:
            dest.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            continue
        with tempfile.NamedTemporaryFile(mode='w', dir=dest.parent, delete=False) as fp:
            path = Path(fp.name)
            try:
                os.fchmod(fp.fileno(), stat.S_IMODE(source.stat().st_mode))
                fp.write(clean)
                fp.flush()
                os.fsync(fp.fileno())
                if source.read_text() != original:
                    raise RuntimeError('认证配置已更改，请重试。')
                if enable and not dest.exists():
                    marker.touch(mode=0o600)
                path.replace(dest)
            finally:
                path.unlink(missing_ok=True)


def sudo_command(uid, parent, tty):
    if (parent/'exe').resolve() != Path('/usr/bin/sudo').resolve():
        return None
    tty = (tty or os.readlink(parent/'fd/0')).removeprefix('/dev/')
    if not re.fullmatch(r'pts/\d+', tty):
        return None
    tty_stat = Path('/dev', tty).stat()
    if not stat.S_ISCHR(tty_stat.st_mode) or tty_stat.st_uid != uid:
        return None
    # The terminal must belong to this request's controlling session.
    fields = (parent/'stat').read_text().rsplit(')', 1)[1].split()
    if int(fields[4]) != tty_stat.st_rdev:
        return None
    argv = [os.fsdecode(s) for s in (parent/'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
    if len(argv) < 2 or '-S' in argv or '--stdin' in argv:
        return None
    command = shlex.join(['sudo', *argv[1:]])
    if len(command) > 320 or not command.isprintable():
        return None
    # A desktop terminal is a positive provenance check, even on desktops
    # where the terminal itself is launched by the systemd user manager.
    desktop = False
    while parent.name != '1':
        exe = (parent/'exe').resolve().name
        if exe.startswith(('sshd', 'tmux', 'screen')) or exe in ('su', 'login'):
            return None
        if str((parent/'exe').resolve()) in ('/usr/bin/ptyxis','/usr/libexec/ptyxis-agent','/usr/libexec/gnome-terminal-server',
                                            '/usr/bin/kgx','/usr/bin/konsole','/usr/bin/xterm'):
            desktop = True
        ppid = (parent/'stat').read_text().rsplit(')', 1)[1].split()[1]
        if not ppid.isdecimal() or ppid == '0':
            return None
        parent = Path('/proc')/ppid
    return command if desktop else None


def shell_peer(user):
    uid = user.pw_uid
    argv = ['/usr/sbin/runuser','-u',user.pw_name,'--','/usr/bin/busctl','--address',
            f'unix:path=/run/user/{uid}/bus','call','org.freedesktop.DBus','/org/freedesktop/DBus',
            'org.freedesktop.DBus','GetConnectionUnixProcessID','s','org.gnome.Shell']
    out = face.call(argv, timeout=2).stdout.strip()
    if not re.fullmatch(r'u [1-9]\d*', out):
        return None
    pid = int(out.split()[1])
    proc = Path('/proc')/str(pid)
    return pid if proc.stat().st_uid == uid and (proc/'exe').resolve() == Path('/usr/bin/gnome-shell') else None


@contextmanager
def consent_channel(uid):
    RUN.mkdir(mode=0o755, exist_ok=True)
    path = RUN/f'consent-{uid}.sock'
    pending = path.with_suffix('.pending')
    with (RUN/f'consent-{uid}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            pending.unlink(missing_ok=True)
            try:
                listener.bind(str(pending))
                os.chown(pending,uid,-1)
                pending.chmod(0o600)
                listener.listen(4)
                pending.replace(path)
                yield listener
            finally:
                pending.unlink(missing_ok=True)
                path.unlink(missing_ok=True)


def authorize():
    service = os.environ.get('PAM_SERVICE')
    if (os.geteuid() or service not in SERVICES or os.environ.get('PAM_TYPE') != 'auth'
            or os.environ.get('PAM_RHOST')):
        return False
    user = pwd.getpwnam(os.environ.get('PAM_USER', ''))
    uid = user.pw_uid
    if uid < 1000 or service not in preferences(uid) or not face.enabled(user):
        return False
    # The systemd polkit helper has private devices. Keep its sandbox intact.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(38)
        client.connect('/run/panasonic-on-ubuntu/broker.sock')
        request = dict(service=service, user=user.pw_name, tty=os.environ.get('PAM_TTY',''))
        client.sendall((json.dumps(request)+'\n').encode())
        with client.makefile('rb') as incoming:
            for line in incoming:
                msg = json.loads(line)
                if 'message' in msg:
                    print(msg['message'], flush=True)
                else:
                    return msg.get('matched') is True
    return False


def broker():
    with socket.socket(fileno=os.dup(0)) as client:
        client.settimeout(3)
        pid, owner, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if owner != 0:
            return
        proc = Path('/proc')/str(pid)
        argv = (proc/'cmdline').read_bytes().split(b'\0')
        if argv[:3] != [b'/usr/bin/python3', b'-I', b'/usr/libexec/pou-face-authorize']:
            return
        parent = Path('/proc')/(proc/'stat').read_text().rsplit(')',1)[1].split()[1]
        with client.makefile('rb') as incoming:
            request = json.loads(incoming.readline(4096))
        def send(**fields):
            client.sendall((json.dumps(fields)+'\n').encode())
        import select
        with os.fdopen(os.pidfd_open(int(proc.name)), 'rb') as caller, os.fdopen(os.pidfd_open(int(parent.name)), 'rb') as origin:
            def live():
                return not select.select([caller,origin,client],[],[],0)[0]
            allowed = confirmed_match(request, parent, send, live)
        send(matched=bool(allowed))


def confirmed_match(request, parent, send, live=lambda:True):
    service = request['service']
    user = pwd.getpwnam(request['user'])
    uid = user.pw_uid
    if uid < 1000 or service not in SERVICES or service not in preferences(uid) or not face.enabled(user):
        return False
    sid = face.active_session(uid,False)
    if not sid or not standard_password_stack():
        return False
    detail = sudo_command(uid, parent, request['tty']) if service == 'sudo' else ''
    if service == 'sudo' and not detail:
        return False
    if service == 'polkit-1' and (parent/'exe').resolve() != Path('/usr/lib/polkit-1/polkit-agent-helper-1'):
        return False
    peer = shell_peer(user)
    if not peer:
        return False
    tag = secrets.token_hex(8)
    hint = '请选择刷脸或使用密码 · ' + tag
    with consent_channel(uid) as listener:
        if service == 'polkit-1':
            send(message=hint)
        else:
            send(message='请在桌面确认本次 sudo 请求。')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            listener.settimeout(max(.01, deadline-time.monotonic()))
            connection, _ = listener.accept()
            with connection:
                pid, owner, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if pid != peer or owner != uid:
                    continue
                connection.settimeout(18)
                connection.sendall((json.dumps(dict(service=service, detail=detail, hint=hint))+'\n').encode())
                if connection.recv(16) != b'face\n':
                    return False
                if not live() or not face.enabled(user) or face.active_session(uid,False) != sid or service not in preferences(uid):
                    return False
                send(message='正在尝试人脸识别，请正对摄像头。')
                matched = face.authenticate(user.pw_name).returncode == 0
                return bool(matched and live() and face.enabled(user) and service in preferences(uid)
                            and face.active_session(uid,False) == sid)
    return False

def standard_password_stack():
    try:
        lines = [line.split('#',1)[0].strip() for line in Path('/etc/pam.d/common-auth').read_text().splitlines()]
    except OSError:
        return False
    active = [' '.join(line.split()) for line in lines if line]
    # Never skip a site-added MFA, lockout or identity policy. Only the known
    # Ubuntu password stacks may be substituted when the user opts in.
    tail = ['auth requisite pam_deny.so','auth required pam_permit.so']
    if active and active[-1] == 'auth optional pam_cap.so':
        active.pop()
    local = ['auth [success=1 default=ignore] pam_unix.so nullok']
    sss = ['auth [success=2 default=ignore] pam_unix.so nullok',
           'auth [success=1 default=ignore] pam_sss.so use_first_pass']
    return active in (local+tail, sss+tail)


def configure(uid, service, enable):
    if enable:
        if not standard_password_stack():
            raise RuntimeError('当前账户使用自定义认证策略。')
        edit_hooks(True,(service,))
    set_option(uid, service, enable)
    if not enable and service not in selected_services():
        edit_hooks(False,(service,))


def selected_services():
    return {s for p in PREFS.glob('*') if p.name.isdecimal() for s in preferences(int(p.name))}


if __name__ == '__main__':
    try:
        broker()
    except (OSError, ValueError, KeyError, TimeoutError):
        pass

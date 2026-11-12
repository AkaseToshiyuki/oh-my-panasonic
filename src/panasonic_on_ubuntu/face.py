"""Opt-in face authentication for local GDM login and locked GNOME sessions."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import select
import stat
import subprocess
import sys
import time

BACKEND = '/usr/bin/facelock'
PAM = Path('/etc/pam.d/gdm-password')
USERS = Path('/var/lib/panasonic-on-ubuntu/face-unlock')
OLD_HOOK = 'auth sufficient pam_exec.so quiet /usr/libexec/pou-face-check\n'
HOOK = 'auth sufficient pam_exec.so quiet stdout /usr/libexec/pou-face-check\n'
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','LC_ALL':'C'}
FACE_ENV = {**ENV, 'GLIBC_TUNABLES':'glibc.malloc.hugetlb=1'}
GPU_DIR = Path('/usr/lib/panasonic-on-ubuntu-face-gpu')
GPU_ENABLED = Path('/var/cache/pou-face-gpu/enabled')
LABEL = 'Oh My Panasonic'
LEGACY_LABEL = 'Panasonic On Ubuntu'


def call(argv, timeout=4, check=True):
    return subprocess.run(argv, timeout=timeout, env=FACE_ENV if argv[0]==BACKEND else ENV, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, check=check)


def authenticate(account):
    from .face_gpu import provisioned
    helper = GPU_DIR/'pou-face-gpu'
    profile = provisioned(GPU_ENABLED.parent) if GPU_ENABLED.is_file() and helper.is_file() else None
    if profile is None:
        return call([BACKEND,'auth','-u',account], timeout=12, check=False)
    env = {**FACE_ENV, 'LD_LIBRARY_PATH':f'{GPU_DIR}:{GPU_DIR}/ov',
           'OCL_ICD_VENDORS':profile['driver']}
    import syslog
    start = time.monotonic()
    result = subprocess.run([helper,'auth','-u',account], timeout=12, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    elapsed_ms = round((time.monotonic()-start)*1000)
    syslog.syslog(syslog.LOG_INFO, f'pou-face-gpu exit={result.returncode} elapsed_ms={elapsed_ms}')
    return result


def active_session(uid, locked):
    try:
        result = call(['/usr/libexec/pou-face-session', str(uid), '1' if locked else '0'], check=False)
    except FileNotFoundError:
        return loginctl_session(uid, locked)
    if result.returncode == 2:
        return loginctl_session(uid, locked)
    sid = result.stdout.strip()
    return sid if result.returncode == 0 and sid.isalnum() else None


def loginctl_session(uid, locked):
    sid = call(['/usr/bin/loginctl','show-seat','seat0','-p','ActiveSession','--value']).stdout.strip()
    if not sid or not sid.isalnum():
        return None
    fields = call(['/usr/bin/loginctl','show-session',sid,'-p','User','-p','Remote','-p','Type',
                   '-p','Class','-p','Seat','-p','Active','-p','LockedHint']).stdout.splitlines()
    data = dict(line.split('=',1) for line in fields if '=' in line)
    wanted = dict(User=str(uid), Remote='no', Seat='seat0', Class='user',
                  Active='yes', LockedHint='yes' if locked else 'no')
    return sid if data.get('Type') in ('x11','wayland') and all(data.get(k)==v for k,v in wanted.items()) else None


def enabled(user):
    try:
        hooks = PAM.read_text().splitlines(keepends=True)
        return (USERS/str(user.pw_uid)).read_text().strip() == user.pw_name and (HOOK in hooks or OLD_HOOK in hooks)
    except OSError:
        return False


def status():
    user = pwd.getpwuid(os.getuid())
    from .face_privilege import preferences
    from .face_poses import pose_flag
    return {'supplemented':pose_flag(user.pw_uid).is_file(),'privileges':preferences(user.pw_uid), 'available': Path(BACKEND).is_file(), 'enabled': enabled(user),
            'enrolled': (Path('/var/lib/facelock/enrolled') / user.pw_name).is_file()}


def edit_pam(enable, hook=HOOK):
    text = PAM.read_text()
    lines = text.splitlines(keepends=True)
    clean = ''.join(line for line in lines if line not in (HOOK, OLD_HOOK))
    if enable:
        anchor = '@include common-auth\n'
        if clean.count(anchor) != 1:
            raise RuntimeError('GDM 密码配置已自定义，未能启用解锁。')
        updated = clean.replace(anchor, hook + anchor)
    else:
        updated = clean
    if updated == text:
        return
    original = PAM.stat()
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', dir=PAM.parent, delete=False) as temp:
        path = Path(temp.name)
        try:
            os.fchmod(temp.fileno(), original.st_mode & 0o777)
            os.fchown(temp.fileno(), original.st_uid, original.st_gid)
            temp.write(updated)
            temp.flush()
            os.fsync(temp.fileno())
            if PAM.read_text() != text:
                raise RuntimeError('GDM 配置正在更改，请重试。')
            os.replace(path,PAM)
        finally:
            path.unlink(missing_ok=True)


def upgrade_hook(rollback=False):
    source, target = (HOOK, OLD_HOOK) if rollback else (OLD_HOOK, HOOK)
    if PAM.is_file() and source in PAM.read_text().splitlines(keepends=True):
        with open('/run/lock/pou-face.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if source in PAM.read_text().splitlines(keepends=True):
                edit_pam(True, target)


def gdm_worker_identity():
    """Bind greeter authentication to the real root GDM PAM worker, not env alone."""
    try:
        process=Path('/proc')/str(os.getppid())
        executable=(process/'exe').resolve(strict=True)
        if str(executable) not in ('/usr/libexec/gdm-session-worker','/usr/lib/gdm3/gdm-session-worker','/usr/lib/gdm/gdm-session-worker'):
            return None
        info=executable.stat()
        if info.st_uid!=0 or info.st_mode & (stat.S_IWGRP|stat.S_IWOTH):return None
        fields=dict(line.split(':',1) for line in (process/'status').read_text().splitlines() if ':' in line)
        if fields.get('Uid','').split()!=['0']*4:return None
        # Start time prevents accepting a recycled PID after capture.
        start=(process/'stat').read_text().rsplit(')',1)[1].split()[19]
        return (process.name,start)
    except (OSError,IndexError,ValueError):return None


def greeter_session():
    if os.environ.get('XDG_SEAT')!='seat0':return None
    tty=os.environ.get('PAM_TTY','')
    if not re.fullmatch(r'/dev/tty[1-9][0-9]*',tty):return None
    worker=gdm_worker_identity()
    if worker is None:return None
    sid=call(['/usr/bin/loginctl','show-seat','seat0','-p','ActiveSession','--value']).stdout.strip()
    if not sid or not sid.isalnum():return None
    properties=('Name','User','Remote','Type','Class','Seat','Active','Service','TTY','VTNr')
    args=['/usr/bin/loginctl','show-session',sid]
    for name in properties:args+=['-p',name]
    data=dict(line.split('=',1) for line in call(args).stdout.splitlines() if '=' in line)
    # GNOME 50 uses a transient gdm-greeter account; its UID is not fixed or
    # necessarily below 1000. Validate its name, service and session class.
    wanted=dict(Remote='no',Class='greeter',Seat='seat0',Active='yes',Service='gdm-launch-environment')
    if (data.get('Name') not in ('gdm','Debian-gdm','gdm-greeter') or data.get('User') in (None,'0')
            or data.get('Type') not in ('x11','wayland') or any(data.get(k)!=v for k,v in wanted.items())
            or tty!='/dev/'+data.get('TTY','') or tty!='/dev/tty'+data.get('VTNr','')):
        return None
    return (sid,worker)


def authentication_context(uid):
    sid=active_session(uid,True)
    if sid:return ('lock',sid)
    greeter=greeter_session()
    return ('login',greeter) if greeter else None


def check_locked():
    if (os.geteuid() != 0 or os.environ.get('PAM_SERVICE') != 'gdm-password' or
            os.environ.get('PAM_TYPE') != 'auth' or os.environ.get('PAM_RHOST')):
        return False
    user = pwd.getpwnam(os.environ.get('PAM_USER',''))
    if user.pw_uid < 1000 or not enabled(user):
        return False
    context = authentication_context(user.pw_uid)
    if not context:
        return False
    print('正在尝试人脸解锁，请正对摄像头。', flush=True)
    try:
        # PAM owns the one visible hint on both GDM and the lock screen.
        match = authenticate(user.pw_name)
        if match.returncode == 0 and enabled(user) and authentication_context(user.pw_uid) == context:
            import syslog
            syslog.syslog(syslog.LOG_INFO,f'pou-face context={context[0]} result=success')
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    import syslog
    syslog.syslog(syslog.LOG_INFO,f'pou-face context={context[0]} result=password-fallback')
    print('请使用密码继续。', flush=True)
    return False


def progress(stage, count=None):
    print(json.dumps({'stage':stage, 'count':count}), flush=True)


def app_model(model, user):
    name = model.get('label','')
    return model.get('user') == user and any(name == label or re.fullmatch(re.escape(label) + r' #[0-9a-f]{32}', name)
                                            for label in (LABEL,LEGACY_LABEL))


def guided_command(args, timeout, capture=False):
    """The UI owns stdin. EOF cancels the child, including while it holds the IR camera."""
    if select.select([sys.stdin.buffer], [], [], 0)[0]:
        raise RuntimeError('录入已取消。')
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=FACE_ENV)
    pending = b''
    end = time.monotonic() + timeout
    try:
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('识别超时，请重新对准摄像头。')
            ready, _, _ = select.select([sys.stdin.buffer, proc.stdout], [], [], remaining)
            if sys.stdin.buffer in ready:
                raise RuntimeError('录入已取消。')
            if proc.stdout not in ready:
                continue
            part = os.read(proc.stdout.fileno(), 8192)
            if not part:
                break
            lines = (pending + part).split(b'\n')
            pending = lines.pop()[-4096:]
            for line in lines if capture else ():
                if b'accepted enrollment frame' in line:
                    plain = re.sub(rb'\x1b\[[0-9;]*m', b'', line)
                    count = re.search(rb'\bcount=(\d+)\b', plain)
                    if count:
                        progress('capture', min(10, int(count[1])))
                elif b'skipping low-quality enrollment frame' in line:
                    for cue,stage in [(b'Move closer','near'),(b'Move further','far'),(b'lighting','light'),
                                      (b'Hold still','still'),(b'Center','center')]:
                        if cue in line:
                            progress(stage)
                            break
                elif b'no face detected during enroll' in line:
                    progress('center')
                elif b'multiple faces detected' in line:
                    progress('single')
        if proc.wait(timeout=max(.1, end - time.monotonic())) != 0:
            raise RuntimeError('未通过人脸检查，请正对摄像头并轻微转头后重试。')
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proc.stdout.close()


def models(user):
    return json.loads(call([BACKEND,'list','-u',user,'--json']).stdout)


def manage(action):
    from . import face_privilege as privilege
    choices = {f'{service}-{mode}':(service, mode=='on') for service in privilege.SERVICES for mode in ('on','off')}
    if os.geteuid() or action not in (*choices,'restore-poses','supplement','enroll','guided','enable','disable','forget'):
        raise RuntimeError('此操作需要本机账户认证。')
    uid = int(os.environ['PKEXEC_UID'])
    user = pwd.getpwuid(uid)
    sid = active_session(uid, False)
    if uid < 1000 or not sid:
        raise RuntimeError('请在自己的已解锁桌面操作。')
    USERS.mkdir(mode=0o711, parents=True, exist_ok=True)
    marker = USERS/str(uid)
    with open('/run/lock/pou-face.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        if action in choices:
            service, allow = choices[action]
            if allow and not enabled(user):
                raise RuntimeError('请先启用本账户的人脸解锁。')
            privilege.configure(uid, service, allow)
            return
        if action in ('disable','forget'):
            for service in privilege.preferences(uid):
                privilege.configure(uid, service, False)
            marker.unlink(missing_ok=True)
            if not any(USERS.iterdir()):
                edit_pam(False)
            if action=='forget':
                from .face_poses import pose_flag
                pose_flag(uid).unlink(missing_ok=True)
                for model in models(user.pw_name):
                    if app_model(model,user.pw_name):
                        call([BACKEND,'remove',str(int(model['id'])),'-u',user.pw_name,'-y'])
            return
        if action=='restore-poses':
            from .face_poses import restore
            restore(user)
            return
        if action=='supplement':
            from .face_poses import supplement
            supplement(user,sid)
            return
        if action=='guided':
            if models(user.pw_name):
                raise RuntimeError('已有本账户的录入。重新采集前请先删除旧录入。')
            import uuid
            attempt = LABEL + ' #' + uuid.uuid4().hex
            try:
                progress('capture', 0)
                guided_command([BACKEND,'-vv','enroll','--skip-setup-check','-u',user.pw_name,'-l',attempt], 70, True)
                if not any(m['label'] == attempt and m['user'] == user.pw_name for m in models(user.pw_name)):
                    raise RuntimeError('未找到本次采集结果。')
                progress('verify')
                guided_command([BACKEND,'auth','-u',user.pw_name], 20)
                if active_session(uid,False) != sid or select.select([sys.stdin.buffer], [], [], 0)[0]:
                    raise RuntimeError('已取消启用。')
                enable_user(marker,user)
            except BaseException:
                for model in models(user.pw_name):
                    if model['label'] == attempt and model['user'] == user.pw_name:
                        call([BACKEND,'remove',str(int(model['id'])),'-u',user.pw_name,'-y'])
                raise
            return
        if action=='enroll':
            call([BACKEND,'enroll','--skip-setup-check','-u',user.pw_name,'-l',LABEL],timeout=60)
        if not any(app_model(m,user.pw_name) for m in models(user.pw_name)):
            raise RuntimeError('请先录入人脸。')
        if call([BACKEND,'auth','-u',user.pw_name],timeout=20,check=False).returncode:
            raise RuntimeError('本次识别未通过，请正对摄像头后重试。')
        if active_session(uid,False) != sid:
            raise RuntimeError('桌面会话已切换，请重试。')
        enable_user(marker,user)


def enable_user(marker,user):
    edit_pam(True)
    marker.write_text(user.pw_name+'\n')
    marker.chmod(0o644)

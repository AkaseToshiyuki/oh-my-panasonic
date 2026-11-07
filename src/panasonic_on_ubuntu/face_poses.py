"""Supplement four poses without increasing the number of active comparisons."""
from contextlib import closing
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import tomllib

from . import face

CONFIG = Path('/etc/facelock/config.toml')
DATABASE = Path('/var/lib/facelock/facelock.db')
VERIFIER = '/usr/libexec/pou-pose-verify'


def check_session(uid, sid):
    if face.active_session(uid, False) != sid or select.select([sys.stdin.buffer], [], [], 0)[0]:
        raise RuntimeError('录入已取消。')


def templates(db, user):
    models = db.execute('SELECT id,user,label,embedder_model,device_id FROM face_models WHERE user=?',(user,)).fetchall()
    if len(models) != 1:
        raise RuntimeError('补录需要一个本应用的人脸模板。')
    model, name, label, embedder, device = models[0]
    if not face.app_model(dict(user=name,label=label), user) or not embedder:
        raise RuntimeError('模板的摄像头或模型无法确认。')
    rows = db.execute('SELECT id,sealed,embedding FROM face_embeddings WHERE model_id=? ORDER BY id',(model,)).fetchall()
    if len(rows) != 10 or not all(sealed == 1 and len(blob) == 2077 and blob[0] == 2 for _,sealed,blob in rows):
        raise RuntimeError('此模板不支持安全补录。')
    return (model, embedder, device), [(key,blob) for key,_,blob in rows]


def replace_poses(db, user, original, candidate):
    identity, rows = original
    other, samples = candidate
    if identity[1:] != other[1:]:
        raise RuntimeError('摄像头或识别模型已变化。')
    replacements = [samples[i][1] for i in (1,3,6,8)]
    encrypted = b''.join([blob for _,blob in rows[:6]] + replacements)
    check = subprocess.run([VERIFIER, identity[2] or ''],input=encrypted,env=face.ENV,timeout=10,check=False)
    if check.returncode:
        raise RuntimeError('新姿态与原录入不够接近，请减小转头角度后重试。')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('BEGIN IMMEDIATE')
    if templates(db,user) != original:
        raise RuntimeError('录入已更改，请重新开始。')
    db.execute('CREATE TABLE IF NOT EXISTS pou_original_poses (id INTEGER PRIMARY KEY REFERENCES face_embeddings(id) ON DELETE CASCADE, embedding BLOB NOT NULL)')
    for (key,old), blob in zip(rows[6:],replacements,strict=True):
        db.execute('INSERT OR IGNORE INTO pou_original_poses VALUES(?,?)',(key,old))
        db.execute('UPDATE face_embeddings SET embedding=? WHERE id=?',(blob,key))


def supplement(user, sid):
    text = CONFIG.read_text()
    config = tomllib.loads(text)
    if (config.get('storage') or text.splitlines().count('[storage]') != 1
            or config.get('daemon',{}).get('mode') != 'oneshot'
            or not config.get('security',{}).get('require_ir')
            or not config.get('security',{}).get('require_landmark_liveness')
            or not str(config.get('device',{}).get('path','')).startswith('/dev/v4l/by-path/')
            or config.get('encryption',{}).get('method') != 'tpm' or not face.enabled(user)):
        raise RuntimeError('现有识别配置不支持补录。')
    name = user.pw_name
    with closing(sqlite3.connect(f'file:{DATABASE}?mode=rw',uri=True)) as db:
        original = templates(db,name)
        face.progress('original')
        face.guided_command([face.BACKEND,'auth','-u',name],20)
        with tempfile.TemporaryDirectory(prefix='pou-poses-',dir='/run') as temporary:
            stage = Path(temporary)
            storage = stage/'facelock.db'
            cfg = stage/'config.toml'
            cfg.write_text(text.replace('[storage]\n',f'[storage]\ndb_path = "{storage}"\n',1))
            backend = [face.BACKEND,'--config',str(cfg)]
            face.progress('capture',0)
            face.guided_command([*backend,'-vv','enroll','--skip-setup-check','-u',name,'-l',face.LABEL],70,True)
            face.progress('verify')
            face.guided_command([*backend,'auth','-u',name],20)
            with closing(sqlite3.connect(f'file:{storage}?mode=ro',uri=True)) as staged:
                candidate = templates(staged,name)
            check_session(user.pw_uid,sid)
            with db:
                replace_poses(db,name,original,candidate)
                check_session(user.pw_uid,sid)


        flag = pose_flag(user.pw_uid)
        flag.parent.mkdir(mode=0o711,parents=True,exist_ok=True)
        flag.touch(mode=0o644)


def pose_flag(uid):
    return Path('/var/lib/panasonic-on-ubuntu/supplemented')/str(uid)


def restore(user):
    with closing(sqlite3.connect(f'file:{DATABASE}?mode=rw',uri=True)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        _, rows = templates(db,user.pw_name)
        for key,_ in rows[6:]:
            saved = db.execute('SELECT embedding FROM pou_original_poses WHERE id=?',(key,)).fetchone()
            if not saved:
                raise RuntimeError('原始姿态副本不完整。')
            db.execute('UPDATE face_embeddings SET embedding=? WHERE id=?',(saved[0],key))
            db.execute('DELETE FROM pou_original_poses WHERE id=?',(key,))
    pose_flag(user.pw_uid).unlink(missing_ok=True)

"""Optical capabilities and media metadata, without powering or mounting devices."""
import json
import os
from pathlib import Path
import subprocess
import time
from gi.repository import Gio,GLib

FORMATS={'CD':'CD-ROM','CD_R':'CD-R','CD_RW':'CD-RW','DVD':'DVD-ROM','DVD_R':'DVD-R',
         'DVD_RW':'DVD-RW','DVD_RAM':'DVD-RAM','DVD_PLUS_R':'DVD+R','DVD_PLUS_RW':'DVD+RW',
         'DVD_R_DL':'DVD-R DL','DVD_PLUS_R_DL':'DVD+R DL','BD':'Blu-ray','BD_R':'BD-R','BD_RE':'BD-RE'}
REWRITABLE={'CD_RW','DVD_RW','DVD_RAM','DVD_PLUS_RW','BD_RE'}
RECORDABLE=REWRITABLE|{'CD_R','DVD_R','DVD_PLUS_R','DVD_R_DL','DVD_PLUS_R_DL','BD_R'}


def parse(text):
    return dict(line.removeprefix('E:').split('=',1) for line in text.splitlines()
                if line.removeprefix('E:').startswith(('ID_CDROM','ID_FS_TYPE=')) and '=' in line)


def capacity(value):
    if value is None:return None
    if value==0:return '0 B'
    return f'{value/1e9:.2f} GB' if value>=1e9 else f'{value/1e6:.1f} MB'


def describe(properties,drive,block,mounted=None):
    caps=[label for key,label in FORMATS.items() if properties.get('ID_CDROM_'+key)=='1']
    present=drive.get('MediaAvailable')
    if 'ID_CDROM_MEDIA' in properties:present=properties['ID_CDROM_MEDIA']=='1'
    kinds=[key for key in FORMATS if properties.get('ID_CDROM_MEDIA_'+key)=='1']
    kind=next((k for k in reversed(kinds)),None)
    state=properties.get('ID_CDROM_MEDIA_STATE','')
    blank=state=='blank' or drive.get('OpticalBlank',False)
    audio=int(properties.get('ID_CDROM_MEDIA_TRACK_COUNT_AUDIO',drive.get('OpticalNumAudioTracks',0)) or 0)
    data=int(properties.get('ID_CDROM_MEDIA_TRACK_COUNT_DATA',drive.get('OpticalNumDataTracks',0)) or 0)
    attributes=[FORMATS[kind]] if kind else []
    if blank:attributes.append('空白盘')
    elif audio and data:attributes.append('混合光盘')
    elif audio:attributes.append('音频 CD')
    elif data:attributes.append('数据光盘')
    filesystem=block.get('IdType') or properties.get('ID_FS_TYPE')
    if filesystem:attributes.append(filesystem.upper())
    size=(drive.get('Size') or block.get('Size') or None) if present else None
    used=0 if present and blank else None
    if mounted and present:
        size=size or mounted['total'];used=mounted['used']
    access=None
    if present:
        access='空白盘' if blank else '可读取'
        if kind in RECORDABLE and properties.get('ID_CDROM_'+kind)=='1':
            if kind in REWRITABLE:access+=' · 可擦写'
            elif state in ('blank','appendable'):access+=' · 可刻录' if blank else ' · 可追加'
            elif state=='complete':access+=' · 已封盘'
        elif kind in ('CD','DVD','BD'):access+=' · 只读'
    return dict(capabilities=' / '.join(caps) or None,
                media='有光盘' if present else '无光盘' if present is False else None,
                attributes=(' · '.join(attributes) or None) if present else None,
                capacity=capacity(size) if present else None,used=capacity(used) if present else None,access=access)


def collect(optical):
    cache_path=Path(os.environ.get('XDG_CACHE_HOME',Path.home()/'.cache'))/'panasonic-on-ubuntu/optical-info.json'
    try:cache=json.loads(cache_path.read_text())
    except (OSError,ValueError):cache={}
    if cache.get('model')!=optical.get('model'):cache={}
    cached=cache.get('properties',{})
    result=describe({k:v for k,v in cached.items() if not k.startswith('ID_CDROM_MEDIA')},{},{})
    if not optical.get('block_present') or optical.get('powered') is False:
        result.update(media=None,attributes=None,capacity=None,used=None,access=None)
        return result
    from .optical import drives
    devices=drives()
    if not devices:return result
    device=devices[0]
    node='/dev/'+device.name
    try:
        bus=Gio.bus_get_sync(Gio.BusType.SYSTEM,None)
        objects=bus.call_sync('org.freedesktop.UDisks2','/org/freedesktop/UDisks2',
            'org.freedesktop.DBus.ObjectManager','GetManagedObjects',None,None,Gio.DBusCallFlags.NO_AUTO_START,1500,None).unpack()[0]
    except GLib.Error:objects={}
    block={};drive={};fs={}
    for interfaces in objects.values():
        value=interfaces.get('org.freedesktop.UDisks2.Block',{})
        if bytes(value.get('Device',[])).rstrip(b'\0').decode(errors='replace')==node:
            block=value;drive=objects.get(value.get('Drive'),{}).get('org.freedesktop.UDisks2.Drive',{})
            fs=interfaces.get('org.freedesktop.UDisks2.Filesystem',{});break
    try:
        udev=Path('/run/udev/data')/('b'+(device/'dev').read_text().strip())
        properties=parse(udev.read_text());stamp=udev.stat().st_mtime_ns
    except OSError:properties={};stamp=0
    key=[optical.get('model'),stamp,drive.get('TimeMediaDetected'),drive.get('MediaAvailable')]
    if cache.get('key')==key and cached:
        properties={**properties,**cached}
    elif time.time()-cache.get('attempt',0)>30 or cache.get('key')!=key:
        try:
            response=subprocess.run(['/usr/lib/udev/cdrom_id',node],capture_output=True,text=True,timeout=3)
            if response.returncode==0:properties.update(parse(response.stdout))
        except (OSError,subprocess.TimeoutExpired):pass
        # Cache only non-identifying drive/media properties, not serials or labels.
        try:
            cache_path.parent.mkdir(parents=True,exist_ok=True)
            value=dict(model=optical.get('model'),key=key,attempt=time.time(),properties=properties)
            temporary=cache_path.with_suffix('.new');temporary.write_text(json.dumps(value));temporary.chmod(0o600);temporary.replace(cache_path)
        except OSError:pass
    mounted=None
    for raw in fs.get('MountPoints',[]):
        try:
            path=bytes(raw).rstrip(b'\0').decode()
            info=os.statvfs(path)
            mounted={'total':info.f_blocks*info.f_frsize,'used':(info.f_blocks-info.f_bfree)*info.f_frsize};break
        except (OSError,UnicodeError):continue
    return describe(properties,drive,block,mounted)

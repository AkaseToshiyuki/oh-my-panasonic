"""User-session Wi-Fi activation with NetworkManager-owned secrets and rollback."""
import os
import pwd
import uuid
import gi
gi.require_version('NM','1.0')
from gi.repository import NM, GLib
SECURITY=getattr(NM,'80211ApSecurityFlags')


def security(ap):
    flags=int(ap.get_rsn_flags()) | int(ap.get_wpa_flags())
    if flags & int(SECURITY.KEY_MGMT_802_1X):return 'enterprise'
    if flags & int(SECURITY.KEY_MGMT_SAE):return 'sae'
    if flags & int(SECURITY.KEY_MGMT_PSK):return 'wpa-psk'
    if flags & int(SECURITY.KEY_MGMT_OWE):return 'owe'
    return 'unsupported' if ap.get_flags() or flags else 'open'


def ssid(ap):
    value=ap.get_ssid()
    return bytes(value.get_data()) if value else b''


def title(ap):
    return NM.utils_ssid_to_utf8(ssid(ap)) or '隐藏网络'


def password_valid(kind, value):
    if kind in ('open','owe'):return not value
    if '\x00' in value:return False
    if kind=='sae':return 1<=len(value.encode('utf-8'))<=63
    if kind=='wpa-psk':
        return (8<=len(value)<=63 and value.isascii()) or (len(value)==64 and all(c in '0123456789abcdefABCDEF' for c in value))
    return False


def profile(ap,password='',remember=True,username=None):
    return personal_profile(ssid(ap),security(ap),password,remember,username)


def personal_profile(raw,kind,password='',remember=True,username=None,hidden=False):
    if not isinstance(raw,bytes) or not 1<=len(raw)<=32 or b'\x00' in raw:raise ValueError('网络名称需为 1–32 字节')
    if not password_valid(kind,password):raise ValueError('密码格式无效，或此网络需要系统认证设置')
    connection=NM.SimpleConnection.new()
    setting=NM.SettingConnection.new()
    setting.props.id=NM.utils_ssid_to_utf8(raw);setting.props.uuid=str(uuid.uuid4())
    setting.props.type='802-11-wireless';setting.props.autoconnect=bool(remember)
    setting.add_permission('user',username or pwd.getpwuid(os.getuid()).pw_name,None)
    connection.add_setting(setting)
    wireless=NM.SettingWireless.new();wireless.props.ssid=GLib.Bytes.new(raw);wireless.props.mode='infrastructure'
    wireless.props.hidden=bool(hidden)
    connection.add_setting(wireless)
    if kind!='open':
        secure=NM.SettingWirelessSecurity.new();secure.props.key_mgmt=kind
        if kind in ('sae','wpa-psk'):secure.props.psk=password
        if kind in ('sae','owe'):secure.props.pmf=NM.SettingWirelessSecurityPmf.REQUIRED
        connection.add_setting(secure)
    for cls in (NM.SettingIP4Config,NM.SettingIP6Config):
        setting=cls.new();setting.props.method='auto';connection.add_setting(setting)
    connection.verify()
    return connection


class Activation:
    """Owns only this device's checkpoint; success requires ACTIVATED, not ack."""
    def __init__(self,client,device,ap,connection,new,remember,changed,finished):
        self.client,self.device,self.ap=client,device,ap
        self.connection,self.new,self.remember=connection,new,remember
        self.changed,self.finished=changed,finished
        self.checkpoint=self.active=None
        self.timer=self.handler=0
        self.cancelled=False;self.ending=False;self.done=False;self.phase='prepare'
        self.inflight=False;self.abort_message=None

    def start(self):
        self.changed('正在准备连接…')
        self.client.checkpoint_create([self.device],90,NM.CheckpointCreateFlags.NONE,None,self.prepared)

    def prepared(self,client,result,*_):
        try:self.checkpoint=client.checkpoint_create_finish(result).get_path()
        except GLib.Error:
            self.finish(False,'无法准备网络恢复，请重试或打开系统网络设置');return
        if self.cancelled:self.fail('已取消');return
        self.phase='activate';self.changed('正在连接…')
        self.timer=GLib.timeout_add_seconds(70,self.expired)
        self.inflight=True
        if self.new:
            options=GLib.Variant('a{sv}',{'persist':GLib.Variant('s','volatile')})
            client.add_and_activate_connection2(self.connection,self.device,self.ap.get_path() if self.ap else None,options,None,self.started)
        else:client.activate_connection_async(self.connection,self.device,self.ap.get_path() if self.ap else None,None,self.started)

    def started(self,client,result,*_):
        self.inflight=False
        # No password is copied into process arguments, logs, or application files.
        if self.new and self.connection:self.connection.clear_secrets()
        self.connection=None
        try:
            self.active=client.add_and_activate_connection2_finish(result)[0] if self.new else client.activate_connection_finish(result)
        except GLib.Error:
            if not self.ending:self.fail(self.abort_message or '连接未建立，请检查密码或网络状态')
            return
        if self.ending or self.cancelled:
            self.fail(self.abort_message or '已取消');return
        self.handler=self.active.connect('state-changed',self.state_changed)
        self.state_changed(self.active,self.active.get_state(),0)

    def state_changed(self,active,state,reason):
        if self.ending:return
        if state==NM.ActiveConnectionState.ACTIVATED:
            self.ending=True;self.stop_watch()
            self.phase='commit';self.changed('正在保存连接…')
            self.client.checkpoint_destroy(self.checkpoint,None,self.committed)
        elif state in (NM.ActiveConnectionState.DEACTIVATED,NM.ActiveConnectionState.DEACTIVATING):
            self.fail('连接失败，请检查密码、信号或路由器')

    def committed(self,client,result,*_):
        try:client.checkpoint_destroy_finish(result)
        except GLib.Error:
            self.ending=False;self.fail('未能确认连接，正在恢复原网络');return
        self.checkpoint=None
        if self.new and self.remember:
            remote=self.active.get_connection()
            if remote:
                remote.update2(None,NM.SettingsUpdate2Flags.TO_DISK,None,None,self.saved);return
            self.finish(True,'已连接；未能保存，请在系统设置中保存网络');return
        self.finish(True,'已连接')

    def saved(self,remote,result,*_):
        try:remote.update2_finish(result);message='已连接'
        except GLib.Error:message='已连接；未能保存，请在系统设置中保存网络'
        self.finish(True,message)

    def cancel(self):
        if self.done or self.phase=='commit':return
        self.cancelled=True
        if self.checkpoint:self.fail('已取消')

    def expired(self):
        self.timer=0;self.fail('连接超时');return False

    def fail(self,message):
        if self.ending or self.done:return
        if self.inflight:
            # NM authorizes AddAndActivate asynchronously. Rolling back before its
            # reply permits the late activation to replace the restored network.
            self.cancelled=True;self.abort_message=message
            self.changed('正在取消连接…');return
        self.ending=True;self.stop_watch();self.changed('正在恢复原连接…')
        if not self.checkpoint:self.finish(False,message);return
        def restored(client,result,*_):
            try:
                values=client.checkpoint_rollback_finish(result)
                if any(value!=0 for value in values.values()):raise ValueError('rollback')
                suffix='，原网络配置已恢复'
            except (GLib.Error,ValueError):suffix='；恢复尚未确认，请检查系统网络设置'
            self.checkpoint=None;self.finish(False,message+suffix)
        self.client.checkpoint_rollback(self.checkpoint,None,restored)

    def stop_watch(self):
        if self.timer:GLib.source_remove(self.timer);self.timer=0
        if self.handler and self.active:self.active.disconnect(self.handler);self.handler=0

    def finish(self,ok,message):
        if self.done:return
        self.done=True;self.stop_watch()
        if self.new and self.connection:self.connection.clear_secrets()
        self.connection=None
        self.finished(ok,message)

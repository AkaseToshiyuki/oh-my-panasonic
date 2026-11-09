"""One resident icon, with native Linux controls grouped into submenus."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid
import gi

gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, GLib, AyatanaAppIndicator3 as AppIndicator
from .battery import snapshot
from .firmware import status as firmware_status, fan_label
from .desktop import backlight


def query(argv):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
        return result.stdout.strip() if result.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''


class Tray:
    def __init__(self):
        self.busy = False
        self.indicator = AppIndicator.Indicator.new('panasonic-on-ubuntu', 'pou-symbolic', AppIndicator.IndicatorCategory.HARDWARE)
        self.indicator.set_title('Oh My Panasonic')
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.summary = self.item(self.menu, '读取电池…', sensitive=False)
        self.menu.append(Gtk.SeparatorMenuItem())
        battery = self.submenu('电源与电池')
        self.charge_status = self.item(battery, '充电状态', sensitive=False)
        self.watts = self.item(battery, '电池功率', sensitive=False)
        self.supply = self.item(battery, '供电接口', sensitive=False)
        self.contract = self.item(battery, 'PD 协商档位', sensitive=False)
        self.pdo_choices = self.item(battery, '适配器固定档位', sensitive=False)
        self.input_power = self.item(battery, '接口实测输入功率', sensitive=False)
        self.care = self.item(battery, '电池保养状态', sensitive=False)
        for title,action in [('立刻充满','full'),('恢复系统电池管理','system')]:
            self.item(battery,title,lambda a=action:self.control('battery',a))
        self.item(battery, '电量历史与详情', lambda: self.open_app(['/usr/bin/pou', 'gui']))
        power = self.submenu('电源策略')
        self.profile = self.item(power, '电源模式', sensitive=False)
        self.profiles = {}
        for value, label in [('power-saver', '省电'), ('balanced', '均衡'), ('performance', '性能')]:
            self.profiles[value] = (self.item(power, label, lambda v=value: self.control('profile', v)), label)
        power.append(Gtk.SeparatorMenuItem())
        self.fan = self.item(power, '风扇模式', sensitive=False)
        self.fan_actions = []
        for value, label in [('standard', '标准自动曲线'), ('quiet', '静音自动曲线'), ('restore', '恢复启动时的风扇曲线')]:
            self.fan_actions.append(self.item(power, label, lambda v=value: self.control('fan', v)))
        self.performance_fan = self.item(power, '性能风扇曲线', lambda: self.control('fan', 'performance'))
        self.limits = self.item(power, '动态功率限制', sensitive=False)
        for value, label in [('eco','CPU 极致省电 6 W'),('balanced','CPU 日常均衡 12 W'),('performance','CPU 性能 20 W'),('adaptive','CPU 温控性能 · 90°C 目标'),('boost','CPU 增强性能 · 25 W'),('system','CPU 恢复系统管理')]:
            self.item(power, label, lambda v=value: self.control('cpu', v))
        power.append(Gtk.SeparatorMenuItem())
        optical = power
        self.optical = self.item(optical, '读取光驱…', sensitive=False)
        self.item(optical, '开启光驱供电', lambda: self.control('optical','on'))
        self.item(optical, '关闭并启用默认省电', lambda: self.control('optical','auto'))
        self.settings(power, 'Ubuntu 电源设置', 'power')
        pad = self.submenu('触控板')
        self.pad = self.item(pad, '圆形滚动', sensitive=False)
        self.item(pad, '启用圆形滚动', lambda: self.control('enable'))
        self.item(pad, '恢复原生触控板', lambda: self.control('disable'))
        self.item(pad, '滚动速度与方向…', lambda: self.open_app(['/usr/bin/pou', 'gui']))
        self.settings(pad, '指针速度、轻触与手势', 'mouse')
        sound = self.submenu('声音')
        self.volume = self.item(sound, '输出音量', sensitive=False)
        self.item(sound, '拖动音量滑块…', lambda: self.open_app(['/usr/bin/pou','gui']))
        for label, args in [('切换扬声器静音', ['set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle']),
                            ('切换麦克风静音', ['set-mute', '@DEFAULT_AUDIO_SOURCE@', 'toggle'])]:
            self.command(sound, label, ['wpctl', *args])
        self.mic = self.item(sound, '麦克风', sensitive=False)
        self.settings(sound, '输入、输出设备与音量', 'sound')
        network = self.submenu('Wi-Fi 与网络')
        self.wifi = self.item(network, 'Wi-Fi 状态', sensitive=False)
        self.item(network, '开启 Wi-Fi', lambda: self.control('wifi', 'on'))
        self.item(network, '关闭 Wi-Fi', lambda: self.control('wifi', 'off'))
        self.settings(network, '选择 Wi-Fi 网络', 'wifi')
        self.settings(network, '有线网络与 VPN', 'network')
        bluetooth = self.submenu('蓝牙')
        self.bluetooth = self.item(bluetooth, '蓝牙状态', sensitive=False)
        self.item(bluetooth, '开启蓝牙', lambda: self.control('bluetooth', 'on'))
        self.item(bluetooth, '关闭蓝牙', lambda: self.control('bluetooth', 'off'))
        self.settings(bluetooth, '连接与管理设备', 'bluetooth')
        display = self.submenu('显示与亮度')
        self.brightness = self.item(display, '内屏亮度', sensitive=False)
        self.brightness_actions = []
        for label, direction in [('提高亮度 5%', 'up'), ('降低亮度 5%', 'down')]:
            self.brightness_actions.append(self.command(display, label, ['/usr/bin/python3', '-m', 'panasonic_on_ubuntu.desktop', direction]))
        self.settings(display, '缩放、分辨率与夜灯', 'display')
        self.menu.append(Gtk.SeparatorMenuItem())
        self.settings(self.menu, '全部系统设置', None)
        self.item(self.menu, '刷新状态', self.update)
        self.menu.append(Gtk.SeparatorMenuItem())
        for title,argv in [('挂起',['systemctl','suspend']),('关机',['gnome-session-quit','--power-off']),
                           ('重启',['gnome-session-quit','--reboot'])]:
            self.command(self.menu,title,argv)
        self.item(self.menu,'进入面板',lambda:self.open_app(['/usr/bin/pou','gui']))
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.update()
        GLib.timeout_add_seconds(15, self.update)

    def item(self, menu, title, action=None, sensitive=True):
        item = Gtk.MenuItem(label=title)
        item.set_sensitive(sensitive)
        if action:
            item.connect('activate', lambda _: action())
        menu.append(item)
        return item

    def submenu(self, title):
        child = Gtk.Menu()
        self.item(self.menu, title).set_submenu(child)
        return child

    def command(self, menu, title, argv):
        return self.item(menu, title, lambda: self.execute(argv), bool(shutil.which(argv[0])))

    def settings(self, menu, title, panel):
        self.item(menu, title, lambda: self.open_app(['gnome-control-center', *([panel] if panel else [])]), bool(shutil.which('gnome-control-center')))

    def error(self, message):
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text='操作未完成')
        dialog.format_secondary_text(message[:1500])
        dialog.connect('response', lambda d, _: d.destroy())
        dialog.show()
        return False

    def open_app(self, argv):
        try:
            subprocess.Popen(['systemd-run', '--user', '--quiet', '--collect', '--unit=pou-open-' + uuid.uuid4().hex[:8], *argv])
        except OSError as exc:
            self.error(str(exc))

    def execute(self, argv):
        def worker():
            try:
                result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
                if result.returncode:
                    GLib.idle_add(self.error, result.stderr.strip() or result.stdout.strip() or '授权被取消或服务不可用。')
            except (OSError, subprocess.TimeoutExpired) as exc:
                GLib.idle_add(self.error, str(exc))
            GLib.idle_add(self.refresh_once)
        threading.Thread(target=worker, daemon=True).start()

    def control(self, *args):
        self.execute(['/usr/bin/pkexec', '/usr/libexec/pou-control', *args])

    def refresh_once(self):
        self.update()
        return False

    def update(self):
        if self.busy:
            return True
        self.busy = True
        def worker():
            try:
                data = snapshot()
                from .battery import history
                samples = history(hours=0.1)
                values = {name: query(args) for name, args in {
                    'pad': ['systemctl', 'is-active', 'pou-wheelpad.service'],
                    'volume': ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SINK@'],
                    'mic': ['wpctl', 'get-volume', '@DEFAULT_AUDIO_SOURCE@'],
                    'wifi': ['nmcli', 'radio', 'wifi'],
                    'bluetooth': ['bluetoothctl', 'show'],
                }.items()}
                values['battery_samples'] = samples
                from . import power_profiles
                try:
                    values['profile'] = power_profiles.get_active()
                    values['profiles'] = power_profiles.available()
                except power_profiles.ProfileError:
                    values['profile'], values['profiles'] = None, []
                try:
                    _, monitor = backlight()
                    values['brightness'] = round((monitor['value'] - monitor['min']) * 100 / (monitor['max'] - monitor['min']))
                except Exception:
                    values['brightness'] = None
                GLib.idle_add(self.apply_status, data, values, firmware_status())
            except Exception:
                GLib.idle_add(self.failed_update)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def failed_update(self):
        self.busy = False
        self.summary.set_label('状态暂时不可用；点击刷新重试')
        return False

    def apply_status(self, data, values, fw):
        self.busy = False
        from .care import status as care_status
        care=care_status()
        source=data.get('power_input',{})
        from .power_summary import describe
        display = describe(data,care,values.get('battery_samples', []))
        self.supply.set_label(display['source'])
        self.charge_status.set_label(display['status'])
        watts = ('充电功率 · '+display['charge_power'] if display['charge_power'] else
                 '放电功率 · '+display['discharge_power'] if display['discharge_power'] else '')
        self.watts.set_label(watts)
        self.watts.set_visible(bool(watts))
        self.contract.set_label('PD 协商档位 · '+source.get('contract_label','—'))
        self.pdo_choices.set_label('适配器固定档位 · '+source.get('profiles_label','—'))
        self.input_power.set_label('接口实测输入功率 · '+source.get('input_label','硬件未提供'))
        self.care.set_label(f"{care.get('label','电池服务未运行')} · 上限 {care.get('config',{}).get('limit','—')}%")
        if data['batteries']:
            b = data['batteries'][0]
            capacity = b['fields']['capacity']['value']
            percent = str(int(float(capacity))) + '%' if capacity is not None else '—'
            status = b['fields']['status']['value']
            state = {'Charging': '充电中', 'Discharging': '放电中', 'Full': '已充满', 'Not charging': '未充电'}.get(status, status or '未知')
            text = f"{percent} · {display['summary']}"
            self.indicator.set_icon_full('pou-symbolic', text)
            self.indicator.set_label(percent, '100%')
            self.summary.set_label(text)
        else:
            self.indicator.set_label('—', '100%')
            self.summary.set_label('未检测到电池')
        current = values['profile']
        self.profile.set_label('系统电源：' + {'balanced': '均衡', 'performance': '性能', 'power-saver': '省电'}.get(current, '未知'))
        for value, (item, label) in self.profiles.items():
            item.set_label(('✓ ' if current == value else '') + label)
            item.set_sensitive(value in values['profiles'])
        self.pad.set_label('圆形滚动：' + ('运行中' if values['pad'] == 'active' else '未启用'))
        from .power import status as power_status
        active_fan = power_status().get('fan', {})
        name = {'custom':'自定义曲线','performance':'性能曲线','manual':'手动输出'}.get(active_fan.get('mode'),fan_label(fw['fan_profile']))
        feedback = active_fan.get('feedback')
        self.fan.set_label('风扇：' + name + (f' · {feedback}%' if feedback is not None else ''))
        self.performance_fan.set_sensitive(bool(active_fan.get('available')))
        for item in self.fan_actions:
            item.set_sensitive(fw['fan_profile'] in (0, 1))
        self.limits.set_label('固件动态功率限制：' + {0: '关闭', 1: '开启'}.get(fw['power_limits_enabled'], '未知'))
        from .optical import status as optical_status
        optical = optical_status()
        self.optical.set_label('光驱：' + ('已供电' if optical['powered'] else '已断电' if optical['supported'] else '电源控制不可用'))
        self.volume.set_label('输出：' + (values['volume'].replace('Volume:', '音量').replace('[MUTED]', '（静音）') or '不可用'))
        self.mic.set_label('麦克风：' + ('已静音' if '[MUTED]' in values['mic'] else '开启' if values['mic'] else '不可用'))
        self.wifi.set_label('Wi-Fi：' + {'enabled': '开启', 'disabled': '关闭'}.get(values['wifi'], '未知'))
        bt = values['bluetooth']
        self.bluetooth.set_label('蓝牙：' + ('开启' if 'Powered: yes' in bt else '关闭' if 'Powered: no' in bt else '不可用'))
        percent = values['brightness']
        self.brightness.set_label('内屏亮度：' + ('不可用' if percent is None else str(percent) + '%'))
        for item in self.brightness_actions:
            item.set_sensitive(percent is not None)
        return False


def run():
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    with open(runtime / 'pou-tray.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        Tray()
        Gtk.main()

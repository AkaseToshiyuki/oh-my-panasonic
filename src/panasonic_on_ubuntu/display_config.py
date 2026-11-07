"""Mutter owns the confirmation dialog and automatic display restoration."""
import json
import math
from gi.repository import Gio,GLib

NAME='org.gnome.Mutter.DisplayConfig'
PATH='/org/gnome/Mutter/DisplayConfig'


def call(method,args=None):
    bus=Gio.bus_get_sync(Gio.BusType.SESSION,None)
    return bus.call_sync(NAME,PATH,NAME,method,args,None,Gio.DBusCallFlags.NO_AUTO_START,3000,None).unpack()


def read():return call('GetCurrentState')


def layout(state):
    monitors={m[0][0]:m for m in state[1]}
    result=[]
    for x,y,scale,transform,primary,specs,properties in state[2]:
        outputs=[]
        for spec in specs:
            monitor=monitors[spec[0]]
            mode=next(m for m in monitor[1] if m[6].get('is-current'))
            options={}
            if 'is-underscanning' in monitor[2]:options['underscanning']=monitor[2]['is-underscanning']
            if 'color-mode' in monitor[2]:options['color-mode']=monitor[2]['color-mode']
            outputs.append([spec[0],mode[0],options])
        result.append([x,y,scale,transform,primary,outputs])
    return result


def identities(state):return sorted(tuple(m[0]) for m in state[1])


def target(state,connector,mode_id,scale):
    if not math.isfinite(scale):raise ValueError('缩放比例无效')
    monitor=next((m for m in state[1] if m[0][0]==connector),None)
    if not monitor:raise ValueError('显示器已移除，请刷新')
    mode=next((m for m in monitor[1] if m[0]==mode_id),None)
    if not mode or not any(abs(s-scale)<.00001 for s in mode[5]):raise ValueError('此分辨率不支持所选缩放')
    result=layout(state)
    group=next((g for g in result if any(o[0]==connector for o in g[5])),None)
    if not group:raise ValueError('请先在系统设置启用显示器')
    if len(group[5])!=1:raise ValueError('镜像显示请在系统设置中调整')
    if state[3].get('global-scale-required') and any(abs(g[2]-scale)>.00001 for g in result if g is not group):
        raise ValueError('当前系统要求所有显示器使用相同缩放，请使用系统设置')
    group[2]=scale;group[5][0][1]=mode_id
    return result


def apply(state,configuration,method):
    groups=[]
    for x,y,scale,transform,primary,outputs in configuration:
        converted=[]
        for connector,mode,properties in outputs:
            props={k:GLib.Variant('b' if k=='underscanning' else 'u',v) for k,v in properties.items()}
            converted.append((connector,mode,props))
        groups.append((x,y,scale,transform,primary,converted))
    props={}
    if state[3].get('supports-changing-layout-mode'):
        props['layout-mode']=GLib.Variant('u',state[3].get('layout-mode',1))
    call('ApplyMonitorsConfig',GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})',(state[0],method,groups,props)))


def same_layout(first,second):
    # JSON transport changes tuples to lists; scale is a D-Bus double.
    return json.dumps(first,sort_keys=True)==json.dumps(second,sort_keys=True)


def resolve(original,preview):
    current=read()
    if (identities(current)!=identities(original) or not same_layout(layout(current),preview)
            or current[3].get('layout-mode')!=original[3].get('layout-mode')):
        return 'changed'  # Another manager/hotplug owns the new state; never overwrite it.
    apply(current,layout(original),1)
    return 'restored'


class Preview:
    def __init__(self,state,configuration):
        self.original=state;self.configuration=configuration

    def start(self):
        apply(self.original,self.configuration,0)
        # Method 2 asks GNOME Shell for confirmation. Mutter restores after 20 s
        # unless the user confirms; it persists only after that confirmation.
        # This also works when this client exits or hangs, without a second dialog.
        apply(self.original,self.configuration,2)

    def cancel(self):return resolve(self.original,self.configuration)

    def result(self):
        current=read()
        if same_layout(layout(current),layout(self.original)):return 'restored'
        if same_layout(layout(current),self.configuration):return 'active'
        return 'changed'

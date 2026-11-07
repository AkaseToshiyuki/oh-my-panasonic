"""Immediate GNOME night-light controls with bidirectional settings updates."""
from gi.repository import Gtk,Gio


def build(app,page):
    from .dashboard import box,label
    settings=Gio.Settings.new('org.gnome.settings-daemon.plugins.color')
    app.color_settings=settings
    card=app.card(page,'夜灯')
    def toggle(title,key):
        row=box(False);text=label(title);text.set_hexpand(True);row.append(text)
        widget=Gtk.Switch(valign=Gtk.Align.CENTER);row.append(widget);card.append(row)
        settings.bind(key,widget,'active',Gio.SettingsBindFlags.DEFAULT)
        return widget
    toggle('启用夜灯','night-light-enabled')
    toggle('日落至日出','night-light-schedule-automatic')
    manual=box(False,12);card.append(manual)
    updating=False
    times=[]
    for title,key in (('开始','night-light-schedule-from'),('结束','night-light-schedule-to')):
        column=box();column.set_hexpand(True);column.append(label(title,'pou-note'))
        row=box(False,6)
        hour=Gtk.SpinButton.new_with_range(0,23,1);minute=Gtk.SpinButton.new_with_range(0,59,1)
        hour.set_hexpand(True);minute.set_hexpand(True)
        for widget in (hour,minute):
            widget.connect('output',lambda w:w.set_text(f'{w.get_value_as_int():02d}') or True)
        row.append(hour);row.append(label(':'));row.append(minute);column.append(row);manual.append(column)
        def changed(_,h=hour,m=minute,k=key):
            if not updating:settings.set_double(k,h.get_value_as_int()+m.get_value_as_int()/60)
        hour.connect('value-changed',changed);minute.connect('value-changed',changed)
        times.append((key,hour,minute))
    reading=label('','pou-reading');card.append(reading)
    scale=Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,1700,6500,100)
    scale.set_draw_value(False);scale.set_hexpand(True);card.append(scale)
    def temperature(widget):
        if updating:return
        value=round(widget.get_value()/100)*100
        reading.set_label(f'{value} K')
        settings.set_uint('night-light-temperature',value)
    scale.connect('value-changed',temperature)
    def refresh(*_):
        nonlocal updating
        updating=True
        try:
            value=settings.get_uint('night-light-temperature')
            reading.set_label(f'{value} K');scale.set_value(value)
            for key,hour,minute in times:
                minutes=round(settings.get_double(key)*60)%1440
                hour.set_value(minutes//60);minute.set_value(minutes%60)
            manual.set_sensitive(not settings.get_boolean('night-light-schedule-automatic'))
        finally:updating=False
    settings.connect('changed',refresh);refresh()
    app.night_controls=(scale,reading,times)

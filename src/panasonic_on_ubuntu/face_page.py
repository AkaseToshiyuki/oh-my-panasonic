"""Local colour viewfinder and IR enrollment."""
import json
import math
from pathlib import Path
import subprocess
import threading
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk


def preview_pipeline():
    from .camera import colour_camera
    return (f'v4l2src device={colour_camera()} ! video/x-raw,width=320,height=240 ! '
            'videoflip method=horizontal-flip ! videoconvert ! video/x-raw,format=RGB ! '
            'appsink name=view max-buffers=1 drop=true sync=false')


class Enrollment:
    def __init__(self, app, card):
        self.app = app
        self.pipeline = self.sink = self.worker = None
        self.timer = self.generation = self.frames = 0
        self.bus = self.subscription = None
        self.start_button = Gtk.Button(label='录入并启用', hexpand=True)
        self.start_button.connect('clicked', lambda *_: self.open())
        card.append(self.start_button)
        self.panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, visible=False)
        card.append(self.panel)
        frame = Gtk.AspectFrame(ratio=4/3, obey_child=False, halign=Gtk.Align.CENTER)
        frame.set_size_request(320, 240)
        overlay = Gtk.Overlay()
        frame.set_child(overlay)
        self.panel.append(frame)
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
        overlay.set_child(self.picture)
        guide = Gtk.DrawingArea(can_target=False)
        guide.set_draw_func(self.draw_guide)
        overlay.add_overlay(guide)
        self.note = Gtk.Label(xalign=0, wrap=True)
        self.panel.append(self.note)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        self.panel.append(self.progress)
        row = Gtk.Box(spacing=8)
        self.panel.append(row)
        self.capture = Gtk.Button(label='开始采集', hexpand=True, css_classes=['suggested-action'])
        self.capture.connect('clicked', lambda *_: self.begin())
        row.append(self.capture)
        cancel = Gtk.Button(label='取消', hexpand=True)
        cancel.connect('clicked', lambda *_: self.close())
        row.append(cancel)
        self.panel.connect('unmap', lambda *_: self.close() if self.panel.get_visible() else None)
        app.stack.connect('notify::visible-child-name', self.page_changed)
        app.window.connect('close-request', self.window_closed)

    @staticmethod
    def draw_guide(_area, cr, width, height):
        cr.save()
        cr.translate(width/2, height/2)
        cr.scale(width*.225, height*.415)
        cr.arc(0, 0, 1, 0, 2*math.pi)
        cr.restore()
        cr.set_line_width(4)
        cr.set_source_rgba(0, 0, 0, .65)
        cr.stroke_preserve()
        cr.set_line_width(2)
        cr.set_source_rgb(.72, 1, .86)
        cr.stroke()

    def page_changed(self, stack, _prop):
        if stack.get_visible_child_name() != 'face' and self.panel.get_visible():
            self.close()

    def window_closed(self, *_):
        self.close()
        return False

    def open(self):
        if self.panel.get_visible() or self.worker is not None:
            return
        self.generation += 1
        self.frames = 0
        self.supplement = bool(self.app.get('face','enrolled') or self.app.get('face','enabled'))
        self.capture.set_visible(not self.supplement or bool(self.app.get('face','enabled')))
        self.capture.set_label('开始补录' if self.supplement else '开始采集')
        self.start_button.set_sensitive(False)
        self.panel.set_visible(True)
        self.progress.set_visible(False)
        self.capture.set_sensitive(False)
        self.note.set_label('正在打开摄像头…')
        try:
            gi.require_version('Gst', '1.0')
            from gi.repository import Gst
            self.gst = Gst
            Gst.init(None)
            self.pipeline = Gst.parse_launch(preview_pipeline())
            self.sink = self.pipeline.get_by_name('view')
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError('camera')
            self.opened = time.monotonic()
            self.timer = GLib.timeout_add(85, self.frame)
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.subscription = self.bus.signal_subscribe(
                'org.gnome.ScreenSaver', 'org.gnome.ScreenSaver', 'ActiveChanged',
                '/org/gnome/ScreenSaver', None, Gio.DBusSignalFlags.NONE,
                lambda *args: self.close() if args[5].unpack()[0] else None)
        except (ImportError, ValueError, GLib.Error, RuntimeError):
            self.close('摄像头预览不可用，请关闭占用它的应用后重试。')

    def frame(self):
        sample = self.sink.emit('try-pull-sample', 0)
        if sample:
            buf = sample.get_buffer()
            pixels = GLib.Bytes.new(buf.extract_dup(0, buf.get_size()))
            self.picture.set_paintable(Gdk.MemoryTexture.new(320, 240, Gdk.MemoryFormat.R8G8B8, pixels, 960))
            if not self.frames:
                self.capture.set_sensitive(True)
                self.note.set_label('在常用光照下对准椭圆，准备好后开始补录。' if self.supplement else
                                    '让脸部位于椭圆中央，双眼平视摄像头，然后开始采集。')
            self.frames += 1
            self.opened = time.monotonic()
        elif time.monotonic() - self.opened > 4:
            self.timer = 0
            self.close('未收到摄像头画面，请检查摄像头后重试。')
            return False
        return True

    def begin(self):
        if self.worker is not None or not self.frames:
            return
        self.capture.set_sensitive(False)
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self.progress.set_text('等待账户认证')
        self.note.set_label('请完成本机账户认证。')
        try:
            proc = subprocess.Popen(['/usr/bin/pkexec','/usr/libexec/pou-face-manage','supplement' if self.supplement else 'guided'],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except OSError:
            self.close('无法打开账户认证。')
            return
        self.worker = proc
        generation = self.generation

        def read_events():
            message = ''
            with proc.stdout:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        message = line.strip()[:200]
                        continue
                    if isinstance(event, dict):
                        GLib.idle_add(self.event, generation, event)
            result = proc.wait()
            GLib.idle_add(self.finished, generation, proc, result, message)
        threading.Thread(target=read_events, daemon=True).start()

    def event(self, generation, event):
        if generation != self.generation:
            return False
        stage = event.get('stage')
        if stage == 'capture':
            count = event.get('count')
            if type(count) is int and 0 <= count <= 10:
                self.progress.set_fraction(count/10)
                self.progress.set_text(f'已采集 {count} / 10')
                if count < 3: instruction = '将脸放在椭圆中央，看向摄像头。'
                elif count < 5: instruction = '缓慢向左轻轻转头。'
                elif count < 7: instruction = '缓慢向右轻轻转头。'
                elif count < 9: instruction = '轻抬下巴，保持双眼可见。'
                else: instruction = '回到平时使用电脑的位置。'
                self.note.set_label(instruction)
        elif stage == 'original':
            self.progress.set_text('正在核对原录入')
            self.note.set_label('请正对摄像头，轻微转头。')
        elif stage == 'verify':
            self.progress.set_text('采集完成，正在验证')
            self.note.set_label('请再次正对摄像头，通过后将自动启用。')
        else:
            hints = {'single':'请保持画面中只有你一人。','center':'请将脸部移至椭圆中央。',
                     'near':'稍微靠近摄像头。','far':'稍微远离摄像头。','light':'请调亮环境光。',
                     'still':'请稍稍停住，等待画面清晰。'}
            if stage in hints:
                self.note.set_label(hints[stage])
        return False

    def finished(self, generation, proc, result, message):
        proc.stdin.close()
        self.worker = None
        if generation == self.generation:
            self.close('人脸解锁已启用。' if result == 0 else message or '录入未完成，请重试。')
        self.start_button.set_sensitive(True)
        self.app.refresh_once()
        return False

    def close(self, message=None):
        self.generation += 1
        if self.worker:
            self.worker.stdin.close()
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.pipeline:
            self.pipeline.set_state(self.gst.State.NULL)
            self.pipeline = self.sink = None
        if self.subscription:
            self.bus.signal_unsubscribe(self.subscription)
            self.subscription = None
        self.picture.set_paintable(None)
        self.panel.set_visible(False)
        self.start_button.set_sensitive(self.worker is None)
        if message:
            self.app.message.set_label(message)

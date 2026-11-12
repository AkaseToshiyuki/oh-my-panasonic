"""Session-owned connections for visible desktop telemetry."""
import atexit
from concurrent.futures import Future, InvalidStateError
from copy import deepcopy
import queue
import threading
import gi

gi.require_version('NM', '1.0')
from gi.repository import Gio, GLib, NM


def finish(future, value=None, error=None):
    try:
        if future.done():return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)
    except InvalidStateError:
        pass


class DesktopConnections:
    """Keep libnm and BlueZ caches on the context that owns their signals."""
    def __init__(self):
        self.context = GLib.MainContext.new()
        self.loop = GLib.MainLoop.new(self.context, False)
        self.clients = {}
        self.pending = {}
        self.closed = False
        self.releasing = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name='desktop-metrics')
        self.thread.start()

    def run(self):
        self.context.push_thread_default()
        try:
            self.loop.run()
        finally:
            self.context.pop_thread_default()

    def schedule(self, callback):
        source = GLib.idle_source_new()
        source.set_callback(lambda *_: (callback(), GLib.SOURCE_REMOVE)[1])
        source.attach(self.context)

    def read(self, kind, reader):
        future = Future()
        def request():
            if self.closed:
                finish(future,error=RuntimeError('状态采集已关闭'))
            elif kind in self.clients:
                self.deliver(future, reader, self.clients[kind])
            else:
                if kind in self.pending:
                    self.pending[kind][0].append((future,reader))
                    return
                cancel = Gio.Cancellable()
                timeout = GLib.timeout_source_new(3000)
                timeout.set_callback(lambda *_: (cancel.cancel(),GLib.SOURCE_REMOVE)[1])
                timeout.attach(self.context)
                self.pending[kind] = ([(future,reader)],cancel,timeout)
                def ready(obj, result):
                    requests,_,timer = self.pending.pop(kind)
                    timer.destroy()
                    try:
                        client = NM.Client.new_finish(result) if kind == 'network' else Gio.DBusObjectManagerClient.new_for_bus_finish(result)
                        if self.closed:
                            if kind=='network':self.release_network(client)
                            raise RuntimeError('状态采集已关闭')
                        self.clients[kind] = client
                        for item, build in requests:
                            self.deliver(item, build, client)
                    except Exception as error:
                        for item,_ in requests:
                            finish(item,error=error)
                    finally:self.maybe_quit()
                try:
                    if kind == 'network':
                        NM.Client.new_async(cancel,ready)
                    else:
                        Gio.DBusObjectManagerClient.new_for_bus(Gio.BusType.SYSTEM,
                            Gio.DBusObjectManagerClientFlags.DO_NOT_AUTO_START,
                            'org.bluez','/',None,None,cancel,ready)
                except Exception as error:
                    requests,_,timer = self.pending.pop(kind)
                    timer.destroy()
                    for item,_ in requests:finish(item,error=error)
        self.schedule(request)
        try:
            return future.result(timeout=4)
        except TimeoutError:
            future.cancel()
            raise RuntimeError('状态连接超时') from None

    @staticmethod
    def deliver(future, reader, client):
        if future.done():return
        try:finish(future,reader(client))
        except Exception as error:finish(future,error=error)

    def maybe_quit(self):
        if self.closed and not self.pending and not self.releasing:self.loop.quit()

    def release_network(self,client):
        self.releasing += 1
        def released(_,result):
            try:NM.Client.wait_shutdown_finish(result)
            finally:
                self.releasing -= 1
                self.maybe_quit()
        client.wait_shutdown(False,None,released)

    def close(self):
        def release():
            self.closed = True
            for requests,cancel,_ in self.pending.values():
                cancel.cancel()
                for item,_ in requests:finish(item,error=RuntimeError('状态采集已关闭'))
            client = self.clients.pop('network',None)
            self.clients.clear()
            if client is not None:self.release_network(client)
            self.maybe_quit()
        self.schedule(release)
        self.thread.join(timeout=2)


def bluez_objects(manager):
    if not manager.get_name_owner():
        return {}
    return {obj.get_object_path():{
        interface.get_interface_name():{
            name:interface.get_cached_property(name).unpack()
            for name in interface.get_cached_property_names() or []}
        for interface in obj.get_interfaces()}
        for obj in manager.get_objects()}


class PulseConnection:
    def __init__(self):
        self.pulse = None
        self.dirty = True
        self.cached = None

    def invalidate(self, _):
        self.dirty = True

    def close(self):
        if self.pulse is not None:self.pulse.close()
        self.pulse = None
        self.cached = None
        self.dirty = True

    def read(self):
        import pulsectl
        try:
            if self.pulse is None:
                self.pulse = pulsectl.Pulse('Oh My Panasonic',connect=False)
                self.pulse.connect()
                self.pulse.event_callback_set(self.invalidate)
                self.pulse.event_mask_set('sink','source','server')
            # This round trip also processes queued subscription events. No
            # polling thread or periodic wakeup runs while the view is idle.
            info = self.pulse.server_info()
            if self.cached is None or self.dirty:
                self.dirty = False
                result = {'available':True}
                for kind, default, items in (
                    ('sinks',info.default_sink_name,self.pulse.sink_list()),
                    ('sources',info.default_source_name,self.pulse.source_list())):
                    result[kind] = [dict(id=item.index,name=item.description or item.name,
                        default=item.name==default,mute=bool(item.mute),
                        volume=round(sum(item.volume.values)*100/len(item.volume.values)) if item.volume.values else None,
                        state=str(item.state._value).upper())
                        for item in items if kind!='sources' or not item.name.endswith('.monitor')]
                self.cached = result
            return deepcopy(self.cached)
        except Exception:
            self.close()
            raise


class AudioReader:
    def __init__(self):
        self.connection = PulseConnection()
        self.pending = None
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=1)
        self.closed = False
        self.thread = threading.Thread(target=self.run,daemon=True,name='audio-metrics')
        self.thread.start()

    def run(self):
        try:
            while not self.closed:
                future = self.queue.get()
                if future is None:break
                try:finish(future,self.connection.read())
                except Exception as error:finish(future,error=error)
        finally:self.connection.close()

    def read(self):
        with self.lock:
            if self.closed:raise RuntimeError('音频采集已关闭')
            if self.pending is None or self.pending.done():
                self.pending = Future()
                self.queue.put_nowait(self.pending)
            future = self.pending
        try:return future.result(timeout=3)
        except TimeoutError:raise RuntimeError('音频服务响应超时') from None

    def close(self):
        self.closed = True
        try:self.queue.put_nowait(None)
        except queue.Full:pass
        self.thread.join(timeout=1)


_desktop = _audio = None
_lock = threading.Lock()


def desktop(kind, reader):
    global _desktop
    with _lock:
        if _desktop is None:_desktop = DesktopConnections()
        connection = _desktop
    return connection.read(kind,reader)


def audio():
    global _audio
    with _lock:
        if _audio is None:_audio = AudioReader()
        connection = _audio
    return connection.read()


def close():
    global _desktop,_audio
    with _lock:
        desktop_reader,audio_reader = _desktop,_audio
        _desktop = _audio = None
    if desktop_reader is not None:desktop_reader.close()
    if audio_reader is not None:audio_reader.close()


atexit.register(close)

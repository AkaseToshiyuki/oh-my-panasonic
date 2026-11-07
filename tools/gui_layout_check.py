"""Check that overview actions stay readable and use the card width."""
from gi.repository import GLib, Gio
from panasonic_on_ubuntu.dashboard import App, Gtk


def children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from children(child)
        child = child.get_next_sibling()


app = App()
app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
widths = []
errors = []

def measure():
    try:
        page = app.stack.get_child_by_name('overview')
        buttons = [w for w in children(page) if isinstance(w, Gtk.Button) and w.get_label() in ('显示图标','隐藏图标')]
        assert len(buttons) == 2
        cells = [b.get_parent() if isinstance(b.get_parent(),Gtk.FlowBoxChild) else b for b in buttons]
        flow = cells[0].get_parent()
        values = [b.get_width() for b in buttons]
        assert min(values) >= 65 and abs(values[0]-values[1]) < 3, values
        # Gtk.Button.get_width excludes CSS padding and border. Compare row allocations.
        cells = [c.get_allocation() for c in cells]
        assert cells[-1].x + cells[-1].width >= flow.get_width() - 12, (cells[-1].x, cells[-1].width, flow.get_width())
        widths.append((app.window.get_width(),values[0]))
        if len(widths) == 1:
            app.window.set_default_size(1200,720)
            return True
        assert widths[1][0] > widths[0][0] and widths[1][1] > widths[0][1], widths
        print('GUI_LAYOUT_OK', widths, flush=True)
    except Exception as exc:
        errors.append(repr(exc))
    app.quit()
    return False

def start():
    app.window.set_default_size(800,720)
    GLib.timeout_add(650,measure)
    return False

GLib.timeout_add_seconds(2,start)
GLib.timeout_add_seconds(15,lambda: app.quit() or False)
app.run(None)
if errors or len(widths)!=2:
    raise SystemExit(f'Layout check failed: {errors}, {widths}')

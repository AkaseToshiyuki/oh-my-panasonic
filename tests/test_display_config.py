import copy
import unittest
from unittest.mock import patch
from panasonic_on_ubuntu import display_config as display


def state():
    spec=('eDP-1','Example','Panel','')
    modes=[('native',1920,1200,60.,1.,[1.,1.25,1.5],{'is-current':True}),
           ('small',1600,1000,60.,1.,[1.,1.25],{})]
    return [42,[(spec,modes,{'color-mode':0})],[(0,0,1.25,0,True,[spec],{})],{'layout-mode':1,'supports-changing-layout-mode':True}]


class DisplayTests(unittest.TestCase):
    def test_supported_modes_only_and_original_unchanged(self):
        original=state();snapshot=copy.deepcopy(original)
        result=display.target(original,'eDP-1','small',1.)
        self.assertEqual(result[0][5][0][1],'small');self.assertEqual(result[0][2],1.)
        self.assertEqual(original,snapshot)
        for mode,scale in [('invented',1.),('small',1.5),('native',float('nan'))]:
            with self.assertRaises(ValueError):display.target(original,'eDP-1',mode,scale)
    def test_cannot_change_one_member_of_mirror_group(self):
        original=state();second=copy.deepcopy(original[1][0]);second=(('HDMI-1','Example','Panel2',''),second[1],second[2])
        original[1].append(second);original[2][0][5].append(second[0])
        with self.assertRaises(ValueError):display.target(original,'eDP-1','small',1.)
    def test_cancel_restores_only_the_owned_preview(self):
        original=state();preview=display.target(original,'eDP-1','native',1.)
        current=state();current[2][0]=(0,0,1.,0,True,current[2][0][5],{})
        current[0]=45
        with patch.object(display,'read',return_value=current),patch.object(display,'apply') as apply:
            self.assertEqual(display.resolve(original,preview),'restored')
            apply.assert_called_once_with(current,display.layout(original),1)
    def test_preview_requests_compositor_confirmation_after_verification(self):
        original=state();configuration=display.target(original,'eDP-1','native',1.)
        with patch.object(display,'apply') as apply:
            display.Preview(original,configuration).start()
            self.assertEqual([c.args[2] for c in apply.call_args_list],[0,2])
    def test_external_change_or_hotplug_is_never_overwritten(self):
        original=state();preview=display.target(original,'eDP-1','native',1.)
        for mutate in (lambda s:None,lambda s:s[3].update({'layout-mode':2}),lambda s:s[1].clear()):
            current=state();mutate(current)
            with patch.object(display,'read',return_value=current),patch.object(display,'apply') as apply:
                self.assertEqual(display.resolve(original,preview),'changed');apply.assert_not_called()
    def test_dbus_signature_and_monitor_properties(self):
        original=state()
        with patch.object(display,'call') as call:
            display.apply(original,display.layout(original),0)
            args=call.call_args.args[1]
            self.assertEqual(args.get_type_string(),'(uua(iiduba(ssa{sv}))a{sv})')
            self.assertEqual(args.unpack()[1],0)
            self.assertEqual(args.unpack()[2][0][5][0][2],{'color-mode':0})

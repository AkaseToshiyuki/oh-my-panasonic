import unittest
from panasonic_on_ubuntu.care import Policy, config_valid, DEFAULT, record_maintenance

def config(**updates):
    return {'mode':'care','limit':80,'maintenance_days':0,**updates}
def battery(capacity=80, **updates):
    return {'present':True,'capacity':capacity,'ac':True,'status':'Not charging','watts':0,**updates}

class CareTest(unittest.TestCase):
    def test_natural_cycle_keeps_first_phase_and_verified_end_across_restart(self):
        cfg = config(maintenance_days=60)
        policy = Policy({'ac_since':1, 'last_maintenance':1})
        now = 61*86400
        driver = 1
        for offset, charge in enumerate((90, 91, 100, 93, 80)):
            sample = battery(charge)
            driver = policy.tick(cfg, sample, driver, now+offset)
            record_maintenance(policy.state, dict(timestamp=now+offset, driver_mode=driver,
                               config=cfg, sample=sample, error='', phase=policy.phase))
            policy = Policy(policy.state)
        record = policy.state['maintenance_observation']
        self.assertEqual(record['origin'], 'scheduled')
        self.assertEqual(record['maintenance-full']['capacity'], 90)
        self.assertEqual(record['maintenance-drain']['mode'], 2)
        self.assertEqual((record['end']['capacity'],record['end']['phase'],record['end']['mode']),
                         (80,'holding',1))

    def test_usb_unplug_cancels_maintenance_during_forced_battery_operation(self):
        policy=Policy()
        request=config(action='maintain', request_id='usb-drain')
        self.assertEqual(policy.tick(request,battery(100,usb_online=True),0,100),2)
        self.assertEqual(policy.tick(request,battery(99,ac=False,usb_online=True),2,105),2)
        self.assertEqual(policy.tick(request,battery(99,ac=False,usb_online=False),2,110),0)
        self.assertEqual(policy.phase,'battery')
        self.assertNotIn('maintenance_phase',policy.state)

    def test_recharge_threshold_explanation_only_when_waiting_on_firmware(self):
        policy=Policy()
        policy.tick(config(mode='system'),battery(96),0,100)
        self.assertIn('94%',policy.reason)
        policy.tick(config(),battery(96),0,105)
        self.assertFalse(policy.reason)

    def test_hardware_verification_failure_needs_new_request(self):
        policy=Policy({'verification_error':'readback mismatch','request_id':'old'})
        with self.assertRaises(ValueError): policy.tick(config(request_id='old'),battery(),0,100)
        self.assertEqual(policy.tick(config(request_id='new'),battery(),0,105),1)
        self.assertNotIn('verification_error',policy.state)

    def test_hysteresis_and_new_ceiling(self):
        policy=Policy()
        self.assertEqual(policy.tick(config(),battery(79),0,100),0)
        self.assertEqual(policy.tick(config(),battery(80),0,105),1)
        self.assertEqual(policy.tick(config(),battery(79),1,110),1)
        self.assertEqual(policy.tick(config(),battery(77),1,115),0)
        policy.tick(config(),battery(80),0,120)
        self.assertEqual(policy.tick(config(limit=90),battery(88),1,125),0)

    def test_full_override_survives_restart_but_not_unplug(self):
        request=config(action='full',request_id='full1')
        policy=Policy()
        self.assertEqual(policy.tick(request,battery(90),1,100),0)
        restored=Policy(policy.state)
        self.assertEqual(restored.tick(request,battery(100),0,200),1)
        self.assertNotIn('override_until',restored.state)
        restored.tick(request,battery(100,ac=False),0,205)
        self.assertEqual(restored.tick(request,battery(100),0,210),1)
        self.assertNotIn('override_until',restored.state)

    def test_override_expires(self):
        policy=Policy()
        request=config(action='full',request_id='full2')
        policy.tick(request,battery(),0,100)
        self.assertEqual(policy.tick(request,battery(),0,100+86401),1)

    def test_default_cycle_long_ac_then_returns_to_adapter_at_limit(self):
        policy = Policy()
        self.assertEqual((DEFAULT['mode'], DEFAULT['maintenance_days']), ('care',60))
        self.assertEqual(policy.tick(DEFAULT,battery(81),0,100),1)
        due = 100 + 60*86400
        self.assertEqual(policy.tick(DEFAULT,battery(81),1,due-1),1)
        self.assertEqual(policy.tick(DEFAULT,battery(81),1,due),0)
        self.assertEqual(policy.tick(DEFAULT,battery(100),0,due+50),2)
        self.assertEqual(policy.tick(DEFAULT,battery(83,ac=False,watts=6),2,due+100),2)
        self.assertEqual(policy.tick(DEFAULT,battery(80,ac=False,watts=6),2,due+200),1)
        self.assertEqual(policy.tick(DEFAULT,battery(79),1,due+205),1)

    def test_slider_takes_over_from_full_or_system_on_usb_power(self):
        policy = Policy({'override_until': 99999})
        request = config(action='limit',request_id='drag',limit=82)
        self.assertEqual(policy.tick(request,battery(85,ac=False,usb_online=True),0,300),1)
        self.assertNotIn('override_until',policy.state)
        self.assertEqual(policy.tick(config(mode='system'),battery(85),1,305),0)

    def test_cycle_full_idle_drain_hold(self):
        policy=Policy()
        request=config(action='maintain',request_id='cycle1')
        self.assertEqual(policy.tick(request,battery(90,status='Charging'),0,100),0)
        self.assertEqual(policy.phase,'maintenance-full')
        self.assertEqual(policy.tick(request,battery(100),0,200),2)
        # Logical AC goes offline during controlled battery use.
        self.assertEqual(policy.tick(request,battery(90,ac=False,status='Discharging',watts=5),2,300),2)
        self.assertEqual(policy.tick(request,battery(80,ac=False,status='Discharging',watts=5),2,400),1)
        self.assertEqual(policy.phase,'holding')
        self.assertEqual(policy.state['last_maintenance'],400)

    def test_cycle_pauses_at_high_power(self):
        policy=Policy({'maintenance_phase':'drain','maintenance_started':100})
        self.assertEqual(policy.tick(config(),battery(99,ac=False,watts=20),2,200),1)
        self.assertEqual(policy.phase,'maintenance-pause')
        self.assertEqual(policy.tick(config(),battery(99),1,250),1)
        self.assertEqual(policy.tick(config(),battery(99),1,321),2)

    def test_cancel_cycle_restores_ac_without_charging(self):
        policy=Policy({'maintenance_phase':'drain','maintenance_started':100})
        request=config(action='cancel-maintenance',request_id='cancel1')
        self.assertEqual(policy.tick(request,battery(90,ac=False),2,200),1)
        self.assertNotIn('maintenance_phase',policy.state)

    def test_cancel_scheduled_cycle_does_not_immediately_restart(self):
        now=100+31*86400
        policy=Policy({'maintenance_phase':'drain','maintenance_started':now-100,
                       'last_maintenance':100,'ac_since':100})
        request=config(maintenance_days=30,action='cancel-maintenance',request_id='cancel2')
        self.assertEqual(policy.tick(request,battery(90,ac=False),2,now),1)
        self.assertEqual(policy.tick(request,battery(90),1,now+5),1)
        self.assertNotIn('maintenance_phase',policy.state)

    def test_system_mode_always_releases(self):
        policy=Policy({'maintenance_phase':'drain','override_until':99999})
        self.assertEqual(policy.tick(config(mode='system'),battery(90,ac=False),2,200),0)
        self.assertNotIn('maintenance_phase',policy.state)

    def test_cancel_consumed_without_hardware_cannot_replay_after_reinstall(self):
        policy=Policy({'maintenance_phase':'drain','override_until':99999,'request_id':'cycle'})
        request=config(mode='system',action='configure',request_id='release')
        policy.request(request,200)
        self.assertEqual(policy.state['request_id'],'release')
        self.assertNotIn('maintenance_phase',policy.state)
        self.assertNotIn('override_until',policy.state)
        restored=Policy(policy.state)
        self.assertEqual(restored.tick(request,battery(90),0,205),0)

    def test_maintenance_requires_interval_and_long_ac_session(self):
        policy=Policy({'last_maintenance':100,'ac_since':100+30*86400-3600})
        now=100+30*86400
        self.assertEqual(policy.tick(config(maintenance_days=30),battery(90),1,now),1)
        self.assertNotIn('maintenance_phase',policy.state)
        self.assertEqual(policy.tick(config(maintenance_days=30),battery(90),1,now+72*3600),0)
        self.assertEqual(policy.phase,'maintenance-full')

    def test_maintenance_time_limit_and_disabled_default(self):
        policy=Policy({'maintenance_phase':'drain','maintenance_started':100})
        self.assertEqual(policy.tick(config(),battery(90,ac=False),2,100+86401),1)
        self.assertNotIn('maintenance_phase',policy.state)
        old=Policy({'last_maintenance':1,'ac_since':1})
        old.tick(config(),battery(90),1,200*86400)
        self.assertNotIn('maintenance_phase',old.state)

    def test_invalid_policy_and_battery_rejected(self):
        for value in (0,49,101,True,'80'):
            with self.assertRaises(ValueError): config_valid(config(limit=value))
        for value in (1,7,365):
            with self.assertRaises(ValueError): config_valid(config(maintenance_days=value))
        with self.assertRaises(ValueError): Policy().tick(config(),battery(present=False),0,100)
        self.assertEqual(Policy().tick(config(limit=100),battery(100),0,100),0)

if __name__=='__main__': unittest.main()

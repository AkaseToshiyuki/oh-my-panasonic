import copy
import unittest
from panasonic_on_ubuntu.battery_estimate import rates
from panasonic_on_ubuntu.power_summary import describe


def sample(stamp, watts=10, status='Discharging', source='battery'):
    fields = {key: {'value': str(value)} for key, value in {
        'model_name': 'CF-VZSU', 'status': status, 'capacity': 50,
        'energy_now': 20_000_000, 'energy_full': 40_000_000,
        'power_now': watts*1_000_000}.items()}
    return {'timestamp': stamp, 'power_input': {'source': source},
            'batteries': [{'name': 'BAT1', 'fields': fields,
                          'metrics': {'battery_power_w': watts}}]}


class EstimateTests(unittest.TestCase):
    def test_spike_changes_raw_power_but_not_smoothed_eta(self):
        past = [sample(t) for t in range(0, 300, 30)]
        now = sample(300, 40)
        original = copy.deepcopy(now)
        self.assertEqual(rates(now, past), {'power_now': 10_000_000})
        text = describe(now, {}, past)
        self.assertEqual(text['power'], '放电 40 W')
        self.assertEqual(text['status'], '预计可用 2 小时')
        self.assertEqual(now, original)

    def test_sustained_load_updates_estimate(self):
        past = [sample(t, 10 if t < 120 else 20) for t in range(0, 300, 30)]
        self.assertEqual(rates(sample(300, 20), past)['power_now'], 20_000_000)

    def test_transition_and_gap_never_mix_sessions(self):
        now = sample(300)
        for past in ([sample(180)], [sample(240, source='dc'), sample(270)],
                     [sample(240, status='Charging'), sample(270)]):
            self.assertEqual(rates(now, past), {})

    def test_invalid_rate_and_short_history_fall_back(self):
        now = sample(300)
        for value in (0, float('nan'), float('inf'), 'bad'):
            broken = sample(240)
            broken['batteries'][0]['fields']['power_now']['value'] = value
            self.assertEqual(rates(now, [broken, sample(270)]), {})
        self.assertEqual(rates(now, [sample(270)]), {})
        self.assertEqual(describe(now, {}, [sample(270)])['status'], '预计可用 2 小时')

    def test_cap_eta_and_multiple_packs(self):
        now = sample(300, 40, 'Charging', 'dc')
        past = [sample(t, 10, 'Charging', 'dc') for t in range(0, 300, 30)]
        care = {'phase': 'charging', 'config': {'mode': 'care', 'limit': 80}}
        self.assertEqual(describe(now, care, past)['status'], '约 1 小时 12 分钟后充至 80%')
        now['batteries'].append(copy.deepcopy(now['batteries'][0]))
        self.assertEqual(rates(now, past), {})

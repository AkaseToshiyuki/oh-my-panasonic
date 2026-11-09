import unittest
from panasonic_on_ubuntu.readings import text


class ReadingTests(unittest.TestCase):
    def test_missing_values_do_not_leave_units(self):
        for value in (None, '', '  ', '—', '未知', '未提供', '不可用', float('nan'), float('inf')):
            self.assertEqual(text(value, ' W'), '')

    def test_zero_and_known_off_states_are_kept(self):
        self.assertEqual(text(0, ' W'), '0 W')
        self.assertEqual(text('0', '%'), '0%')
        for value in ('已关闭', '未连接', '未充电', '已暂停充电'):
            self.assertEqual(text(value), value)

    def test_actionable_errors_are_not_placeholder_tokens(self):
        for value in ('温度传感器不可用，请检查连接', '电池服务未运行', '操作失败：权限不足'):
            self.assertEqual(text(value), value)

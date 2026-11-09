"""Format optional readouts without suppressing zero or known off states."""
import math

MISSING = {'', '—', '未知', '未提供', '未上报', '未汇报', '不可用', '读取中',
           '等待服务', '接口未提供', '硬件未提供', '供电来源待确认',
           '供电接口未知', '充电状态待确认', '等待电池读数'}


def text(value, unit=''):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ''
    value = str(value).strip()
    return '' if value in MISSING else value + unit

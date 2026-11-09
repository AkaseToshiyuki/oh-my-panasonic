"""Presentation shared by the panel and both tray implementations."""
import math


def duration(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f'{hours} 小时 {minutes} 分钟'
    return f'{hours} 小时' if hours else f'{minutes} 分钟'


def number(fields, key):
    try:
        value = float(fields[key]['value'])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def estimate(fields, charging, target, rates=None):
    rates = rates or {}
    if not rates and (not charging or target == 100):
        seconds = number(fields, 'time_to_full_now' if charging else 'time_to_empty_now')
        if seconds is not None and 0 < seconds < 7 * 86400:
            return duration(seconds)
    for unit, rate in [('energy', 'power_now'), ('charge', 'current_now')]:
        amount = number(fields, unit + '_now')
        speed = rates.get(rate, number(fields, rate))
        if amount is None or amount < 0 or not speed:
            continue
        if charging:
            full = number(fields, unit + '_full')
            if full is None or full <= 0:
                continue
            amount = full * target / 100 - amount
        seconds = 3600 * amount / abs(speed)
        if 0 < seconds < 7 * 86400:
            return duration(seconds)
    return None


def describe(snapshot, care, samples=()):
    supply = snapshot.get('power_input', {})
    source = supply.get('source', 'unknown')
    name = {'typec':'USB-C 供电', 'dc':'DC 供电', 'external':'外接电源',
            'battery':'电池供电', 'unknown':'供电来源待确认'}[source]
    items = snapshot.get('batteries') or []
    if not items:
        return {'source':name, 'status':'未检测到电池', 'summary':name, 'power':'',
                'charge_power':'', 'discharge_power':''}
    fields = items[0]['fields']
    from .battery_estimate import rates as recent_rates
    rates = recent_rates(snapshot, samples) if samples else {}
    status = fields.get('status', {}).get('value')
    capacity = number(fields, 'capacity')
    phase = care.get('phase') if not care.get('stale') else None
    config = care.get('config', {})
    target = config.get('limit', 100) if phase and config.get('mode') == 'care' else 100
    if phase in ('maintenance-full', 'full'):
        target = 100
    watts = items[0].get('metrics', {}).get('battery_power_w')
    battery_power = f'{abs(watts):g} W' if isinstance(watts, (int, float)) and math.isfinite(watts) else ''
    reading = supply.get('measured_input_w')
    power = f'取电 {reading:g} W' if reading is not None else ''
    if status == 'Discharging':
        if source in ('typec','dc','external'):
            name = {'typec':'电池供电（USB-C 已连接）','dc':'电池供电（DC 已连接）',
                    'external':'电池供电（电源已连接）'}[source]
        else:
            name = '电池供电'
        remaining = estimate(fields, False, 0, rates)
        state = '预计可用 ' + remaining if remaining else '正在放电'
        power = f'放电 {abs(watts):g} W' if watts is not None else ''
    elif status == 'Full' or (capacity == 100 and status == 'Not charging'):
        state = '已充满'
    elif phase == 'holding' and capacity is not None and capacity >= target:
        state = f'已达到 {target}% 充电上限'
    elif status == 'Charging':
        remaining = estimate(fields, True, target, rates)
        goal = '充满' if target == 100 else f'充至 {target}%'
        state = f'约 {remaining}后{goal}' if remaining else '充电中'
        if not power and watts is not None and watts > 0:
            power = f'电池充入 {watts:g} W'
    elif source == 'battery':
        state = '等待电池读数'
    else:
        state = '已暂停充电' if status == 'Not charging' else '充电状态待确认'
        if status == 'Not charging' and phase in ('charging', 'system', 'full', 'maintenance-full') and care.get('driver_mode') == 0 and not care.get('error'):
            state = '等待充电'
            ports = supply.get('ports', [])
            minimum = supply.get('charging_minimum_w')
            if source == 'typec' and supply.get('mains_online') is False and len(ports) == 1:
                port = ports[0]
                contract = port.get('contract_w')
                if port.get('protocol') in ('PD', 'PD_PPS') and minimum and contract and 0 < contract < minimum:
                    state = 'PD 供电不足，等待充电'
    return {'source':name, 'status':state, 'power':power,
            'charge_power':battery_power if status == 'Charging' else '',
            'discharge_power':battery_power if status == 'Discharging' else '',
            'summary':' · '.join(filter(None,(name,power,state)))}

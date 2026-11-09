"""Read the UCSI sink contract and adapter capabilities without changing the port."""
from pathlib import Path
import re
from .pd_request import CACHE, read_cached_contract, cached_profiles


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def micro_value(fields, key):
    try:
        value = int(fields[key])
    except (KeyError, ValueError):
        return None
    return value / 1_000_000 if value >= 0 else None


def matching_port(supply, typec):
    # A UCSI controller can own several connectors. Only attach a partner's
    # capabilities when its port can be matched unambiguously to this supply.
    parent = supply / 'device'
    if not parent.exists():
        return None
    ports = [p for p in typec.glob('port*') if re.fullmatch(r'port\d+', p.name)
             and (p/'device').resolve() == parent.resolve()]
    return ports[0] if len(ports) == 1 else None


def fixed_profiles(partner):
    if partner is None:
        return []
    pd = partner / 'usb_power_delivery'
    if not pd.is_dir():
        children = [p for p in partner.glob('pd*') if re.fullmatch(r'pd\d+', p.name)]
        if len(children) != 1:
            return []
        pd = children[0]
    profiles = []
    for entry in (pd/'source-capabilities').glob('*:fixed_supply'):
        position = entry.name.split(':')[0]
        voltage = re.fullmatch(r'(\d+)mV', read(entry/'voltage'))
        current = re.fullmatch(r'(\d+)mA', read(entry/'maximum_current'))
        if position.isdigit() and voltage and current:
            profiles.append({'position':int(position), 'volts':int(voltage[1])/1000,
                             'amps':int(current[1])/1000})
    return sorted(profiles, key=lambda profile: profile['position'])


def detect(root=Path('/sys/class/power_supply'), typec=Path('/sys/class/typec'), cache=CACHE,
           dmi=Path('/sys/class/dmi/id')):
    ports = []
    adapters = [p for p in root.glob('*') if read(p/'type') == 'Mains']
    mains = [read(p/'online') for p in adapters]
    ucsi_states = []
    for supply in sorted(root.glob('ucsi-source-psy-*')):
        fields = dict(line.split('=', 1) for line in read(supply/'uevent').splitlines() if '=' in line)
        if fields.get('POWER_SUPPLY_SCOPE') == 'Device':
            continue
        online = fields.get('POWER_SUPPLY_ONLINE')
        ucsi_states.append(online)
        if online not in ('1', '2'):
            continue
        types = fields.get('POWER_SUPPLY_USB_TYPE', '')
        selected = re.search(r'\[([^]]+)\]', types)
        protocol = selected[1] if selected else types
        volts = micro_value(fields, 'POWER_SUPPLY_VOLTAGE_NOW')
        amps = micro_value(fields, 'POWER_SUPPLY_CURRENT_NOW')
        # Linux UCSI voltage_now/current_now decode the PDO/RDO. They are not
        # ADC samples. Only a separate power_now may populate measured input.
        measured = micro_value(fields, 'POWER_SUPPLY_POWER_NOW')
        port = matching_port(supply, typec)
        partner = typec / (port.name + '-partner') if port else None
        profiles = fixed_profiles(partner)
        record = read_cached_contract(partner, cache) if port and port.name == 'port0' and supply.name == 'ucsi-source-psy-USBC000:001' else None
        if not profiles:
            profiles = cached_profiles(record)
        position = None
        if record and protocol == 'PD':
            position = record['rdo'] >> 28
            profile = next((p for p in profiles if p['position'] == position), None)
            if profile:
                requested = ((record['rdo'] >> 10) & 0x3ff) / 100
                volts, amps = (profile['volts'], requested) if 0 < requested <= profile['amps'] else (None, None)
        # Some SV7 connections retain the Type-C 5 V/3 A defaults in the
        # supply class after the Type-C port and the PPM have switched to PD.
        if (port and read(port/'power_operation_mode') == 'usb_power_delivery'
                and (protocol not in ('PD', 'PD_PPS') or not volts)):
            protocol = 'PD' if protocol != 'PD_PPS' else protocol
            volts = amps = None
            request = record['rdo'] if record else None
            if request:
                position = request >> 28
                profile = next((p for p in profiles if p['position'] == position), None)
                if profile and protocol == 'PD':
                    requested = ((request >> 10) & 0x3ff) / 100
                    if 0 < requested <= profile['amps']:
                        volts, amps = profile['volts'], requested
        pd = protocol in ('PD', 'PD_PPS')
        watts = round(volts*amps, 3) if pd and volts and amps else None
        matches = [p for p in profiles if p['volts'] == volts and amps and p['amps'] >= amps]
        fixed = position if position and volts else (matches[0]['position'] if protocol == 'PD' and len(matches) == 1 else None)
        ports.append({'name':supply.name, 'protocol':protocol, 'volts':volts, 'amps':amps,
                      'contract_w':watts, 'measured_input_w':measured, 'fixed_pdo':fixed,
                      'pdo_position':position or fixed, 'profiles':profiles})
    mains_online = True if '1' in mains else (False if mains and all(s == '0' for s in mains) else None)
    path_label = '外部电源' if ports else {True:'外部电源', False:'电池', None:'未提供'}.get(mains_online)
    result = {'ports':ports, 'mains_online':mains_online, 'path_label':path_label, 'contract_label':'—',
              'input_label':'', 'profiles_label':'—'}
    if len(ports) == 1:
        port = ports[0]
        result['label'] = 'USB-C PD' if port['protocol'] in ('PD', 'PD_PPS') else 'USB-C'
        if port['protocol'] in ('PD','PD_PPS'):
            volts = port['volts']
            pos = port['pdo_position']
            result['voltage_label'] = f'{volts:g} V' if volts else (f'未上报（PDO {pos}）' if pos else '未上报')
        if port['contract_w'] is not None:
            prefix = f"固定档 {port['fixed_pdo']} · " if port['fixed_pdo'] else ''
            result['contract_label'] = f"{prefix}{port['volts']:g} V × {port['amps']:g} A · {port['contract_w']:g} W"
            result['contract_details'] = result['contract_label']
        elif port['pdo_position']:
            result['contract_label'] = f"第 {port['pdo_position']} 档"
        elif port['protocol'] in ('PD', 'PD_PPS'):
            result['contract_label'] = 'PD 已连接'
        if port['measured_input_w'] is not None:
            result['input_label'] = f"{port['measured_input_w']:g} W"
        if port['profiles']:
            result['profiles_label'] = '、'.join(f"{p['volts']:g} V/{p['amps']:g} A" for p in port['profiles'])
    elif ports:
        result['label'] = f'USB-C · {len(ports)} 个受电端口在线'
    elif mains_online:
        result['label'] = '外接电源（USB-C 离线）' if ucsi_states and all(s == '0' for s in ucsi_states) else '外接电源（接口未确认）'
    else:
        result['label'] = '电池供电' if mains_online is False else '供电接口未知'
    if ports:
        source = 'typec'
    elif mains_online and ucsi_states and all(s == '0' for s in ucsi_states):
        source = 'dc'
    elif mains_online:
        source = 'external'
    else:
        source = 'battery' if mains_online is False else 'unknown'
    measured = ports[0]['measured_input_w'] if len(ports) == 1 else None
    active = [p for p in adapters if read(p/'online') == '1']
    if not ports and len(active) == 1:
        measured = micro_value({'w':read(active[0]/'power_now')}, 'w')
    result.update(source=source, measured_input_w=measured)
    if (read(dmi/'sys_vendor') == 'Panasonic Corporation'
            and read(dmi/'product_name') in ('CFSV7-3L', 'CF-SV7HD9VS')):
        # Panasonic's SV7 developer specifies 27 W for charging while powered on.
        # See docs/hardware.zh-CN.md; this is a requirement, not a power reading.
        result['charging_minimum_w'] = 27.0
    if measured is not None:
        result['input_label'] = f'{measured:g} W'
    return result

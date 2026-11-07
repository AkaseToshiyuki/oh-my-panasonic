"""Read the SV7's active USB-PD request when Linux lacks the partner PDOs."""
import json
from pathlib import Path
import re
import time


PORT = Path('/sys/class/typec/port0')
PARTNER = PORT.parent / 'port0-partner'
SUPPLY = Path('/sys/class/power_supply/ucsi-source-psy-USBC000:001')
DEBUG = Path('/sys/kernel/debug/usb/ucsi/USBC000:00')
CACHE = Path('/run/panasonic-on-ubuntu/pd-request.json')


def partner_id(partner):
    stat = partner.stat()
    return f'{stat.st_ino}/{stat.st_ctime_ns}'


def status_request(response):
    if not re.fullmatch(r'0x[0-9a-fA-F]{32}', response):
        return None
    value = int(response, 16)
    # UCSI 1.0 GET_CONNECTOR_STATUS: PD operating mode, connected and sinking.
    if (value >> 16) & 0x1f != 0x0b:
        return None
    request = (value >> 32) & 0xffffffff
    return request if 1 <= request >> 28 <= 7 else None


def read_cached_contract(partner, cache=CACHE):
    try:
        record = json.loads(cache.read_text())
        request = record['rdo']
        if (record['partner'] == partner_id(partner)
                and 0 <= time.time() - record['timestamp'] <= 25
                and type(request) is int and 1 <= request >> 28 <= 7):
            return record
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def read_cached_request(partner, cache=CACHE):
    record = read_cached_contract(partner, cache)
    return record['rdo'] if record else None


def decode_pdos(response):
    if not re.fullmatch(r'0x[0-9a-fA-F]{32}', response):
        raise ValueError('Invalid UCSI PDO response')
    value = int(response, 16)
    words = [(value >> (32*i)) & 0xffffffff for i in range(4)]
    if 0 in words:
        end = words.index(0)
        if any(words[end:]):
            raise ValueError('Non-contiguous PDO response')
        words = words[:end]
    return words


def cached_profiles(record):
    words = record.get('pdos', []) if record else []
    if not isinstance(words, list) or not 1 <= len(words) <= 7:
        return []
    if any(type(word) is not int or not 0 < word <= 0xffffffff for word in words):
        return []
    profiles = []
    for position, word in enumerate(words, 1):
        if word >> 30:  # Variable, battery and PPS PDOs are not fixed contracts.
            continue
        volts, amps = ((word >> 10) & 0x3ff)/20, (word & 0x3ff)/100
        if not 5 <= volts <= 50 or not 0 < amps <= 5:
            return []
        profiles.append(dict(position=position, volts=volts, amps=amps))
    return profiles


def query(command):
    DEBUG.joinpath('command').write_text(hex(command)+'\n')
    return DEBUG.joinpath('response').read_text().strip()


def query_pdos():
    # UCSI GET_PDOS, connector 1, partner source, at most four objects.
    first = decode_pdos(query(0x700810010))
    if len(first) < 4:
        return first
    try:
        # A failure after the first chunk must not discard valid first PDOs.
        rest = decode_pdos(query(0x604810010))
        return first + rest[:3]
    except OSError:
        return first


def sample():
    # This board has one verified system-scope sink. Query only the active PD
    # attachment when the normal API lacks a contract or the partner's PDOs.
    identity = partner_id(PARTNER)
    if (SUPPLY.joinpath('online').read_text().strip() not in ('1', '2')
            or PORT.joinpath('power_operation_mode').read_text().strip() != 'usb_power_delivery'
            or '[sink]' not in PORT.joinpath('power_role').read_text()):
        return None
    from .power_input import fixed_profiles
    if ('[PD]' in SUPPLY.joinpath('usb_type').read_text()
            and int(SUPPLY.joinpath('voltage_now').read_text()) > 0
            and fixed_profiles(PARTNER)):
        return None
    # The command and response files are shared debugfs state. Recheck the
    # result and the attachment before publishing anything to the desktop.
    before = status_request(query(0x10012))
    pdos = []
    if before:
        try:
            first, second = query_pdos(), query_pdos()
            if first == second:
                pdos = first
        except (OSError, ValueError):
            pass
    after = status_request(query(0x10012))
    if before and before == after and identity == partner_id(PARTNER):
        return {'partner': identity, 'rdo': before, 'pdos': pdos, 'timestamp': time.time()}
    return None


def start(atomic):
    """Retire automatic debugfs probes; normal telemetry uses power_supply."""
    # Repeated GET_PDOS causes UCSI errors and adapter notifications on SV7.
    # Keep raw queries explicit, outside the recurring power service.
    CACHE.unlink(missing_ok=True)
    return None

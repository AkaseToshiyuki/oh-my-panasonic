export const KEYS = ['battery','cpu','remaining','battery_power','cpu_power','platform_power','temperature','memory'];
export const POLLED = ['cpu','cpu_power','platform_power','temperature','memory','remaining'];
export function selection(items) {
    return Array.isArray(items) ? [...new Set(items.filter(k => KEYS.includes(k)))].slice(0,5) : ['battery'];
}
const finite = n => typeof n === 'number' && Number.isFinite(n);
export function cpuSample(text) {
    const values = text.split('\n')[0].trim().split(/\s+/);
    if (values.shift() !== 'cpu') return null;
    const n = values.slice(0,8).map(Number);
    if (n.length < 4 || n.some(v => !finite(v) || v < 0)) return null;
    return {total:n.reduce((a,b)=>a+b,0), idle:n[3]+(n[4]??0)};
}
export function cpuPercent(before, after) {
    if (!before || !after) return null;
    const total = after.total-before.total, idle = after.idle-before.idle;
    return total > 0 && idle >= 0 && idle <= total ? 100*(1-idle/total) : null;
}
export function memoryPercent(text) {
    const values = Object.fromEntries([...text.matchAll(/^(MemTotal|MemAvailable):\s+(\d+)/gm)].map(m=>[m[1],Number(m[2])]));
    const total = values.MemTotal, available = values.MemAvailable;
    return total > 0 && available >= 0 && available <= total ? 100*(1-available/total) : null;
}
export function powerValues(data, now, monotonic) {
    const sample = data?.sample;
    if (!sample || !finite(data.timestamp) || !finite(sample.received) ||
        now-data.timestamp < -2 || now-data.timestamp > 12 ||
        monotonic-sample.received < 0 || monotonic-sample.received > 8) return {};
    return {cpu_power:sample.watts, platform_power:sample.platform_w, temperature:sample.temperature};
}
export function batteryValues(get) {
    if (get('IsPresent') !== true) return {};
    const state = get('State'), rate = get('EnergyRate');
    let seconds = state === 2 ? get('TimeToEmpty') : null;
    if (state === 2 && !(seconds > 0) && get('Energy') > 0 && rate > 0)
        seconds = 3600*get('Energy')/rate;
    return {battery:get('Percentage'), remaining:seconds, energy:get('Energy'), discharging:state===2,
        battery_power:state === 1 || state === 2 ? rate : null, charging:state === 1};
}
export function render(items, data) {
    const parts = [];
    for (const key of selection(items)) {
        let n = data[key];
        if (key==='remaining' && !(n>0) && finite(data.energy) && data.energy>0 && finite(data.platform_power) && data.platform_power>0)
            n=3600*data.energy/data.platform_power;
        if (!finite(n) || n < 0) continue;
        if (['battery','cpu','memory'].includes(key)) {
            if (n <= 100) parts.push(`${{battery:'',cpu:'CPU ',memory:'内存 '}[key]}${key==='cpu' ? n.toFixed(1) : Math.round(n)}%`);
        } else if (key === 'remaining') {
            if (n <= 0 || n >= 7*86400) continue;
            const minutes = Math.max(1,Math.ceil(n/60));
            parts.push(`${data.discharging ? '剩余' : '约'} ${Math.floor(minutes/60) ? `${Math.floor(minutes/60)}h` : ''}${minutes%60 ? `${minutes%60}m` : ''}`);
        } else if (key === 'temperature') {
            if (n <= 150) parts.push(`${Math.round(n)}°C`);
        } else if (n <= 1000) {
            parts.push(`${{cpu_power:'CPU',platform_power:'平台',battery_power:data.charging?'充电':'放电'}[key]} ${n.toFixed(1)}W`);
        }
    }
    return parts.join(' · ');
}

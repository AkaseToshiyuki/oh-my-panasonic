const missing = new Set(['', '—', '未知', '未提供', '未上报', '未汇报', '不可用', '读取中',
    '等待服务', '接口未提供', '硬件未提供', '供电来源待确认', '供电接口未知',
    '充电状态待确认', '等待电池读数']);

export function fmt(value, unit = '') {
    if (value == null || (typeof value === 'number' && !Number.isFinite(value))) return '';
    const result = String(value).trim();
    return missing.has(result) ? '' : result + unit;
}

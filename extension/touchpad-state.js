export function parseState(text) {
    if (text.length>1024) throw new Error('Touchpad frame too large');
    const s=JSON.parse(text);
    if (!Array.isArray(s.points) || s.points.length>2 || s.points.some(p=>!Array.isArray(p) || p.length!==2 || p.some(n=>!Number.isInteger(n) || n<0 || n>100)) ||
        ![-1,0,1].includes(s.scroll) || ![0,1,2].includes(s.click) || ![0,1,2].includes(s.tap) ||
        ![0,1,2].includes(s.fingers) || !Number.isInteger(s.seq) || s.seq<0 || s.seq>0xffffffff)
        throw new Error('Invalid touchpad frame');
    if (s.multi !== undefined) {
        const m=s.multi;
        if (!m || !Array.isArray(m.points) || m.points.length>3 ||
            m.points.some(p=>!Array.isArray(p) || p.length!==2 || p.some(n=>!Number.isInteger(n) || n<0 || n>100)) ||
            ![0,1,2,3].includes(m.click) || ![0,1,2,3].includes(m.tap) || ![0,1,2,3].includes(m.fingers))
            throw new Error('Invalid multi-touch frame');
        return {...s,points:m.points,click:m.click,tap:m.tap,fingers:m.fingers};
    }
    return s;
}
export function pulseKind(state, tapEnabled, clickMethod) {
    if (state.tap) return tapEnabled ? state.tap : 0;
    return state.click===1 && state.fingers>=2 && clickMethod==='fingers' ? state.fingers : state.click;
}
export function visiblePoints(state) {
    if (state.fingers!==3 || !state.points.length) return state.points;
    const points=state.points;
    // Some touchpads report three fingers but only two positions. The small
    // icon also needs separated dots when the physical positions overlap.
    if (points.length<3 || points.some((p,i)=>points.slice(i+1).some(q=>Math.hypot(p[0]-q[0],p[1]-q[1])<25)))
        return [[22,62],[50,25],[78,62]];
    return points;
}
export function position(point, radius) {
    let x=(point[0]-50)/50, y=(point[1]-50)/50;
    const scale=Math.max(1,Math.hypot(x,y));
    return [x/scale*radius*.66,y/scale*radius*.66];
}

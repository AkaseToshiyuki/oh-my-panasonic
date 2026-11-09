export function parseState(text) {
    if (text.length>1024) throw new Error('Touchpad frame too large');
    const s=JSON.parse(text);
    if (!Array.isArray(s.points) || s.points.length>2 || s.points.some(p=>!Array.isArray(p) || p.length!==2 || p.some(n=>!Number.isInteger(n) || n<0 || n>100)) ||
        ![-1,0,1].includes(s.scroll) || ![0,1,2].includes(s.click) || ![0,1,2].includes(s.tap) ||
        ![0,1,2].includes(s.fingers) || !Number.isInteger(s.seq) || s.seq<0 || s.seq>0xffffffff)
        throw new Error('Invalid touchpad frame');
    return s;
}
export function pulseKind(state, tapEnabled, clickMethod) {
    if (state.tap) return tapEnabled ? state.tap : 0;
    return state.click===1 && state.fingers===2 && clickMethod==='fingers' ? 2 : state.click;
}
export function position(point, radius) {
    let x=(point[0]-50)/50, y=(point[1]-50)/50;
    const scale=Math.max(1,Math.hypot(x,y));
    return [x/scale*radius*.66,y/scale*radius*.66];
}

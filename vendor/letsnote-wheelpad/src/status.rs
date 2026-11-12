//! Bounded, nonblocking visual feedback for the active local desktop.
use std::fs;
use std::collections::BTreeMap;
use std::io;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::{FileTypeExt,PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use evdev::Key;
use crate::evdev::PhysicalFrame;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct VisualState {
    pub points: Vec<[u8; 2]>,
    pub scroll: i8,
    pub click: u8,
    pub tap: u8,
    pub fingers: u8,
    pub seq: u32,
}

impl VisualState {
    fn line(&self) -> Vec<u8> {
        let points = self.points.iter().map(|p| format!("[{},{}]", p[0], p[1])).collect::<Vec<_>>().join(",");
        // Keep old desktop sessions readable until the extension is reloaded.
        let legacy = self.points.iter().take(2).map(|p| format!("[{},{}]", p[0], p[1])).collect::<Vec<_>>().join(",");
        format!("{{\"points\":[{legacy}],\"scroll\":{},\"click\":{},\"tap\":{},\"fingers\":{},\"seq\":{},\"multi\":{{\"points\":[{points}],\"click\":{},\"tap\":{},\"fingers\":{}}}}}\n",
                self.scroll, if self.click<=2 {self.click} else {0}, if self.tap<=2 {self.tap} else {0}, self.fingers.min(2), self.seq,
                self.click, self.tap, self.fingers).into_bytes()
    }
}

pub struct Observer {
    bounds: [i32; 4],
    state: VisualState,
    origins: BTreeMap<i32,[u8; 2]>,
    down: Option<Instant>,
    peak: usize,
    cancelled: bool,
    pressed: bool,
    previous: Option<[u8; 2]>,
}

impl Observer {
    pub fn new(bounds: [i32; 4]) -> Self {
        Self { bounds, state: VisualState::default(), origins: BTreeMap::new(), down: None,
               peak: 0, cancelled: false, pressed: false, previous: None }
    }

    pub fn observe(&mut self, frame: &PhysicalFrame, scrolling: bool, now: Instant) -> VisualState {
        let contacts: Vec<_> = frame.contacts.positioned_contacts().take(3).map(|(id,p)| {
            let normalize = |v: i32, lo: i32, hi: i32| ((i64::from(v)-i64::from(lo))*100/i64::from((hi-lo).max(1))).clamp(0,100) as u8;
            (id,[normalize(p.x,self.bounds[0],self.bounds[1]),normalize(p.y,self.bounds[2],self.bounds[3])])
        }).collect();
        let points:Vec<_>=contacts.iter().map(|(_,point)|*point).collect();
        let count = frame.contacts.visual_count();
        let button = if frame.contacts.button_down(Key::BTN_RIGHT) { 2 }
                     else if frame.contacts.button_down(Key::BTN_MIDDLE) { 3 }
                     else if frame.contacts.button_down(Key::BTN_LEFT) { 1 } else { 0 };
        let mut state = VisualState { points: points.clone(), seq: self.state.seq,
                                     fingers: count.min(3) as u8, ..VisualState::default() };
        if !points.is_empty() && self.down.is_none() {
            self.down=Some(now); self.origins.clear(); self.peak=0; self.cancelled=false;
        }
        self.peak=self.peak.max(count);
        for (id, point) in &contacts {
            if let Some(origin) = self.origins.get(id) {
                let dx=i32::from(point[0])-i32::from(origin[0]);
                let dy=i32::from(point[1])-i32::from(origin[1]);
                if dx*dx+dy*dy>36 { self.cancelled=true; }
            } else { self.origins.insert(*id,*point); }
        }
        self.cancelled |= scrolling || frame.reconciled || count>3 || button!=0;
        if button!=0 && !self.pressed {
            state.click=button; state.seq=state.seq.wrapping_add(1);
        }
        self.pressed=button!=0;
        if points.is_empty() {
            if let Some(start)=self.down.take() {
                if !self.cancelled && self.peak>0 && self.peak<=3 && now.duration_since(start)<=Duration::from_millis(220) {
                    state.tap=self.peak as u8; state.fingers=state.tap; state.seq=state.seq.wrapping_add(1);
                }
            }
        } else if scrolling {
            state.scroll=self.state.scroll;
            if let (Some(before),Some(after))=(self.previous,points.first()) {
                let cross=(i32::from(before[0])-50)*(i32::from(after[1])-50)
                    -(i32::from(before[1])-50)*(i32::from(after[0])-50);
                if cross.abs()>8 { state.scroll=cross.signum() as i8; }
            }
        }
        self.previous=points.first().copied(); self.state=state.clone(); state
    }
}

#[link(name = "systemd")]
extern "C" {
    fn sd_seat_get_active(seat: *const libc::c_char, session: *mut *mut libc::c_char, uid: *mut libc::uid_t) -> libc::c_int;
}

fn active_uid() -> Option<u32> {
    let mut uid=0;
    // Only the UID is requested; libsystemd does not allocate a session string.
    let result=unsafe { sd_seat_get_active(c"seat0".as_ptr(), std::ptr::null_mut(), &mut uid) };
    (result>=0).then_some(uid)
}

fn peer_uid(stream: &UnixStream) -> Option<u32> {
    let mut credentials=libc::ucred { pid:0,uid:0,gid:0 };
    let mut size=std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SO_PEERCRED is supplied by the kernel, not by the subscriber.
    let result=unsafe { libc::getsockopt(stream.as_raw_fd(),libc::SOL_SOCKET,libc::SO_PEERCRED,
                        (&mut credentials as *mut libc::ucred).cast(),&mut size) };
    (result==0).then_some(credentials.uid)
}

fn permitted(uid: Option<u32>, active: Option<u32>) -> bool {
    uid==Some(0) || (uid.is_some() && uid==active)
}

fn send(stream: &UnixStream, bytes: &[u8]) -> bool {
    // A slow or disconnected reader is dropped; input forwarding never waits.
    unsafe { libc::send(stream.as_raw_fd(),bytes.as_ptr().cast(),bytes.len(),libc::MSG_DONTWAIT|libc::MSG_NOSIGNAL) == bytes.len() as isize }
}

fn connected(stream: &UnixStream) -> bool {
    let mut byte=0u8;
    let result=unsafe { libc::recv(stream.as_raw_fd(),(&mut byte as *mut u8).cast(),1,libc::MSG_PEEK|libc::MSG_DONTWAIT) };
    result<0 && io::Error::last_os_error().kind()==io::ErrorKind::WouldBlock
}

pub struct Publisher {
    listener: UnixListener,
    path: PathBuf,
    clients: Vec<UnixStream>,
    state: VisualState,
    sent: VisualState,
    last: Instant,
}

impl Publisher {
    pub fn bind(path: &Path) -> io::Result<Self> {
        if let Ok(metadata)=fs::symlink_metadata(path) {
            if !metadata.file_type().is_socket() { return Err(io::Error::new(io::ErrorKind::AlreadyExists,"status path is not a socket")); }
        }
        match fs::remove_file(path) { Ok(())=>{}, Err(e) if e.kind()==io::ErrorKind::NotFound=>{}, Err(e)=>return Err(e) }
        let listener=UnixListener::bind(path)?;
        listener.set_nonblocking(true)?;
        fs::set_permissions(path,fs::Permissions::from_mode(0o666))?;
        Ok(Self { listener,path:path.into(),clients:vec![],state:VisualState::default(),sent:VisualState::default(),last:Instant::now() })
    }
    pub fn fd(&self) -> RawFd { self.listener.as_raw_fd() }
    pub fn accept(&mut self) {
        self.maintain();
        let active=active_uid();
        for _ in 0..4 {
            let Ok((stream,_))=self.listener.accept() else { break; };
            if self.clients.len()>=4 || !permitted(peer_uid(&stream),active) { continue; }
            if stream.set_nonblocking(true).is_ok() && send(&stream,&self.state.line()) { self.clients.push(stream); }
        }
    }
    pub fn maintain(&mut self) {
        if !self.clients.is_empty() {
            let active=active_uid(); self.clients.retain(|c| connected(c) && permitted(peer_uid(c),active));
        }
    }
    pub fn publish(&mut self, state: VisualState, now: Instant) {
        self.state=state;
        if self.clients.is_empty() || self.state==self.sent { return; }
        let transition=self.state.seq!=self.sent.seq || self.state.fingers!=self.sent.fingers || self.state.points.len()!=self.sent.points.len() || self.state.scroll!=self.sent.scroll;
        if !transition && now.duration_since(self.last)<Duration::from_millis(50) { return; }
        let bytes=self.state.line(); self.clients.retain(|c| send(c,&bytes));
        self.sent=self.state.clone(); self.last=now;
    }
}

impl Drop for Publisher {
    fn drop(&mut self) { let _=fs::remove_file(&self.path); }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::evdev::ContactSnapshot;
    fn frame(points: &[(i32,i32)], buttons: &[Key]) -> PhysicalFrame {
        let slots:Vec<_>=points.iter().enumerate().map(|(i,(x,y))|(i as i32,*x,*y)).collect();
        let slots=if slots.is_empty(){vec![(-1,0,0)]}else{slots};
        PhysicalFrame::from_parts(ContactSnapshot::from_slot_values_and_buttons(&slots,Some(0),!points.is_empty(),buttons).unwrap(),vec![],false)
    }
    #[test]
    fn quick_two_finger_tap_but_not_drag_or_scroll_flashes() {
        let mut observer=Observer::new([0,100,0,100]); let now=Instant::now();
        observer.observe(&frame(&[(40,40),(60,40)],&[]),false,now);
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(100)).tap,2);
        observer.observe(&frame(&[(40,40)],&[]),false,now+Duration::from_millis(200));
        observer.observe(&frame(&[(60,40)],&[]),false,now+Duration::from_millis(230));
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(260)).tap,0);
        observer.observe(&frame(&[(90,50)],&[]),true,now+Duration::from_millis(300));
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(350)).tap,0);
    }
    #[test]
    fn direction_is_physical_and_click_only_flashes_on_press() {
        let mut observer=Observer::new([0,100,0,100]); let now=Instant::now();
        observer.observe(&frame(&[(90,50)],&[]),false,now);
        assert_eq!(observer.observe(&frame(&[(50,90)],&[]),true,now).scroll,1);
        assert_eq!(observer.observe(&frame(&[(90,50)],&[]),true,now).scroll,-1);
        assert_eq!(observer.observe(&frame(&[(90,50)],&[Key::BTN_RIGHT]),false,now).click,2);
        assert_eq!(observer.observe(&frame(&[(90,50)],&[Key::BTN_RIGHT]),false,now).click,0);
    }
    #[test]
    fn authorization_fails_closed_without_active_seat() {
        assert!(permitted(Some(0),None)); assert!(permitted(Some(1000),Some(1000)));
        assert!(!permitted(Some(1001),Some(1000))); assert!(!permitted(None,None)); assert!(!permitted(Some(1000),None));
    }
    #[test]
    fn staggered_two_finger_release_retains_tap() {
        let mut observer=Observer::new([0,100,0,100]);let now=Instant::now();
        observer.observe(&frame(&[(30,40),(70,40)],&[]),false,now);
        let remaining=PhysicalFrame::from_parts(ContactSnapshot::from_slot_values(&[(-1,30,40),(1,70,40)],Some(1),true).unwrap(),vec![],false);
        observer.observe(&remaining,false,now+Duration::from_millis(90));
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(110)).tap,2);
    }
    #[test]
    fn three_fingers_keep_all_positions_and_staggered_tap() {
        let mut observer=Observer::new([0,100,0,100]);let now=Instant::now();
        let state=observer.observe(&frame(&[(20,50),(50,40),(80,50)],&[]),false,now);
        assert_eq!(state.points,vec![[20,50],[50,40],[80,50]]);
        assert_eq!(state.fingers,3);
        let wire=String::from_utf8(state.line()).unwrap();
        assert!(wire.contains("\"points\":[[20,50],[50,40]],"));
        assert!(wire.contains("\"multi\":{\"points\":[[20,50],[50,40],[80,50]]"));
        observer.observe(&frame(&[(20,50),(50,40)],&[]),false,now+Duration::from_millis(60));
        let lifted=observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(100));
        assert_eq!(lifted.tap,3);
        assert!(String::from_utf8(lifted.line()).unwrap().contains("\"click\":0,\"tap\":0,\"fingers\":2"));
    }
    #[test]
    fn limited_slots_use_reported_finger_count_without_inventing_coordinates() {
        let mut observer=Observer::new([0,100,0,100]);let now=Instant::now();
        let state=observer.observe(&frame(&[(20,50),(80,50)],&[Key::BTN_TOOL_TRIPLETAP]),false,now);
        assert_eq!(state.fingers,3);assert_eq!(state.points.len(),2);
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(100)).tap,3);
        observer.observe(&frame(&[(20,50),(80,50)],&[Key::BTN_TOOL_QUADTAP]),false,now);
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(100)).tap,0);
    }
    #[test]
    fn three_finger_swipe_does_not_flash_as_tap() {
        let mut observer=Observer::new([0,100,0,100]);let now=Instant::now();
        observer.observe(&frame(&[(20,50),(50,50),(80,50)],&[]),false,now);
        observer.observe(&frame(&[(20,50),(50,50),(80,70)],&[]),false,now);
        assert_eq!(observer.observe(&frame(&[],&[]),false,now+Duration::from_millis(100)).tap,0);
        assert_eq!(observer.observe(&frame(&[(20,50),(50,50),(80,50)],&[Key::BTN_MIDDLE]),false,now).click,3);
    }
    #[test]
    fn disconnected_reader_never_blocks_or_raises_sigpipe() {
        let (sender,reader)=UnixStream::pair().unwrap();drop(reader);
        assert!(!send(&sender,b"{}\n"));
    }
    #[test]
    fn unread_socket_has_bounded_nonblocking_send() {
        let (sender,_reader)=UnixStream::pair().unwrap();
        let started=Instant::now();
        let mut full=false;
        for _ in 0..4096 { if !send(&sender,&[0;512]) {full=true;break;} }
        assert!(full);assert!(started.elapsed()<Duration::from_secs(2));
    }
}

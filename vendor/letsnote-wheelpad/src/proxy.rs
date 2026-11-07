use std::io;

use evdev::InputEvent;

use crate::config::Scroll;
use crate::detector::{CircularDetector, CoordinateTransform};
use crate::evdev::{ContactSnapshot, PhysicalFrame};
use crate::fsm::{Action, ContactId, Fsm, FsmState};
use crate::router::{Router, RoutingMode};

pub struct FrameProcessor {
    fsm: Fsm,
    detector: CircularDetector,
    router: Router,
    multi_blocked: bool,
}

impl FrameProcessor {
    pub fn new(
        center_x: i32,
        center_y: i32,
        transform: CoordinateTransform,
        minimum_rotation_radius: f64,
        initial: &ContactSnapshot,
    ) -> Self {
        Self {
            fsm: Fsm::with_transform(center_x, center_y, transform),
            detector: CircularDetector::with_geometry(transform, minimum_rotation_radius),
            router: Router::from_snapshot(initial),
            multi_blocked: initial.active_count() > 1,
        }
    }

    pub fn process_frame(
        &mut self,
        frame: &PhysicalFrame,
        scroll: &Scroll,
        routed_events: &mut Vec<InputEvent>,
    ) -> io::Result<Action> {
        // Multi-finger gestures belong to libinput. Do not reinterpret the
        // remaining finger as a new circular gesture until all fingers lift.
        if frame.contacts.active_count() > 1 {
            self.multi_blocked = true;
        }
        if self.multi_blocked {
            self.fsm.force_idle(&mut self.detector);
            self.router.route_frame(&frame.events, RoutingMode::Passthrough, routed_events)?;
            if frame.reconciled {
                self.router.reconcile(&frame.contacts);
            }
            if frame.contacts.is_quiescent() {
                self.multi_blocked = false;
            }
            return Ok(Action::None);
        }
        let previous = self.fsm.state();
        let tracked_before = self.fsm.contact_id();
        let touch = match tracked_before {
            Some(contact) => frame.contacts.for_contact(contact),
            None => frame.contacts.primary(),
        };
        let action = self.fsm.step(touch, &mut self.detector, scroll);
        let current = self.fsm.state();
        let capture = if matches!(previous, FsmState::Scrolling { .. }) {
            tracked_before
        } else if matches!(current, FsmState::Scrolling { .. })
            || (matches!(previous, FsmState::Moving { .. })
                && matches!(current, FsmState::Moving { .. }))
        {
            // Keep the touchdown position as the virtual contact's origin,
            // then freeze motion while waiting for the engagement angle.
            // Otherwise the pre-scroll arc moves the cursor before capture.
            self.fsm.contact_id()
        } else {
            None
        };
        self.router.route_frame(
            &frame.events,
            capture.map_or(RoutingMode::Passthrough, RoutingMode::Capture),
            routed_events,
        )?;
        if frame.reconciled {
            self.router.reconcile(&frame.contacts);
        }
        Ok(action)
    }

    pub fn state(&self) -> FsmState {
        self.fsm.state()
    }

    pub fn tracked_contact(&self) -> Option<ContactId> {
        self.fsm.contact_id()
    }

    pub fn is_scrolling(&self) -> bool {
        matches!(self.fsm.state(), FsmState::Scrolling { .. })
    }

    #[cfg(test)]
    fn router_selected_slot(&self) -> Option<usize> {
        self.router.selected_slot()
    }
}

#[cfg(test)]
mod tests {
    use evdev::{AbsoluteAxisType, EventType, InputEvent};

    use super::*;
    use crate::evdev::{ContactSnapshot, PhysicalFrame};
    use crate::uinput::mt_slot_event;

    #[test]
    fn multifinger_passthrough_latches_until_every_finger_lifts() {
        let quiet = ContactSnapshot::from_slot_values(&[(-1, 0, 0); 2], Some(0), false).unwrap();
        let scroll = Scroll::default();
        let mut processor = FrameProcessor::new(500, 500, CoordinateTransform::default(), scroll.minimum_rotation_radius, &quiet);
        let mut routed = Vec::new();
        let positions = [(900, 500), (850, 650), (700, 850), (500, 900)];
        for (i, (x, y)) in positions.into_iter().enumerate() {
            let contacts = ContactSnapshot::from_slot_values(&[(1, x, y), (if i == 0 { 2 } else { -1 }, 100, 500)], Some(0), true).unwrap();
            let events = vec![InputEvent::new(EventType::ABSOLUTE, AbsoluteAxisType::ABS_X.0, x), InputEvent::new(EventType::SYNCHRONIZATION, 0, 0)];
            let frame = PhysicalFrame::from_parts(contacts, events.clone(), false);
            assert_eq!(processor.process_frame(&frame, &scroll, &mut routed).unwrap(), Action::None);
            assert_eq!(processor.state(), FsmState::Idle);
            let fields = |items: &[InputEvent]| items.iter().map(|e| (e.event_type(), e.code(), e.value())).collect::<Vec<_>>();
            // VirtualDevice::emit appends SYN_REPORT after routing.
            assert_eq!(fields(&routed), fields(&events[..events.len() - 1]));
            routed.clear();
        }
        processor.process_frame(&PhysicalFrame::from_parts(quiet, vec![], false), &scroll, &mut routed).unwrap();
        let new_touch = ContactSnapshot::from_slot_values(&[(3, 900, 500), (-1, 0, 0)], Some(0), true).unwrap();
        processor.process_frame(&PhysicalFrame::from_parts(new_touch, vec![], false), &scroll, &mut routed).unwrap();
        assert!(matches!(processor.state(), FsmState::Moving { .. }));
    }

    struct VirtualTypeB {
        current_slot: usize,
        positions: Vec<(i32, i32)>,
    }

    impl VirtualTypeB {
        fn new(slot_count: usize) -> Self {
            Self {
                current_slot: 0,
                positions: vec![(0, 0); slot_count],
            }
        }

        fn apply(&mut self, events: &[InputEvent]) {
            for event in events {
                if event.event_type() != EventType::ABSOLUTE {
                    continue;
                }
                match AbsoluteAxisType(event.code()) {
                    AbsoluteAxisType::ABS_MT_SLOT => {
                        self.current_slot = usize::try_from(event.value()).unwrap();
                    }
                    AbsoluteAxisType::ABS_MT_POSITION_X => {
                        self.positions[self.current_slot].0 = event.value();
                    }
                    AbsoluteAxisType::ABS_MT_POSITION_Y => {
                        self.positions[self.current_slot].1 = event.value();
                    }
                    _ => {}
                }
            }
        }
    }

    #[test]
    fn startup_aligns_router_and_virtual_selected_slot_before_position_only_frame() {
        let confirmed =
            ContactSnapshot::from_slot_values(&[(-1, 0, 0); 4], Some(2), false).unwrap();
        let mut virtual_device = VirtualTypeB::new(confirmed.slot_count());
        virtual_device.apply(&[mt_slot_event(confirmed.selected_slot().unwrap()).unwrap()]);
        let mut processor = FrameProcessor::new(
            500,
            500,
            CoordinateTransform::default(),
            Scroll::default().minimum_rotation_radius,
            &confirmed,
        );
        let physical = PhysicalFrame::from_parts(
            confirmed,
            vec![
                InputEvent::new(
                    EventType::ABSOLUTE,
                    AbsoluteAxisType::ABS_MT_POSITION_X.0,
                    700,
                ),
                InputEvent::new(
                    EventType::ABSOLUTE,
                    AbsoluteAxisType::ABS_MT_POSITION_Y.0,
                    400,
                ),
                InputEvent::new(EventType::SYNCHRONIZATION, 0, 0),
            ],
            false,
        );
        let mut routed = Vec::new();

        processor
            .process_frame(&physical, &Scroll::default(), &mut routed)
            .unwrap();
        virtual_device.apply(&routed);

        assert_eq!(processor.router_selected_slot(), Some(2));
        assert_eq!(virtual_device.current_slot, 2);
        assert_eq!(virtual_device.positions[2], (700, 400));
        assert_eq!(virtual_device.positions[0], (0, 0));
    }
}

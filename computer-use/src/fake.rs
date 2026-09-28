//! Test-only fake desktop (cargo feature `fake-desktop`, UDR-0173 D8).
//!
//! The same desktop as ``FakeDesktop`` in ``tests/integration/test_ctr0229_computer_use_tools.py``
//! -- Notepad, a shell, the chat window and Mail; a Save button and an editor; a screen that can
//! be toggled -- so the CTR-0236 / CTR-0237 scenarios run against THIS binary. Modes:
//!
//! * `default`  -- every REQUIRED and OPTIONAL feature, and `screen.changes`;
//! * `minimal`  -- only the REQUIRED features (RES-0007);
//! * `hostile`  -- clicks are refused like UIPI, the caret probe fails internally.
//!
//! Two test tools exist only here and are never listed: `fake_events` (the input log) and
//! `fake_set` (foreground / cursor / locked / change_screen, and for `screen.changes`:
//! `dirty` -- a repaint without a pixel change -- and `changes_available`; for `input.path`:
//! `takeover_after_ms` -- the "user" grabs the mouse that long into the next path -- and `held`,
//! what `input_release` finds down). A path runs the REAL follower (`path.rs`) on a real clock,
//! so release, takeover and cancel behave as on Windows. A published wheel never contains this
//! module.

use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};

use crate::changes::{ChangeLog, ChangeReport};
use crate::desktop::{Captured, Desktop};
use crate::path::{PathIo, PathReport, PathSpec};
use crate::protocol::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Default,
    Minimal,
    Hostile,
}

impl Mode {
    pub fn parse(raw: &str) -> Option<Self> {
        match raw {
            "" | "default" => Some(Mode::Default),
            "minimal" => Some(Mode::Minimal),
            "hostile" => Some(Mode::Hostile),
            _ => None,
        }
    }
}

struct FakeFrame {
    rect: Rect,
    gray: Vec<u8>,
}

impl Captured for FakeFrame {
    fn rect(&self) -> Rect {
        self.rect
    }
    fn gray(&self) -> Vec<u8> {
        self.gray.clone()
    }
    /// The fake's "PNG" is the marker the Python fake returns: ``b"PNG{w}x{h}"``.
    fn encode(&self, w: u32, h: u32) -> OpResult<Vec<u8>> {
        Ok(format!("PNG{w}x{h}").into_bytes())
    }
}

pub struct FakeDesktop {
    mode: Mode,
    windows: Vec<WindowInfo>,
    fg: usize,
    screen_changed: bool,
    cursor: (i32, i32),
    locked: bool,
    events: Vec<Value>,
    elements: Vec<ElementInfo>,
    repaints: ChangeLog,
    changes_available: bool,
    takeover_after_ms: Option<u64>,
    held: Vec<&'static str>,
}

/// The follower's view of the fake: a real clock, the fake cursor, events for the tests.
struct FakeIo<'a> {
    desk: &'a mut FakeDesktop,
    t0: Instant,
    grab_at: Option<u64>,
}

impl PathIo for FakeIo<'_> {
    fn now_ms(&mut self) -> u64 {
        self.t0.elapsed().as_millis() as u64
    }
    fn sleep_ms(&mut self, ms: u32) {
        std::thread::sleep(Duration::from_millis(u64::from(ms)));
    }
    fn cursor(&mut self) -> (i32, i32) {
        let now = self.now_ms();
        match self.grab_at {
            Some(t) if now >= t => (self.desk.cursor.0 + 500, self.desk.cursor.1),
            _ => self.desk.cursor,
        }
    }
    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()> {
        self.desk.cursor = (x, y);
        Ok(())
    }
    fn press(&mut self, spec: &PathSpec) -> OpResult<()> {
        self.desk.event(json!(["press", spec.button, spec.modifiers]));
        Ok(())
    }
    fn release(&mut self, spec: &PathSpec) {
        self.desk.event(json!(["release", spec.button, spec.modifiers]));
    }
}

fn win(hwnd: i64, title: &str, process: &str, pid: u32, rect: Rect, monitor: i32) -> WindowInfo {
    WindowInfo { hwnd, title: title.into(), process: process.into(), pid, rect, monitor, minimized: false }
}

impl FakeDesktop {
    pub fn new(mode: Mode) -> Self {
        Self {
            mode,
            windows: vec![
                win(100, "Untitled - Notepad", "notepad.exe", 42, Rect::new(0, 0, 1920, 1080), 1),
                win(200, "Command Prompt", "cmd.exe", 43, Rect::new(0, 0, 800, 600), 1),
                win(300, "ChatWala'au - Google Chrome", "chrome.exe", 44, Rect::new(0, 0, 1600, 900), 1),
                win(400, "Mail", "outlook.exe", 45, Rect::new(0, 0, 1600, 900), 2),
            ],
            fg: 2,
            screen_changed: false,
            cursor: (5, 5),
            locked: false,
            events: Vec::new(),
            elements: vec![
                ElementInfo { id: 1, control: "Button".into(), name: "Save".into(), rect: Rect::new(100, 100, 200, 140) },
                ElementInfo { id: 2, control: "Edit".into(), name: "Text editor".into(), rect: Rect::new(10, 200, 1900, 1000) },
            ],
            repaints: {
                let mut log = ChangeLog::new();
                log.restart();
                log
            },
            changes_available: true,
            takeover_after_ms: None,
            held: Vec::new(),
        }
    }

    fn screen(&self) -> Vec<u8> {
        let size = (THUMB_W * THUMB_H) as usize;
        if self.screen_changed { (0..size).map(|b| ((b + 97) % 256) as u8).collect() } else { vec![0; size] }
    }

    fn event(&mut self, e: Value) {
        self.events.push(e);
    }
}

impl Desktop for FakeDesktop {
    fn features(&self) -> Vec<&'static str> {
        let mut f: Vec<&'static str> = REQUIRED_FEATURES.to_vec();
        if self.mode != Mode::Minimal {
            f.extend(OPTIONAL_FEATURES);
            f.extend(EVENT_FEATURES);
        }
        f
    }
    fn list_windows(&mut self) -> OpResult<Vec<WindowInfo>> {
        Ok(self.windows.clone())
    }
    fn window(&mut self, hwnd: i64) -> OpResult<Option<WindowInfo>> {
        Ok(self.windows.iter().find(|w| w.hwnd == hwnd).cloned())
    }
    fn foreground(&mut self) -> OpResult<Option<WindowInfo>> {
        Ok(self.windows.get(self.fg).cloned())
    }
    fn focus(&mut self, hwnd: i64, size: Option<(i32, i32)>) -> OpResult<Option<WindowInfo>> {
        let Some(i) = self.windows.iter().position(|w| w.hwnd == hwnd) else {
            return Ok(None);
        };
        self.fg = i;
        self.event(json!(["focus", hwnd, size.map(|(w, h)| json!([w, h]))]));
        Ok(Some(self.windows[i].clone()))
    }
    fn has_modal_dialog(&mut self, _target: &WindowInfo) -> OpResult<bool> {
        Ok(false)
    }
    fn desktop_locked(&mut self) -> OpResult<bool> {
        Ok(self.locked)
    }
    fn capture(&mut self, rect: Rect) -> OpResult<Box<dyn Captured>> {
        Ok(Box::new(FakeFrame { rect, gray: self.screen() }))
    }
    fn elements(&mut self, _hwnd: i64, max: usize, _budget: Duration) -> OpResult<Vec<ElementInfo>> {
        Ok(self.elements.iter().take(max).cloned().collect())
    }
    fn element_rect(&mut self, id: u64) -> OpResult<Option<Rect>> {
        Ok(self.elements.iter().find(|e| e.id == id).map(|e| e.rect))
    }
    fn forget_elements(&mut self, _ids: &[u64]) {}
    fn find_element(&mut self, _hwnd: i64, name: &str, _control: Option<&str>) -> OpResult<bool> {
        Ok(self.elements.iter().any(|e| e.name == name))
    }
    fn caret_rect(&mut self) -> OpResult<Option<Rect>> {
        if self.mode == Mode::Hostile {
            return Err(ProviderError::new(ErrorCode::Internal, "RuntimeError"));
        }
        Ok(None)
    }
    fn cursor_pos(&mut self) -> OpResult<(i32, i32)> {
        Ok(self.cursor)
    }
    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()> {
        self.cursor = (x, y);
        self.event(json!(["move", x, y]));
        Ok(())
    }
    fn click(&mut self, x: i32, y: i32, button: &str, count: u32) -> OpResult<()> {
        if self.mode == Mode::Hostile {
            return Err(ProviderError::new(ErrorCode::InputBlocked, "SendInput delivered 0/2 events"));
        }
        self.cursor = (x, y);
        self.event(json!(["click", x, y, button, count]));
        Ok(())
    }
    fn drag(&mut self, x1: i32, y1: i32, x2: i32, y2: i32) -> OpResult<()> {
        self.cursor = (x2, y2);
        self.event(json!(["drag", x1, y1, x2, y2]));
        Ok(())
    }
    fn scroll(&mut self, x: i32, y: i32, dy: i32, dx: i32) -> OpResult<()> {
        self.cursor = (x, y);
        self.event(json!(["scroll", dy, dx]));
        Ok(())
    }
    fn keys(&mut self, chord: &[String]) -> OpResult<()> {
        self.event(json!(["keys", chord]));
        Ok(())
    }
    fn type_text(&mut self, text: &str) -> OpResult<()> {
        self.event(json!(["type", text]));
        Ok(())
    }
    fn paste_text(&mut self, text: &str) -> OpResult<()> {
        self.event(json!(["paste", text]));
        Ok(())
    }
    fn changes(&mut self, rect: Rect, since: Option<u64>, timeout: Duration, ignore: &[Rect]) -> OpResult<ChangeReport> {
        if !self.changes_available {
            return Ok(ChangeReport::unavailable(self.repaints.seq(), "unavailable"));
        }
        let Some(since) = since else {
            return Ok(self.repaints.report(&rect, self.repaints.seq(), ignore));
        };
        let report = self.repaints.report(&rect, since, ignore);
        if report.changed || timeout.is_zero() {
            return Ok(report);
        }
        // Nothing can repaint the fake screen while this thread waits: the wait just elapses.
        std::thread::sleep(timeout);
        Ok(self.repaints.report(&rect, since, ignore))
    }
    fn path(&mut self, spec: &PathSpec) -> OpResult<PathReport> {
        let first = spec.points.first().copied().unwrap_or((0, 0));
        let last = spec.points.last().copied().unwrap_or(first);
        self.event(json!([
            "path", spec.button, spec.modifiers, spec.points.len(), [first.0, first.1], [last.0, last.1],
            spec.hold_ms, spec.duration_ms, spec.hover_ms
        ]));
        let plan = crate::path::plan(spec, 4);
        let grab_at = self.takeover_after_ms.take();
        let mut io = FakeIo { desk: self, t0: Instant::now(), grab_at };
        crate::path::follow(&mut io, spec, &plan)
    }
    fn release_input(&mut self) -> OpResult<Vec<&'static str>> {
        let released = std::mem::take(&mut self.held);
        self.event(json!(["release_all", released]));
        Ok(released)
    }
    fn overlay_show(&mut self, hwnd: i64, ttl: Duration) -> OpResult<(bool, Option<&'static str>)> {
        self.event(json!(["overlay_show", hwnd, ttl.as_millis() as u64]));
        Ok((true, None))
    }
    fn overlay_hide(&mut self) -> OpResult<()> {
        self.event(json!(["overlay_hide"]));
        Ok(())
    }
    fn test_tool(&mut self, name: &str, args: &Map<String, Value>) -> Option<OpResult<Value>> {
        match name {
            "fake_events" => Some(Ok(json!({"events": self.events}))),
            "fake_set" => {
                if let Some(h) = args.get("foreground").and_then(Value::as_i64)
                    && let Some(i) = self.windows.iter().position(|w| w.hwnd == h)
                {
                    self.fg = i;
                }
                if let Some(c) = args.get("cursor").and_then(Value::as_array)
                    && let (Some(x), Some(y)) = (c.first().and_then(Value::as_i64), c.get(1).and_then(Value::as_i64))
                {
                    self.cursor = (x as i32, y as i32);
                }
                if let Some(l) = args.get("locked").and_then(Value::as_bool) {
                    self.locked = l;
                }
                if args.get("change_screen").and_then(Value::as_bool) == Some(true) {
                    self.screen_changed = !self.screen_changed;
                    self.repaints.push_frame([Rect::new(-100_000, -100_000, 100_000, 100_000)]);
                }
                if let Some(raw) = args.get("dirty") {
                    match rect_of(Some(raw)) {
                        Ok(r) => self.repaints.push_frame([r]),
                        Err(e) => return Some(Err(e)),
                    }
                }
                if let Some(a) = args.get("changes_available").and_then(Value::as_bool) {
                    self.changes_available = a;
                }
                if let Some(ms) = args.get("takeover_after_ms").and_then(Value::as_u64) {
                    self.takeover_after_ms = Some(ms);
                }
                if let Some(names) = args.get("held").and_then(Value::as_array) {
                    self.held = names
                        .iter()
                        .filter_map(Value::as_str)
                        .filter_map(|n| ["left", "right", "middle", "ctrl", "shift", "alt", "win"].into_iter().find(|k| *k == n))
                        .collect();
                }
                Some(Ok(json!({})))
            }
            _ => None,
        }
    }
}

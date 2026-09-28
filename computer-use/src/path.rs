//! `input_path` (CTR-0236 `input.path`, PRP-0192 Part B, UDR-0174 D5-D8): follow a polyline
//! with a held button, in time.
//!
//! The HOST did the geometry (image -> screen, curve expansion, every point inside the target);
//! this module only turns the points into a timed plan and follows it. Rules:
//!
//! * the first move crosses the system drag threshold, then `hold_ms` passes, then the rest of
//!   the path takes `duration_ms` (every vertex is visited; long segments get a move about every
//!   16 ms), then `hover_ms` passes with the button still down;
//! * before every move and during every pause the real cursor is compared with the position set
//!   last: beyond `takeover_px` the user has the mouse (`interrupted: "user_mouse"`), and the
//!   shared cancel flag (`input_cancel`) stops it (`interrupted: "cancelled"`);
//! * EVERY exit releases -- the button, then the modifiers in reverse order -- through ONE call
//!   (`PathIo::release`) made by `follow` itself (UDR-0174 D6).
//!
//! PANIC-FREE: the release profile aborts on panic, so no destructor would release anything.
//! This module uses no `unwrap`, `expect`, slice indexing or unchecked arithmetic that can panic
//! (checked by tests/invariants/test_prp0192_computer_use_path_overlay.py).

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use serde_json::{Map, Value, json};

use crate::protocol::{OpResult, ProviderError};

/// Wire limits (UDR-0174 D8). The host's model-facing limits are tighter (200 points).
pub const MAX_POINTS: usize = 2000;
pub const MAX_DURATION_MS: u32 = 5000;
pub const MIN_DURATION_MS: u32 = 50;
pub const MAX_PAUSE_MS: u32 = 2000;
pub const DEFAULT_TAKEOVER_PX: i32 = 40;
/// About 60 moves a second on long straight segments.
const TICK_MS: u32 = 16;
pub const MODIFIER_NAMES: [&str; 4] = ["ctrl", "shift", "alt", "win"];

/// Set by `input_cancel` (answered on the MCP task, never behind the desktop thread) while a
/// path is pending; checked by the follower before every move and during every pause.
pub static CANCEL: AtomicBool = AtomicBool::new(false);
/// `input_path` calls received and not answered yet (the server counts them).
pub static PENDING: AtomicUsize = AtomicUsize::new(0);

/// `input_cancel`: stop the running (or queued) path; no effect when none is pending.
pub fn request_cancel() -> bool {
    if PENDING.load(Ordering::SeqCst) > 0 {
        CANCEL.store(true, Ordering::SeqCst);
        return true;
    }
    false
}

/// An `input_path` call finished: the last one clears a cancel meant for it.
pub fn path_done() {
    let before = PENDING.fetch_sub(1, Ordering::SeqCst);
    if before <= 1 {
        PENDING.store(0, Ordering::SeqCst);
        CANCEL.store(false, Ordering::SeqCst);
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct PathSpec {
    pub points: Vec<(i32, i32)>,
    pub button: &'static str,
    pub modifiers: Vec<&'static str>,
    pub hold_ms: u32,
    pub duration_ms: u32,
    pub hover_ms: u32,
    pub takeover_px: i32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PathReport {
    pub completed: bool,
    pub interrupted: Option<&'static str>,
    pub moved: u32,
}

impl PathReport {
    pub fn to_json(&self) -> Value {
        let mut body = json!({"completed": self.completed, "moved": self.moved});
        if let (Some(why), Some(obj)) = (self.interrupted, body.as_object_mut()) {
            obj.insert("interrupted".into(), Value::String(why.into()));
        }
        body
    }
}

fn pause_arg(args: &Map<String, Value>, name: &str, default: u32, lo: u32, hi: u32) -> OpResult<u32> {
    match args.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_u64()
            .and_then(|n| u32::try_from(n).ok())
            .filter(|n| (lo..=hi).contains(n))
            .ok_or_else(|| ProviderError::invalid(format!("{name} is {lo}..{hi}"))),
    }
}

/// Parse and check `input_path` arguments; nothing moves when this fails (UDR-0174 D8).
pub fn parse(args: &Map<String, Value>) -> OpResult<PathSpec> {
    let rows = args
        .get("points")
        .and_then(Value::as_array)
        .ok_or_else(|| ProviderError::invalid("points is a list of [x, y]"))?;
    if rows.len() < 2 || rows.len() > MAX_POINTS {
        return Err(ProviderError::invalid(format!("points has 2..{MAX_POINTS} entries")));
    }
    let mut points = Vec::with_capacity(rows.len());
    for row in rows {
        let pair = row.as_array().filter(|p| p.len() == 2);
        let xy = pair.and_then(|p| {
            let x = p.first().and_then(Value::as_i64).and_then(|n| i32::try_from(n).ok())?;
            let y = p.get(1).and_then(Value::as_i64).and_then(|n| i32::try_from(n).ok())?;
            Some((x, y))
        });
        points.push(xy.ok_or_else(|| ProviderError::invalid("each point is [x, y] (integers)"))?);
    }
    let button = match args.get("button").and_then(Value::as_str).unwrap_or("left") {
        "" | "left" => "left",
        "right" => "right",
        "middle" => "middle",
        _ => return Err(ProviderError::invalid("button is left, right or middle")),
    };
    let mut modifiers: Vec<&'static str> = Vec::new();
    if let Some(raw) = args.get("modifiers").filter(|v| !v.is_null()) {
        let list = raw.as_array().ok_or_else(|| ProviderError::invalid("modifiers is a list"))?;
        for m in list {
            let name = m.as_str().map(str::to_lowercase).unwrap_or_default();
            let known = MODIFIER_NAMES
                .iter()
                .find(|k| **k == name)
                .ok_or_else(|| ProviderError::invalid("modifiers are ctrl, shift, alt, win"))?;
            if modifiers.contains(known) {
                return Err(ProviderError::invalid("a modifier is listed twice"));
            }
            modifiers.push(known);
        }
    }
    let takeover_px = match args.get("takeover_px") {
        None | Some(Value::Null) => DEFAULT_TAKEOVER_PX,
        Some(v) => v
            .as_i64()
            .and_then(|n| i32::try_from(n).ok())
            .filter(|n| (1..=1000).contains(n))
            .ok_or_else(|| ProviderError::invalid("takeover_px is 1..1000"))?,
    };
    Ok(PathSpec {
        points,
        button,
        modifiers,
        hold_ms: pause_arg(args, "hold_ms", 0, 0, MAX_PAUSE_MS)?,
        duration_ms: pause_arg(args, "duration_ms", 400, MIN_DURATION_MS, MAX_DURATION_MS)?,
        hover_ms: pause_arg(args, "hover_ms", 0, 0, MAX_PAUSE_MS)?,
        takeover_px,
    })
}

/// One move of the plan: go to (x, y) at `at_ms` after the button went down.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Timed {
    pub x: i32,
    pub y: i32,
    pub at_ms: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Plan {
    pub start: (i32, i32),
    pub moves: Vec<Timed>,
    pub release_at_ms: u32,
}

fn dist(a: (i32, i32), b: (i32, i32)) -> f64 {
    let dx = f64::from(b.0) - f64::from(a.0);
    let dy = f64::from(b.1) - f64::from(a.1);
    (dx * dx + dy * dy).sqrt()
}

/// The point at distance `d` along the polyline (clamped to its ends).
fn at_distance(points: &[(i32, i32)], cumulative: &[f64], d: f64) -> (i32, i32) {
    let mut prev: Option<((i32, i32), f64)> = None;
    for (p, c) in points.iter().zip(cumulative) {
        if *c >= d {
            return match prev {
                Some((q, cq)) if *c > cq => {
                    let t = (d - cq) / (*c - cq);
                    let x = f64::from(q.0) + (f64::from(p.0) - f64::from(q.0)) * t;
                    let y = f64::from(q.1) + (f64::from(p.1) - f64::from(q.1)) * t;
                    (x.round() as i32, y.round() as i32)
                }
                _ => *p,
            };
        }
        prev = Some((*p, *c));
    }
    points.last().copied().unwrap_or((0, 0))
}

/// The timed plan (pure; unit-tested). `threshold` is the system drag distance in px.
pub fn plan(spec: &PathSpec, threshold: i32) -> Plan {
    let points = &spec.points;
    let start = points.first().copied().unwrap_or((0, 0));
    let mut cumulative = Vec::with_capacity(points.len());
    let mut total = 0.0_f64;
    let mut last = start;
    for p in points {
        total += dist(last, *p);
        cumulative.push(total);
        last = *p;
    }
    let mut moves = Vec::new();
    // 1. Cross the drag threshold (along the path, so a drawing is not distorted).
    let nudge = f64::from(threshold.saturating_add(1)).min(total);
    let first_at = TICK_MS;
    if nudge > 0.0 {
        let (x, y) = at_distance(points, &cumulative, nudge);
        moves.push(Timed { x, y, at_ms: first_at });
    }
    // 2. hold_ms, then 3. the rest of the path over duration_ms.
    let begin = first_at.saturating_add(spec.hold_ms);
    let rest = (total - nudge).max(0.0);
    let duration = f64::from(spec.duration_ms.max(1));
    let mut marks: Vec<f64> = cumulative.iter().copied().filter(|c| *c > nudge).collect();
    let ticks = (spec.duration_ms / TICK_MS).max(1);
    for i in 1..=ticks {
        marks.push(nudge + rest * f64::from(i) / f64::from(ticks));
    }
    marks.sort_by(f64::total_cmp);
    marks.dedup_by(|a, b| (*a - *b).abs() < 0.5);
    for d in marks {
        let share = if rest > 0.0 { (d - nudge) / rest } else { 1.0 };
        let at = f64::from(begin) + duration * share.clamp(0.0, 1.0);
        let (x, y) = at_distance(points, &cumulative, d);
        let at_ms = at.round() as u32;
        if moves.last().is_some_and(|m: &Timed| m.x == x && m.y == y) {
            continue;
        }
        moves.push(Timed { x, y, at_ms });
    }
    // Always end exactly on the last point.
    let end = points.last().copied().unwrap_or(start);
    let end_at = begin.saturating_add(spec.duration_ms);
    if moves.last().is_none_or(|m| (m.x, m.y) != end) {
        moves.push(Timed { x: end.0, y: end.1, at_ms: end_at });
    }
    Plan { start, moves, release_at_ms: end_at.saturating_add(spec.hover_ms) }
}

/// What the follower needs from a desktop (Windows, or the fake).
pub trait PathIo {
    fn now_ms(&mut self) -> u64;
    fn sleep_ms(&mut self, ms: u32);
    fn cursor(&mut self) -> (i32, i32);
    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()>;
    /// Modifiers down, then the button down.
    fn press(&mut self, spec: &PathSpec) -> OpResult<()>;
    /// The button up, then the modifiers up in reverse order. Best effort, never fails.
    fn release(&mut self, spec: &PathSpec);
}

fn interrupted(io: &mut dyn PathIo, last: (i32, i32), takeover_px: i32) -> Option<&'static str> {
    if CANCEL.load(Ordering::SeqCst) {
        return Some("cancelled");
    }
    if dist(io.cursor(), last) > f64::from(takeover_px) {
        return Some("user_mouse");
    }
    None
}

/// Wait until `until` ms after `t0`, checking cancel and takeover at least every tick.
fn wait_until(io: &mut dyn PathIo, t0: u64, until: u32, last: (i32, i32), takeover_px: i32) -> Option<&'static str> {
    loop {
        if let Some(why) = interrupted(io, last, takeover_px) {
            return Some(why);
        }
        let elapsed = io.now_ms().saturating_sub(t0);
        let left = u64::from(until).saturating_sub(elapsed);
        if left == 0 {
            return None;
        }
        io.sleep_ms(u32::try_from(left.min(u64::from(TICK_MS))).unwrap_or(TICK_MS));
    }
}

fn pressed(io: &mut dyn PathIo, spec: &PathSpec, plan: &Plan) -> OpResult<PathReport> {
    io.press(spec)?;
    let t0 = io.now_ms();
    let mut last = plan.start;
    let mut moved = 0_u32;
    for m in &plan.moves {
        if let Some(why) = wait_until(io, t0, m.at_ms, last, spec.takeover_px) {
            return Ok(PathReport { completed: false, interrupted: Some(why), moved });
        }
        io.move_to(m.x, m.y)?;
        last = (m.x, m.y);
        moved = moved.saturating_add(1);
    }
    if let Some(why) = wait_until(io, t0, plan.release_at_ms, last, spec.takeover_px) {
        return Ok(PathReport { completed: false, interrupted: Some(why), moved });
    }
    Ok(PathReport { completed: true, interrupted: None, moved })
}

/// Follow `plan`. The ONE place that releases: every exit after `press` passes through it.
pub fn follow(io: &mut dyn PathIo, spec: &PathSpec, plan: &Plan) -> OpResult<PathReport> {
    if CANCEL.load(Ordering::SeqCst) {
        return Ok(PathReport { completed: false, interrupted: Some("cancelled"), moved: 0 });
    }
    io.move_to(plan.start.0, plan.start.1)?; // nothing is pressed yet
    let result = pressed(io, spec, plan);
    io.release(spec);
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(points: Vec<(i32, i32)>, hold: u32, duration: u32, hover: u32) -> PathSpec {
        PathSpec {
            points,
            button: "left",
            modifiers: vec!["ctrl"],
            hold_ms: hold,
            duration_ms: duration,
            hover_ms: hover,
            takeover_px: 40,
        }
    }

    /// A scripted desktop on a virtual clock.
    struct Io {
        now: u64,
        cursor: (i32, i32),
        log: Vec<String>,
        grab_at: Option<u64>,
        fail_move_at: Option<usize>,
        moves: usize,
    }

    impl Io {
        fn new() -> Self {
            Self { now: 0, cursor: (0, 0), log: Vec::new(), grab_at: None, fail_move_at: None, moves: 0 }
        }
    }

    impl PathIo for Io {
        fn now_ms(&mut self) -> u64 {
            self.now
        }
        fn sleep_ms(&mut self, ms: u32) {
            self.now += u64::from(ms);
        }
        fn cursor(&mut self) -> (i32, i32) {
            match self.grab_at {
                Some(t) if self.now >= t => (self.cursor.0 + 500, self.cursor.1),
                _ => self.cursor,
            }
        }
        fn move_to(&mut self, x: i32, y: i32) -> OpResult<()> {
            self.moves += 1;
            if self.fail_move_at == Some(self.moves) {
                return Err(ProviderError::new(crate::protocol::ErrorCode::InputBlocked, "blocked"));
            }
            self.cursor = (x, y);
            Ok(())
        }
        fn press(&mut self, spec: &PathSpec) -> OpResult<()> {
            self.log.push(format!("down {:?} {}", spec.modifiers, spec.button));
            Ok(())
        }
        fn release(&mut self, spec: &PathSpec) {
            self.log.push(format!("up {} {:?}", spec.button, spec.modifiers));
        }
    }

    #[test]
    fn the_plan_crosses_the_threshold_then_holds_then_moves_then_hovers() {
        let s = spec(vec![(0, 0), (200, 0)], 150, 400, 300);
        let p = plan(&s, 4);
        let first = p.moves.first().copied().unwrap_or(Timed { x: -1, y: -1, at_ms: 0 });
        assert_eq!((first.x, first.y, first.at_ms), (5, 0, 16), "threshold + 1 px, one tick after down");
        let second = p.moves.get(1).copied().unwrap_or(first);
        assert!(second.at_ms >= 16 + 150, "the hold comes after the drag start");
        let last = p.moves.last().copied().unwrap_or(first);
        assert_eq!((last.x, last.y, last.at_ms), (200, 0, 16 + 150 + 400));
        assert_eq!(p.release_at_ms, 16 + 150 + 400 + 300);
        assert!(p.moves.len() >= 25, "about 60 moves a second: {}", p.moves.len());
        assert!(p.moves.windows(2).all(|w| w[0].at_ms <= w[1].at_ms), "times never go back");
    }

    #[test]
    fn every_vertex_is_visited() {
        let star: Vec<(i32, i32)> = (0..40).map(|i| if i % 2 == 0 { (i * 10, 0) } else { (i * 10, 100) }).collect();
        let p = plan(&spec(star.clone(), 0, 100, 0), 4);
        for v in star.iter().skip(1) {
            assert!(p.moves.iter().any(|m| (m.x, m.y) == *v), "vertex {v:?} skipped");
        }
    }

    #[test]
    fn a_completed_path_releases_once_in_order() {
        let s = spec(vec![(0, 0), (100, 50)], 0, 100, 0);
        let mut io = Io::new();
        let report = follow(&mut io, &s, &plan(&s, 4)).unwrap_or(PathReport { completed: false, interrupted: None, moved: 0 });
        assert!(report.completed && report.interrupted.is_none());
        assert_eq!(io.log, vec!["down [\"ctrl\"] left".to_string(), "up left [\"ctrl\"]".to_string()]);
        assert_eq!(io.cursor, (100, 50));
    }

    #[test]
    fn the_user_taking_the_mouse_stops_and_releases() {
        let s = spec(vec![(0, 0), (300, 0)], 0, 1000, 0);
        let mut io = Io::new();
        io.grab_at = Some(200);
        let report = follow(&mut io, &s, &plan(&s, 4)).unwrap_or(PathReport { completed: true, interrupted: None, moved: 0 });
        assert!(!report.completed);
        assert_eq!(report.interrupted, Some("user_mouse"));
        assert_eq!(io.log.last().map(String::as_str), Some("up left [\"ctrl\"]"));
    }

    #[test]
    fn an_input_error_still_releases() {
        let s = spec(vec![(0, 0), (300, 0)], 0, 400, 0);
        let mut io = Io::new();
        io.fail_move_at = Some(5);
        assert!(follow(&mut io, &s, &plan(&s, 4)).is_err());
        assert_eq!(io.log.len(), 2, "pressed once, released once");
        assert!(io.log.last().is_some_and(|l| l.starts_with("up")));
    }

    #[test]
    fn arguments_are_checked_before_anything_moves() {
        let bad = [
            json!({"points": [[0, 0]]}),
            json!({"points": [[0, 0], [1]]}),
            json!({"points": [[0, 0], [1, 1]], "duration_ms": 6000}),
            json!({"points": [[0, 0], [1, 1]], "hold_ms": 2001}),
            json!({"points": [[0, 0], [1, 1]], "modifiers": ["ctrl", "ctrl"]}),
            json!({"points": [[0, 0], [1, 1]], "modifiers": ["hyper"]}),
            json!({"points": [[0, 0], [1, 1]], "button": "side"}),
        ];
        for args in bad {
            let map = args.as_object().cloned().unwrap_or_default();
            assert!(parse(&map).is_err(), "{args}");
        }
        let ok = json!({"points": [[0, 0], [1, 1]], "modifiers": ["CTRL", "shift"], "hover_ms": 300});
        let parsed = parse(ok.as_object().unwrap_or(&Map::new())).ok();
        assert_eq!(parsed.map(|p| (p.modifiers, p.hover_ms, p.duration_ms)), Some((vec!["ctrl", "shift"], 300, 400)));
    }
}

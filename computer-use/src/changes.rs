//! Repaint reports for `screen_changes` (CTR-0236 `screen.changes`, PRP-0191 A1, UDR-0173 D10-D12).
//!
//! A [`ChangeLog`] numbers every frame that carried a new desktop image and remembers the
//! rectangles it repainted. It answers "was anything inside `rect` repainted since token N?"
//! -- a TRIGGER for the host, never a verdict: a repaint is not a pixel change (a blinking
//! caret, an identical redraw), and the host still compares thumbnails (UDR-0173 D10).
//!
//! When the log cannot say (the token predates the current duplication, or its entries were
//! evicted) the answer is "changed", so the host looks rather than misses a change.

use std::collections::VecDeque;

use serde_json::{Value, json};

use crate::protocol::{Rect, rect_json};

/// Frames' repaint rectangles kept per log.
pub const LOG_ENTRIES: usize = 512;
/// Upper bound of `timeout_ms`.
pub const MAX_TIMEOUT_MS: i32 = 2000;
/// Upper bound of `ignore` rectangles.
pub const MAX_IGNORE: usize = 64;
/// Rectangles returned per answer (informational).
pub const MAX_RECTS: usize = 16;

/// The answer to one `screen_changes` call.
#[derive(Clone, Debug, PartialEq)]
pub struct ChangeReport {
    pub available: bool,
    pub seq: u64,
    pub changed: bool,
    pub rects: Vec<Rect>,
    pub reason: Option<&'static str>,
}

impl ChangeReport {
    /// This rectangle cannot be watched now; the host polls instead.
    pub fn unavailable(seq: u64, reason: &'static str) -> Self {
        Self { available: false, seq, changed: false, rects: Vec::new(), reason: Some(reason) }
    }

    pub fn to_json(&self) -> Value {
        let mut body = json!({
            "available": self.available,
            "seq": self.seq,
            "changed": self.changed,
            "rects": self.rects.iter().map(rect_json).collect::<Vec<_>>(),
        });
        if let Some(reason) = self.reason {
            body["reason"] = Value::String(reason.into());
        }
        body
    }
}

pub fn intersect(a: &Rect, b: &Rect) -> Option<Rect> {
    let r = Rect::new(a.left.max(b.left), a.top.max(b.top), a.right.min(b.right), a.bottom.min(b.bottom));
    (r.width() > 0 && r.height() > 0).then_some(r)
}

/// `r` minus `cut`: up to four pieces.
fn subtract(r: &Rect, cut: &Rect) -> Vec<Rect> {
    let Some(i) = intersect(r, cut) else { return vec![*r] };
    let mut out = Vec::with_capacity(4);
    if r.top < i.top {
        out.push(Rect::new(r.left, r.top, r.right, i.top));
    }
    if i.bottom < r.bottom {
        out.push(Rect::new(r.left, i.bottom, r.right, r.bottom));
    }
    if r.left < i.left {
        out.push(Rect::new(r.left, i.top, i.left, i.bottom));
    }
    if i.right < r.right {
        out.push(Rect::new(i.right, i.top, r.right, i.bottom));
    }
    out
}

/// Whether the union of `ignore` covers `r` completely.
pub fn covered(r: &Rect, ignore: &[Rect]) -> bool {
    let mut left = vec![*r];
    for cut in ignore {
        left = left.iter().flat_map(|piece| subtract(piece, cut)).collect();
        if left.is_empty() {
            return true;
        }
    }
    left.is_empty()
}

#[derive(Default)]
pub struct ChangeLog {
    /// The last frame number handed out (tokens are frame numbers).
    seq: u64,
    /// Tokens older than this cannot be answered (a new duplication, or evicted entries).
    complete_from: u64,
    entries: VecDeque<(u64, Rect)>,
}

impl ChangeLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn seq(&self) -> u64 {
        self.seq
    }

    /// A new source of frames (a fresh duplication): earlier tokens lose their history.
    pub fn restart(&mut self) {
        self.seq += 1;
        self.complete_from = self.seq;
        self.entries.clear();
    }

    /// One frame that repainted `rects` (virtual-desktop coordinates).
    pub fn push_frame(&mut self, rects: impl IntoIterator<Item = Rect>) {
        self.seq += 1;
        for r in rects {
            if r.width() > 0 && r.height() > 0 {
                self.entries.push_back((self.seq, r));
            }
        }
        while self.entries.len() > LOG_ENTRIES {
            if let Some((evicted, _)) = self.entries.pop_front() {
                // A token before the evicted frame no longer sees all of its repaints.
                self.complete_from = self.complete_from.max(evicted);
            }
        }
    }

    /// Repaints inside `rect` since token `since`, minus those covered by `ignore`.
    pub fn report(&self, rect: &Rect, since: u64, ignore: &[Rect]) -> ChangeReport {
        if since < self.complete_from {
            return ChangeReport { available: true, seq: self.seq, changed: true, rects: Vec::new(), reason: None };
        }
        let mut rects = Vec::new();
        let mut changed = false;
        for (seq, r) in &self.entries {
            if *seq <= since {
                continue;
            }
            let Some(clipped) = intersect(r, rect) else { continue };
            if covered(&clipped, ignore) {
                continue;
            }
            changed = true;
            if rects.len() < MAX_RECTS && !rects.contains(&clipped) {
                rects.push(clipped);
            }
        }
        ChangeReport { available: true, seq: self.seq, changed, rects, reason: None }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(l: i32, t: i32, rr: i32, b: i32) -> Rect {
        Rect::new(l, t, rr, b)
    }

    #[test]
    fn repaints_are_reported_after_the_token_only() {
        let mut log = ChangeLog::new();
        log.restart();
        log.push_frame([r(0, 0, 10, 10)]);
        let token = log.seq();
        assert!(!log.report(&r(0, 0, 100, 100), token, &[]).changed);
        log.push_frame([r(50, 50, 60, 60)]);
        let got = log.report(&r(0, 0, 100, 100), token, &[]);
        assert!(got.changed);
        assert_eq!(got.rects, vec![r(50, 50, 60, 60)]);
        assert!(!log.report(&r(0, 0, 40, 40), token, &[]).changed, "outside the watched rect");
    }

    #[test]
    fn ignored_areas_do_not_count() {
        let mut log = ChangeLog::new();
        log.restart();
        let token = log.seq();
        log.push_frame([r(10, 10, 30, 20)]);
        // Two ignore rects that only TOGETHER cover the repaint.
        let ignore = [r(0, 0, 20, 40), r(20, 0, 40, 40)];
        assert!(!log.report(&r(0, 0, 100, 100), token, &ignore).changed);
        assert!(log.report(&r(0, 0, 100, 100), token, &ignore[..1]).changed);
    }

    #[test]
    fn unknown_history_is_a_change() {
        let mut log = ChangeLog::new();
        log.restart();
        let old = log.seq();
        log.restart(); // the duplication was re-created
        let got = log.report(&r(0, 0, 10, 10), old, &[]);
        assert!(got.changed && got.rects.is_empty());

        let token = log.seq();
        for i in 0..(LOG_ENTRIES as i32 + 1) {
            log.push_frame([r(1000 + i, 0, 1001 + i, 1)]);
        }
        assert!(log.report(&r(0, 0, 10, 10), token, &[]).changed, "evicted entries");
    }

    #[test]
    fn subtraction_covers_exactly() {
        assert!(covered(&r(0, 0, 10, 10), &[r(-5, -5, 15, 15)]));
        assert!(!covered(&r(0, 0, 10, 10), &[r(0, 0, 10, 9)]));
        assert!(covered(&r(0, 0, 10, 10), &[r(0, 0, 10, 5), r(0, 5, 5, 10), r(5, 5, 10, 10)]));
    }
}

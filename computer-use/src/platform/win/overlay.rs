//! The glow around the controlled window (`ui.overlay`, PRP-0192 Part A, UDR-0174 D1-D4).
//!
//! Four thin layered windows (top, bottom, left, right) form a soft band around the target's
//! visible frame. Rules:
//!
//! * CAPTURE-EXCLUDED: every strip gets `WDA_EXCLUDEFROMCAPTURE` before it is shown; if that
//!   fails (Windows older than 10 2004) the glow is never shown (D1);
//! * NEVER IN THE WAITS: the band lies OUTSIDE the frame, where the waits do not look; when the
//!   target covers its monitor the band goes inside the edges and its rectangles (and those it
//!   just left) are subtracted from the change log through `self_rects` (D2). The glow is
//!   static: it is redrawn only when the target moves or resizes;
//! * INVISIBLE: layered, click-through, non-activating tool windows of this process -- no
//!   taskbar, no Alt+Tab, never foreground; `windows_list` drops this process's windows (D3);
//! * LIFETIME: shown / renewed by `overlay_show`, hidden by `overlay_hide`, after `ttl_ms`, and
//!   while the target is minimized, hidden or gone; it dies with the process (D4).
//!
//! The strips live on their own thread with a message pump (the desktop thread blocks during
//! waits and input). The target is followed by polling its frame every 50 ms (simpler and more
//! robust than a cross-process WinEvent hook; see PRP-0192 implementation notes).

use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use windows::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, POINT, RECT, SIZE, WPARAM};
use windows::Win32::Graphics::Dwm::{DWMWA_EXTENDED_FRAME_BOUNDS, DwmGetWindowAttribute};
use windows::Win32::Graphics::Gdi::{
    AC_SRC_ALPHA, AC_SRC_OVER, BI_RGB, BITMAPINFO, BITMAPINFOHEADER, BLENDFUNCTION, CreateCompatibleDC,
    CreateDIBSection, DIB_RGB_COLORS, DeleteDC, DeleteObject, GetDC, GetMonitorInfoW, HGDIOBJ,
    MONITOR_DEFAULTTONEAREST, MONITORINFO, MonitorFromWindow, ReleaseDC, SelectObject,
};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::HiDpi::{DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, GetDpiForWindow, SetThreadDpiAwarenessContext};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, GetWindowRect, HWND_TOPMOST, IsIconic, IsWindow,
    IsWindowVisible, MSG, PM_REMOVE, PeekMessageW, RegisterClassW, SW_HIDE, SW_SHOWNOACTIVATE, SWP_NOACTIVATE,
    SWP_NOMOVE, SWP_NOSIZE, SetWindowDisplayAffinity, SetWindowPos, ShowWindow, TranslateMessage, ULW_ALPHA,
    UpdateLayeredWindow, WDA_EXCLUDEFROMCAPTURE, WINDOW_EX_STYLE, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_POPUP,
};
use windows::core::w;

use crate::protocol::Rect;

/// The brand's ocean teal (PRP-0192 Q3), as B, G, R.
const COLOR: (f64, f64, f64) = (0xB7 as f64, 0xA5 as f64, 0x0E as f64);
/// Peak opacity at the frame edge.
const MAX_ALPHA: f64 = 0.55;
/// Band width in logical px (scaled by the target's DPI).
const BAND_LOGICAL: i32 = 10;
/// How often the target's frame is re-read.
const TICK: Duration = Duration::from_millis(50);
/// A rectangle the band just left stays subtracted from the change log this long.
const VACATED_FOR: Duration = Duration::from_millis(500);

type Reply = Sender<(bool, Option<&'static str>)>;

enum Cmd {
    Show { hwnd: isize, ttl: Duration, reply: Reply },
    Hide,
}

static CONTROL: OnceLock<Mutex<Option<Sender<Cmd>>>> = OnceLock::new();
/// Rectangles the glow covers now, and those it left recently (for the change log).
static SELF_RECTS: Mutex<Vec<(Rect, Option<Instant>)>> = Mutex::new(Vec::new());

/// The glow's own screen area (current and recently vacated): the change log ignores it.
pub fn self_rects() -> Vec<Rect> {
    let now = Instant::now();
    let Ok(mut rects) = SELF_RECTS.lock() else { return Vec::new() };
    rects.retain(|(_, until)| until.is_none_or(|t| now < t));
    rects.iter().map(|(r, _)| *r).collect()
}

fn publish(current: &[Rect]) {
    let Ok(mut rects) = SELF_RECTS.lock() else { return };
    let now = Instant::now();
    for (_, until) in rects.iter_mut() {
        if until.is_none() {
            *until = Some(now + VACATED_FOR);
        }
    }
    rects.extend(current.iter().map(|r| (*r, None)));
}

fn sender() -> Option<Sender<Cmd>> {
    let slot = CONTROL.get_or_init(|| Mutex::new(None));
    let mut guard = slot.lock().ok()?;
    if guard.is_none() {
        let (tx, rx) = mpsc::channel();
        let spawned = std::thread::Builder::new().name("overlay".into()).spawn(move || run(rx));
        if spawned.is_err() {
            return None;
        }
        *guard = Some(tx);
    }
    guard.clone()
}

/// Show (or renew) the glow around `hwnd`: (shown, reason).
pub fn show(hwnd: i64, ttl: Duration) -> (bool, Option<&'static str>) {
    let Some(tx) = sender() else { return (false, Some("unavailable")) };
    let (reply, answer) = mpsc::channel();
    if tx.send(Cmd::Show { hwnd: hwnd as isize, ttl, reply }).is_err() {
        return (false, Some("unavailable"));
    }
    answer.recv_timeout(Duration::from_secs(2)).unwrap_or((false, Some("unavailable")))
}

pub fn hide() {
    if let Some(Ok(guard)) = CONTROL.get().map(Mutex::lock)
        && let Some(tx) = guard.as_ref()
    {
        let _ = tx.send(Cmd::Hide);
    }
}

unsafe extern "system" fn wndproc(h: HWND, msg: u32, w: WPARAM, l: LPARAM) -> LRESULT {
    unsafe { DefWindowProcW(h, msg, w, l) }
}

struct Glow {
    strips: [HWND; 4],
    target: Option<HWND>,
    until: Instant,
    drawn: Option<(RECT, bool, i32)>,
}

fn create_strips() -> Option<[HWND; 4]> {
    unsafe {
        let instance = GetModuleHandleW(None).ok()?;
        let class = WNDCLASSW {
            lpfnWndProc: Some(wndproc),
            hInstance: instance.into(),
            lpszClassName: w!("ChatWalaauComputerUseGlow"),
            ..Default::default()
        };
        RegisterClassW(&class);
        let ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST;
        let mut strips = [HWND::default(); 4];
        for slot in strips.iter_mut() {
            let h = CreateWindowExW(
                WINDOW_EX_STYLE(ex.0),
                w!("ChatWalaauComputerUseGlow"),
                w!(""),
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                Some(instance.into()),
                None,
            )
            .ok()?;
            // D1: never shown unless excluded from every capture path.
            if SetWindowDisplayAffinity(h, WDA_EXCLUDEFROMCAPTURE).is_err() {
                tracing::debug!("WDA_EXCLUDEFROMCAPTURE unavailable; the glow is disabled");
                return None;
            }
            *slot = h;
        }
        Some(strips)
    }
}

fn frame(h: HWND) -> Option<RECT> {
    unsafe {
        if !IsWindow(Some(h)).as_bool() || !IsWindowVisible(h).as_bool() || IsIconic(h).as_bool() {
            return None;
        }
        let mut r = RECT::default();
        let got = DwmGetWindowAttribute(
            h,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            &mut r as *mut RECT as *mut _,
            size_of::<RECT>() as u32,
        );
        if got.is_err() && GetWindowRect(h, &mut r).is_err() {
            return None;
        }
        (r.right > r.left && r.bottom > r.top).then_some(r)
    }
}

fn monitor_of(h: HWND) -> RECT {
    unsafe {
        let mut info = MONITORINFO { cbSize: size_of::<MONITORINFO>() as u32, ..Default::default() };
        let _ = GetMonitorInfoW(MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST), &mut info);
        info.rcMonitor
    }
}

/// The four strips for `f` (outside the frame, or inside when there is no room).
fn strips_for(f: RECT, b: i32, inside: bool) -> [RECT; 4] {
    if inside {
        [
            RECT { left: f.left, top: f.top, right: f.right, bottom: f.top + b },
            RECT { left: f.left, top: f.bottom - b, right: f.right, bottom: f.bottom },
            RECT { left: f.left, top: f.top + b, right: f.left + b, bottom: f.bottom - b },
            RECT { left: f.right - b, top: f.top + b, right: f.right, bottom: f.bottom - b },
        ]
    } else {
        [
            RECT { left: f.left - b, top: f.top - b, right: f.right + b, bottom: f.top },
            RECT { left: f.left - b, top: f.bottom, right: f.right + b, bottom: f.bottom + b },
            RECT { left: f.left - b, top: f.top, right: f.left, bottom: f.bottom },
            RECT { left: f.right, top: f.top, right: f.right + b, bottom: f.bottom },
        ]
    }
}

/// Opacity of the pixel centred at (x, y): strongest at the frame edge, fading over the band.
fn alpha_at(f: RECT, b: i32, inside: bool, x: f64, y: f64) -> f64 {
    let (l, t, r, btm) = (f64::from(f.left), f64::from(f.top), f64::from(f.right), f64::from(f.bottom));
    let d = if inside {
        (x - l).min(r - x).min(y - t).min(btm - y).max(0.0)
    } else {
        let dx = (l - x).max(x - r).max(0.0);
        let dy = (t - y).max(y - btm).max(0.0);
        (dx * dx + dy * dy).sqrt()
    };
    let k = (1.0 - d / f64::from(b.max(1))).clamp(0.0, 1.0);
    MAX_ALPHA * k * k
}

fn paint(h: HWND, s: RECT, f: RECT, b: i32, inside: bool) {
    let (w, ht) = (s.right - s.left, s.bottom - s.top);
    if w <= 0 || ht <= 0 {
        unsafe {
            let _ = ShowWindow(h, SW_HIDE);
        }
        return;
    }
    unsafe {
        let screen = GetDC(None);
        let mem = CreateCompatibleDC(Some(screen));
        let info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -ht,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        if let Ok(bmp) = CreateDIBSection(Some(mem), &info, DIB_RGB_COLORS, &mut bits, None, 0)
            && !bits.is_null()
        {
            let px = std::slice::from_raw_parts_mut(bits as *mut u8, (w * ht * 4) as usize);
            for (i, chunk) in px.chunks_exact_mut(4).enumerate() {
                let (xi, yi) = ((i as i32) % w, (i as i32) / w);
                let a = alpha_at(f, b, inside, f64::from(s.left + xi) + 0.5, f64::from(s.top + yi) + 0.5);
                // Premultiplied BGRA.
                chunk.copy_from_slice(&[(COLOR.0 * a) as u8, (COLOR.1 * a) as u8, (COLOR.2 * a) as u8, (255.0 * a) as u8]);
            }
            let old = SelectObject(mem, HGDIOBJ(bmp.0));
            let blend = BLENDFUNCTION {
                BlendOp: AC_SRC_OVER as u8,
                BlendFlags: 0,
                SourceConstantAlpha: 255,
                AlphaFormat: AC_SRC_ALPHA as u8,
            };
            let pos = POINT { x: s.left, y: s.top };
            let size = SIZE { cx: w, cy: ht };
            let src = POINT { x: 0, y: 0 };
            let _ = UpdateLayeredWindow(
                h,
                Some(screen),
                Some(&pos),
                Some(&size),
                Some(mem),
                Some(&src),
                COLORREF(0),
                Some(&blend),
                ULW_ALPHA,
            );
            SelectObject(mem, old);
            let _ = DeleteObject(HGDIOBJ(bmp.0));
            let _ = ShowWindow(h, SW_SHOWNOACTIVATE);
            let _ = SetWindowPos(h, Some(HWND_TOPMOST), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
    }
}

impl Glow {
    fn hide(&mut self) {
        for h in self.strips {
            unsafe {
                let _ = ShowWindow(h, SW_HIDE);
            }
        }
        self.drawn = None;
        publish(&[]);
    }

    /// Follow the target; false when it cannot be shown (not visible / gone).
    fn update(&mut self) -> bool {
        let Some(target) = self.target else { return false };
        let Some(f) = frame(target) else {
            self.hide();
            return false;
        };
        let dpi = unsafe { GetDpiForWindow(target) }.max(96);
        let b = (BAND_LOGICAL * dpi as i32 + 48) / 96;
        let m = monitor_of(target);
        let inside = f.left - b < m.left || f.top - b < m.top || f.right + b > m.right || f.bottom + b > m.bottom;
        if self.drawn == Some((f, inside, b)) {
            return true;
        }
        let rects = strips_for(f, b, inside);
        for (h, s) in self.strips.iter().zip(rects.iter()) {
            paint(*h, *s, f, b, inside);
        }
        let r = |s: &RECT| Rect::new(s.left, s.top, s.right, s.bottom);
        publish(&rects.iter().map(r).collect::<Vec<_>>());
        self.drawn = Some((f, inside, b));
        true
    }
}

fn run(rx: Receiver<Cmd>) {
    unsafe {
        let _ = SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    let strips = create_strips();
    let mut glow = strips.map(|strips| Glow { strips, target: None, until: Instant::now(), drawn: None });
    loop {
        match rx.recv_timeout(TICK) {
            Ok(Cmd::Show { hwnd, ttl, reply }) => {
                let answer = match glow.as_mut() {
                    None => (false, Some("capture_exclusion_unavailable")),
                    Some(g) => {
                        let target = HWND(hwnd as *mut _);
                        if !unsafe { IsWindow(Some(target)) }.as_bool() {
                            (false, Some("not_found"))
                        } else {
                            if g.target != Some(target) {
                                g.drawn = None;
                            }
                            g.target = Some(target);
                            g.until = Instant::now() + ttl;
                            if g.update() { (true, None) } else { (false, Some("not_visible")) }
                        }
                    }
                };
                let _ = reply.send(answer);
            }
            Ok(Cmd::Hide) => {
                if let Some(g) = glow.as_mut() {
                    g.target = None;
                    g.hide();
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return,
        }
        if let Some(g) = glow.as_mut()
            && g.target.is_some()
        {
            if Instant::now() >= g.until {
                g.target = None;
                g.hide();
            } else {
                g.update();
            }
        }
        unsafe {
            let mut msg = MSG::default();
            while PeekMessageW(&mut msg, None, 0, 0, PM_REMOVE).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_sit_outside_or_inside_the_frame() {
        let f = RECT { left: 100, top: 100, right: 500, bottom: 400 };
        let outside = strips_for(f, 10, false);
        for s in outside {
            let overlaps = s.left < f.right && f.left < s.right && s.top < f.bottom && f.top < s.bottom;
            assert!(!overlaps, "an outside strip never covers the watched frame: {s:?}");
        }
        let inside = strips_for(f, 10, true);
        for s in inside {
            assert!(s.left >= f.left && s.right <= f.right && s.top >= f.top && s.bottom <= f.bottom);
        }
    }

    #[test]
    fn the_band_fades_away_from_the_edge() {
        let f = RECT { left: 100, top: 100, right: 500, bottom: 400 };
        let at_edge = alpha_at(f, 10, false, 99.5, 250.0);
        let far = alpha_at(f, 10, false, 90.5, 250.0);
        let beyond = alpha_at(f, 10, false, 80.0, 250.0);
        assert!(at_edge > far && far > beyond && beyond == 0.0);
        assert!(at_edge <= MAX_ALPHA);
    }
}

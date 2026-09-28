//! The Windows desktop (PRP-0191 2a) -- a line-by-line port of the Python reference
//! (``chatwalaau_desktop_provider/win32.py``, PRP-0189 / PRP-0190).
//!
//! * windows: ``user32`` / ``dwmapi`` (visible frame bounds, owner, tool windows, cloaking,
//!   process image name, monitor index), restore / resize / foreground with the Alt-tap rule;
//! * capture: GDI ``BitBlt`` (the reference path, what ``mss`` does) and DXGI Desktop
//!   Duplication as an internal fast path with GDI fallback (UDR-0173 D3, `dxgi.rs`);
//! * repaint reports (`screen.changes`, PRP-0191 A1): DXGI dirty / move rectangles, from the
//!   same duplication that captures (`dxgi.rs`);
//! * path input (`input.path`, PRP-0192): the follower of `crate::path` over `SetCursorPos` +
//!   `SendInput`, with the system drag threshold; the capture-excluded glow (`ui.overlay`,
//!   `overlay.rs`);
//! * elements: UI Automation (control view walk, interactive controls first);
//! * input: ``SendInput`` -- mouse, key chords (extended keys, modifier order) and Unicode
//!   text (``KEYEVENTF_UNICODE``); clipboard paste with restore (``arboard``).
//!
//! EVERY method runs on the one desktop thread, which is per-monitor-v2 DPI aware and
//! COM-initialised (STA); UI Automation elements never leave it.

mod dxgi;
mod overlay;

use std::collections::HashMap;
use std::ffi::c_void;
use std::time::{Duration, Instant};

use uiautomation::types::Handle;
use uiautomation::{UIAutomation, UIElement, UITreeWalker};
use windows::Win32::Foundation::{CloseHandle, HWND, LPARAM, POINT, RECT};
use windows::Win32::Graphics::Dwm::{DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS, DwmGetWindowAttribute};
use windows::Win32::Graphics::Gdi::{
    BI_RGB, BITMAPINFO, BITMAPINFOHEADER, BitBlt, CAPTUREBLT, ClientToScreen, CreateCompatibleBitmap,
    CreateCompatibleDC, DIB_RGB_COLORS, DeleteDC, DeleteObject, EnumDisplayMonitors, GetDC, GetDIBits,
    GetMonitorInfoW, HDC, HGDIOBJ, HMONITOR, MONITOR_DEFAULTTONEAREST, MONITORINFO, MonitorFromWindow, ReleaseDC,
    SRCCOPY, SelectObject,
};
use windows::Win32::System::Com::{COINIT_APARTMENTTHREADED, CoInitializeEx};
use windows::Win32::System::StationsAndDesktops::{
    CloseDesktop, DESKTOP_CONTROL_FLAGS, DESKTOP_SWITCHDESKTOP, GetUserObjectInformationW, OpenInputDesktop, UOI_NAME,
};
use windows::Win32::System::Threading::{
    GetCurrentProcessId, OpenProcess, PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION,
    QueryFullProcessImageNameW,
};
use windows::Win32::UI::HiDpi::{DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, SetThreadDpiAwarenessContext};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    INPUT, INPUT_0, INPUT_KEYBOARD, INPUT_MOUSE, IsWindowEnabled, KEYBD_EVENT_FLAGS, KEYBDINPUT, KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, MOUSE_EVENT_FLAGS, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_WHEEL,
    MOUSEEVENTF_MOVE, MOUSEINPUT, SendInput, VIRTUAL_KEY, GetAsyncKeyState,
};
use windows::Win32::UI::WindowsAndMessaging::{
    BringWindowToTop, EnumWindows, GUITHREADINFO, GW_OWNER, GWL_EXSTYLE, GetCursorPos, GetForegroundWindow,
    GetGUIThreadInfo, GetWindow, GetWindowLongW, GetWindowRect, GetWindowTextLengthW, GetWindowTextW,
    GetWindowThreadProcessId, IsIconic, IsWindow, IsWindowVisible, IsZoomed, SW_RESTORE, SWP_NOACTIVATE,
    SWP_NOZORDER, SetCursorPos, SetForegroundWindow, SetWindowPos, ShowWindow, WS_EX_TOOLWINDOW, GetSystemMetrics,
    SM_CXDRAG, SM_CYDRAG,
};
use windows::core::{BOOL, PWSTR};

use crate::changes::ChangeReport;
use crate::desktop::{Captured, Desktop};
use crate::path::{PathIo, PathReport, PathSpec};
use crate::imaging::{RgbImage, encode_png, gray_thumbnail};
use crate::protocol::*;

const WHEEL_DELTA: i32 = 120;
const VK_MENU: u16 = 0x12;
const VK_RETURN: u16 = 0x0D;
/// Keys that need ``KEYEVENTF_EXTENDEDKEY`` (page / arrows / home / end / ins / del / apps).
/// and the Windows keys, which only `input_path` modifiers press.
const EXTENDED_VK: [u16; 13] = [0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5D, 0x5B, 0x5C];
const MODIFIERS: [&str; 3] = ["ctrl", "shift", "alt"];

/// Control types offered as click targets, in priority order (PRP-0189 Section 2.7).
/// UIA control type ids: Button 50000, CheckBox 50002, ComboBox 50003, Edit 50004,
/// Hyperlink 50005, ListItem 50007, MenuItem 50011, RadioButton 50013, Slider 50015,
/// Spinner 50016, TabItem 50019, TreeItem 50024, DataItem 50029, Document 50030,
/// SplitButton 50031, MenuBar 50010; secondary: Text 50020, HeaderItem 50035, Image 50006.
fn control_kind(id: i32) -> Option<(&'static str, bool)> {
    Some(match id {
        50000 => ("Button", true),
        50004 => ("Edit", true),
        50003 => ("ComboBox", true),
        50002 => ("CheckBox", true),
        50013 => ("RadioButton", true),
        50005 => ("Hyperlink", true),
        50011 => ("MenuItem", true),
        50019 => ("TabItem", true),
        50007 => ("ListItem", true),
        50024 => ("TreeItem", true),
        50029 => ("DataItem", true),
        50031 => ("SplitButton", true),
        50015 => ("Slider", true),
        50016 => ("Spinner", true),
        50030 => ("Document", true),
        50010 => ("MenuBar", true),
        50020 => ("Text", false),
        50035 => ("HeaderItem", false),
        50006 => ("Image", false),
        _ => return None,
    })
}

/// Kinds that are useful targets even without a name (an input box, a document).
const UNNAMED_OK: [&str; 3] = ["Edit", "Document", "ComboBox"];
/// All UIA control type names by id, for ``ui_find``'s control filter.
fn control_name(id: i32) -> &'static str {
    const NAMES: [&str; 41] = [
        "Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink", "Image", "ListItem", "List", "Menu",
        "MenuBar", "MenuItem", "ProgressBar", "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab",
        "TabItem", "Text", "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom", "Group", "Thumb", "DataGrid",
        "DataItem", "Document", "SplitButton", "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar",
        "Separator", "SemanticZoom", "AppBar",
    ];
    usize::try_from(id - 50000).ok().and_then(|i| NAMES.get(i)).copied().unwrap_or("")
}

fn vk_of(key: &str) -> Option<u16> {
    let k = match key {
        "enter" => 0x0D,
        "tab" => 0x09,
        "esc" | "escape" => 0x1B,
        "space" => 0x20,
        "backspace" => 0x08,
        "delete" => 0x2E,
        "insert" => 0x2D,
        "home" => 0x24,
        "end" => 0x23,
        "pageup" => 0x21,
        "pagedown" => 0x22,
        "left" => 0x25,
        "up" => 0x26,
        "right" => 0x27,
        "down" => 0x28,
        "ctrl" => 0x11,
        "shift" => 0x10,
        "alt" => 0x12,
        "apps" => 0x5D,
        "-" => 0xBD,
        "=" => 0xBB,
        "," => 0xBC,
        "." => 0xBE,
        "/" => 0xBF,
        ";" => 0xBA,
        "'" => 0xDE,
        "[" => 0xDB,
        "]" => 0xDD,
        "\\" => 0xDC,
        "`" => 0xC0,
        _ => {
            let b = key.as_bytes();
            if b.len() == 1 && b[0].is_ascii_lowercase() {
                (b[0] - 32) as u16
            } else if b.len() == 1 && b[0].is_ascii_digit() {
                b[0] as u16
            } else if let Some(n) = key.strip_prefix('f').and_then(|n| n.parse::<u16>().ok()) {
                if (1..=24).contains(&n) { 0x6F + n } else { return None }
            } else {
                return None;
            }
        }
    };
    Some(k)
}

pub fn init_desktop_thread() {
    unsafe {
        // Per-monitor-v2: rectangles, capture and input in physical pixels on every monitor.
        if SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2).0.is_null() {
            tracing::warn!("per-monitor DPI awareness unavailable; coordinates may be scaled");
        }
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
    }
}

fn hwnd(h: i64) -> HWND {
    HWND(h as isize as *mut c_void)
}

fn rect_of(r: &RECT) -> Rect {
    Rect::new(r.left, r.top, r.right, r.bottom)
}

fn blocked(sent: u32, total: usize) -> ProviderError {
    let err = unsafe { windows::Win32::Foundation::GetLastError() };
    ProviderError::new(
        ErrorCode::InputBlocked,
        format!("SendInput delivered {sent}/{total} events (error {}); UIPI may block it", err.0),
    )
}

fn internal(what: &str) -> ProviderError {
    ProviderError::new(ErrorCode::Internal, what.to_string())
}

// ---- capture ----------------------------------------------------------------------------------

struct Frame {
    rect: Rect,
    image: RgbImage,
    gray: Vec<u8>,
}

impl Captured for Frame {
    fn rect(&self) -> Rect {
        self.rect
    }
    fn gray(&self) -> Vec<u8> {
        self.gray.clone()
    }
    fn encode(&self, w: u32, h: u32) -> OpResult<Vec<u8>> {
        encode_png(&self.image, w, h).map_err(|_| internal("PngEncodeError"))
    }
}

/// GDI ``BitBlt`` of a virtual-desktop rectangle (the reference path; cursor not drawn).
fn capture_gdi(rect: Rect) -> OpResult<RgbImage> {
    let (w, h) = (rect.width(), rect.height());
    unsafe {
        let screen = GetDC(None);
        if screen.is_invalid() {
            return Err(internal("GetDCFailed"));
        }
        let mem = CreateCompatibleDC(Some(screen));
        let bmp = CreateCompatibleBitmap(screen, w, h);
        let old = SelectObject(mem, HGDIOBJ(bmp.0));
        let blit = BitBlt(mem, 0, 0, w, h, Some(screen), rect.left, rect.top, SRCCOPY | CAPTUREBLT);
        let mut info = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h, // top-down
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut buf = vec![0u8; (w * h * 4) as usize];
        let lines = if blit.is_ok() {
            GetDIBits(mem, bmp, 0, h as u32, Some(buf.as_mut_ptr().cast()), &mut info, DIB_RGB_COLORS)
        } else {
            0
        };
        SelectObject(mem, old);
        let _ = DeleteObject(HGDIOBJ(bmp.0));
        let _ = DeleteDC(mem);
        ReleaseDC(None, screen);
        if blit.is_err() || lines != h {
            return Err(internal("BitBltFailed"));
        }
        Ok(RgbImage::from_bgra(w as u32, h as u32, &buf, (w * 4) as usize))
    }
}

// ---- the desktop ------------------------------------------------------------------------------

pub struct Win32Desktop {
    uia: UIAutomation,
    walker: UITreeWalker,
    elements: HashMap<u64, UIElement>,
    next_element: u64,
    dxgi: dxgi::Duplicator,
}

impl Win32Desktop {
    pub fn new() -> Result<Self, String> {
        let uia = UIAutomation::new_direct().map_err(|e| format!("UI Automation: {e}"))?;
        let walker = uia.get_control_view_walker().map_err(|e| format!("UI Automation walker: {e}"))?;
        Ok(Self { uia, walker, elements: HashMap::new(), next_element: 0, dxgi: dxgi::Duplicator::new() })
    }

    fn visible_rect(&self, h: HWND) -> Rect {
        let mut r = RECT::default();
        unsafe {
            if DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, (&mut r as *mut RECT).cast(), size_of::<RECT>() as u32)
                .is_err()
            {
                let _ = GetWindowRect(h, &mut r);
            }
        }
        rect_of(&r)
    }

    fn cloaked(h: HWND) -> bool {
        let mut value: u32 = 0;
        unsafe {
            let _ = DwmGetWindowAttribute(h, DWMWA_CLOAKED, (&mut value as *mut u32).cast(), size_of::<u32>() as u32);
        }
        value != 0
    }

    fn process(h: HWND) -> (String, u32) {
        let mut pid: u32 = 0;
        let mut name = String::new();
        unsafe {
            GetWindowThreadProcessId(h, Some(&mut pid));
            if let Ok(handle) = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
                let mut buf = [0u16; 1024];
                let mut size = buf.len() as u32;
                if QueryFullProcessImageNameW(handle, PROCESS_NAME_WIN32, PWSTR(buf.as_mut_ptr()), &mut size).is_ok() {
                    let full = String::from_utf16_lossy(&buf[..size as usize]);
                    name = full.rsplit(['\\', '/']).next().unwrap_or("").to_string();
                }
                let _ = CloseHandle(handle);
            }
        }
        (name, pid)
    }

    fn title(h: HWND) -> String {
        unsafe {
            let n = GetWindowTextLengthW(h);
            if n <= 0 {
                return String::new();
            }
            let mut buf = vec![0u16; n as usize + 1];
            let got = GetWindowTextW(h, &mut buf);
            String::from_utf16_lossy(&buf[..got.max(0) as usize])
        }
    }

    fn monitors() -> Vec<isize> {
        unsafe extern "system" fn cb(hmon: HMONITOR, _hdc: HDC, _rect: *mut RECT, lp: LPARAM) -> BOOL {
            let list = unsafe { &mut *(lp.0 as *mut Vec<isize>) };
            list.push(hmon.0 as isize);
            BOOL(1)
        }
        let mut list: Vec<isize> = Vec::new();
        unsafe {
            let _ = EnumDisplayMonitors(None, None, Some(cb), LPARAM(&mut list as *mut Vec<isize> as isize));
        }
        list
    }

    fn info(&self, h: HWND, monitors: &[isize]) -> WindowInfo {
        let (process, pid) = Self::process(h);
        let hmon = unsafe { MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST) }.0 as isize;
        WindowInfo {
            hwnd: h.0 as isize as i64,
            title: Self::title(h),
            process,
            pid,
            rect: self.visible_rect(h),
            monitor: monitors.iter().position(|m| *m == hmon).map(|i| i as i32 + 1).unwrap_or(0),
            minimized: unsafe { IsIconic(h) }.as_bool(),
        }
    }

    fn work_area(h: HWND) -> Rect {
        let mut info = MONITORINFO { cbSize: size_of::<MONITORINFO>() as u32, ..Default::default() };
        unsafe {
            let hmon = MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST);
            let _ = GetMonitorInfoW(hmon, &mut info);
        }
        rect_of(&info.rcWork)
    }

    fn send(events: &[INPUT]) -> OpResult<()> {
        if events.is_empty() {
            return Ok(());
        }
        let sent = unsafe { SendInput(events, size_of::<INPUT>() as i32) };
        if sent as usize != events.len() { Err(blocked(sent, events.len())) } else { Ok(()) }
    }

    fn key(vk: u16, up: bool) -> INPUT {
        let mut flags = KEYBD_EVENT_FLAGS(0);
        if up {
            flags |= KEYEVENTF_KEYUP;
        }
        if EXTENDED_VK.contains(&vk) {
            flags |= KEYEVENTF_EXTENDEDKEY;
        }
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(vk), wScan: 0, dwFlags: flags, time: 0, dwExtraInfo: 0 } },
        }
    }

    fn unicode(code: u16, up: bool) -> INPUT {
        let mut flags = KEYEVENTF_UNICODE;
        if up {
            flags |= KEYEVENTF_KEYUP;
        }
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 { ki: KEYBDINPUT { wVk: VIRTUAL_KEY(0), wScan: code, dwFlags: flags, time: 0, dwExtraInfo: 0 } },
        }
    }

    fn mouse(flags: MOUSE_EVENT_FLAGS, data: i32) -> INPUT {
        INPUT {
            r#type: INPUT_MOUSE,
            Anonymous: INPUT_0 {
                mi: MOUSEINPUT { dx: 0, dy: 0, mouseData: data as u32, dwFlags: flags, time: 0, dwExtraInfo: 0 },
            },
        }
    }

    fn walk(&self, h: i64, budget: Duration, mut visit: impl FnMut(&UIElement) -> bool) {
        let Ok(root) = self.uia.element_from_handle(Handle::from(h as isize)) else {
            return;
        };
        let deadline = Instant::now() + budget;
        // Pre-order depth-first, top excluded, depth <= 30 (``WalkControl(maxDepth=30)``).
        let mut stack: Vec<(UIElement, u32)> = Vec::new();
        if let Ok(first) = self.walker.get_first_child(&root) {
            stack.push((first, 1));
        }
        while let Some((el, depth)) = stack.pop() {
            if Instant::now() > deadline {
                tracing::debug!("UI Automation walk budget exhausted");
                return;
            }
            if let Ok(next) = self.walker.get_next_sibling(&el) {
                stack.push((next, depth));
            }
            if depth < 30
                && let Ok(child) = self.walker.get_first_child(&el)
            {
                stack.push((child, depth + 1));
            }
            if !visit(&el) {
                return;
            }
        }
    }
}

impl Desktop for Win32Desktop {
    fn features(&self) -> Vec<&'static str> {
        // screen.changes is declared always; whether DXGI can watch a rectangle is decided per call.
        REQUIRED_FEATURES.iter().chain(OPTIONAL_FEATURES.iter()).chain(EVENT_FEATURES.iter()).copied().collect()
    }

    fn list_windows(&mut self) -> OpResult<Vec<WindowInfo>> {
        unsafe extern "system" fn cb(h: HWND, lp: LPARAM) -> BOOL {
            let found = unsafe { &mut *(lp.0 as *mut Vec<HWND>) };
            unsafe {
                let mut pid = 0u32;
                GetWindowThreadProcessId(h, Some(&mut pid));
                if pid != GetCurrentProcessId()
                    && IsWindowVisible(h).as_bool()
                    && GetWindow(h, GW_OWNER).map(|o| o.0.is_null()).unwrap_or(true)
                    && (GetWindowLongW(h, GWL_EXSTYLE) as u32 & WS_EX_TOOLWINDOW.0) == 0
                    && GetWindowTextLengthW(h) > 0
                    && !Win32Desktop::cloaked(h)
                {
                    found.push(h);
                }
            }
            BOOL(1)
        }
        let mut found: Vec<HWND> = Vec::new();
        unsafe {
            let _ = EnumWindows(Some(cb), LPARAM(&mut found as *mut Vec<HWND> as isize));
        }
        let monitors = Self::monitors();
        Ok(found.into_iter().map(|h| self.info(h, &monitors)).collect())
    }

    fn window(&mut self, id: i64) -> OpResult<Option<WindowInfo>> {
        let h = hwnd(id);
        unsafe {
            if !IsWindow(Some(h)).as_bool() || !IsWindowVisible(h).as_bool() {
                return Ok(None);
            }
        }
        Ok(Some(self.info(h, &Self::monitors())))
    }

    fn foreground(&mut self) -> OpResult<Option<WindowInfo>> {
        let h = unsafe { GetForegroundWindow() };
        if h.0.is_null() {
            return Ok(None);
        }
        Ok(Some(self.info(h, &Self::monitors())))
    }

    fn focus(&mut self, id: i64, size: Option<(i32, i32)>) -> OpResult<Option<WindowInfo>> {
        let h = hwnd(id);
        unsafe {
            if IsIconic(h).as_bool() || (size.is_some() && IsZoomed(h).as_bool()) {
                let _ = ShowWindow(h, SW_RESTORE);
            }
            if let Some((sw, sh)) = size {
                let work = Self::work_area(h);
                let mut outer = RECT::default();
                let _ = GetWindowRect(h, &mut outer);
                let visible = self.visible_rect(h);
                // SetWindowPos sizes the OUTER rectangle, which includes invisible borders.
                let pad_w = (outer.right - outer.left) - visible.width();
                let pad_h = (outer.bottom - outer.top) - visible.height();
                let w = sw.min(work.width());
                let hh = sh.min(work.height());
                let x = work.left.max(visible.left.min(work.right - w));
                let y = work.top.max(visible.top.min(work.bottom - hh));
                let _ = SetWindowPos(
                    h,
                    None,
                    x - (visible.left - outer.left),
                    y - (visible.top - outer.top),
                    w + pad_w,
                    hh + pad_h,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                );
            }
            for _ in 0..10 {
                if GetForegroundWindow() == h {
                    break;
                }
                // An Alt tap satisfies the foreground-lock rule for SetForegroundWindow.
                let _ = Self::send(&[Self::key(VK_MENU, false), Self::key(VK_MENU, true)]);
                let _ = SetForegroundWindow(h);
                let _ = BringWindowToTop(h);
                std::thread::sleep(Duration::from_millis(50));
            }
            if GetForegroundWindow() != h {
                return Ok(None);
            }
        }
        Ok(Some(self.info(h, &Self::monitors())))
    }

    fn has_modal_dialog(&mut self, target: &WindowInfo) -> OpResult<bool> {
        let h = hwnd(target.hwnd);
        unsafe {
            if !IsWindow(Some(h)).as_bool() {
                return Ok(false);
            }
            if !IsWindowEnabled(h).as_bool() {
                return Ok(true); // a modal dialog disables its owner
            }
            let fg = GetForegroundWindow();
            Ok(!fg.0.is_null() && fg != h && GetWindow(fg, GW_OWNER).map(|o| o == h).unwrap_or(false))
        }
    }

    fn desktop_locked(&mut self) -> OpResult<bool> {
        unsafe {
            let Ok(desk) = OpenInputDesktop(DESKTOP_CONTROL_FLAGS(0), false, DESKTOP_SWITCHDESKTOP) else {
                return Ok(true);
            };
            let mut buf = [0u16; 256];
            let mut needed: u32 = 0;
            let _ = GetUserObjectInformationW(
                windows::Win32::Foundation::HANDLE(desk.0),
                UOI_NAME,
                Some(buf.as_mut_ptr().cast()),
                (buf.len() * 2) as u32,
                Some(&mut needed),
            );
            let _ = CloseDesktop(desk);
            let end = buf.iter().position(|c| *c == 0).unwrap_or(buf.len());
            Ok(!String::from_utf16_lossy(&buf[..end]).eq_ignore_ascii_case("default"))
        }
    }

    fn capture(&mut self, rect: Rect) -> OpResult<Box<dyn Captured>> {
        let image = match self.dxgi.capture(rect) {
            Some(img) => img,
            None => capture_gdi(rect)?,
        };
        let gray = gray_thumbnail(&image);
        Ok(Box::new(Frame { rect, image, gray }))
    }

    fn changes(&mut self, rect: Rect, since: Option<u64>, timeout: Duration, ignore: &[Rect]) -> OpResult<ChangeReport> {
        // The glow's own band (and what it just left) is never a repaint (UDR-0174 D2).
        let mut ignore = ignore.to_vec();
        ignore.extend(overlay::self_rects());
        Ok(self.dxgi.changes(rect, since, timeout, &ignore))
    }

    fn path(&mut self, spec: &PathSpec) -> OpResult<PathReport> {
        let threshold = unsafe { GetSystemMetrics(SM_CXDRAG).max(GetSystemMetrics(SM_CYDRAG)) }.max(1);
        let plan = crate::path::plan(spec, threshold);
        let mut io = WinIo { t0: Instant::now() };
        crate::path::follow(&mut io, spec, &plan)
    }

    fn release_input(&mut self) -> OpResult<Vec<&'static str>> {
        // Mouse buttons first, then the modifiers (UDR-0174 D6: after a provider was lost mid-path).
        const HELD: [(u16, &str); 8] = [
            (0x01, "left"),
            (0x02, "right"),
            (0x04, "middle"),
            (0x11, "ctrl"),
            (0x10, "shift"),
            (0x12, "alt"),
            (0x5B, "win"),
            (0x5C, "win"),
        ];
        let mut released = Vec::new();
        for (vk, name) in HELD {
            let down = unsafe { GetAsyncKeyState(i32::from(vk)) } as u16 & 0x8000 != 0;
            if !down {
                continue;
            }
            let event = match vk {
                0x01 => Self::mouse(MOUSEEVENTF_LEFTUP, 0),
                0x02 => Self::mouse(MOUSEEVENTF_RIGHTUP, 0),
                0x04 => Self::mouse(MOUSEEVENTF_MIDDLEUP, 0),
                _ => Self::key(vk, true),
            };
            if Self::send(&[event]).is_ok() && !released.contains(&name) {
                released.push(name);
            }
        }
        Ok(released)
    }

    fn overlay_show(&mut self, id: i64, ttl: Duration) -> OpResult<(bool, Option<&'static str>)> {
        Ok(overlay::show(id, ttl))
    }

    fn overlay_hide(&mut self) -> OpResult<()> {
        overlay::hide();
        Ok(())
    }

    fn elements(&mut self, id: i64, max: usize, budget: Duration) -> OpResult<Vec<ElementInfo>> {
        if max == 0 {
            return Ok(Vec::new());
        }
        let mut primary: Vec<(ElementInfo, UIElement)> = Vec::new();
        let mut secondary: Vec<(ElementInfo, UIElement)> = Vec::new();
        self.walk(id, budget, |el| {
            let Ok(ct) = el.get_control_type() else { return true };
            let Some((kind, interactive)) = control_kind(ct as i32) else { return true };
            if el.is_offscreen().unwrap_or(true) || !el.is_enabled().unwrap_or(false) {
                return true;
            }
            let Ok(r) = el.get_bounding_rectangle() else { return true };
            let rect = Rect::new(r.get_left(), r.get_top(), r.get_right(), r.get_bottom());
            let name = el.get_name().unwrap_or_default();
            if rect.width() <= 1 || rect.height() <= 1 || (name.is_empty() && !UNNAMED_OK.contains(&kind)) {
                return true;
            }
            let info = ElementInfo { id: 0, control: kind.to_string(), name, rect };
            if interactive { primary.push((info, el.clone())) } else { secondary.push((info, el.clone())) }
            primary.len() < max
        });
        let mut out = Vec::new();
        for (mut info, el) in primary.into_iter().chain(secondary).take(max) {
            self.next_element += 1;
            info.id = self.next_element;
            self.elements.insert(info.id, el);
            out.push(info);
        }
        Ok(out)
    }

    fn element_rect(&mut self, id: u64) -> OpResult<Option<Rect>> {
        let Some(el) = self.elements.get(&id) else { return Ok(None) };
        Ok(el.get_bounding_rectangle().ok().and_then(|r| {
            let rect = Rect::new(r.get_left(), r.get_top(), r.get_right(), r.get_bottom());
            (rect.width() > 1 && rect.height() > 1).then_some(rect)
        }))
    }

    fn forget_elements(&mut self, ids: &[u64]) {
        for id in ids {
            self.elements.remove(id);
        }
    }

    fn find_element(&mut self, id: i64, name: &str, control: Option<&str>) -> OpResult<bool> {
        let wanted = name.to_lowercase();
        let kind = control.unwrap_or("").to_lowercase();
        let kind = kind.strip_suffix("control").unwrap_or(&kind).to_string();
        let mut exact = false;
        let mut loose = false;
        self.walk(id, Duration::from_secs(1), |el| {
            if !kind.is_empty() {
                let Ok(ct) = el.get_control_type() else { return true };
                if control_name(ct as i32).to_lowercase() != kind {
                    return true;
                }
            }
            if el.is_offscreen().unwrap_or(true) {
                return true;
            }
            let label = el.get_name().unwrap_or_default().to_lowercase();
            if label == wanted {
                exact = true;
                return false;
            }
            if !wanted.is_empty() && label.contains(&wanted) {
                loose = true;
            }
            true
        });
        Ok(exact || loose)
    }

    fn caret_rect(&mut self) -> OpResult<Option<Rect>> {
        let mut info = GUITHREADINFO { cbSize: size_of::<GUITHREADINFO>() as u32, ..Default::default() };
        unsafe {
            if GetGUIThreadInfo(0, &mut info).is_err() || info.hwndCaret.0.is_null() {
                return Ok(None);
            }
            let mut pt = POINT { x: info.rcCaret.left, y: info.rcCaret.top };
            let _ = ClientToScreen(info.hwndCaret, &mut pt);
            let w = (info.rcCaret.right - info.rcCaret.left).max(2);
            let h = (info.rcCaret.bottom - info.rcCaret.top).max(2);
            Ok(Some(Rect::new(pt.x - 2, pt.y - 2, pt.x + w + 2, pt.y + h + 2)))
        }
    }

    fn cursor_pos(&mut self) -> OpResult<(i32, i32)> {
        let mut pt = POINT::default();
        unsafe {
            let _ = GetCursorPos(&mut pt);
        }
        Ok((pt.x, pt.y))
    }

    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()> {
        unsafe { SetCursorPos(x, y) }.map_err(|_| ProviderError::new(ErrorCode::InputBlocked, "SetCursorPos failed"))
    }

    fn click(&mut self, x: i32, y: i32, button: &str, count: u32) -> OpResult<()> {
        let (down, up) = match button {
            "right" => (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle" => (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
            _ => (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        };
        self.move_to(x, y)?;
        let mut events = Vec::new();
        for _ in 0..count.max(1) {
            events.push(Self::mouse(down, 0));
            events.push(Self::mouse(up, 0));
        }
        Self::send(&events)
    }

    fn drag(&mut self, x1: i32, y1: i32, x2: i32, y2: i32) -> OpResult<()> {
        self.move_to(x1, y1)?;
        Self::send(&[Self::mouse(MOUSEEVENTF_LEFTDOWN, 0)])?;
        let steps = 12;
        for i in 1..=steps {
            self.move_to(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)?;
            std::thread::sleep(Duration::from_millis(10)); // an input interval, not a settle wait
        }
        Self::send(&[Self::mouse(MOUSEEVENTF_LEFTUP, 0)])
    }

    fn scroll(&mut self, x: i32, y: i32, dy: i32, dx: i32) -> OpResult<()> {
        self.move_to(x, y)?;
        let mut events = Vec::new();
        if dy != 0 {
            // Positive dy scrolls DOWN the content (the wheel moves towards the user).
            events.push(Self::mouse(MOUSEEVENTF_WHEEL, -dy * WHEEL_DELTA));
        }
        if dx != 0 {
            events.push(Self::mouse(MOUSEEVENTF_HWHEEL, dx * WHEEL_DELTA));
        }
        Self::send(&events)
    }

    fn keys(&mut self, chord: &[String]) -> OpResult<()> {
        let mods = chord.iter().filter(|k| MODIFIERS.contains(&k.as_str()));
        let rest = chord.iter().filter(|k| !MODIFIERS.contains(&k.as_str()));
        let order: Vec<&String> = mods.chain(rest).collect();
        let vks: Vec<u16> = order
            .iter()
            .map(|k| vk_of(k).ok_or_else(|| ProviderError::invalid("unknown key name")))
            .collect::<OpResult<_>>()?;
        let mut events: Vec<INPUT> = vks.iter().map(|vk| Self::key(*vk, false)).collect();
        events.extend(vks.iter().rev().map(|vk| Self::key(*vk, true)));
        Self::send(&events)
    }

    fn type_text(&mut self, text: &str) -> OpResult<()> {
        let mut events = Vec::new();
        for code in text.replace("\r\n", "\n").encode_utf16() {
            if code == 0x0A || code == 0x0D {
                events.push(Self::key(VK_RETURN, false));
                events.push(Self::key(VK_RETURN, true));
                continue;
            }
            events.push(Self::unicode(code, false));
            events.push(Self::unicode(code, true));
        }
        for chunk in events.chunks(200) {
            Self::send(chunk)?;
        }
        Ok(())
    }

    fn paste_text(&mut self, text: &str) -> OpResult<()> {
        let mut clipboard = arboard::Clipboard::new().map_err(|_| internal("ClipboardUnavailable"))?;
        let previous = clipboard.get_text().ok();
        clipboard.set_text(text.to_string()).map_err(|_| internal("ClipboardWriteFailed"))?;
        let pasted = self.keys(&["ctrl".to_string(), "v".to_string()]);
        // The target reads the clipboard while handling WM_PASTE; restoring before it has done
        // so would paste the old text. An input interval, not a settle wait.
        std::thread::sleep(Duration::from_millis(250));
        if let Some(previous) = previous {
            let _ = clipboard.set_text(previous);
        }
        pasted
    }
}

/// `crate::path::PathIo` on the real desktop. Moves are `SetCursorPos` (pixel-exact) plus a
/// zero relative `SendInput` move, so applications receive a real mouse-move input event.
struct WinIo {
    t0: Instant,
}

fn modifier_vk(name: &str) -> u16 {
    match name {
        "ctrl" => 0x11,
        "shift" => 0x10,
        "alt" => 0x12,
        _ => 0x5B, // win
    }
}

fn button_flags(button: &str) -> (MOUSE_EVENT_FLAGS, MOUSE_EVENT_FLAGS) {
    match button {
        "right" => (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle" => (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        _ => (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    }
}

impl PathIo for WinIo {
    fn now_ms(&mut self) -> u64 {
        u64::try_from(self.t0.elapsed().as_millis()).unwrap_or(u64::MAX)
    }
    fn sleep_ms(&mut self, ms: u32) {
        // std's sleep uses a high-resolution waitable timer on Windows (not the 15.6 ms tick).
        std::thread::sleep(Duration::from_millis(u64::from(ms)));
    }
    fn cursor(&mut self) -> (i32, i32) {
        let mut pt = POINT::default();
        unsafe {
            let _ = GetCursorPos(&mut pt);
        }
        (pt.x, pt.y)
    }
    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()> {
        unsafe { SetCursorPos(x, y) }.map_err(|_| ProviderError::new(ErrorCode::InputBlocked, "SetCursorPos failed"))?;
        Win32Desktop::send(&[Win32Desktop::mouse(MOUSEEVENTF_MOVE, 0)])
    }
    fn press(&mut self, spec: &PathSpec) -> OpResult<()> {
        for m in &spec.modifiers {
            Win32Desktop::send(&[Win32Desktop::key(modifier_vk(m), false)])?;
        }
        Win32Desktop::send(&[Win32Desktop::mouse(button_flags(spec.button).0, 0)])
    }
    fn release(&mut self, spec: &PathSpec) {
        // Each event on its own: one refused "up" must not keep the others down.
        let _ = Win32Desktop::send(&[Win32Desktop::mouse(button_flags(spec.button).1, 0)]);
        for m in spec.modifiers.iter().rev() {
            let _ = Win32Desktop::send(&[Win32Desktop::key(modifier_vk(m), true)]);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Real desktop only (`cargo test -- --ignored`): a DXGI frame must equal a GDI grab of
    /// the same rectangle (UDR-0173 D3). Waits up to 10 s for the screen to present a frame.
    #[test]
    #[ignore]
    fn dxgi_matches_gdi_on_the_real_desktop() {
        init_desktop_thread();
        let mut dup = dxgi::Duplicator::new();
        let area = Rect::new(0, 0, 800, 600);
        let deadline = Instant::now() + Duration::from_secs(10);
        let fast = loop {
            if let Some(img) = dup.capture(area) {
                break img;
            }
            assert!(Instant::now() < deadline, "no DXGI frame within 10 s (a completely still screen?)");
            std::thread::sleep(Duration::from_millis(100));
        };
        let reference = capture_gdi(area).expect("GDI capture");
        assert_eq!((fast.width, fast.height), (reference.width, reference.height));
        let same = fast.data.iter().zip(&reference.data).filter(|(a, b)| a == b).count();
        let share = same as f64 / fast.data.len() as f64;
        eprintln!("DXGI vs GDI: {:.4} of the bytes identical", share);
        // Anything that redrew between the two grabs differs; the rest must be identical.
        assert!(share > 0.95, "DXGI and GDI disagree: only {share:.4} identical");
    }

    /// Real desktop only (`cargo test --release -- --ignored --test-threads=1`: one process gets
    /// one duplication per output, so the ignored tests must not run in parallel): a token, a
    /// quiet wait and a repaint report on the primary output (PRP-0191 A1). Prints timings.
    #[test]
    #[ignore]
    fn screen_changes_on_the_real_desktop() {
        init_desktop_thread();
        let mut dup = dxgi::Duplicator::new();
        let area = Rect::new(0, 0, 800, 600);
        let token = dup.changes(area, None, Duration::ZERO, &[]);
        assert!(token.available, "DXGI unavailable: {:?}", token.reason);
        assert!(!token.changed);
        let started = Instant::now();
        let quiet = dup.changes(area, Some(token.seq), Duration::from_millis(300), &[]);
        eprintln!(
            "wait 300 ms: changed={} after {:?} ({} rects)",
            quiet.changed,
            started.elapsed(),
            quiet.rects.len()
        );
        assert!(quiet.available);
        if !quiet.changed {
            assert!(started.elapsed() >= Duration::from_millis(290), "a quiet wait lasts its timeout");
        }
        // Everything ignored: nothing counts, however busy the screen.
        let all = dup.changes(area, Some(quiet.seq), Duration::from_millis(100), &[area]);
        assert!(all.available && !all.changed);
        // A rectangle spanning no output is unavailable, not an error.
        let far = dup.changes(Rect::new(-1_000_000, 0, -999_000, 10), None, Duration::ZERO, &[]);
        assert!(!far.available);
        // Captures still work from the same duplication.
        assert!(dup.capture(area).is_some() || capture_gdi(area).is_ok());
    }

    /// Real desktop only (`cargo test --release -- --ignored --test-threads=1`): the glow around
    /// the foreground window never appears in a capture (UDR-0174 D1). The band area is grabbed
    /// with GDI while the glow is shown and after it is hidden; teal-tinted pixels must not grow.
    #[test]
    #[ignore]
    fn the_glow_is_not_captured_on_the_real_desktop() {
        init_desktop_thread();
        let fg = unsafe { GetForegroundWindow() };
        assert!(!fg.0.is_null(), "no foreground window");
        let mut r = RECT::default();
        unsafe { GetWindowRect(fg, &mut r) }.expect("window rect");
        // A strip just above the window's top edge (inside the band when it fits outside).
        let area = Rect::new(r.left.max(0), (r.top - 8).max(0), (r.left + 400).max(1), (r.top + 2).max(1));
        let (shown, reason) = overlay::show(fg.0 as i64, Duration::from_secs(10));
        if !shown {
            eprintln!("glow not shown ({reason:?}); nothing to check on this machine");
            return;
        }
        std::thread::sleep(Duration::from_millis(300));
        let with_glow = capture_gdi(area).expect("GDI capture with the glow");
        overlay::hide();
        std::thread::sleep(Duration::from_millis(300));
        let without = capture_gdi(area).expect("GDI capture without the glow");
        let teal = |img: &RgbImage| img.data.chunks_exact(3).filter(|p| p[2] > p[0] + 60 && p[1] > p[0] + 40).count();
        eprintln!("teal-ish pixels: with glow {}, without {}", teal(&with_glow), teal(&without));
        assert!(teal(&with_glow) <= teal(&without) + 4, "the glow leaked into a GDI capture");
    }
}

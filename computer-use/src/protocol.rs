//! Wire types and vocabulary of `chatwalaau.computer-use/1` (CTR-0236, RES-0007).
//!
//! Coordinates are PHYSICAL virtual-desktop pixels. Window handles travel as opaque
//! strings (`"hwnd:0x1A2B"` on Windows); element keys as opaque strings.

use serde::Serialize;
use serde_json::{Map, Value, json};

pub const PROTOCOL: &str = "chatwalaau.computer-use/1";
pub const PROVIDER_NAME: &str = "chatwalaau-computer-use";
pub const PROVIDER_VERSION: &str = env!("CARGO_PKG_VERSION");

/// The minimum any provider implements (RES-0007 F2).
pub const REQUIRED_FEATURES: [&str; 6] = [
    "windows.list",
    "windows.focus",
    "screen.capture",
    "input.pointer",
    "input.keys",
    "input.text",
];
/// Precision / convenience layers (RES-0007 F2).
pub const OPTIONAL_FEATURES: [&str; 6] = [
    "windows.resize",
    "windows.dialog",
    "session.lock",
    "ui.elements",
    "ui.caret",
    "input.clipboard",
];
/// Transport features of the MCP protocol (CTR-0236, PRP-0190 Section 2.3).
pub const PROTOCOL_FEATURES: [&str; 2] = ["screen.thumbnail", "screen.frames"];
/// OS change events (PRP-0191 A1, UDR-0173 D11): repaint reports, never verdicts. Declared by
/// the platform desktop; availability is decided per call.
pub const EVENT_FEATURES: [&str; 1] = ["screen.changes"];

/// Grayscale thumbnail every capture carries.
pub const THUMB_W: u32 = 192;
pub const THUMB_H: u32 = 108;
/// Captures kept in memory for `screen_encode` (UDR-0172 D5).
pub const FRAME_CACHE: usize = 4;
/// `ui_elements` generations whose keys stay valid.
pub const ELEMENT_GENERATIONS: usize = 2;

/// operation (tool) name -> the feature that must be declared for it.
pub const OPERATIONS: [(&str, Option<&str>); 22] = [
    ("describe", None),
    ("windows_list", Some("windows.list")),
    ("windows_get", Some("windows.list")),
    ("windows_foreground", Some("windows.list")),
    ("windows_focus", Some("windows.focus")),
    ("windows_dialog", Some("windows.dialog")),
    ("session_locked", Some("session.lock")),
    ("screen_capture", Some("screen.capture")),
    ("screen_encode", Some("screen.frames")),
    ("screen_changes", Some("screen.changes")),
    ("ui_elements", Some("ui.elements")),
    ("ui_element_rect", Some("ui.elements")),
    ("ui_find", Some("ui.elements")),
    ("ui_caret", Some("ui.caret")),
    ("input_cursor", Some("input.pointer")),
    ("input_move", Some("input.pointer")),
    ("input_click", Some("input.pointer")),
    ("input_drag", Some("input.pointer")),
    ("input_scroll", Some("input.pointer")),
    ("input_keys", Some("input.keys")),
    ("input_type_text", Some("input.text")),
    ("input_paste", Some("input.clipboard")),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct Rect {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

impl Rect {
    pub fn new(left: i32, top: i32, right: i32, bottom: i32) -> Self {
        Self { left, top, right, bottom }
    }
    pub fn width(&self) -> i32 {
        (self.right - self.left).max(0)
    }
    pub fn height(&self) -> i32 {
        (self.bottom - self.top).max(0)
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WindowInfo {
    pub hwnd: i64,
    pub title: String,
    pub process: String,
    pub pid: u32,
    pub rect: Rect,
    pub monitor: i32,
    pub minimized: bool,
}

/// One UI element as a backend reports it; `id` is the backend's own handle.
#[derive(Clone, Debug)]
pub struct ElementInfo {
    pub id: u64,
    pub control: String,
    pub name: String,
    pub rect: Rect,
}

/// The CTR-0236 error code set.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorCode {
    Unsupported,
    NotFound,
    FocusFailed,
    InputBlocked,
    #[allow(dead_code)] // in the CTR-0236 code set; no Windows operation raises it today
    Locked,
    InvalidArgument,
    Internal,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::Unsupported => "unsupported",
            ErrorCode::NotFound => "not_found",
            ErrorCode::FocusFailed => "focus_failed",
            ErrorCode::InputBlocked => "input_blocked",
            ErrorCode::Locked => "locked",
            ErrorCode::InvalidArgument => "invalid_argument",
            ErrorCode::Internal => "internal",
        }
    }
}

/// An operation refused with a CTR-0236 code. The message never carries an argument
/// (the text to type can be a secret value, UDR-0172 D9).
#[derive(Clone, Debug)]
pub struct ProviderError {
    pub code: ErrorCode,
    pub message: String,
}

impl ProviderError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        Self { code, message: message.into() }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(ErrorCode::InvalidArgument, message)
    }
    pub fn to_json(&self) -> Value {
        json!({"error": self.code.as_str(), "message": self.message})
    }
}

pub type OpResult<T> = Result<T, ProviderError>;

pub fn handle_of(hwnd: i64) -> String {
    format!("hwnd:0x{:X}", hwnd)
}

pub fn hwnd_of(args: &Map<String, Value>) -> OpResult<i64> {
    let raw = args.get("handle").and_then(Value::as_str).unwrap_or("");
    raw.strip_prefix("hwnd:0x")
        .filter(|hex| !hex.is_empty())
        .and_then(|hex| i64::from_str_radix(hex, 16).ok())
        .ok_or_else(|| ProviderError::invalid("handle must look like 'hwnd:0x1A2B'"))
}

pub fn rect_json(r: &Rect) -> Value {
    json!({"left": r.left, "top": r.top, "right": r.right, "bottom": r.bottom})
}

pub fn rect_of(raw: Option<&Value>) -> OpResult<Rect> {
    let bad = || ProviderError::invalid("rect is {left, top, right, bottom}");
    let obj = raw.and_then(Value::as_object).ok_or_else(bad)?;
    let get = |k: &str| obj.get(k).and_then(int_of).ok_or_else(bad);
    Ok(Rect::new(get("left")?, get("top")?, get("right")?, get("bottom")?))
}

pub fn window_json(w: &WindowInfo) -> Value {
    json!({
        "handle": handle_of(w.hwnd),
        "title": w.title,
        "process": w.process,
        "pid": w.pid,
        "rect": rect_json(&w.rect),
        "monitor": w.monitor,
        "minimized": w.minimized,
    })
}

/// Integers may arrive as JSON numbers or numeric strings (as Python's ``int()`` accepts).
fn int_of(v: &Value) -> Option<i32> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)).map(|i| i as i32),
        Value::String(s) => s.trim().parse::<i64>().ok().map(|i| i as i32),
        Value::Bool(b) => Some(*b as i32),
        _ => None,
    }
}

pub fn int_arg(args: &Map<String, Value>, name: &str, default: Option<i32>) -> OpResult<i32> {
    match args.get(name) {
        None | Some(Value::Null) => {
            default.ok_or_else(|| ProviderError::invalid(format!("{name} is required")))
        }
        Some(v) => int_of(v).ok_or_else(|| ProviderError::invalid(format!("{name} must be an integer"))),
    }
}

pub fn str_arg<'a>(args: &'a Map<String, Value>, name: &str) -> &'a str {
    args.get(name).and_then(Value::as_str).unwrap_or("")
}

pub fn bool_arg(args: &Map<String, Value>, name: &str) -> bool {
    match args.get(name) {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64().unwrap_or(0) != 0,
        Some(Value::String(s)) => !s.is_empty(),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handles_round_trip() {
        let mut args = Map::new();
        args.insert("handle".into(), Value::String(handle_of(0x1A2B)));
        assert_eq!(hwnd_of(&args).unwrap(), 0x1A2B);
        args.insert("handle".into(), Value::String("notepad".into()));
        assert_eq!(hwnd_of(&args).unwrap_err().code, ErrorCode::InvalidArgument);
    }

    #[test]
    fn operations_match_ctr0236() {
        assert_eq!(OPERATIONS.len(), 22);
        assert_eq!(OPERATIONS[0].0, "describe");
    }
}

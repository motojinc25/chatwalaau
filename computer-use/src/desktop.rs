//! The desktop seam and the provider's operation table (CTR-0236).
//!
//! [`Desktop`] is what a platform implements (Windows today, `fake.rs` for tests).
//! [`Provider`] is the transport-free operation table -- the same rules as the Python
//! reference's ``DesktopProvider``: feature gating, the frame cache (4), element-key
//! generations (2), the error code set. [`DesktopThread`] runs a `Provider` on ONE thread
//! so COM objects never cross threads and input never interleaves (RES-0007 P2 / P3).

use std::collections::{HashMap, VecDeque};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use serde_json::{Map, Value, json};

use crate::changes::{ChangeReport, MAX_IGNORE, MAX_TIMEOUT_MS};
use crate::path::{PathReport, PathSpec};
use crate::protocol::*;

/// One capture. `gray` is the 192x108 thumbnail; `encode` renders a PNG of it at w x h.
pub trait Captured {
    fn rect(&self) -> Rect;
    fn gray(&self) -> Vec<u8>;
    fn encode(&self, w: u32, h: u32) -> OpResult<Vec<u8>>;
}

/// What a platform implements. Every call runs on the desktop thread.
pub trait Desktop {
    fn features(&self) -> Vec<&'static str>;
    fn list_windows(&mut self) -> OpResult<Vec<WindowInfo>>;
    fn window(&mut self, hwnd: i64) -> OpResult<Option<WindowInfo>>;
    fn foreground(&mut self) -> OpResult<Option<WindowInfo>>;
    fn focus(&mut self, hwnd: i64, size: Option<(i32, i32)>) -> OpResult<Option<WindowInfo>>;
    fn has_modal_dialog(&mut self, target: &WindowInfo) -> OpResult<bool>;
    fn desktop_locked(&mut self) -> OpResult<bool>;
    fn capture(&mut self, rect: Rect) -> OpResult<Box<dyn Captured>>;
    fn elements(&mut self, hwnd: i64, max: usize, budget: Duration) -> OpResult<Vec<ElementInfo>>;
    fn element_rect(&mut self, id: u64) -> OpResult<Option<Rect>>;
    /// The provider no longer references these element ids (their generation expired).
    fn forget_elements(&mut self, ids: &[u64]);
    fn find_element(&mut self, hwnd: i64, name: &str, control: Option<&str>) -> OpResult<bool>;
    fn caret_rect(&mut self) -> OpResult<Option<Rect>>;
    fn cursor_pos(&mut self) -> OpResult<(i32, i32)>;
    fn move_to(&mut self, x: i32, y: i32) -> OpResult<()>;
    fn click(&mut self, x: i32, y: i32, button: &str, count: u32) -> OpResult<()>;
    fn drag(&mut self, x1: i32, y1: i32, x2: i32, y2: i32) -> OpResult<()>;
    fn scroll(&mut self, x: i32, y: i32, dy: i32, dx: i32) -> OpResult<()>;
    fn keys(&mut self, chord: &[String]) -> OpResult<()>;
    fn type_text(&mut self, text: &str) -> OpResult<()>;
    fn paste_text(&mut self, text: &str) -> OpResult<()>;
    /// `screen.changes` (PRP-0191 A1): repaints inside `rect` since token `since` (None: a fresh
    /// token at once), waiting up to `timeout` for one. Only called when declared.
    fn changes(&mut self, _rect: Rect, since: Option<u64>, _timeout: Duration, _ignore: &[Rect]) -> OpResult<ChangeReport> {
        Ok(ChangeReport::unavailable(since.unwrap_or(0), "unsupported"))
    }
    /// `input.path` (PRP-0192): follow `spec` with a held button; releases on EVERY exit.
    fn path(&mut self, _spec: &PathSpec) -> OpResult<PathReport> {
        Err(ProviderError::new(ErrorCode::Unsupported, "input.path is not available"))
    }
    /// `input_release`: lift every mouse button / modifier reported down; their names.
    fn release_input(&mut self) -> OpResult<Vec<&'static str>> {
        Ok(Vec::new())
    }
    /// `ui.overlay` (PRP-0192): show the glow around `hwnd` for `ttl`; (shown, reason).
    fn overlay_show(&mut self, _hwnd: i64, _ttl: Duration) -> OpResult<(bool, Option<&'static str>)> {
        Ok((false, Some("unsupported")))
    }
    fn overlay_hide(&mut self) -> OpResult<()> {
        Ok(())
    }
    /// Test builds only: extra tools a fake desktop answers (never listed).
    fn test_tool(&mut self, _name: &str, _args: &Map<String, Value>) -> Option<OpResult<Value>> {
        None
    }
}

/// A tool's result: structured JSON plus an optional PNG.
pub type ToolOutput = (Value, Option<Vec<u8>>);

pub struct Provider {
    desktop: Box<dyn Desktop>,
    features: Vec<&'static str>,
    frames: VecDeque<(String, Box<dyn Captured>)>,
    frame_seq: u64,
    elements: VecDeque<(u64, HashMap<String, u64>)>,
    element_gen: u64,
}

impl Provider {
    pub fn new(desktop: Box<dyn Desktop>) -> Self {
        let declared = desktop.features();
        let mut features: Vec<&'static str> = REQUIRED_FEATURES
            .iter()
            .chain(OPTIONAL_FEATURES.iter())
            .chain(EVENT_FEATURES.iter())
            .copied()
            .filter(|f| declared.contains(f))
            .collect();
        features.extend(PROTOCOL_FEATURES);
        Self { desktop, features, frames: VecDeque::new(), frame_seq: 0, elements: VecDeque::new(), element_gen: 0 }
    }

    fn has(&self, feature: &str) -> bool {
        self.features.contains(&feature)
    }

    pub fn describe(&self) -> Value {
        let mut features: Vec<&str> = self.features.clone();
        features.sort_unstable();
        json!({
            "protocol": PROTOCOL,
            "provider": {"name": PROVIDER_NAME, "version": PROVIDER_VERSION},
            "platform": if cfg!(windows) { "win32" } else { std::env::consts::OS },
            "pid": std::process::id(),
            "features": features,
            "thumbnail": {"w": THUMB_W, "h": THUMB_H},
            "frame_cache": FRAME_CACHE,
        })
    }

    /// One operation (the dispatch of the Python reference's ``DesktopProvider.call``).
    pub fn call(&mut self, name: &str, args: &Map<String, Value>) -> OpResult<ToolOutput> {
        if let Some(result) = self.desktop.test_tool(name, args) {
            return result.map(|v| (v, None));
        }
        let Some((_, feature)) = OPERATIONS.iter().find(|(op, _)| *op == name) else {
            return Err(ProviderError::new(ErrorCode::Unsupported, format!("unknown operation {name:?}")));
        };
        if let Some(feature) = feature
            && !self.has(feature)
        {
            return Err(ProviderError::new(
                ErrorCode::Unsupported,
                format!("{name} needs the {feature} feature, which this provider lacks"),
            ));
        }
        let none = |v: Value| Ok((v, None));
        match name {
            "describe" => none(self.describe()),
            "windows_list" => {
                let rows: Vec<Value> = self.desktop.list_windows()?.iter().map(window_json).collect();
                none(json!({"windows": rows}))
            }
            "windows_get" => match self.desktop.window(hwnd_of(args)?)? {
                Some(w) => none(json!({"window": window_json(&w)})),
                None => Err(ProviderError::new(ErrorCode::NotFound, "no such visible window")),
            },
            "windows_foreground" => {
                let fg = self.desktop.foreground()?;
                none(json!({"window": fg.as_ref().map(window_json)}))
            }
            "windows_focus" => {
                let hwnd = hwnd_of(args)?;
                let size = match args.get("size") {
                    Some(Value::Array(v)) if self.has("windows.resize") => {
                        let get = |i: usize| v.get(i).and_then(Value::as_i64).map(|n| n as i32);
                        match (get(0), get(1)) {
                            (Some(w), Some(h)) => Some((w, h)),
                            _ => return Err(ProviderError::invalid("size is [width, height]")),
                        }
                    }
                    Some(Value::Null) | None => None,
                    Some(_) if self.has("windows.resize") => {
                        return Err(ProviderError::invalid("size is [width, height]"));
                    }
                    Some(_) => None,
                };
                match self.desktop.focus(hwnd, size)? {
                    Some(w) => none(json!({"window": window_json(&w)})),
                    None => Err(ProviderError::new(
                        ErrorCode::FocusFailed,
                        "the window could not be brought to the foreground",
                    )),
                }
            }
            "windows_dialog" => {
                let modal = match self.desktop.window(hwnd_of(args)?)? {
                    Some(target) => self.desktop.has_modal_dialog(&target)?,
                    None => false,
                };
                none(json!({"modal": modal}))
            }
            "session_locked" => none(json!({"locked": self.desktop.desktop_locked()?})),
            "screen_capture" => self.capture(args),
            "screen_encode" => self.encode(args),
            "screen_changes" => self.changes(args),
            "input_path" => {
                let spec = crate::path::parse(args)?;
                none(self.desktop.path(&spec)?.to_json())
            }
            // Normally answered by the server without queueing (server.rs); kept for completeness.
            "input_cancel" => none(json!({"cancelled": crate::path::request_cancel()})),
            "input_release" => none(json!({"released": self.desktop.release_input()?})),
            "overlay_show" => {
                let hwnd = hwnd_of(args)?;
                let ttl = int_arg(args, "ttl_ms", Some(300_000))?;
                if !(1_000..=600_000).contains(&ttl) {
                    return Err(ProviderError::invalid("ttl_ms is 1000..600000"));
                }
                let (shown, reason) = self.desktop.overlay_show(hwnd, Duration::from_millis(ttl as u64))?;
                let mut body = json!({"shown": shown});
                if let Some(reason) = reason {
                    body["reason"] = Value::String(reason.into());
                }
                none(body)
            }
            "overlay_hide" => {
                self.desktop.overlay_hide()?;
                none(json!({}))
            }
            "ui_elements" => self.ui_elements(args),
            "ui_element_rect" => {
                let key = str_arg(args, "key");
                let id = self
                    .elements
                    .iter()
                    .find_map(|(_, keyed)| keyed.get(key).copied())
                    .ok_or_else(|| ProviderError::new(ErrorCode::NotFound, "unknown or expired element key"))?;
                let rect = self.desktop.element_rect(id)?;
                none(json!({"rect": rect.as_ref().map(rect_json)}))
            }
            "ui_find" => {
                let control = args.get("control").and_then(Value::as_str).filter(|c| !c.is_empty());
                let found = self.desktop.find_element(hwnd_of(args)?, str_arg(args, "name"), control)?;
                none(json!({"found": found}))
            }
            "ui_caret" => {
                let rect = self.desktop.caret_rect()?;
                none(json!({"rect": rect.as_ref().map(rect_json)}))
            }
            "input_cursor" => {
                let (x, y) = self.desktop.cursor_pos()?;
                none(json!({"x": x, "y": y}))
            }
            "input_move" => {
                self.desktop.move_to(int_arg(args, "x", None)?, int_arg(args, "y", None)?)?;
                none(json!({}))
            }
            "input_click" => {
                let button = match args.get("button").and_then(Value::as_str).unwrap_or("left") {
                    "" => "left",
                    b @ ("left" | "right" | "middle") => b,
                    _ => return Err(ProviderError::invalid("button is left, right or middle")),
                };
                let count = int_arg(args, "count", Some(1))?;
                if count != 1 && count != 2 {
                    return Err(ProviderError::invalid("count is 1 or 2"));
                }
                let (x, y) = (int_arg(args, "x", None)?, int_arg(args, "y", None)?);
                self.desktop.click(x, y, button, count as u32)?;
                none(json!({}))
            }
            "input_drag" => {
                let g = |k: &str| int_arg(args, k, None);
                self.desktop.drag(g("x1")?, g("y1")?, g("x2")?, g("y2")?)?;
                none(json!({}))
            }
            "input_scroll" => {
                let (x, y) = (int_arg(args, "x", None)?, int_arg(args, "y", None)?);
                let (dy, dx) = (int_arg(args, "dy", Some(0))?, int_arg(args, "dx", Some(0))?);
                self.desktop.scroll(x, y, dy, dx)?;
                none(json!({}))
            }
            "input_keys" => {
                let keys: Option<Vec<String>> = args.get("keys").and_then(Value::as_array).and_then(|v| {
                    v.iter().map(|k| k.as_str().map(str::to_lowercase)).collect()
                });
                match keys {
                    Some(keys) if !keys.is_empty() => {
                        self.desktop.keys(&keys)?;
                        none(json!({}))
                    }
                    _ => Err(ProviderError::invalid("keys is a non-empty list of key names")),
                }
            }
            "input_type_text" => {
                self.desktop.type_text(str_arg(args, "text"))?;
                none(json!({}))
            }
            "input_paste" => {
                self.desktop.paste_text(str_arg(args, "text"))?;
                none(json!({}))
            }
            _ => Err(ProviderError::new(ErrorCode::Unsupported, format!("unknown operation {name:?}"))),
        }
    }

    fn capture(&mut self, args: &Map<String, Value>) -> OpResult<ToolOutput> {
        let rect = rect_of(args.get("rect"))?;
        if rect.width() <= 0 || rect.height() <= 0 {
            return Err(ProviderError::invalid("rect is empty"));
        }
        let frame = self.desktop.capture(rect)?;
        let got = frame.rect();
        let mut body = json!({"rect": rect_json(&got), "width": got.width(), "height": got.height()});
        if bool_arg(args, "thumbnail") {
            body["thumbnail"] = json!({"w": THUMB_W, "h": THUMB_H, "gray": B64.encode(frame.gray())});
        }
        if bool_arg(args, "keep") {
            self.frame_seq += 1;
            let id = format!("f{}", self.frame_seq);
            body["frame"] = Value::String(id.clone());
            self.frames.push_back((id, frame));
            while self.frames.len() > FRAME_CACHE {
                self.frames.pop_front();
            }
            return Ok((body, None));
        }
        // No handle asked for: the full capture travels (a host without screen.frames).
        let png = frame.encode(got.width() as u32, got.height() as u32)?;
        Ok((body, Some(png)))
    }

    fn encode(&mut self, args: &Map<String, Value>) -> OpResult<ToolOutput> {
        let id = str_arg(args, "frame");
        let (w, h) = (int_arg(args, "width", None)?, int_arg(args, "height", None)?);
        let frame = self
            .frames
            .iter()
            .find(|(fid, _)| fid == id)
            .map(|(_, f)| f)
            .ok_or_else(|| ProviderError::new(ErrorCode::NotFound, "the frame was evicted; capture again"))?;
        if !(1..=10_000).contains(&w) || !(1..=10_000).contains(&h) {
            return Err(ProviderError::invalid("width / height out of range"));
        }
        let png = frame.encode(w as u32, h as u32)?;
        Ok((json!({"width": w, "height": h}), Some(png)))
    }

    fn changes(&mut self, args: &Map<String, Value>) -> OpResult<ToolOutput> {
        let rect = rect_of(args.get("rect"))?;
        if rect.width() <= 0 || rect.height() <= 0 {
            return Err(ProviderError::invalid("rect is empty"));
        }
        let since = match args.get("since") {
            None | Some(Value::Null) => None,
            Some(v) => Some(v.as_u64().ok_or_else(|| ProviderError::invalid("since is a seq from an earlier answer"))?),
        };
        let timeout_ms = int_arg(args, "timeout_ms", Some(0))?;
        if !(0..=MAX_TIMEOUT_MS).contains(&timeout_ms) {
            return Err(ProviderError::invalid(format!("timeout_ms is 0..{MAX_TIMEOUT_MS}")));
        }
        let ignore = match args.get("ignore") {
            None | Some(Value::Null) => Vec::new(),
            Some(Value::Array(rows)) if rows.len() <= MAX_IGNORE => {
                rows.iter().map(|r| rect_of(Some(r))).collect::<OpResult<Vec<Rect>>>()?
            }
            Some(_) => return Err(ProviderError::invalid(format!("ignore is a list of at most {MAX_IGNORE} rects"))),
        };
        let timeout = Duration::from_millis(timeout_ms as u64);
        let report = self.desktop.changes(rect, since, timeout, &ignore)?;
        Ok((report.to_json(), None))
    }

    fn ui_elements(&mut self, args: &Map<String, Value>) -> OpResult<ToolOutput> {
        let hwnd = hwnd_of(args)?;
        let max = int_arg(args, "max", Some(80))?.max(0) as usize;
        let budget = Duration::from_millis(int_arg(args, "budget_ms", Some(1500))?.max(0) as u64);
        let found = self.desktop.elements(hwnd, max, budget)?;
        self.element_gen += 1;
        let generation = self.element_gen;
        let mut keyed = HashMap::new();
        let mut rows = Vec::with_capacity(found.len());
        for (i, el) in found.iter().enumerate() {
            let key = format!("k{generation}.{}", i + 1);
            keyed.insert(key.clone(), el.id);
            rows.push(json!({"key": key, "control": el.control, "name": el.name, "rect": rect_json(&el.rect)}));
        }
        self.elements.push_back((generation, keyed));
        while self.elements.len() > ELEMENT_GENERATIONS {
            if let Some((_, expired)) = self.elements.pop_front() {
                let ids: Vec<u64> = expired.into_values().collect();
                self.desktop.forget_elements(&ids);
            }
        }
        Ok((json!({"elements": rows}), None))
    }
}

type Job = Box<dyn FnOnce(&mut Provider) + Send>;

/// The ONE desktop thread: it creates the platform desktop and runs every operation in
/// order. The MCP side sends jobs and awaits the answers.
pub struct DesktopThread {
    tx: mpsc::Sender<Job>,
}

impl DesktopThread {
    pub fn spawn<F>(factory: F) -> std::io::Result<Self>
    where
        F: FnOnce() -> Result<Box<dyn Desktop>, String> + Send + 'static,
    {
        let (tx, rx) = mpsc::channel::<Job>();
        thread::Builder::new().name("desktop".into()).spawn(move || {
            crate::platform::init_desktop_thread();
            let mut provider = match factory() {
                Ok(desktop) => Provider::new(desktop),
                Err(reason) => {
                    tracing::error!("desktop backend unavailable: {reason}");
                    Provider::new(Box::new(crate::platform::Unavailable(reason)))
                }
            };
            while let Ok(job) = rx.recv() {
                job(&mut provider);
            }
        })?;
        Ok(Self { tx })
    }

    /// Run one operation on the desktop thread and wait for it.
    pub async fn call(&self, name: String, args: Map<String, Value>) -> OpResult<ToolOutput> {
        let (reply, answer) = tokio::sync::oneshot::channel();
        let job: Job = Box::new(move |provider: &mut Provider| {
            let _ = reply.send(provider.call(&name, &args));
        });
        self.tx
            .send(job)
            .map_err(|_| ProviderError::new(ErrorCode::Internal, "DesktopThreadGone"))?;
        answer.await.map_err(|_| ProviderError::new(ErrorCode::Internal, "DesktopThreadGone"))?
    }
}

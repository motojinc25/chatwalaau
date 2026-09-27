//! Platform selection. 2a implements Windows; other platforms report every operation as
//! internal / declare no feature (2c adds macOS and Linux, UDR-0173 D2).

use std::time::Duration;

use crate::desktop::{Captured, Desktop};
use crate::protocol::*;

#[cfg(windows)]
mod win;

/// Prepare THE desktop thread (per-monitor-v2 DPI awareness, COM STA on Windows).
pub fn init_desktop_thread() {
    #[cfg(windows)]
    win::init_desktop_thread();
}

pub fn real_desktop() -> Result<Box<dyn Desktop>, String> {
    #[cfg(windows)]
    {
        win::Win32Desktop::new().map(|d| Box::new(d) as Box<dyn Desktop>)
    }
    #[cfg(not(windows))]
    {
        Err(format!("{} is not supported yet (phase 2c)", std::env::consts::OS))
    }
}

/// The backend that could not be created: declares nothing, refuses everything.
pub struct Unavailable(pub String);

impl Unavailable {
    fn err<T>(&self) -> OpResult<T> {
        tracing::debug!("desktop backend unavailable: {}", self.0);
        Err(ProviderError::new(ErrorCode::Internal, "BackendUnavailable"))
    }
}

impl Desktop for Unavailable {
    fn features(&self) -> Vec<&'static str> {
        Vec::new()
    }
    fn list_windows(&mut self) -> OpResult<Vec<WindowInfo>> {
        self.err()
    }
    fn window(&mut self, _: i64) -> OpResult<Option<WindowInfo>> {
        self.err()
    }
    fn foreground(&mut self) -> OpResult<Option<WindowInfo>> {
        self.err()
    }
    fn focus(&mut self, _: i64, _: Option<(i32, i32)>) -> OpResult<Option<WindowInfo>> {
        self.err()
    }
    fn has_modal_dialog(&mut self, _: &WindowInfo) -> OpResult<bool> {
        self.err()
    }
    fn desktop_locked(&mut self) -> OpResult<bool> {
        self.err()
    }
    fn capture(&mut self, _: Rect) -> OpResult<Box<dyn Captured>> {
        self.err()
    }
    fn elements(&mut self, _: i64, _: usize, _: Duration) -> OpResult<Vec<ElementInfo>> {
        self.err()
    }
    fn element_rect(&mut self, _: u64) -> OpResult<Option<Rect>> {
        self.err()
    }
    fn forget_elements(&mut self, _: &[u64]) {}
    fn find_element(&mut self, _: i64, _: &str, _: Option<&str>) -> OpResult<bool> {
        self.err()
    }
    fn caret_rect(&mut self) -> OpResult<Option<Rect>> {
        self.err()
    }
    fn cursor_pos(&mut self) -> OpResult<(i32, i32)> {
        self.err()
    }
    fn move_to(&mut self, _: i32, _: i32) -> OpResult<()> {
        self.err()
    }
    fn click(&mut self, _: i32, _: i32, _: &str, _: u32) -> OpResult<()> {
        self.err()
    }
    fn drag(&mut self, _: i32, _: i32, _: i32, _: i32) -> OpResult<()> {
        self.err()
    }
    fn scroll(&mut self, _: i32, _: i32, _: i32, _: i32) -> OpResult<()> {
        self.err()
    }
    fn keys(&mut self, _: &[String]) -> OpResult<()> {
        self.err()
    }
    fn type_text(&mut self, _: &str) -> OpResult<()> {
        self.err()
    }
    fn paste_text(&mut self, _: &str) -> OpResult<()> {
        self.err()
    }
}

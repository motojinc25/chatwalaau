//! DXGI Desktop Duplication: the fast capture path (UDR-0173 D3) and the repaint reports of
//! `screen.changes` (PRP-0191 A1, UDR-0173 D10-D12).
//!
//! ONE owner: this `Duplicator` lives on the desktop thread, and every frame it acquires --
//! for a capture or while waiting for a repaint -- updates both the kept desktop image and the
//! change log, so the two uses never steal each other's frames (D12). Rules:
//!
//! * a rectangle is served only when it lies inside ONE output with identity rotation;
//! * the output's latest desktop image is kept as a GPU texture (a GPU-to-GPU copy per frame)
//!   and read back only for a capture, and only if it changed since the last read-back
//!   (Desktop Duplication hands a new frame only when the screen changed, so "no new frame"
//!   means "the kept image is current");
//! * each frame with a desktop image appends its dirty and move rectangles (virtual-desktop
//!   coordinates) to the change log; pointer-only frames (`LastPresentTime == 0`) are neither
//!   images nor repaints -- the cursor is excluded, as GDI `BitBlt` excludes it;
//! * any error (access lost on a mode change, the lock screen, UAC / the secure desktop, a
//!   session without duplication support) drops every output and disables DXGI for a while:
//!   captures use GDI, `screen_changes` answers `available: false`, and the next duplication
//!   starts a new history (earlier tokens read "changed").

use std::time::{Duration, Instant};

use windows::Win32::Foundation::RECT;
use windows::Win32::Graphics::Direct3D::D3D_DRIVER_TYPE_UNKNOWN;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ, D3D11_MAPPED_SUBRESOURCE,
    D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT, D3D11_USAGE_STAGING, D3D11CreateDevice,
    ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_MODE_ROTATION_IDENTITY, DXGI_SAMPLE_DESC};
use windows::Win32::Graphics::Dxgi::{
    CreateDXGIFactory1, DXGI_ERROR_NOT_FOUND, DXGI_ERROR_WAIT_TIMEOUT, DXGI_OUTDUPL_FRAME_INFO,
    DXGI_OUTDUPL_MOVE_RECT, IDXGIAdapter1, IDXGIFactory1, IDXGIOutput1, IDXGIOutputDuplication, IDXGIResource,
};
use windows::core::Interface;

use crate::changes::{ChangeLog, ChangeReport};
use crate::imaging::RgbImage;
use crate::protocol::Rect;

/// After a failure, GDI serves captures (and `screen_changes` is unavailable) this long.
const BACKOFF: Duration = Duration::from_secs(5);
/// How long a fresh duplication may take to deliver its first desktop image:
/// FIRST_FRAME_ATTEMPTS x FIRST_FRAME_MS; GDI serves the capture meanwhile.
const FIRST_FRAME_MS: u32 = 50;
const FIRST_FRAME_ATTEMPTS: u32 = 4;

struct Output {
    rect: RECT,
    device: ID3D11Device,
    context: ID3D11DeviceContext,
    dup: IDXGIOutputDuplication,
    /// The latest desktop image of this output (GPU, BGRA).
    latest: Option<ID3D11Texture2D>,
    /// CPU-readable copy of `latest`, refreshed on demand.
    staging: Option<ID3D11Texture2D>,
    /// `staging` holds the current `latest`.
    staged: bool,
    have_image: bool,
    /// The one-time wait for a fresh duplication's first image has been spent.
    waited: bool,
    dirty: Vec<RECT>,
    moved: Vec<DXGI_OUTDUPL_MOVE_RECT>,
}

impl Output {
    fn holds(&self, r: &Rect) -> bool {
        self.rect.left <= r.left && self.rect.top <= r.top && r.right <= self.rect.right && r.bottom <= self.rect.bottom
    }
}

pub struct Duplicator {
    outputs: Option<Vec<Output>>,
    disabled_until: Option<Instant>,
    log: ChangeLog,
}

impl Duplicator {
    pub fn new() -> Self {
        Self { outputs: None, disabled_until: None, log: ChangeLog::new() }
    }

    fn fail(&mut self, why: &str) {
        tracing::debug!("DXGI unavailable ({why}); captures use GDI, screen_changes is unavailable");
        self.outputs = None;
        self.disabled_until = Some(Instant::now() + BACKOFF);
    }

    /// Open the outputs if needed; false while disabled or when duplication is impossible.
    fn ensure(&mut self) -> bool {
        if self.disabled_until.is_some_and(|t| Instant::now() < t) {
            return false;
        }
        if self.outputs.is_some() {
            return true;
        }
        match open_outputs() {
            Ok(outputs) if !outputs.is_empty() => {
                self.outputs = Some(outputs);
                self.log.restart();
                true
            }
            Ok(_) => {
                self.fail("no output");
                false
            }
            Err(e) => {
                self.fail(&e.to_string());
                false
            }
        }
    }

    /// A capture of `rect`, or `None` to fall back to GDI.
    pub fn capture(&mut self, rect: Rect) -> Option<RgbImage> {
        if !self.ensure() {
            return None;
        }
        let Self { outputs, log, .. } = self;
        // Spans outputs or lies outside every output: GDI handles it.
        let out = outputs.as_mut()?.iter_mut().find(|o| o.holds(&rect))?;
        let result = refresh(out, log).and_then(|()| read(out, rect));
        match result {
            Ok(image) => image, // None: no desktop image yet -- GDI this time
            Err(e) => {
                self.fail(&e.to_string());
                None
            }
        }
    }

    /// `screen_changes`: repaints inside `rect` since `since`, waiting up to `timeout`.
    pub fn changes(&mut self, rect: Rect, since: Option<u64>, timeout: Duration, ignore: &[Rect]) -> ChangeReport {
        if !self.ensure() {
            return ChangeReport::unavailable(self.log.seq(), "unavailable");
        }
        let deadline = Instant::now() + timeout;
        let Self { outputs, log, .. } = self;
        let Some(out) = outputs.as_mut().and_then(|o| o.iter_mut().find(|o| o.holds(&rect))) else {
            return ChangeReport::unavailable(log.seq(), "rect_not_on_one_output");
        };
        // Take what is pending first: it happened before this call.
        if let Err(e) = refresh(out, log) {
            let why = e.to_string();
            self.fail(&why);
            return ChangeReport::unavailable(self.log.seq(), "access_lost");
        }
        let Some(since) = since else {
            return log.report(&rect, log.seq(), ignore);
        };
        loop {
            let report = log.report(&rect, since, ignore);
            let left = deadline.saturating_duration_since(Instant::now());
            if report.changed || left.is_zero() {
                return report;
            }
            let wait_ms = left.as_millis().clamp(1, u32::MAX as u128) as u32;
            if let Err(e) = pump(out, log, wait_ms) {
                let why = e.to_string();
                self.fail(&why);
                return ChangeReport::unavailable(self.log.seq(), "access_lost");
            }
        }
    }
}

fn open_outputs() -> windows::core::Result<Vec<Output>> {
    let mut outputs = Vec::new();
    unsafe {
        let factory: IDXGIFactory1 = CreateDXGIFactory1()?;
        let mut a = 0;
        loop {
            let adapter: IDXGIAdapter1 = match factory.EnumAdapters1(a) {
                Ok(adapter) => adapter,
                Err(e) if e.code() == DXGI_ERROR_NOT_FOUND => break,
                Err(e) => return Err(e),
            };
            a += 1;
            let mut o = 0;
            loop {
                let output = match adapter.EnumOutputs(o) {
                    Ok(output) => output,
                    Err(e) if e.code() == DXGI_ERROR_NOT_FOUND => break,
                    Err(e) => return Err(e),
                };
                o += 1;
                let desc = output.GetDesc()?;
                if !desc.AttachedToDesktop.as_bool() || desc.Rotation != DXGI_MODE_ROTATION_IDENTITY {
                    continue;
                }
                let mut device: Option<ID3D11Device> = None;
                let mut context: Option<ID3D11DeviceContext> = None;
                D3D11CreateDevice(
                    &adapter,
                    D3D_DRIVER_TYPE_UNKNOWN,
                    Default::default(),
                    D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                    None,
                    D3D11_SDK_VERSION,
                    Some(&mut device),
                    None,
                    Some(&mut context),
                )?;
                let (Some(device), Some(context)) = (device, context) else { continue };
                let output1: IDXGIOutput1 = output.cast()?;
                let dup = output1.DuplicateOutput(&device)?;
                outputs.push(Output {
                    rect: desc.DesktopCoordinates,
                    device,
                    context,
                    dup,
                    latest: None,
                    staging: None,
                    staged: false,
                    have_image: false,
                    waited: false,
                    dirty: Vec::new(),
                    moved: Vec::new(),
                });
            }
        }
    }
    Ok(outputs)
}

/// Take the pending frame if there is one. Until the first frame that carries a desktop image
/// arrives, `have_image` stays false (the caller uses GDI); a fresh duplication waits for it
/// once, briefly, and never again.
fn refresh(out: &mut Output, log: &mut ChangeLog) -> windows::core::Result<()> {
    let waiting = !out.have_image && !out.waited;
    out.waited = true;
    let (attempts, timeout) = if waiting { (FIRST_FRAME_ATTEMPTS, FIRST_FRAME_MS) } else { (1, 0) };
    for _ in 0..attempts {
        pump(out, log, timeout)?;
        if out.have_image {
            break;
        }
    }
    Ok(())
}

/// Wait up to `timeout_ms` for ONE frame; keep its image and log its repaints.
fn pump(out: &mut Output, log: &mut ChangeLog, timeout_ms: u32) -> windows::core::Result<()> {
    let mut info = DXGI_OUTDUPL_FRAME_INFO::default();
    let mut resource: Option<IDXGIResource> = None;
    unsafe {
        match out.dup.AcquireNextFrame(timeout_ms, &mut info, &mut resource) {
            Ok(()) => {}
            Err(e) if e.code() == DXGI_ERROR_WAIT_TIMEOUT => return Ok(()), // nothing new
            Err(e) => return Err(e),
        }
        let result = take_frame(out, log, &info, resource);
        let _ = out.dup.ReleaseFrame();
        result
    }
}

unsafe fn take_frame(
    out: &mut Output,
    log: &mut ChangeLog,
    info: &DXGI_OUTDUPL_FRAME_INFO,
    resource: Option<IDXGIResource>,
) -> windows::core::Result<()> {
    // LastPresentTime 0: no new desktop image (a pointer-only update, or a fresh duplication's
    // first frame, whose texture can still be black). Not an image, not a repaint.
    if info.LastPresentTime == 0 {
        return Ok(());
    }
    let Some(resource) = resource else { return Ok(()) };
    let texture: ID3D11Texture2D = resource.cast()?;
    if out.latest.is_none() {
        let mut desc = D3D11_TEXTURE2D_DESC::default();
        unsafe { texture.GetDesc(&mut desc) };
        let keep = D3D11_TEXTURE2D_DESC {
            MipLevels: 1,
            ArraySize: 1,
            Format: DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
            Usage: D3D11_USAGE_DEFAULT,
            BindFlags: 0,
            CPUAccessFlags: 0,
            MiscFlags: 0,
            ..desc
        };
        let mut latest: Option<ID3D11Texture2D> = None;
        unsafe { out.device.CreateTexture2D(&keep, None, Some(&mut latest))? };
        out.latest = latest;
    }
    let Some(latest) = out.latest.as_ref() else { return Ok(()) };
    unsafe { out.context.CopyResource(latest, &texture) };
    out.staged = false;
    let first = !out.have_image;
    out.have_image = true;
    log.push_frame(if first { vec![to_rect(&out.rect, 0, 0)] } else { repaints(out, info) });
    Ok(())
}

/// This frame's dirty and move rectangles in virtual-desktop coordinates; the whole output when
/// the metadata cannot be read (unknown is reported as a repaint, never as quiet).
fn repaints(out: &mut Output, info: &DXGI_OUTDUPL_FRAME_INFO) -> Vec<Rect> {
    let whole = vec![to_rect(&out.rect, 0, 0)];
    let total = info.TotalMetadataBufferSize as usize;
    if total == 0 {
        return whole;
    }
    let (dx, dy) = (out.rect.left, out.rect.top);
    let mut rects = Vec::new();
    unsafe {
        out.moved.resize(total / size_of::<DXGI_OUTDUPL_MOVE_RECT>() + 1, DXGI_OUTDUPL_MOVE_RECT::default());
        let mut used = 0u32;
        let bytes = (out.moved.len() * size_of::<DXGI_OUTDUPL_MOVE_RECT>()) as u32;
        if out.dup.GetFrameMoveRects(bytes, out.moved.as_mut_ptr(), &mut used).is_err() {
            return whole;
        }
        for m in &out.moved[..used as usize / size_of::<DXGI_OUTDUPL_MOVE_RECT>()] {
            let d = m.DestinationRect;
            rects.push(Rect::new(d.left + dx, d.top + dy, d.right + dx, d.bottom + dy));
            let (sx, sy) = (m.SourcePoint.x + dx, m.SourcePoint.y + dy);
            rects.push(Rect::new(sx, sy, sx + (d.right - d.left), sy + (d.bottom - d.top)));
        }
        out.dirty.resize(total / size_of::<RECT>() + 1, RECT::default());
        let mut used = 0u32;
        let bytes = (out.dirty.len() * size_of::<RECT>()) as u32;
        if out.dup.GetFrameDirtyRects(bytes, out.dirty.as_mut_ptr(), &mut used).is_err() {
            return whole;
        }
        for d in &out.dirty[..used as usize / size_of::<RECT>()] {
            rects.push(to_rect(d, dx, dy));
        }
    }
    rects
}

fn to_rect(r: &RECT, dx: i32, dy: i32) -> Rect {
    Rect::new(r.left + dx, r.top + dy, r.right + dx, r.bottom + dy)
}

/// Read `rect` back from the kept image; `None` while there is no image yet.
fn read(out: &mut Output, rect: Rect) -> windows::core::Result<Option<RgbImage>> {
    let Some(latest) = out.latest.clone() else { return Ok(None) };
    if !out.have_image {
        return Ok(None);
    }
    let mut desc = D3D11_TEXTURE2D_DESC::default();
    unsafe { latest.GetDesc(&mut desc) };
    let (ow, oh) = (out.rect.right - out.rect.left, out.rect.bottom - out.rect.top);
    if desc.Width as i32 != ow || desc.Height as i32 != oh {
        return Err(windows::core::Error::new(
            windows::Win32::Foundation::E_UNEXPECTED,
            "frame size differs from the output",
        ));
    }
    if out.staging.is_none() {
        let staging_desc = D3D11_TEXTURE2D_DESC {
            Usage: D3D11_USAGE_STAGING,
            BindFlags: 0,
            CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
            MiscFlags: 0,
            ..desc
        };
        let mut staging: Option<ID3D11Texture2D> = None;
        unsafe { out.device.CreateTexture2D(&staging_desc, None, Some(&mut staging))? };
        out.staging = staging;
        out.staged = false;
    }
    let Some(staging) = out.staging.clone() else { return Ok(None) };
    unsafe {
        if !out.staged {
            out.context.CopyResource(&staging, &latest);
            out.staged = true;
        }
        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        out.context.Map(&staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))?;
        let pitch = mapped.RowPitch as usize;
        let (x0, y0) = ((rect.left - out.rect.left) as usize, (rect.top - out.rect.top) as usize);
        let (w, h) = (rect.width() as usize, rect.height() as usize);
        let src = std::slice::from_raw_parts(mapped.pData as *const u8, pitch * desc.Height as usize);
        let image = RgbImage::from_bgra(w as u32, h as u32, &src[y0 * pitch + x0 * 4..], pitch);
        out.context.Unmap(&staging, 0);
        Ok(Some(image))
    }
}

//! The MCP stdio server (CTR-0236): `describe`, the 20 RES-0007 operations and
//! `screen_changes` (PRP-0191 A1) as tools.
//!
//! Results: JSON as structured content and as text; a PNG as `image` content. Errors: an
//! `isError` result whose text and structured content are `{"error": code, "message": ...}`.
//! Arguments are never logged (the text to type can be a secret value, UDR-0172 D9).

use std::sync::Arc;

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as B64;
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation, ListToolsResult,
    PaginatedRequestParams, ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::RequestContext;
use rmcp::{ErrorData as McpError, RoleServer, ServerHandler};
use serde_json::{Map, Value, json};

use crate::desktop::DesktopThread;
use crate::protocol::{PROVIDER_NAME, PROVIDER_VERSION};

pub struct DesktopServer {
    desktop: Arc<DesktopThread>,
    tools: Vec<Tool>,
}

fn schema(props: Value, required: &[&str]) -> Map<String, Value> {
    let v = json!({"type": "object", "properties": props, "required": required});
    v.as_object().cloned().unwrap_or_default()
}

fn tools() -> Vec<Tool> {
    let rect = json!({
        "type": "object",
        "properties": {"left": {"type": "integer"}, "top": {"type": "integer"},
                       "right": {"type": "integer"}, "bottom": {"type": "integer"}},
        "required": ["left", "top", "right", "bottom"]
    });
    let handle = json!({"type": "string", "description": "Opaque window handle, e.g. 'hwnd:0x1A2B'."});
    let int = json!({"type": "integer"});
    let s = json!({"type": "string"});
    let defs: Vec<(&str, &str, Value, Vec<&str>)> = vec![
        ("describe", "Protocol, provider, platform and declared features.", json!({}), vec![]),
        ("windows_list", "Visible, unowned, uncloaked top-level windows.", json!({}), vec![]),
        ("windows_get", "One window by handle (not_found when gone).", json!({"handle": handle}), vec!["handle"]),
        ("windows_foreground", "The foreground window (owned windows included).", json!({}), vec![]),
        (
            "windows_focus",
            "Restore and bring a window to the front; optional size [w, h] (windows.resize).",
            json!({"handle": handle, "size": {"type": "array", "items": {"type": "integer"}}}),
            vec!["handle"],
        ),
        ("windows_dialog", "Whether a modal dialog blocks the window.", json!({"handle": handle}), vec!["handle"]),
        ("session_locked", "Whether the interactive session is locked.", json!({}), vec![]),
        (
            "screen_capture",
            "Capture a rectangle of the virtual desktop (physical pixels). thumbnail: add the grayscale \
             thumbnail. keep: keep the capture in memory and return a frame handle instead of the PNG.",
            json!({"rect": rect, "thumbnail": {"type": "boolean"}, "keep": {"type": "boolean"}}),
            vec!["rect"],
        ),
        (
            "screen_encode",
            "PNG of a kept frame resized to width x height (not_found once evicted).",
            json!({"frame": s, "width": int, "height": int}),
            vec!["frame", "width", "height"],
        ),
        (
            "screen_changes",
            "Repaints of the screen inside rect since token `since` (no since: a fresh token at once),              waiting up to timeout_ms (0..2000) for one; repaints covered by `ignore` do not count. A              repaint is a trigger to look, not a pixel change. available false: poll instead.",
            json!({"rect": rect, "since": int, "timeout_ms": int, "ignore": {"type": "array", "items": rect}}),
            vec!["rect"],
        ),
        (
            "ui_elements",
            "Interactive UI elements of a window, interactive controls first.",
            json!({"handle": handle, "max": int, "budget_ms": int}),
            vec!["handle"],
        ),
        ("ui_element_rect", "The CURRENT rectangle of an element key.", json!({"key": s}), vec!["key"]),
        (
            "ui_find",
            "Whether an element with this name (and control type) exists in the window.",
            json!({"handle": handle, "name": s, "control": s}),
            vec!["handle", "name"],
        ),
        ("ui_caret", "The text caret rectangle, or null.", json!({}), vec![]),
        ("input_cursor", "The cursor position.", json!({}), vec![]),
        ("input_move", "Move the cursor.", json!({"x": int, "y": int}), vec!["x", "y"]),
        (
            "input_click",
            "Click at a point.",
            json!({"x": int, "y": int, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "count": int}),
            vec!["x", "y"],
        ),
        (
            "input_drag",
            "Drag with the left button.",
            json!({"x1": int, "y1": int, "x2": int, "y2": int}),
            vec!["x1", "y1", "x2", "y2"],
        ),
        (
            "input_scroll",
            "Scroll at a point; dy > 0 scrolls down (notches).",
            json!({"x": int, "y": int, "dy": int, "dx": int}),
            vec!["x", "y"],
        ),
        (
            "input_keys",
            "Press a key chord, e.g. ['ctrl', 's'].",
            json!({"keys": {"type": "array", "items": {"type": "string"}}}),
            vec!["keys"],
        ),
        ("input_type_text", "Type Unicode text with key events.", json!({"text": s}), vec!["text"]),
        ("input_paste", "Paste text through the clipboard (restored afterwards).", json!({"text": s}), vec!["text"]),
    ];
    defs.into_iter()
        .map(|(name, desc, props, required)| Tool::new(name, desc, Arc::new(schema(props, &required))))
        .collect()
}

impl DesktopServer {
    pub fn new(desktop: DesktopThread) -> Self {
        Self { desktop: Arc::new(desktop), tools: tools() }
    }

    pub fn tool_names() -> Vec<String> {
        tools().into_iter().map(|t| t.name.to_string()).collect()
    }
}

fn error_result(body: Value) -> CallToolResult {
    let mut result = CallToolResult::error(vec![ContentBlock::text(body.to_string())]);
    result.structured_content = Some(body);
    result
}

impl ServerHandler for DesktopServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(PROVIDER_NAME, PROVIDER_VERSION))
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult { tools: self.tools.clone(), ..Default::default() })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, McpError> {
        let name = request.name.to_string();
        let args = request.arguments.unwrap_or_default();
        let result = match self.desktop.call(name, args).await {
            Ok((body, png)) => {
                let mut content = vec![ContentBlock::text(body.to_string())];
                if let Some(png) = png {
                    content.push(ContentBlock::image(B64.encode(png), "image/png"));
                }
                let mut ok = CallToolResult::success(content);
                ok.structured_content = Some(body);
                ok
            }
            Err(err) => error_result(err.to_json()),
        };
        Ok(CallToolResponse::Complete(result))
    }
}

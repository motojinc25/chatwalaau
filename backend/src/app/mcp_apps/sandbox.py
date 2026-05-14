"""MCP Apps sandbox proxy server (CTR-0066, PRP-0034).

Serves sandbox proxy HTML on a separate port with CSP headers
for double-iframe origin isolation per the MCP Apps specification.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import re
import threading
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Sandbox proxy HTML (inline, no build step needed)
SANDBOX_PROXY_HTML = r"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>MCP Apps Sandbox</title>
<style>*{margin:0;padding:0}html,body{width:100%;height:100%;overflow:hidden}
iframe{width:100%;height:100%;border:none}</style>
</head>
<body>
<script>
"use strict";
// MCP Apps Sandbox Proxy (CTR-0066)
// Double-iframe architecture: Host <-> Sandbox (this) <-> View (inner)

if (window.self === window.top) {
  throw new Error("Sandbox must run inside an iframe");
}

// Determine expected host origin.
// In cross-origin iframes, document.referrer may be empty depending on
// Referrer-Policy and browser settings. Fall back to learning the host
// origin from the first postMessage received from window.parent.
var EXPECTED_HOST_ORIGIN = document.referrer
  ? new URL(document.referrer).origin
  : null;
var OWN_ORIGIN = new URL(window.location.href).origin;

// Create inner iframe for the View
var inner = document.createElement("iframe");
inner.style.cssText = "width:100%;height:100%;border:none";
inner.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms");
document.body.appendChild(inner);

var RESOURCE_READY = "ui/notifications/sandbox-resource-ready";
var PROXY_READY = "ui/notifications/sandbox-proxy-ready";

// Bidirectional message relay
window.addEventListener("message", function(event) {
  if (event.source === window.parent) {
    // Learn host origin from first parent message if referrer was empty
    if (!EXPECTED_HOST_ORIGIN) {
      EXPECTED_HOST_ORIGIN = event.origin;
    }
    // Validate origin on subsequent messages
    if (event.origin !== EXPECTED_HOST_ORIGIN) return;

    if (event.data && event.data.method === RESOURCE_READY) {
      var params = event.data.params || {};
      if (typeof params.sandbox === "string") {
        inner.setAttribute("sandbox", params.sandbox);
      }
      if (typeof params.html === "string") {
        var doc = inner.contentDocument || (inner.contentWindow && inner.contentWindow.document);
        if (doc) {
          doc.open();
          doc.write(params.html);
          doc.close();
        } else {
          inner.srcdoc = params.html;
        }
      }
    } else if (inner.contentWindow) {
      inner.contentWindow.postMessage(event.data, "*");
    }
  } else if (event.source === inner.contentWindow) {
    // Message from View -> relay to Host
    if (EXPECTED_HOST_ORIGIN) {
      window.parent.postMessage(event.data, EXPECTED_HOST_ORIGIN);
    } else {
      window.parent.postMessage(event.data, "*");
    }
  }
});

// Notify Host that sandbox is ready.
// Use "*" as target origin because we may not know the host origin yet
// (referrer empty in cross-origin dev mode). The host validates the
// message by checking event.source === iframe.contentWindow.
window.parent.postMessage({
  jsonrpc: "2.0",
  method: PROXY_READY,
  params: {}
}, "*");
</script>
</body>
</html>"""


def _sanitize_csp_domains(domains: list | None) -> list[str]:
    """Sanitize CSP domain values to prevent injection."""
    if not domains:
        return []
    return [d for d in domains if isinstance(d, str) and not re.search(r'[;\r\n\'" ]', d)]


def _build_csp_header(csp: dict | None) -> str:
    """Build Content-Security-Policy header from MCP server metadata."""
    resource_domains = " ".join(_sanitize_csp_domains(csp.get("resourceDomains") if csp else None))
    connect_domains = " ".join(_sanitize_csp_domains(csp.get("connectDomains") if csp else None))
    frame_domains = " ".join(_sanitize_csp_domains(csp.get("frameDomains") if csp else None))
    base_uri_domains = " ".join(_sanitize_csp_domains(csp.get("baseUriDomains") if csp else None))

    directives = [
        "default-src 'self' 'unsafe-inline'",
        f"script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: data: {resource_domains}".strip(),
        f"style-src 'self' 'unsafe-inline' blob: data: {resource_domains}".strip(),
        f"img-src 'self' data: blob: {resource_domains}".strip(),
        f"font-src 'self' data: blob: {resource_domains}".strip(),
        f"media-src 'self' data: blob: {resource_domains}".strip(),
        f"connect-src 'self' {connect_domains}".strip(),
        f"worker-src 'self' blob: {resource_domains}".strip(),
        f"frame-src {frame_domains}" if frame_domains else "frame-src 'none'",
        "object-src 'none'",
        f"base-uri {base_uri_domains}" if base_uri_domains else "base-uri 'none'",
    ]
    return "; ".join(directives)


class SandboxHandler(BaseHTTPRequestHandler):
    """HTTP handler for the sandbox proxy server."""

    def do_GET(self):
        parsed = urlparse(self.path)

        # Only serve sandbox.html (any path)
        csp_config = None
        qs = parse_qs(parsed.query)
        if "csp" in qs:
            import contextlib

            with contextlib.suppress(json.JSONDecodeError, IndexError):
                csp_config = json.loads(qs["csp"][0])

        csp_header = _build_csp_header(csp_config)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", csp_header)
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        # Allow referrer from parent page for origin validation
        self.send_header("Referrer-Policy", "origin")
        # CORS for sandbox origin access
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(SANDBOX_PROXY_HTML.encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


_sandbox_server: HTTPServer | None = None
_sandbox_thread: threading.Thread | None = None


def start_sandbox_server(port: int, host: str = "127.0.0.1") -> bool:
    """Start the sandbox proxy server on a separate port.

    Returns True if started successfully, False otherwise.
    """
    global _sandbox_server, _sandbox_thread

    try:
        _sandbox_server = HTTPServer((host, port), SandboxHandler)
        _sandbox_thread = threading.Thread(
            target=_sandbox_server.serve_forever,
            daemon=True,
            name="mcp-apps-sandbox",
        )
        _sandbox_thread.start()
        logger.info("MCP Apps sandbox proxy started on http://%s:%d", host, port)
        return True
    except OSError as e:
        logger.error("Failed to start MCP Apps sandbox proxy on port %d: %s", port, e)
        return False


def stop_sandbox_server() -> None:
    """Stop the sandbox proxy server."""
    global _sandbox_server, _sandbox_thread

    if _sandbox_server:
        _sandbox_server.shutdown()
        _sandbox_server = None
        _sandbox_thread = None
        logger.info("MCP Apps sandbox proxy stopped")

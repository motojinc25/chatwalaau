/**
 * Host style variables for MCP App Views (CTR-0068, PRP-0034).
 * Follows the MCP Apps style variable schema (McpUiStyles type).
 * Uses light-dark() CSS function for automatic theme adaptation.
 * Variable names must match the strict schema defined by the MCP Apps SDK.
 */
export const HOST_STYLE_VARIABLES: Record<string, string> = {
  // Background colors
  '--color-background-primary': 'light-dark(#ffffff, #1a1a1a)',
  '--color-background-secondary': 'light-dark(#f5f5f5, #2d2d2d)',
  '--color-background-tertiary': 'light-dark(#e5e5e5, #404040)',
  '--color-background-inverse': 'light-dark(#1a1a1a, #ffffff)',
  '--color-background-ghost': 'light-dark(rgba(255,255,255,0), rgba(26,26,26,0))',
  '--color-background-info': 'light-dark(#eff6ff, #1e3a5f)',
  '--color-background-danger': 'light-dark(#fef2f2, #7f1d1d)',
  '--color-background-success': 'light-dark(#f0fdf4, #14532d)',
  '--color-background-warning': 'light-dark(#fefce8, #713f12)',
  '--color-background-disabled': 'light-dark(rgba(255,255,255,0.5), rgba(26,26,26,0.5))',

  // Text colors
  '--color-text-primary': 'light-dark(#1f2937, #f3f4f6)',
  '--color-text-secondary': 'light-dark(#6b7280, #9ca3af)',
  '--color-text-tertiary': 'light-dark(#9ca3af, #6b7280)',
  '--color-text-inverse': 'light-dark(#f3f4f6, #1f2937)',
  '--color-text-ghost': 'light-dark(rgba(107,114,128,0.5), rgba(156,163,175,0.5))',
  '--color-text-info': 'light-dark(#1d4ed8, #60a5fa)',
  '--color-text-danger': 'light-dark(#b91c1c, #f87171)',
  '--color-text-success': 'light-dark(#15803d, #4ade80)',
  '--color-text-warning': 'light-dark(#a16207, #fbbf24)',
  '--color-text-disabled': 'light-dark(rgba(31,41,55,0.5), rgba(243,244,246,0.5))',

  // Border colors
  '--color-border-primary': 'light-dark(#e5e7eb, #404040)',
  '--color-border-secondary': 'light-dark(#d1d5db, #525252)',
  '--color-border-tertiary': 'light-dark(#f3f4f6, #374151)',
  '--color-border-inverse': 'light-dark(rgba(255,255,255,0.3), rgba(0,0,0,0.3))',
  '--color-border-ghost': 'light-dark(rgba(229,231,235,0), rgba(64,64,64,0))',
  '--color-border-info': 'light-dark(#93c5fd, #1e40af)',
  '--color-border-danger': 'light-dark(#fca5a5, #991b1b)',
  '--color-border-success': 'light-dark(#86efac, #166534)',
  '--color-border-warning': 'light-dark(#fde047, #854d0e)',
  '--color-border-disabled': 'light-dark(rgba(229,231,235,0.5), rgba(64,64,64,0.5))',

  // Ring colors (focus)
  '--color-ring-primary': 'light-dark(#3b82f6, #60a5fa)',
  '--color-ring-secondary': 'light-dark(#6b7280, #9ca3af)',

  // Typography
  '--font-sans': "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
  '--font-mono': "ui-monospace, 'SF Mono', Monaco, 'Cascadia Code', monospace",
  '--font-weight-normal': '400',
  '--font-weight-medium': '500',
  '--font-weight-semibold': '600',
  '--font-weight-bold': '700',
  '--font-text-sm-size': '0.875rem',
  '--font-text-md-size': '1rem',

  // Border radius
  '--border-radius-sm': '4px',
  '--border-radius-md': '6px',
  '--border-radius-lg': '8px',

  // Shadows
  '--shadow-sm': '0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px -1px rgba(0, 0, 0, 0.1)',
}

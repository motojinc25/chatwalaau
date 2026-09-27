# chatwalaau-computer-use

The desktop provider of [ChatWalaʻau](https://github.com/motojinc25/chatwalaau) Computer Use:
a native executable that speaks the `chatwalaau.computer-use/1` protocol over MCP stdio. It
lists and focuses windows, captures the screen, reads UI Automation elements and sends mouse,
keyboard and text input -- and nothing else. It listens on no network port, runs no code and
never writes screenshots to disk.

ChatWalaʻau installs this package automatically on Windows x64 and starts the executable when an
agent first uses the desktop. It is not meant to be run by hand.

- Platform: Windows x64 (Windows on ARM runs it under x64 emulation).
- License: Apache-2.0.

<div align="center">

# 📖 papershell

### A quiet, no-JS bridge between your Kindle and tmux.

papershell is not a terminal emulator. It captures a tmux pane as text and sends
your input back through plain HTML forms — simple enough to use from a Kindle
browser. It can drive ordinary shell programs, but it is especially well suited
to text-first TUIs such as **Claude Code** and **Codex**.

![License](https://img.shields.io/badge/license-MIT-blue?style=plastic)
![Version](https://img.shields.io/badge/version-v0.1.0-6f42c1?style=plastic)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB?style=plastic&logo=python&logoColor=white)
![Python dependencies](https://img.shields.io/badge/pip%20dependencies-0-brightgreen?style=plastic)
![JavaScript](https://img.shields.io/badge/JavaScript-0%20lines-critical?style=plastic)
![tmux](https://img.shields.io/badge/powered%20by-tmux-1BB91F?style=plastic&logo=tmux&logoColor=white)
![Made for Kindle](https://img.shields.io/badge/made%20for-Kindle-FF9900?style=plastic&logo=amazon&logoColor=white)

</div>

---

<div align="center">

![papershell running Codex on a Kindle](docs/kindle.png)

</div>

## Why

I built papershell because checking on a running project often meant reopening my
laptop or phone. Those devices put the work next to everything else — another tab,
a feed, a short video — and it was easy to lose focus before I even noticed.

I wanted a quieter way to stay in touch with the work: check a long-running task,
send a short reply when needed, then look up and return to the physical world
around me. A Kindle makes that interaction intentionally narrow.

papershell does not try to replace a full terminal. It is a focused window into a
tmux session, designed for workflows where most of the interaction is reading and
responding one line at a time.

That constraint also fits the Kindle's limited browser. Instead of shipping a
JavaScript terminal or opening a WebSocket, papershell uses plain HTML forms and a
`<pre>`. **tmux** provides the PTY and terminal screen buffer; the server captures
the currently rendered pane as text and sends submitted text or named keys back.

```
Kindle browser ──plain HTML form──▶  server.py  ──tmux──▶  shell / claude / codex
  (a <pre> + a <form>, no JS)                    capture-pane → plain-text screen
                                                 send-keys    → text + named keys
```

The server is a single Python file with no third-party Python packages or frontend
build step.

## Features

- 🪶 **No third-party Python packages** — a single-file stdlib server, plus the
  system-installed `tmux` executable.
- 🚫 **No client-side JavaScript or WebSockets** — navigation, input and refresh
  use regular HTML requests.
- 🔡 **Kindle-friendly text normalization** — common box-drawing characters,
  TUI symbols and icons are rewritten or blanked; wide glyphs are given a fixed
  width to improve mixed CJK/Latin alignment.
- ⌨️ **Thumb-friendly layout** — input + `Esc` on the left, `▲ up / ▼ down`
  stacked on the right, docked to the bottom of the screen. Type, press Enter, done.
- 🔀 **Use existing sessions** — list and select running tmux sessions, including
  ones created outside papershell. The selected window is resized to the configured
  fixed dimensions.
- 📐 **Fixed-size e-ink layout** — the selected tmux window is pinned to a configured
  column/row size, and the browser viewport is sized from that width.
- 📜 **Page keys** — the main controls send `PageUp` and `PageDown`; the result
  depends on how the program inside tmux handles those keys.
- 🔒 **LAN-first** — optional token gate; never meant to face the public internet.

> **Not just for Kindles.** The plain-HTML interface can also be opened from a
> phone or laptop on the same network, although the layout and interaction model
> are tuned for e-ink rather than a full-featured web terminal.

## Quick start

```bash
git clone https://github.com/tiankaixie/papershell.git
cd papershell
./run.sh                     # serves on http://<this-box-ip>:8090/
```

On the Kindle, open **`http://<this-box-ip>:8090/`** and tap **⚙ → Launch claude**.

Override the main settings with environment variables:

```bash
KINDLE_PORT=9000 KINDLE_CMD=codex ./run.sh
```

**Requirements:** a Linux/macOS box with **Python 3.8+**, **tmux**, and the command
you want to run (for example [`claude`](https://claude.com/claude-code) or
`codex`); plus a device on the same LAN or [Tailscale](https://tailscale.com/).
The server itself needs no `pip install`, `npm install` or build step.

## Using it

**Main page** — one screen, docked to the bottom:

- The `<pre>` box shows the latest captured contents of the active tmux pane.
- **Type in the field and press Enter** to send your line. (No send button — the
  keyboard's Enter/Go does it.)
- **Esc** sends `Escape`; **▲ up / ▼ down** send `PageUp` and `PageDown`.
- **↻** re-reads the screen; **Auto-refresh 2s/3s/5s** polls while the agent
  works — turn it off before typing.

**⚙ Menu page** — everything else:

- **Switch session** — pick a running tmux session (`▶` = current, `●` = attached elsewhere).
- **Keys**, grouped — **Move** (↑ ↓ ← →), **Edit** (⏎ ⇥ ␣ ⌫), **Ctrl** (^C ^D Home End).
- **Launch** claude / codex / a custom command, and **Kill** the current session.

papershell is designed as a small, single-user tool: the selected session is
shared by all connected browser clients, and viewing it may resize its active
tmux window to `KINDLE_COLS`×`KINDLE_ROWS`.

## Configuration

All via environment variables:

| Variable         | Default   | Meaning                                          |
|------------------|-----------|--------------------------------------------------|
| `KINDLE_PORT`    | `8090`    | HTTP port                                        |
| `KINDLE_HOST`    | `0.0.0.0` | Bind address                                     |
| `KINDLE_CMD`     | `claude`  | Default command to launch                        |
| `KINDLE_COLS`    | `58`      | Terminal width — the view is pinned to this      |
| `KINDLE_ROWS`    | `32`      | Terminal height — the view is pinned to this     |
| `KINDLE_ASCII`   | `1`       | Rewrite Kindle-unfriendly glyphs to ASCII (`0` to disable) |
| `KINDLE_WORKDIR` | `$HOME`   | Directory the launched command starts in         |
| `KINDLE_SESSION` | `kindle`  | Name of the tmux session it spawns               |
| `KINDLE_TOKEN`   | *(empty)* | Optional shared token; first visit uses `?t=TOKEN`, then a cookie |
| `KINDLE_SETTLE`  | `0.4`     | Seconds to wait after input before re-capturing  |

## Security

There is **no authentication or TLS by default** — it exposes a shell-capable
session, so keep it on your **LAN or Tailscale only** and never port-forward it
to the public internet. For a light gate, set `KINDLE_TOKEN=...` and open the page
once with `?t=...` (it's kept in a cookie afterwards). This shared token is a
convenience gate, not a replacement for proper authentication or encrypted transport.

## Run as a service (systemd)

```bash
mkdir -p ~/.config/systemd/user
cp papershell.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now papershell
loginctl enable-linger "$USER"   # survive logout/reboot
```

The supplied unit assumes the repository is at `~/codebase/papershell`. Edit its
`WorkingDirectory=`, `ExecStart=` and `Environment=` lines if your path or settings
are different.

## How it works

1. On **Launch**, the server starts a detached tmux session running your command.
2. On each page load it runs `tmux capture-pane -p`, normalizes common TUI glyphs
   for the Kindle-oriented display, and puts the captured text into a `<pre>`.
3. Typing posts a form; the server runs `tmux send-keys -l "<your text>"` then
   `Enter`. Key buttons send named keys (`Up`, `C-c`, `Escape`, `PageUp`, …).
4. The page's `viewport` width is derived from the fixed column count, and CSS
   hides horizontal overflow to keep the controls within the e-ink-oriented layout.

That's the whole trick: **the client stays dumb, tmux stays smart.**

## Troubleshooting

- **Can't reach the page** — make sure the Kindle and the server are on the same
  network, and the port is open: `curl http://localhost:8090/` on the server
  first, then check your firewall (`ufw allow 8090` or equivalent).
- **Blank page / "Session not running"** — no tmux session exists yet. Tap
  **⚙ → Launch claude** (or start one yourself: `tmux new -s kindle`).
- **Text looks garbled or misaligned** — `KINDLE_ASCII=1` (default) normalizes
  many common TUI glyphs. If you disabled it, re-enable it. If a specific glyph
  still renders as an empty box, open an issue with the character.
- **Screen is stale** — the page only updates on load. Tap **↻** or turn on
  **Auto-refresh** while the agent is working.
- **Another terminal keeps resizing the session** — the connector pins the
  session to `KINDLE_COLS`×`KINDLE_ROWS` on every view. Attach from SSH with
  `tmux attach -t kindle` and you'll share the same fixed-size screen.

## License

MIT — see [LICENSE](LICENSE).

<div align="center">
<sub>Built for the corner of the couch where the Wi-Fi is good and the light is low.</sub>
</div>

#!/usr/bin/env python3
"""
papershell — a no-JS web terminal for Kindle's primitive browser.

Design goals:
  * The Kindle "Experimental Browser" runs an ancient WebKit with flaky/absent
    JavaScript and no usable WebSocket support. So the CLIENT must be dumb:
    plain HTML forms, a <pre> for output, optional <meta refresh>. No JS required.
  * All the hard work (PTY, ANSI/TUI rendering, screen buffer) is delegated to
    `tmux`. We run the agent (claude / codex) inside a detached tmux session and:
        - read the screen with `tmux capture-pane -p`   (already plain text!)
        - send input with     `tmux send-keys`          (text + named keys)
  * Pure Python stdlib. No pip dependencies.

Open  http://<server-ip>:8090/  on the Kindle (LAN or Tailscale).
"""

import os
import html
import time
import re
import subprocess
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

__version__ = "0.1.0"

# ----------------------------------------------------------------------------
# Config (override via environment)
# ----------------------------------------------------------------------------
SESSION = os.environ.get("KINDLE_SESSION", "kindle")
COLS    = int(os.environ.get("KINDLE_COLS", "58"))
ROWS    = int(os.environ.get("KINDLE_ROWS", "32"))
PORT    = int(os.environ.get("KINDLE_PORT", "8090"))
HOST    = os.environ.get("KINDLE_HOST", "0.0.0.0")
WORKDIR = os.environ.get("KINDLE_WORKDIR", os.path.expanduser("~"))
DEFCMD  = os.environ.get("KINDLE_CMD", "claude")
# Optional shared secret. If set, every request must carry ?t=TOKEN (kept in a
# cookie after the first hit). Keep it simple — this is LAN-only by default.
TOKEN   = os.environ.get("KINDLE_TOKEN", "")

# Settle time after sending input before we re-capture the screen (seconds).
SETTLE  = float(os.environ.get("KINDLE_SETTLE", "0.4"))

# Named keys we expose as buttons -> the literal arg passed to `tmux send-keys`.
KEYS = {
    "Enter": "Enter", "Esc": "Escape", "Ctrl-C": "C-c", "Ctrl-D": "C-d",
    "Tab": "Tab", "Space": "Space", "Bksp": "BSpace",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    "PgUp": "PageUp", "PgDn": "PageDown", "Home": "Home", "End": "End",
}

# Short glyph labels so the key buttons take little space.
KEY_LABEL = {
    "Enter": "&#x23ce;", "Esc": "Esc", "Ctrl-C": "^C", "Ctrl-D": "^D",
    "Tab": "&#x21e5;", "Space": "&#x2423;", "Bksp": "&#x232b;",
    "Up": "&#x2191;", "Down": "&#x2193;", "Left": "&#x2190;", "Right": "&#x2192;",
    "PgUp": "&#x21de;", "PgDn": "&#x21df;", "Home": "Home", "End": "End",
}
# Main page stays minimal: the text input plus one control row (scroll
# up/down + Esc). Everything else lives on the /menu page, grouped into tidy
# labelled rows.
# Grouped keys for the /menu page — one labelled row each.
KEY_GROUPS = [
    ("Move", ["Up", "Down", "Left", "Right"]),
    ("Edit", ["Enter", "Tab", "Space", "Bksp"]),
    ("Ctrl", ["Ctrl-C", "Ctrl-D", "Home", "End"]),
]


# ----------------------------------------------------------------------------
# tmux helpers
# ----------------------------------------------------------------------------
def tmux(*args, check=False):
    """Run a tmux command, return CompletedProcess (text)."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True, text=True, check=check,
    )


# Which tmux session the web UI is currently driving, plus the scrollback
# offset (0 = live bottom; N = scrolled N lines up into history).
STATE = {"target": SESSION, "scroll": 0}


def current():
    return STATE["target"]


def list_sessions():
    r = tmux("list-sessions", "-F",
             "#{session_name}\t#{session_windows}\t#{?session_attached,*,}")
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            out.append({
                "name": parts[0],
                "windows": parts[1] if len(parts) > 1 else "?",
                "attached": (len(parts) > 2 and parts[2] == "*"),
            })
    return out


def session_alive(name=None):
    name = name or current()
    return tmux("has-session", "-t", name).returncode == 0


def start_session(cmd):
    """(Re)create the tmux session running a login shell, then launch `cmd`."""
    tmux("kill-session", "-t", SESSION)  # ignore errors
    # A plain shell so the session survives if the agent exits (then you can
    # just re-launch from /start without losing the web session).
    tmux("new-session", "-d", "-s", SESSION, "-x", str(COLS), "-y", str(ROWS),
         "-c", WORKDIR)
    # Make capture deterministic: hide status bar, pin the window size so an
    # absent client can't shrink it.
    tmux("set-option", "-t", SESSION, "status", "off")
    tmux("set-option", "-t", SESSION, "window-size", "manual")
    tmux("resize-window", "-t", SESSION, "-x", str(COLS), "-y", str(ROWS))
    if cmd.strip():
        tmux("send-keys", "-t", SESSION, "-l", cmd)
        tmux("send-keys", "-t", SESSION, "Enter")
    STATE["target"] = SESSION  # drive the freshly started session


def history_size(name=None):
    """Lines available in the scrollback above the visible screen."""
    name = name or current()
    r = tmux("display-message", "-p", "-t", name, "#{history_size}")
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


# ----------------------------------------------------------------------------
# Glyph normalization for Kindle's primitive WebKit
# ----------------------------------------------------------------------------
# Claude/codex TUIs draw with Unicode box-drawing (U+2500..257F) and block
# elements (U+2580..259F): the input-box borders and the Claude logo. These are
# grid-width-1 in the terminal, but the Kindle monospace font lacks them, so
# WebKit substitutes glyphs from a PROPORTIONAL fallback font whose advance
# width != the ASCII cell width. The result: every line after such a glyph
# drifts sideways and the art/logo/boxes look skewed.
#
# The fix that works on ANY font: map these width-1 glyphs 1:1 to ASCII, which
# is always present in the monospace font at exactly one cell. Applied only to
# the DISPLAYED screen — never to the keystrokes we send back to tmux.
#
# We handle three kinds of glyph the Kindle font can't draw:
#   1. Box-drawing / block elements    -> ASCII borders + '#' (grid art).
#   2. Claude/codex UI symbols with meaning (prompt ❯, bullet ●, tool corner
#      ⎿, spinner ✻, auto-mode ⏵⏵)     -> a matching ASCII char.
#   3. Pure decoration that renders as an empty box on Kindle: Nerd-Font /
#      Powerline / devicon glyphs in the Private Use Area, plus emoji (☁️ 🧠
#      from the shell prompt, memory banners, etc.) -> blanked to space(s),
#      preserving the terminal cell width so nothing shifts.
ASCII_NORMALIZE = os.environ.get("KINDLE_ASCII", "1") not in ("0", "", "off")


def _build_ascii_table():
    m = {}
    # Box drawing: default every corner/junction/dash to '+', then override the
    # pure horizontals and verticals so borders read as clean ASCII boxes.
    for cp in range(0x2500, 0x2580):
        m[cp] = ord("+")
    for cp in (0x2500, 0x2501, 0x2504, 0x2505, 0x2508, 0x2509,
               0x254C, 0x254D, 0x2550, 0x2574, 0x2576, 0x2578, 0x257A):
        m[cp] = ord("-")
    for cp in (0x2502, 0x2503, 0x2506, 0x2507, 0x250A, 0x250B,
               0x254E, 0x254F, 0x2551, 0x2575, 0x2577, 0x2579, 0x257B):
        m[cp] = ord("|")
    # Block elements -> '#'; the three shades get graded ASCII so gradients
    # still read as light/medium/dark.
    for cp in range(0x2580, 0x25A0):
        m[cp] = ord("#")
    m[0x2591] = ord(".")   # ░ light shade
    m[0x2592] = ord(":")   # ▒ medium shade
    m[0x2593] = ord("#")   # ▓ dark shade
    # Meaningful Claude/codex + shell UI symbols -> ASCII (all width-1).
    m[0x276F] = ord(">")   # ❯ shell prompt
    m[0x23F5] = ord(">")   # ⏵ auto-mode / play triangle
    m[0x23BF] = ord("\\")  # ⎿ tool-result branch corner
    m[0x2570] = ord("\\")  # ╰ (rounded corner claude uses for the same branch)
    m[0x25CF] = ord("*")   # ● message bullet
    m[0x25CB] = ord("o")   # ○ empty bullet
    m[0x25A0] = ord("#")   # ■ filled square
    m[0x25A1] = ord(".")   # □ empty square
    m[0x2022] = ord("*")   # • bullet
    for cp in (0x2732, 0x2733, 0x2734, 0x2736, 0x2739, 0x273B, 0x273D):
        m[cp] = ord("*")   # ✲✳✴✶✹✻✽ spinner asterisks
    m[0x2713] = ord("v")   # ✓ check
    m[0x2714] = ord("v")   # ✔ heavy check
    m[0x2717] = ord("x")   # ✗ cross
    m[0x2718] = ord("x")   # ✘ heavy cross
    m[0x00A0] = ord(" ")   # NBSP -> plain space
    # Zero-width joiners / variation selectors: delete (they carry no cell).
    for cp in (0x200D, 0xFE0E, 0xFE0F):
        m[cp] = None
    return m


ASCII_TABLE = _build_ascii_table()

# Decorative glyphs that have no ASCII meaning and simply render as tofu boxes
# on Kindle: Private Use Area (Nerd Font / Powerline / devicons), emoji and
# pictographs, and the misc-symbol/dingbat blocks. Blanked to space(s) so the
# line reads cleanly and the grid stays aligned. Symbols we mapped above are
# already ASCII by the time this runs, so they're never matched here.
_ICON_RE = re.compile(
    "[-"                    # BMP Private Use Area
    "☀-➿"                     # Misc Symbols + Dingbats (☁ ★ ✂ …)
    "\U0001f000-\U0001faff"             # Emoji & pictographs
    "\U000f0000-\U0010fffd]"            # Supplementary Private Use Areas
)


def _blank_icon(match):
    ch = match.group(0)
    # Keep the terminal's cell footprint so surrounding columns don't shift.
    return "  " if unicodedata.east_asian_width(ch) in ("W", "F") else " "


def normalize(text):
    """Map box/block/UI glyphs to ASCII and blank tofu icons, so the Kindle
    screen never skews and never shows empty boxes."""
    if not ASCII_NORMALIZE or text is None:
        return text
    text = text.translate(ASCII_TABLE)
    text = _ICON_RE.sub(_blank_icon, text)
    return text


def screen_html(text):
    """HTML-escape the captured screen, wrapping every double-width (CJK)
    glyph in <span class=w> so CSS can pin it to exactly 2 monospace cells.

    A CJK char is one grid cell pair (width 2) in the terminal, but the Kindle
    font renders Han glyphs at some non-2x width, so any row with CJK drifts and
    box borders no longer line up. Locking each such glyph to `width:2ch`
    restores the grid. Latin/space/punct stay as-is (1 cell each in monospace).
    """
    if text is None:
        return ""
    out = []
    for ch in text:
        esc = html.escape(ch)
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            out.append('<span class=w>%s</span>' % esc)
        else:
            out.append(esc)
    return "".join(out)


def capture(name=None):
    """Return the rendered screen of `name`, honoring the scrollback offset."""
    name = name or current()
    if not session_alive(name):
        return None
    off = STATE.get("scroll", 0)
    if off <= 0:
        r = tmux("capture-pane", "-p", "-t", name)
    else:
        # Grab a window of `h` rows that sits `off` lines up in history.
        # tmux line 0 = top of visible screen; negative = scrollback.
        _, h = session_dims(name)
        r = tmux("capture-pane", "-p", "-t", name,
                 "-S", str(-off), "-E", str(h - 1 - off))
    if r.returncode != 0:
        return None
    return normalize(r.stdout)


def send_text(text, name=None):
    name = name or current()
    if not session_alive(name):
        return
    if text:
        # -l = literal: never interpret as key names. No shell involved, so any
        # text (quotes, semicolons, unicode) is safe.
        tmux("send-keys", "-t", name, "-l", text)


def send_key(keyname, name=None):
    name = name or current()
    if not session_alive(name):
        return
    key = KEYS.get(keyname)
    if key:
        tmux("send-keys", "-t", name, key)


# ----------------------------------------------------------------------------
# HTML rendering (minimal, e-ink friendly, no JS)
# ----------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
{viewport}
<title>papershell</title>
{refresh}
<style>
  * {{ box-sizing: border-box; }}
  html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%;
         overflow-x: hidden; }}
  body {{ margin: 0; padding: 6px; padding-bottom: 80px; background: #fff;
         color: #000; font-family: monospace; -webkit-text-size-adjust: 100%;
         overflow-x: hidden; max-width: 100%; }}
  /* Pin the control cluster to the bottom of the screen (no gap below it).
     Falls back to normal inline flow on browsers without position:fixed. */
  .dock {{ position: fixed; left: 0; right: 0; bottom: 0; background: #fff;
          padding: 3px 6px; border-top: 1px solid #000; }}
  /* Height of the display region (the bordered box), independent of the tmux
     row count. Bump this vh value to make the terminal area taller. */
  pre.screen {{ font-size: 16px; line-height: 1.15; white-space: pre;
               height: 82vh; overflow-x: hidden; overflow-y: auto;
               border: 1px solid #000; padding: 6px;
               margin: 0 0 8px 0; -webkit-text-size-adjust: 100%;
               scrollbar-width: none; -ms-overflow-style: none; }}
  /* Hide scrollbars (content still scrolls, just no visible bar). */
  ::-webkit-scrollbar {{ width: 0; height: 0; display: none; }}
  form {{ margin: 0; }}
  .row {{ margin: 4px 0; }}
  input[type=text] {{ display: block; width: 100%; font-size: 18px;
                      padding: 7px; border: 1px solid #000; }}
  button {{ font-size: 14px; padding: 5px 8px; margin: 1px;
           border: 1px solid #000; background: #fff;
           -webkit-appearance: none; appearance: none;
           color: #000; -webkit-text-fill-color: #000;
           border-radius: 0; }}
  a {{ color: #000; }}
  .bar {{ font-size: 13px; margin: 4px 0; }}
  .dead {{ border: 2px solid #000; padding: 10px; margin-bottom: 8px; }}
  .keys button {{ font-size: 18px; padding: 5px 0; min-width: 11.5%;
                 margin: 1px 0.4%; }}
  .sessions button {{ font-size: 14px; padding: 6px 8px; }}
  small {{ font-size: 10px; color: #555; -webkit-text-fill-color: #555; }}
  .klabel {{ display: inline-block; width: 3.4em; font-size: 13px;
            color: #555; -webkit-text-fill-color: #555; }}
  .offscreen {{ position: absolute; left: -9999px; width: 1px; height: 1px;
               padding: 0; border: 0; }}
  table.ctl {{ width: 100%; border-collapse: collapse; margin: 3px 0; }}
  table.ctl td {{ padding: 2px; vertical-align: middle; }}
  table.ctl td.side {{ width: 18%; }}
  table.ctl form {{ margin: 0; }}
  table.ctl td.esccell {{ width: 16%; }}
  table.ctl button.esc {{ width: 100%; height: 60px; font-size: 13px;
                         padding: 0; }}
  table.ctl td.main input {{ width: 100%; height: 60px; font-size: 14px;
                            padding: 0 6px; }}
  table.ctl td.side button {{ width: 100%; height: 29px; font-size: 13px;
                             padding: 0; line-height: 1.0; }}
  /* Lock every double-width (CJK) glyph to exactly 2 monospace cells so tables
     drawn on the 'CJK = 2 columns' assumption stay aligned even when the
     device font renders Han glyphs at some other width. */
  .w {{ display: inline-block; width: 2ch; vertical-align: baseline; }}
</style>
</head><body>
{body}
</body></html>
"""


# Reference glyph metrics for the screen <pre> (must match its CSS font-size).
# We derive the viewport width from the session's column count so the browser
# zooms the WHOLE page to fit the device width — no horizontal scrolling, ever.
FONT_PX = 16
CHAR_W = 0.72        # monospace advance ÷ font-size (over-estimate -> always fits)
CHROME_PX = 28       # body padding + pre border/padding


def session_dims(name=None):
    """Return (cols, rows) of the session's window."""
    name = name or current()
    r = tmux("display-message", "-p", "-t", name,
             "#{window_width}\t#{window_height}")
    try:
        w, h = r.stdout.strip().split("\t")
        return int(w), int(h)
    except Exception:
        return COLS, ROWS


def session_width(name=None):
    return session_dims(name)[0]


def pin_size(name=None):
    """Keep the driven session at the fixed COLSxROWS size. The connector shows
    one consistent 58x29 view to everyone; if the session drifted (or was made
    another size), reflow it once. No-op once it already matches, so no churn."""
    name = name or current()
    if not session_alive(name):
        return
    w, h = session_dims(name)
    if w != COLS or h != ROWS:
        tmux("set-option", "-t", name, "window-size", "manual")
        tmux("resize-window", "-t", name, "-x", str(COLS), "-y", str(ROWS))


def page(viewport_html, refresh_html, body_html):
    return PAGE.format(viewport=viewport_html, refresh=refresh_html,
                       body=body_html)


def render(token_q, auto):
    """Build the full page for GET /."""
    pin_size()           # keep the view locked to the fixed COLSxROWS size
    scr = capture()

    # Fit the page to the device: viewport width = current session width in px.
    cols = session_width() if scr is not None else COLS
    vw = int(round(cols * FONT_PX * CHAR_W)) + CHROME_PX
    viewport_html = '<meta name="viewport" content="width=%d">' % vw

    # token query suffix to keep on every link/form action
    tq = ("?t=" + token_q) if token_q else ""

    # --- auto-refresh links ----------------------------------------------
    def q(extra):
        # build query string preserving token + given auto value
        parts = []
        if token_q:
            parts.append("t=" + token_q)
        if extra is not None:
            parts.append("r=" + str(extra))
        return ("?" + "&".join(parts)) if parts else ""

    refresh_html = ""
    if auto:
        refresh_html = '<meta http-equiv="refresh" content="%d; url=/%s">' % (
            auto, q(auto).lstrip("/"))

    auto_links = (
        'Auto-refresh: '
        '<a href="/%s">off</a> | '
        '<a href="/%s">2s</a> | '
        '<a href="/%s">3s</a> | '
        '<a href="/%s">5s</a>'
    ) % (q(None).lstrip("/") or "", q(2).lstrip("/"),
         q(3).lstrip("/"), q(5).lstrip("/"))

    cur = current()
    body = []

    if scr is None:
        body.append('<div class="dead"><b>Session &laquo;%s&raquo; not running.</b>'
                    '<br>Open <a href="/menu%s">&#x2699; Menu</a> to pick or launch one.</div>'
                    % (html.escape(cur), tq))
    else:
        body.append('<pre class="screen">%s</pre>' % screen_html(scr))

    # --- compact bar: session name + refresh + auto + menu ----------------
    body.append(
        '<div class="bar">[%s] '
        '<a href="/%s"><b>&#x21bb;</b></a> &nbsp; %s &nbsp; '
        '<a href="/menu%s"><b>&#x2699; Menu</b></a></div>'
        % (html.escape(cur),
           q(auto if auto else None).lstrip("/") or "", auto_links, tq)
    )

    if scr is not None:
        # --- keyboard-style control grid (right-handed) -------------------
        #   Esc (left) and text input sit in the same row, same height (both
        #   span the two rows). Scroll ▲ up / ▼ down stack on the right edge
        #   so the thumb sits there.
        # An off-screen submit button lets old WebKit submit the input on Enter
        # (implicit submission needs a submit button in the form).
        body.append(
            '<div class="dock"><table class="ctl"><tr>'
            '<td class="esccell" rowspan="2">'
            '<form method="post" action="/key%s">'
            '<button type="submit" name="key" value="Esc" class="esc">Esc</button>'
            '</form></td>'
            '<td class="main" rowspan="2">'
            '<form method="post" action="/send%s">'
            '<input type="text" name="text" autofocus autocomplete="off" '
            'autocapitalize="off" placeholder="type here, then press Enter">'
            '<button type="submit" class="offscreen" tabindex="-1">send</button>'
            '</form></td>'
            '<td class="side">'
            '<form method="post" action="/key%s">'
            '<button type="submit" name="key" value="PgUp">&#x25B2; up</button>'
            '</form></td>'
            '</tr><tr>'
            '<td class="side">'
            '<form method="post" action="/key%s">'
            '<button type="submit" name="key" value="PgDn">&#x25BC; down</button>'
            '</form></td>'
            '</tr></table></div>' % (tq, tq, tq, tq)
        )

    return page(viewport_html, refresh_html, "\n".join(body))


def render_menu(token_q):
    """Secondary page: session switching, fit-width, launch, kill, more keys."""
    tq = ("?t=" + token_q) if token_q else ""
    cur = current()
    vw = int(round(COLS * FONT_PX * CHAR_W)) + CHROME_PX
    viewport_html = '<meta name="viewport" content="width=%d">' % vw

    body = ['<div class="bar"><a href="/%s"><b>&#x2190; Back to terminal</b></a></div>'
            % tq]

    # --- session picker ---------------------------------------------------
    sessions = list_sessions()
    if sessions:
        chips = []
        for s in sessions:
            name = s["name"]
            mark = "&#x25B6; " if name == cur else ""   # ▶ current
            att = " &#x25CF;" if s["attached"] else ""   # ● attached elsewhere
            chips.append(
                '<button type="submit" name="s" value="%s">%s%s<small> (%sw%s)</small></button>'
                % (html.escape(name, quote=True), mark, html.escape(name),
                   s["windows"], att)
            )
        body.append(
            '<form method="post" action="/select%s"><div class="row sessions">'
            'Switch session: %s</div></form>' % (tq, "".join(chips))
        )

    # Width is fixed at COLS (58) for a consistent view — no manual picker.

    # Height is fixed at ROWS (29) for a consistent view — no manual picker.

    # --- keys, grouped into tidy labelled rows ----------------------------
    for label, names in KEY_GROUPS:
        btns = "".join(
            '<button type="submit" name="key" value="%s">%s</button>'
            % (k, KEY_LABEL[k]) for k in names
        )
        body.append(
            '<form method="post" action="/key%s">'
            '<div class="row keys"><span class="klabel">%s</span>%s</div>'
            '</form>' % (tq, label, btns)
        )

    # --- launch / kill ----------------------------------------------------
    body.append(
        '<form method="post" action="/start%s"><div class="row">Launch: '
        '<button type="submit" name="cmd" value="claude">claude</button>'
        '<button type="submit" name="cmd" value="codex">codex</button>'
        '<button type="submit" name="cmd" value="">run custom &#x2193;</button>'
        '<input type="text" name="custom" placeholder="custom command">'
        '</div></form>' % tq
    )
    body.append(
        '<form method="post" action="/kill%s"><div class="row">'
        '<button type="submit">Kill current session</button></div></form>' % tq
    )

    return page(viewport_html, "", "\n".join(body))


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "papershell/" + __version__
    protocol_version = "HTTP/1.1"

    # ---- auth -----------------------------------------------------------
    def _token_ok(self, qs):
        if not TOKEN:
            return True, ""
        # accept ?t= or cookie
        t = (qs.get("t", [""])[0])
        if not t:
            cookie = self.headers.get("Cookie", "")
            for part in cookie.split(";"):
                if part.strip().startswith("kc="):
                    t = part.strip()[3:]
        return (t == TOKEN), t

    def _send(self, body, code=200, set_cookie=None, location=None):
        data = body.encode("utf-8")
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", "kc=%s; Path=/" % set_cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _deny(self):
        self._send("<h3>403 — token required (?t=...)</h3>", code=403)

    def _read_post(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        return parse_qs(raw, keep_blank_values=True)

    # ---- GET ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        ok, t = self._token_ok(qs)
        if not ok:
            return self._deny()

        if u.path == "/favicon.ico":
            return self._send("", code=204)

        cookie = t if (TOKEN and t == TOKEN) else None

        if u.path == "/menu":
            return self._send(render_menu(t if TOKEN else ""),
                              set_cookie=cookie)

        auto = 0
        try:
            auto = int(qs.get("r", ["0"])[0])
        except ValueError:
            auto = 0
        if auto not in (0, 2, 3, 5):
            auto = 0

        self._send(render(t if TOKEN else "", auto), set_cookie=cookie)

    do_HEAD = do_GET

    # ---- POST -----------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        ok, t = self._token_ok(qs)
        if not ok:
            return self._deny()

        form = self._read_post()
        path = u.path

        # Any input/navigation action jumps back to the live screen.
        if path != "/scroll":
            STATE["scroll"] = 0

        if path == "/scroll":
            name = current()
            _, h = session_dims(name)
            step = max(1, h // 2)
            d = form.get("dir", [""])[0]
            off = STATE.get("scroll", 0)
            if d == "up":
                off += step
            elif d == "down":
                off -= step
            elif d == "bottom":
                off = 0
            off = max(0, min(off, history_size(name)))
            STATE["scroll"] = off
        elif path == "/send":
            text = form.get("text", [""])[0]
            send_text(text)
            if "noenter" not in form:
                send_key("Enter")
        elif path == "/key":
            send_key(form.get("key", [""])[0])
        elif path == "/select":
            name = form.get("s", [""])[0]
            if name and session_alive(name):
                STATE["target"] = name
        elif path == "/resize":
            name = current()
            if session_alive(name):
                w, h = session_dims(name)
                try:
                    w = int(form.get("cols", [str(w)])[0])
                except ValueError:
                    pass
                try:
                    h = int(form.get("rows", [str(h)])[0])
                except ValueError:
                    pass
                w = max(20, min(w, 400))
                h = max(8, min(h, 200))
                # manual window-size lets us pin the size even while the
                # session is attached elsewhere; reflows the TUI to fit.
                tmux("set-option", "-t", name, "window-size", "manual")
                tmux("resize-window", "-t", name, "-x", str(w), "-y", str(h))
        elif path == "/start":
            cmd = form.get("cmd", [""])[0]
            if not cmd:
                cmd = form.get("custom", [""])[0] or DEFCMD
            start_session(cmd)
        elif path == "/kill":
            victim = current()
            tmux("kill-session", "-t", victim)
            # fall back to any remaining session so the UI isn't left dangling
            remaining = list_sessions()
            STATE["target"] = remaining[0]["name"] if remaining else SESSION

        time.sleep(SETTLE)
        # Post/Redirect/Get so a Kindle "back/refresh" doesn't re-submit.
        tq = ("?t=" + t) if (TOKEN and t == TOKEN) else ""
        self._send("", code=303, location="/" + tq)

    def log_message(self, *a):  # quieter logs
        pass


def main():
    print("papershell  http://%s:%d/   session=%s  %dx%d  cmd=%s"
          % (HOST, PORT, SESSION, COLS, ROWS, DEFCMD))
    if TOKEN:
        print("  token auth ON — open with  ?t=%s" % TOKEN)
    else:
        print("  token auth OFF — LAN/Tailscale only, no auth")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

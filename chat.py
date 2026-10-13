"""Claude Room terminal client.

  python chat.py [NAME]                        on the PC running server.py (NAME: room.json's host by default)
  python chat.py http://HOST:8765 TOKEN        from anywhere else
  python chat.py "http://HOST:8765/?t=TOKEN"   same, as one link

  --no-pc   don't let Claude do anything on this PC
  --auto    start in auto mode: run Claude's actions on this PC without asking

Claude can act on this PC only in turns where you wrote something. You approve
each action (y / n / a = auto from now on); type /auto to switch auto mode on/off.

Enter sends · Esc clears the line · type / for commands (Tab completes,
up/down picks) · Ctrl+C quits
"""
import getpass
import json
import os
import platform
import queue
import re
import shutil
import textwrap
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

VERSION = 39   # the server tells older clients to update; bump when the protocol changes
ROOT = os.path.dirname(os.path.abspath(__file__))
CSI = "\x1b["
RESET, BOLD, DIM = CSI + "0m", CSI + "1m", CSI + "2m"
COLOR = {"cyan": "36", "yellow": "33", "magenta": "35", "green": "32", "blue": "34", "red": "31"}
CLAUDE_COLOR = CSI + "38;5;209m"
HEADER = "[for "
PROMPT = "› "
HOME = os.path.expanduser("~")
YES, NO, ALWAYS = set("yYдДъЪ"), set("nNнН"), set("aAаА")
COMMANDS = [
    ("/new", "start a fresh conversation for everyone"),
    ("/stop", "stop Claude's work on your request"),
    ("/stop all", "stop every running task"),
    ("/usage", "how much of Claude's session and week is used"),
    ("/timeline", "the project's story so far (the page's Timeline tab shows the pictures)"),
    ("/poll ", "ask everyone to vote: /poll Which theme? | Desert | Jungle"),
    ("/vote ", "vote in the newest poll: /vote 2"),
    ("/recap", "what happened while you were away (/recap 24 = the last 24 hours)"),
    ("/continue", "after a Stop: carry on (/continue opus = with another model, this turn only)"),
    ("/threads all", "what every thread is for and what it has done"),
    ("/remind ", "a reminder: /remind 18:00 playtest · /remind 30m … · /remind me tomorrow 10:00 …"),
    ("/reminders", "the reminders coming up"),
    ("/saved", "messages you saved with ☆ in the browser"),
    ("/shot", "put a picture of your screen in the chat"),
    ("/effort auto", "quick questions on Medium (faster), real work on High"),
    ("/p ", "a message for the people only (Claude doesn't read it): /p hi"),
    ("/sync", "whose copy of the project is behind, and the latest check-ins"),
    ("/compact", "shrink this thread's conversation to a summary (saves usage)"),
    ("/model", "show the current model"),
    ("/model fable", "switch to Claude Fable 5.1"),
    ("/model opus", "switch to Claude Opus 5.5"),
    ("/model sonnet", "switch to Claude Sonnet 5.5"),
    ("/model haiku", "switch to Claude Haiku 5.5"),
    ("/model default", "back to the room's default model"),
    ("/effort", "how hard your Claude thinks"),
    ("/effort low", "think briefly"),
    ("/effort medium", "think a bit"),
    ("/effort high", "think more"),
    ("/effort xhigh", "think a lot"),
    ("/effort max", "think the most"),
    ("/effort default", "Claude Code's default"),
    ("/auto", "toggle auto mode: run Claude's actions on your PC without asking"),
    ("/tested", "done playtesting: free UEFN for your other threads"),
    ("/auto on", "auto mode on"),
    ("/sound approvals off", "no beep when Claude asks to act on your PC (on: beep again)"),
    ("/auto off", "auto mode off: approve each action"),
    ("/board", "open the task board (or Tab on an empty line)"),
    ("/draw", "open the whiteboard"),
    ("/files", "open the shared files"),
    ("/image ", "make a picture: /image a lighthouse on a cliff at dusk"),
    ("/paste", "share the picture (or files) you copied"),
    ("/pin ", "pin onto Claude's last answer: /pin a note · /pin card · /pin sketch · /pin file <id>"),
    ("/send ", "share a file with the room: /send C:\\path\\file"),
    ("/get ", "download a shared file to Downloads: /get <id>"),
    ("/web", "open the room in your browser: task board + whiteboard"),
    ("/update", "get the newest chat.py and restart it in this window"),
    ("/tool", "open the last tool call in full (Ctrl+O) · /tool N opens line [N]"),
    ("/thread", "your threads: conversations with Claude that run at the same time"),
    ("/thread new ", "open another thread: /thread new Inventory UI"),
    ("/thread main", "back to your main thread"),
    ("/thread rename ", "rename the thread you're in"),
    ("/thread close", "close the thread you're in"),
    ("/locks", "who holds which file's Lore lock right now"),
    ("/release", "release your Lore locks (only files already checked in)"),
    ("/notifications", "the last notifications"),
    ("/who", "who is online"),
    ("/help", "list the commands"),
]
MENU_MAX = 8
FUN = ["Pondering", "Noodling", "Brewing", "Conjuring", "Percolating", "Mulling", "Scheming", "Tinkering", "Cogitating",
       "Ruminating", "Simmering", "Wrangling", "Untangling", "Spelunking", "Marinating", "Finagling", "Hatching", "Whirring", "Puzzling",
       "Sleuthing", "Crunching", "Moseying", "Doodling", "Summoning", "Haunting", "Lurking", "Creeping", "Flickering", "Rummaging",
       "Cooking", "Vibing", "Musing", "Churning", "Fiddling", "Plotting", "Bamboozling", "Clauding", "Herding pixels", "Consulting the fog",
       "Sharpening pencils", "Reticulating splines", "Waking the editor", "Counting pages", "Tiptoeing"]


TIPS = ["/saved lists your ★ saved messages", "/remind 18:00 playtest sets a reminder", "/shot puts a picture of your screen in the chat",
        "/recap: a “Previously on…” of what happened while you were away", "/continue opus picks up after a Stop on another model",
        "/compact shrinks a long conversation now and saves usage", "/poll and /vote run a quick poll",
        "Esc Esc on an empty line stops Claude's running answer", "Ctrl+O or /tool N shows a tool call in full",
        "Tab opens tasks · files · whiteboard", "/tested frees the playtest for your other threads",
        "/locks shows the Lore locks · /release frees yours on checked-in files", "/sound approvals off mutes the approval beep",
        "/operator turns a thread into a control thread answered by the host's Claude Code", "/effort auto: quick questions answer faster"]


def room_tip():
    """A tip about the room for the status line, changing every 20 seconds."""
    return "Tip: " + TIPS[int(time.time() // 20) % len(TIPS)]


def fun_word(lane, since):
    """A funny word for "Claude is busy", changing every 6 seconds."""
    h = 7
    for c in lane + str(round(since)):
        h = (h * 31 + ord(c)) % (1 << 32)
    return FUN[(h + int((time.time() - since) // 6)) % len(FUN)]

# full-screen views
RGB = {"orange": "38;2;232;135;91", "dim": "38;2;122;128;140", "line": "38;2;58;64;74", "fg": "38;2;215;218;224"}
COL_RGB = {"ideas": "38;2;198;120;221", "backlog": "38;2;141;151;170", "todo": "38;2;97;175;239", "doing": "38;2;229;192;123",
           "done": "38;2;152;195;121"}
LABEL_RGB = {"red": "38;2;224;108;117", "yellow": "38;2;229;192;123", "green": "38;2;152;195;121",
             "blue": "38;2;97;175;239", "magenta": "38;2;198;120;221", "cyan": "38;2;86;200;216"}
PEN_COLORS = ["#e8eaed", "#e5c07b", "#e8875b", "#e06c75", "#c678dd", "#61afef", "#56c8d8", "#98c379"]
BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))   # dot bit for [y % 4][x % 2]
VIEWS = ("chat", "board", "files", "draw")


def owner(lane):
    """A lane is a person ("alice") or one of their threads ("alice/inventory-ui")."""
    return str(lane or "").split("/", 1)[0]


def hex_rgb(color):
    try:
        return f"38;2;{int(color[1:3], 16)};{int(color[3:5], 16)};{int(color[5:7], 16)}"
    except (ValueError, TypeError, IndexError):
        return RGB["fg"]


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def ago(ts):
    s = time.time() - ts
    return "now" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 86400 else f"{int(s // 86400)}d"


class Grid:
    """A screen-sized grid of (character, SGR style) cells, turned into ANSI rows in one go."""

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.ch = [[" "] * cols for _ in range(rows)]
        self.st = [[""] * cols for _ in range(rows)]

    def put(self, r, c, text, style=""):
        if 0 <= r < self.rows:
            for ch in text:
                if 0 <= c < self.cols:
                    self.ch[r][c], self.st[r][c] = ch, style
                c += 1

    def row(self, r, upto=None):
        out, cur = [], None
        for c in range(self.cols if upto is None else max(0, min(upto, self.cols))):
            st = self.st[r][c]
            if st != cur:
                out.append(RESET + (f"{CSI}{st}m" if st else ""))
                cur = st
            out.append(self.ch[r][c])
        return "".join(out) + RESET


def braille_lines(strokes, cols, rows):
    """A small drawing fitted into cols x rows characters of braille dots (2x4 dots each)."""
    pts = [p for st in strokes for p in zip(st["pts"][0::2], st["pts"][1::2])]
    if not pts:
        return []
    x1, x2, y1, y2 = min(p[0] for p in pts), max(p[0] for p in pts), min(p[1] for p in pts), max(p[1] for p in pts)
    k = min((cols * 2 - 1) / max(1, x2 - x1), (rows * 4 - 1) / max(1, y2 - y1))
    dots, color = bytearray(cols * rows), [""] * (cols * rows)
    for st in strokes:
        style, p = hex_rgb(st.get("color")), st["pts"]
        line = list(zip(p[0::2], p[1::2]))
        if st["type"] == "rect" and len(line) == 2:
            (a, b), (c, d) = line
            line = [(a, b), (c, b), (c, d), (a, d), (a, b)]
        for (ax, ay), (bx, by) in zip(line, line[1:] or line):
            ax, ay, bx, by = (ax - x1) * k, (ay - y1) * k, (bx - x1) * k, (by - y1) * k
            steps = int(max(abs(bx - ax), abs(by - ay))) + 1
            for t in range(steps + 1):
                px, py = int(ax + (bx - ax) * t / steps), int(ay + (by - ay) * t / steps)
                if 0 <= px < cols * 2 and 0 <= py < rows * 4:
                    i = (py >> 2) * cols + (px >> 1)
                    dots[i] |= BRAILLE[py & 3][px & 1]
                    color[i] = style
    out = []
    for r in range(rows):
        row = "".join((f"{CSI}{color[r * cols + c]}m" + chr(0x2800 + dots[r * cols + c])) if dots[r * cols + c] else " "
                      for c in range(cols))
        out.append(row + RESET)
    while out and not out[-1].replace(RESET, "").strip():
        out.pop()
    return out


def clip_line(x1, y1, x2, y2, w, h):
    """Liang-Barsky: the part of a segment inside [0,w)x[0,h), or None."""
    t0, t1, dx, dy = 0.0, 1.0, x2 - x1, y2 - y1
    for p, q in ((-dx, x1), (dx, w - 1 - x1), (-dy, y1), (dy, h - 1 - y1)):
        if p == 0:
            if q < 0:
                return None
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return None if t0 > t1 else (x1 + t0 * dx, y1 + t0 * dy, x1 + t1 * dx, y1 + t1 * dy)


def parse_args(args):
    if not args and os.path.exists(os.path.join(ROOT, "room.json")):
        args = [""]   # on the PC running server.py: join as the room's host
    if len(args) == 1 and not args[0].startswith("http"):
        with open(os.path.join(ROOT, "room.json"), encoding="utf-8-sig") as f:
            cfg = json.load(f)
        name = (args[0] or cfg.get("host", "")).lower()
        if name not in cfg.get("people", {}):
            sys.exit(f"{name!r} isn't in room.json's people (python server.py invite NAME adds someone)")
        return f"http://127.0.0.1:{cfg['port']}", cfg["people"][name]
    if len(args) == 1:
        url = urllib.parse.urlparse(args[0])
        token = urllib.parse.parse_qs(url.query)["t"][0]
        return f"{url.scheme}://{url.netloc}", token
    if len(args) == 2:
        return args[0].rstrip("/"), args[1]
    sys.exit(__doc__)


# one TLS setup for the whole run: building a context loads the Windows certificate
# store, which is slow and used to happen on every /typing post
OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()))


class Connection:
    def __init__(self, base, token, events, share_pc):
        self.base, self.token, self.events = base, token, events
        self.agent_query = f"&v={VERSION}"
        if share_pc:
            info = {"agent": "1", "os": f"{platform.system()} {platform.release()}",
                    "user": getpass.getuser(), "home": HOME}
            self.agent_query += "&" + urllib.parse.urlencode(info)
        self.outbox = queue.Queue()
        self.closed = False
        self.resp = None
        self.post_ms = []         # how long recent POSTs took, for lag reports
        threading.Thread(target=self.read_loop, daemon=True).start()
        threading.Thread(target=self.send_loop, daemon=True).start()

    def read_loop(self):
        while not self.closed:
            try:
                url = f"{self.base}/events?t={self.token}{self.agent_query}"
                with OPENER.open(url, timeout=60) as r:
                    self.resp = r
                    for raw in r:
                        if self.closed:
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data: "):
                            self.events.put(json.loads(line[6:]))
            except Exception as e:  # noqa: BLE001 - any failure means reconnect
                if self.closed:
                    return
                self.events.put({"t": "_lost", "err": str(e)})
            time.sleep(2)

    def close(self):
        """Hang up for good (the updated chat.py opens its own connection)."""
        self.closed = True
        try:
            self.resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)   # wake the blocked read now
        except Exception:  # noqa: BLE001 - best effort; the read loop also stops at the next ping
            pass

    def send_loop(self):
        while True:
            path, data = self.outbox.get()
            req = urllib.request.Request(
                f"{self.base}{path}?t={self.token}", data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            started = time.time()
            try:
                OPENER.open(req, timeout=15).read()
                self.post_ms = (self.post_ms + [int((time.time() - started) * 1000)])[-20:]
            except Exception as e:  # noqa: BLE001
                if path == "/say":
                    self.events.put({"t": "sys", "text": f"not sent ({e}): {data['text'][:60]}"})

    def post(self, path, data=None):
        self.outbox.put((path, data or {}))


class Screen:
    """Scrolling log on top, a status row and an input row pinned at the bottom."""

    def __init__(self):
        self.size = None
        self.log_text = ""        # everything written to the log, for redraw on resize
        self.at_line_start = True
        self.header_buf = None    # characters held while deciding if a line is a [for ...] header
        self.buf = ""
        self.cursor = 0
        self.me = ""
        self.colors = {}
        self.online = []
        self.busy = {}            # person -> {"since", "note"} while Claude works on their request
        self.live = None          # whose answer is streaming into the log right now
        self.held = {}            # person -> answer parts held back while another answer streams
        self.tool_ids = []        # (tool id, name) per tool line shown: [N] on the line, Ctrl+O / /tool N
        self.threads = []         # everyone's threads: {"id": "alice/inventory-ui", "owner", "name", "closed"?}
        self.thread = ""          # where your messages go: "" = your main thread
        self.lane_order = []      # held answers, in the order they'll be printed
        self.interrupted = False  # a message was printed in the middle of the streaming answer
        self.latest = 0           # newest chat.py VERSION the room has
        self.announced = 0        # last version we told the user about
        self.want_update = False  # /update typed: main() restarts with the new file
        self.typing = {}
        self.connected = False
        self.last_error = ""
        self.mode = ""
        self.conn = None
        self.events = None
        self.auto = False
        self.mute_approval = False   # the person's setting: no beep on approval prompts
        self.approvals = []       # exec requests waiting for y/n
        self.approval_since = 0.0
        self.sel = 0              # highlighted command suggestion
        self.complete_base = None # what was typed before Tab started cycling
        self.menu_rows = 0        # rows the command menu is covering above the status row
        self.model = ""
        # full-screen views: Tab cycles chat -> board -> draw
        self.view = "chat"
        self.board = {"columns": [], "cards": []}
        self.board_loaded = self.wb_loaded = False
        self.bcol, self.bsel = 1, None      # selected column index and card id
        self.note_open = False
        self.prompt = None                  # {"label", "buf", "done"}: one-line input in a view's footer
        self.flash = ("", 0.0)              # short note in a view's footer
        self.wb = {"strokes": [], "live": {}, "cursors": {}}
        self.dview = None                   # whiteboard camera: (world x, world y, pixels per unit)
        self.cross = None                   # crosshair cell (col, row) on the whiteboard
        self.pen = None                     # stroke being drawn here
        self.pen_color = 0
        self.mine = []                      # ids of my strokes, for undo
        self.last_cursor_post = 0.0
        self.vt_mode = None                 # saved console input mode while the mouse is on
        # pins on chat messages
        self.snips = {}                     # message id -> a few words of it, for "pinned onto ..." lines
        self.esc_at = 0                     # first Esc of the Esc Esc that stops Claude
        self.usage = None                   # the host's Claude plan usage, from the server
        self.answer_text, self.lane_aid = {}, {}
        self.pin_pick = False               # /pin card: the board picks a card to pin
        self.sketch = None                  # /pin sketch: strokes drawn for a pin instead of the whiteboard
        self.files, self.files_loaded = [], False
        self.fsel = 0                       # selected row in the Files view
        self.previews = {}                  # file id -> RGB grid (or None while loading)
        self.locks = []                     # the room's live Lore lock list
        self.notes = {}                     # version -> what's new
        self.notices = []                   # (time, text, kind): the notifications you got
        self.notice = None                  # (text, style, until): shown in the status line
        self.update_at = None               # self-update countdown target
        self.update_postponed = 0.0
        self.last_key = time.time()

    # ---- low level ------------------------------------------------------
    def w(self, s):
        sys.stdout.write(s)

    def setup(self):
        self.w("\x1b]0;Claude Room\x07")
        self.resize(force=True)

    def resize(self, force=False):
        size = shutil.get_terminal_size()
        if size == self.size and not force:
            return
        self.size = size
        if self.view != "chat":
            self.dview = None if self.view == "draw" else self.dview
            self.draw_view()
            return
        cols, rows = size
        # set the scroll region, then replay the tail of the log into it
        self.w(CSI + "r" + CSI + "2J" + CSI + f"1;{rows - 2}r" + CSI + "1;1H" + "\x1b7")
        tail = self.log_text[-cols * rows * 3:]
        if len(tail) < len(self.log_text) and "\n" in tail:
            tail = tail[tail.index("\n") + 1:]
        self.w("\x1b8" + tail + "\x1b7")
        self.draw_bottom()

    def restore(self):
        rows = self.size[1]
        self.mouse(False)
        self.w(CSI + "r" + CSI + "?25h" + CSI + f"{rows};1H" + CSI + "2K" + RESET + "\n")
        sys.stdout.flush()

    def color(self, name, text=None):
        code = COLOR.get(self.colors.get(name, ""), "37")
        return f"{BOLD}{CSI}{code}m{text if text is not None else name}{RESET}"

    # ---- bottom rows ----------------------------------------------------
    def status_text(self):
        if not self.connected:
            return f"✗ disconnected, retrying… {self.last_error}"
        if self.approvals:
            return f"⚠ Claude is waiting for your approval ({len(self.approvals)})"
        bits = []
        if self.update_at:
            bits.append(f"⬆ updating to v{self.latest} in {max(0, int(self.update_at - time.time()))}s (Esc: later)")
        elif self.latest > VERSION:
            bits.append(f"⬆ v{self.latest} is ready: /update")
        mine = [l for l in self.locks if l.get("owner") == self.me]
        if self.locks:
            bits.append(f"🔒 {len(self.locks)} locked" + (f" ({len(mine)} yours)" if mine else "") + " · /locks")
        lanes = []
        for lane, b in self.busy.items():
            if owner(lane) != self.me:   # only your own threads: the other person's are on their PC
                continue
            note = f" · {b['note']}" if b["note"] and b["note"] != "thinking" else ""
            later = " (shown when done)" if lane in self.held else ""
            label = f"#{self.thread_name(lane)}" if self.thread_name(lane) else "Main"
            lanes.append(f"✻ {fun_word(lane, b['since'])}… {label} {int(time.time() - b['since'])}s{note}{later}")
        if lanes:
            bits.append("  ".join(lanes))
        typers = [n for n, t in self.typing.items() if time.time() - t < 3 and n != self.me]
        if typers:
            bits.append(", ".join(typers) + " typing…")
        lims = [l for l in (self.usage or {}).get("limits") or [] if l.get("kind") in ("session", "weekly_all")]
        used = " · ".join(f"{l['label'][:1].lower() + l['label'][1:]} {l['percent']}%" for l in lims)
        if not bits:
            mode = "auto mode" if self.auto and self.mode == "your PC shared" else self.mode
            bits.append("online: " + ", ".join(self.online) + f" · {mode} · {self.model}" + (f" · usage: {used}" if used else "")
                        + " · Tab: tasks · files · whiteboard · /help")
        elif used and max(l["percent"] for l in lims) >= 80:
            bits.append(f"⚠ usage: {used}")
        if len(lanes) <= 1:
            bits.append(room_tip())
        return " · ".join(bits)

    def draw_bottom(self):
        if self.view != "chat":
            self.draw_view()
            return
        cols, rows = self.size
        matches = [] if self.approvals else self.matches()
        n = min(len(matches), MENU_MAX, max(0, rows - 6))
        if n < self.menu_rows:
            # the menu shrank or closed: repaint the log rows it was covering
            self.menu_rows = 0
            self.resize(force=True)
            return
        self.menu_rows = n
        out = [CSI + "?25l"]
        if n:
            # vertical command menu just above the status row, scrolled to keep the selection visible
            sel = self.sel % len(matches)
            top = min(max(0, sel - n + 1), len(matches) - n)
            width = max(len(c) for c, _ in matches) + 3
            for i in range(n):
                cmd, desc = matches[top + i]
                text = (cmd.ljust(width) + desc)[: cols - 4]
                line = (BOLD + CLAUDE_COLOR + "❯ " + text + RESET) if top + i == sel else ("  " + DIM + text + RESET)
                out.append(CSI + f"{rows - 1 - n + i};1H" + line + CSI + "K")
        status = self.status_text()[: cols - 2]
        if self.notice and time.time() < self.notice[2] and not self.approvals:   # a fresh notification wins the line
            text, style, _ = self.notice
            out.append(CSI + f"{rows - 1};1H" + f"{CSI}{style}m" + text[: cols - 1] + RESET + DIM + " " + status[: max(0, cols - len(text) - 2)]
                       + RESET + CSI + "K" + CSI + f"{rows};1H")
        else:
            out.append(CSI + f"{rows - 1};1H" + DIM + "─ " + status + RESET + CSI + "K" + CSI + f"{rows};1H")
        if self.approvals:
            left = max(0, int(self.approvals[0]["_deadline"] - time.time()))
            ask = f"Allow this on your PC?  y = yes   n = no   a = auto   (skipped in {left // 60}:{left % 60:02d})"
            self.w("".join(out) + BOLD + CSI + "33m" + ask[: cols - 1] + RESET + CSI + "K" + CSI + "?25h")
            sys.stdout.flush()
            return
        ghost = ""
        if n and self.complete_base is None and self.cursor == len(self.buf):
            ghost = matches[self.sel % len(matches)][0][len(self.buf):]
        shown = self.buf.replace("\n", "↵")
        where = f"#{self.thread_name(self.thread)[:24]} " if self.thread else ""
        avail = cols - len(where) - len(PROMPT) - 1
        start = max(0, self.cursor - avail)
        visible = shown[start:start + avail]
        ghost = ghost[: max(0, avail - len(visible))]
        out.append(CLAUDE_COLOR + where + RESET + self.color(self.me, PROMPT) + visible + DIM + ghost + RESET + CSI + "K"
                   + CSI + f"{rows};{len(where) + len(PROMPT) + 1 + self.cursor - start}H" + CSI + "?25h")
        self.w("".join(out))
        sys.stdout.flush()

    # ---- log ------------------------------------------------------------
    def log(self, s):
        if not s:
            return
        self.log_text = (self.log_text + s)[-400_000:]
        self.at_line_start = s.endswith("\n")
        if self.view == "chat":   # in the board/whiteboard it's kept and shown when you come back
            self.w("\x1b8" + s + "\x1b7")

    def newline_if_needed(self):
        self.flush_header()
        if not self.at_line_start:
            self.log("\n")

    def stream(self, text):
        """Write Claude text, coloring `[for name]` header lines as they arrive."""
        out = []
        for ch in text:
            if self.header_buf is None:
                if self.at_line_start and ch == "[":
                    self.header_buf = ch
                    continue
                out.append(ch)
                self.at_line_start = ch == "\n"
                continue
            self.header_buf += ch
            b = self.header_buf
            if len(b) <= len(HEADER) and HEADER.startswith(b):
                continue
            if b.startswith(HEADER) and ch == "]":
                names = [n.strip() for n in b[len(HEADER):-1].split(",")]
                out.append(BOLD + "[for " + ", ".join(self.color(n) for n in names) + BOLD + "]" + RESET)
                self.header_buf = None
                self.at_line_start = False
            elif b.startswith(HEADER) and ch != "\n" and len(b) < 60:
                continue
            else:
                out.append(b)
                self.header_buf = None
                self.at_line_start = ch == "\n"
        self.log("".join(out))

    def flush_header(self):
        if self.header_buf:
            held, self.header_buf = self.header_buf, None
            self.log(held)

    def show_message(self, name, text, lane=None):
        self.newline_if_needed()
        self.interrupted = self.live is not None
        body = text.replace("\n", "\n" + " " * (len(name) + 3))
        self.log(f"{self.color(name)}{self.thread_tag(lane)} › {body}\n")

    def show_tool(self, name, detail, tid=None):
        """● sam's PC · uefn_call  call_tool …  [3]   (Ctrl+O or /tool 3 shows it in full)"""
        self.newline_if_needed()
        num = ""
        if tid:
            self.tool_ids.append((tid, name))
            num = f"  [{len(self.tool_ids)}]"
        where, _, what = name.rpartition(" · ")
        label = (f"{DIM}{where} · {RESET}" if where else "") + f"{BOLD}{what}{RESET}"
        self.log(f"  {CLAUDE_COLOR}●{RESET} {label} {DIM}{detail}{num}{RESET}\n")

    def show_tool_result(self, ok, preview, lines=0):
        if ok is None:
            return
        more = f" (+{lines - 1} lines)" if lines and lines > 1 else ""
        color = f"{CSI}38;2;236;119;130m" if ok is False else DIM
        self.log(f"{color}    ⎿  {'✗ ' if ok is False else ''}{preview or '(no output)'}{more}{RESET}\n")

    def expand_tool(self, n=None):
        """Ctrl+O / "/tool N": print a tool call's full input and output, fetched from the room."""
        if not self.tool_ids:
            self.show_sys("no tool calls yet")
            return
        if n is not None and not 1 <= n <= len(self.tool_ids):
            self.show_sys(f"there is no tool line [{n}] (1–{len(self.tool_ids)})")
            return
        n = n or len(self.tool_ids)
        tid, name = self.tool_ids[n - 1]
        conn = self.conn

        def work():
            try:
                t = json.loads(OPENER.open(f"{conn.base}/tool/{urllib.parse.quote(tid)}?t={urllib.parse.quote(conn.token)}",
                                           timeout=20).read())
                self.events.put({"t": "_tool_full", "n": n, "tool": t})
            except Exception as e:  # noqa: BLE001
                self.events.put({"t": "sys", "text": f"tool [{n}] {name}: its full text isn't available ({e})"})
        threading.Thread(target=work, daemon=True).start()

    def show_tool_full(self, n, t):
        self.newline_if_needed()
        self.interrupted = self.live is not None
        status = {True: "ok", False: "error", None: "no result"}[t.get("ok")]
        secs = f" · {t['secs']}s" if t.get("secs") is not None else ""
        bar = f"{CLAUDE_COLOR}│{RESET} "
        self.log(f"{CLAUDE_COLOR}┌ [{n}] {t.get('name', '')}{RESET}{DIM} · {status}{secs}{RESET}\n")

        def block(title, text, limit):
            rows = (text or "").splitlines() or ["(empty)"]
            self.log(f"{bar}{BOLD}{title}{RESET}\n")
            for row in rows[:limit]:
                self.log(f"{bar}{DIM}  {row}{RESET}\n")
            if len(rows) > limit:
                self.log(f"{bar}{DIM}  … {len(rows) - limit} more lines (the browser shows all: click the tool line){RESET}\n")
        block("input", t.get("input"), 40)
        out = t.get("output")
        block("error" if t.get("ok") is False else "output",
              out if out is not None else "(stopped before it finished)", 60)
        self.log(f"{CLAUDE_COLOR}└{RESET}\n")

    def show_image(self, ev):
        """A picture posted in the room, drawn with half blocks: each character is two pixels."""
        self.newline_if_needed()
        self.interrupted = self.live is not None
        who = ev.get("from", "?")
        what = f" · {ev['caption'][:120]}" if ev.get("caption") else ""
        self.log("\n" + f"{self.color(who) if who != 'Claude' else CLAUDE_COLOR + BOLD + 'Claude' + RESET} {DIM}🖼 "
                 f"{ev.get('name', '')}{what}{RESET}\n")
        p = ev.get("preview")
        if p and p.get("px"):
            w, h, px = p["w"], p["h"], bytes.fromhex(p["px"])
            rgb = lambda x, y: ";".join(str(v) for v in px[(y * w + x) * 3:(y * w + x) * 3 + 3])  # noqa: E731
            for y in range(0, h - 1, 2):
                self.log("  " + "".join(f"{CSI}38;2;{rgb(x, y)}m{CSI}48;2;{rgb(x, y + 1)}m▀" for x in range(w)) + RESET + "\n")
        self.log(f"{DIM}  full size: Files tab in the browser, or /get {ev.get('file_id', '')}{RESET}\n")

    def show_pin(self, pin, replay):
        """A pin under its message (history), or a line saying what got pinned where (live)."""
        self.newline_if_needed()
        self.interrupted = self.live is not None
        who = "Claude" if pin["by"] == "Claude" else pin["by"]
        name = (CLAUDE_COLOR + BOLD + "Claude" + RESET) if who == "Claude" else self.color(who)
        if not replay:
            target = " ".join(re.sub(r"\[for [^\]]*\]", "", self.snips.get(pin["target"], "a message")).split())
            self.log(f"{DIM}📌 {RESET}{name}{DIM} pinned onto “{target[:70]}{'…' if len(target) > 70 else ''}”{RESET}\n")
        kind, pad = pin["kind"], "   "
        if kind == "note":
            self.log(f"{pad}{CSI}38;2;241;227;180m📝 {pin['text']}{RESET}" + (f"{DIM} · {who}{RESET}" if replay else "") + "\n")
        elif kind == "card":
            card = next((c for c in self.board["cards"] if c["id"] == pin["card"]), None)
            col = card["col"] if card else ""
            title = card["title"] if card else pin.get("title", "a card")
            column = next((c["title"] for c in self.columns() if c["id"] == col), "")
            self.log(f"{pad}{CSI}{COL_RGB.get(col, RGB['dim'])}m●{RESET} {BOLD}{title}{RESET}{DIM} {column} · card"
                     + (f" · {who}" if replay else "") + f"{RESET}\n")
        elif kind == "file":
            self.log(f"{pad}{DIM}{'🖼' if pin.get('image') else '📎'} {pin.get('name', '')} · /get {pin['file']}"
                     + (f" · {who}" if replay else "") + f"{RESET}\n")
            p = pin.get("preview")
            if p and p.get("px"):
                w, h, px = p["w"], p["h"], bytes.fromhex(p["px"])
                rgb = lambda x, y: ";".join(str(v) for v in px[(y * w + x) * 3:(y * w + x) * 3 + 3])  # noqa: E731
                for y in range(0, h - 1, 2):
                    self.log(pad + "".join(f"{CSI}38;2;{rgb(x, y)}m{CSI}48;2;{rgb(x, y + 1)}m▀" for x in range(w)) + RESET + "\n")
        elif kind == "sketch":
            self.log(f"{pad}{DIM}✎ sketch" + (f" · {who}" if replay else "") + f"{RESET}\n")
            for line in braille_lines(pin["sketch"]["strokes"], 30, 6):
                self.log(pad + line + "\n")

    def show_sys(self, text):
        self.newline_if_needed()
        self.interrupted = self.live is not None
        self.log(f"{DIM}· {text}{RESET}\n")

    def share_screenshot(self):
        self.show_sys("📸 taking a picture of your screen…")

        def work():
            ok, path = take_screenshot()
            if not ok:
                self.events.put({"t": "sys", "text": f"📸 couldn't take it: {path}"})
                return
            ok, msg = upload_file(self.conn, path)
            try:
                os.remove(path)
            except OSError:
                pass
            if not ok:
                self.events.put({"t": "sys", "text": f"📸 couldn't share it: {msg}"})
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def poll_line(ev, short=False):
        opts, votes = ev.get("opts") or [], ev.get("votes") or {}
        counts = [sum(1 for v in votes.values() if v == i) for i in range(len(opts))]
        line = "  ".join(f"{i + 1}) {o} ({c})" for i, (o, c) in enumerate(zip(opts, counts)))
        if short:
            return f"{ev.get('q', '')}: {line}"
        return f"📊 {ev.get('by', '')} asks: {ev.get('q', '')}  ·  {line}  ·  " + ("closed" if ev.get("closed") else "/vote N")

    def my_threads(self):
        return [t for t in self.threads if t.get("owner") == self.me and not t.get("closed")]

    def thread_name(self, lane):
        if "/" not in str(lane or ""):
            return ""
        return next((t["name"] for t in self.threads if t["id"] == lane), lane.split("/", 1)[1])

    def thread_tag(self, lane):
        name = self.thread_name(lane)
        return f" {CLAUDE_COLOR}#{name}{RESET}" if name else ""

    def claude_start(self, lane=None, continued=False):
        self.newline_if_needed()
        to = f" {DIM}→{RESET} {self.color(owner(lane))}{self.thread_tag(lane)}" if lane else ""
        more = f" {DIM}(continued){RESET}" if continued else ""
        self.log(f"\n{CLAUDE_COLOR}{BOLD}● Claude{RESET}{to}{more}\n")

    def claude_end(self):
        self.newline_if_needed()
        self.log("\n")

    def new_version(self, version):
        self.latest = max(self.latest, version)
        if self.latest > VERSION and self.latest > self.announced:
            self.announced = self.latest
            news = " ".join(self.notes[str(v)] for v in range(VERSION + 1, self.latest + 1) if str(v) in self.notes)
            self.add_notice(f"⬆ chat.py v{self.latest} is ready", (news + " " if news else "") +
                            "It updates itself when you're idle (Esc postpones), or type /update now.", "update")

    def add_notice(self, title, body="", kind="info"):
        """A notification: a line in the chat, a highlight in the status line, a bell for important ones."""
        icon = {"update": "⬆", "lock": "🔒", "info": "🛈", "approval": "⚠", "mention": "@"}.get(kind, "🛈")
        icon = "" if title.startswith(("⬆", "🔒")) else icon + " "
        self.notices = (self.notices + [(time.time(), f"{icon}{title}" + (f": {body}" if body else ""), kind)])[-30:]
        style = {"update": "1;30;43", "lock": "1;97;41"}.get(kind, "1;30;47")
        self.notice = (f" {icon}{title} ", style, time.time() + (25 if kind in ("update", "lock") else 10))
        self.show_sys(f"{icon}{title}" + (f" · {body}" if body else ""))
        if kind in ("lock", "update"):
            self.w("\a")

    # ---- full-screen views: task board and whiteboard ------------------------
    def set_view(self, view):
        if view == self.view:
            return
        if self.pen:
            self.finish_pen()
        self.view, self.prompt, self.note_open = view, None, False
        if view != "board":
            self.pin_pick = False
        if view != "draw":
            self.sketch = None
        self.mouse(view == "draw")
        if view == "chat":
            self.w(CSI + "?25h")
            self.resize(force=True)
            return
        self.w(CSI + "r" + CSI + "?25l")
        if view == "board" and not self.board_loaded:
            self.fetch("board")
        if view == "files":
            self.fetch("files")
        if view == "draw":
            if not self.wb_loaded:
                self.fetch("wb")
            self.dview = None
        self.reveal(self.render_view())

    def fetch(self, what):
        def work():
            try:
                url = f"{self.conn.base}/{what}?t={urllib.parse.quote(self.conn.token)}"
                self.events.put({"t": "_loaded", "what": what, "data": json.loads(OPENER.open(url, timeout=20).read())})
            except Exception as e:  # noqa: BLE001
                self.events.put({"t": "sys", "text": f"couldn't load the {what}: {e}"})
        threading.Thread(target=work, daemon=True).start()

    def mouse(self, on):
        """Mouse reporting for drawing (SGR mode); on Windows the console must also send VT input."""
        if on:
            self.w("\x1b[?1002h\x1b[?1006h")
        else:
            self.w("\x1b[?1002l\x1b[?1006l")
        if os.name != "nt":
            return
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        if on and self.vt_mode is None:
            mode = ctypes.c_uint32()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            self.vt_mode = mode.value
            kernel32.SetConsoleMode(handle, mode.value | 0x0200)   # ENABLE_VIRTUAL_TERMINAL_INPUT
        elif not on and self.vt_mode is not None:
            kernel32.SetConsoleMode(handle, self.vt_mode)
            self.vt_mode = None

    def reveal(self, grid):
        """Opening effect: the view sweeps in diagonally behind a glowing edge."""
        self.w(CSI + "2J")
        frames = 10
        for f in range(1, frames + 1):
            p = f / frames
            out = []
            for r in range(grid.rows):
                vis = int(grid.cols * min(1.0, max(0.0, p * 1.7 - 0.7 * r / grid.rows)))
                edge = f"{CSI}{RGB['orange']}m▐{RESET}" if 0 < vis < grid.cols - 1 else ""
                out.append(f"{CSI}{r + 1};1H" + grid.row(r, vis) + edge + CSI + "K")
            self.w("".join(out))
            sys.stdout.flush()
            time.sleep(0.014)
        self.draw_view(grid)

    def draw_view(self, grid=None):
        grid = grid or self.render_view()
        out = [CSI + "?25l"] + [f"{CSI}{r + 1};1H" + grid.row(r) for r in range(grid.rows)]
        if self.prompt:   # show the cursor where you type
            out.append(f"{CSI}{grid.rows};{min(grid.cols, len(self.prompt['label']) + 4 + len(self.prompt['buf']))}H"
                       + CSI + "?25h")
        self.w("".join(out))
        sys.stdout.flush()

    def render_view(self):
        cols, rows = self.size
        grid = Grid(cols, rows)
        if self.view == "board":
            self.render_board(grid)
        elif self.view == "files":
            self.render_files(grid)
        else:
            self.render_draw(grid)
        self.render_footer(grid)
        return grid

    def render_footer(self, grid):
        r = grid.rows - 1
        if self.prompt:
            grid.put(r, 1, self.prompt["label"] + ": ", "1;" + RGB["orange"])
            grid.put(r, len(self.prompt["label"]) + 3, self.prompt["buf"], RGB["fg"])
            return
        text, when = self.flash
        if text and time.time() - when < 5:
            grid.put(r, 1, text[: grid.cols - 2], RGB["orange"])
        else:
            grid.put(r, 1, ("─ " + self.status_text())[: grid.cols - 2], RGB["dim"])

    def person_style(self, name):
        if name in ("claude", "Claude"):
            return RGB["orange"]
        return COLOR.get(self.colors.get(name, ""), "37")

    # ---- board
    def boards(self):
        return self.board.get("boards") or [{"id": "main", "name": "Tasks", "columns": self.board.get("columns") or
                                             [{"id": i, "title": t} for i, t in (("ideas", "Ideas"), ("backlog", "Backlog"),
                                                                                 ("todo", "To do"), ("doing", "Doing"), ("done", "Done"))]}]

    def cur_board(self):
        boards = self.boards()
        self.bidx = max(0, min(getattr(self, "bidx", 0), len(boards) - 1))
        return boards[self.bidx]

    def columns(self):
        return self.cur_board()["columns"]

    def col_cards(self, col_id):
        bid = self.cur_board()["id"]
        return sorted((c for c in self.board["cards"] if c["col"] == col_id and c.get("board", "main") == bid),
                      key=lambda c: c["order"])

    def selected_card(self):
        cards = self.col_cards(self.columns()[self.bcol]["id"])
        card = next((c for c in cards if c["id"] == self.bsel), None)
        if card is None and cards:
            card = cards[0]
            self.bsel = card["id"]
        return card

    def render_board(self, g):
        cols, rows = g.cols, g.rows
        hints = "←→↑↓ pick · < > move · a add · e edit · n note · m mine · d delete · Tab files · Esc chat"
        if self.pin_pick:
            hints = "pick a card with the arrows · Enter pins it onto Claude's last answer · Esc cancels"
        boards = self.boards()
        title = "📌 Pin a card" if self.pin_pick else "✻ " + self.cur_board()["name"]
        g.put(0, 1, title, "1;" + RGB["orange"])
        if len(boards) > 1 and not self.pin_pick:
            g.put(0, 3 + len(title), f"{self.bidx + 1}/{len(boards)} · [ ] boards", RGB["dim"])
        g.put(0, max(10, cols - len(hints) - 1), hints[: max(0, cols - 11)], RGB["dim"])
        columns = self.columns()
        self.bcol = min(self.bcol, len(columns) - 1)
        gap, n = 2, len(columns)
        width = (cols - 2 - gap * (n - 1)) // n
        single = width < 20
        shown = [self.bcol] if single else range(n)
        sel = self.selected_card()
        for i in shown:
            col = columns[i]
            w = cols - 4 if single else width
            x = 2 if single else 1 + i * (width + gap)
            cards = self.col_cards(col["id"])
            here = i == self.bcol
            g.put(2, x, "●", COL_RGB.get(col["id"], RGB["fg"]))
            g.put(2, x + 2, col["title"], "1" if here else RGB["fg"])
            g.put(2, x + 3 + len(col["title"]), str(len(cards)), RGB["dim"])
            if single:
                g.put(2, cols - 12, "◀ ▶ columns", RGB["dim"])
            g.put(3, x, ("━" if here else "─") * w, COL_RGB.get(col["id"]) if here else RGB["line"])
            y, bottom = 4, rows - 2
            heights = [self.card_height(c, w) for c in cards]
            start = 0
            if here and sel in cards:
                k = cards.index(sel)
                while start < k and sum(heights[start:k + 1]) > bottom - y:
                    start += 1
            if start:
                g.put(y, x + w - 10, f"↑ {start} more", RGB["dim"])
                y += 1
            for k in range(start, len(cards)):
                if y + heights[k] > bottom:
                    g.put(bottom - 1, x + w - 10, f"↓ {len(cards) - k} more", RGB["dim"])
                    break
                y += self.draw_card(g, y, x, w, cards[k], here and cards[k] is sel, col["id"] == "done")
            if not cards:
                g.put(y, x + 1, "empty · a adds a card" if here else "empty", RGB["dim"])
        if self.note_open and sel:
            self.draw_note(g, sel)

    def card_lines(self, card, w):
        lines = textwrap.wrap(card["title"], max(4, w - 4)) or [""]
        if len(lines) > 3:
            lines = lines[:3]
            lines[2] = lines[2][: w - 5] + "…"
        return lines

    def card_height(self, card, w):
        return len(self.card_lines(card, w)) + 3

    def draw_card(self, g, y, x, w, card, selected, done):
        border = "1;" + RGB["orange"] if selected else RGB["line"]
        edge = LABEL_RGB.get(card.get("color")) or border
        lines = self.card_lines(card, w)
        g.put(y, x, "╭" + "─" * (w - 2) + "╮", border)
        for i, line in enumerate(lines + [""]):
            g.put(y + 1 + i, x, "┃" if card.get("color") else "│", edge)
            g.put(y + 1 + i, x + w - 1, "│", border)
            if i < len(lines):
                g.put(y + 1 + i, x + 2, line, "1" if selected else (RGB["dim"] if done else RGB["fg"]))
        meta = y + 1 + len(lines)
        key = f"#{card.get('num', '?')}"
        mark = {"highest": ("⇈", "38;2;236;119;130"), "high": ("↑", "38;2;240;160;98"), "medium": ("=", "38;2;233;196;126"),
                "low": ("↓", "38;2;108;182;242"), "lowest": ("⇊", "38;2;108;182;242")}.get(card.get("priority", ""))
        if mark:
            g.put(meta, x + 2, mark[0], "1;" + mark[1])
            key = " " + key
        g.put(meta, x + (4 if mark else 2), key.strip(), RGB["dim"])
        if card.get("note"):
            g.put(meta, x + 3 + len(key), "≡", RGB["dim"])
        if card.get("who"):
            g.put(meta, x + 5 + len(key), "● " + ("Claude" if card["who"] == "claude" else card["who"]), self.person_style(card["who"]))
        age = ago(card["ts"])
        g.put(meta, x + w - 2 - len(age), age, RGB["dim"])
        g.put(meta + 1, x, "╰" + "─" * (w - 2) + "╯", border)
        return len(lines) + 3

    def draw_note(self, g, card):
        w = min(g.cols - 8, 70)
        body = textwrap.wrap(card.get("note") or "(no note · n writes one)", w - 4)[: g.rows - 10]
        x, y, h = (g.cols - w) // 2, 3, len(body) + 6
        style = RGB["orange"]
        for r in range(h):
            g.put(y + r, x, " " * w)
        g.put(y, x, "╭" + "─" * (w - 2) + "╮", style)
        for r in range(1, h - 1):
            g.put(y + r, x, "│", style)
            g.put(y + r, x + w - 1, "│", style)
        g.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", style)
        g.put(y + 1, x + 2, card["title"][: w - 4], "1")
        who = "Claude" if card.get("who") == "claude" else card.get("who") or "nobody yet"
        g.put(y + 2, x + 2, f"for {who} · added by {card.get('by', '?')} {ago(card['ts'])} ago"[: w - 4], RGB["dim"])
        for i, line in enumerate(body):
            g.put(y + 4 + i, x + 2, line, RGB["fg"])
        g.put(y + h - 1, x + w - 22, " Enter/Esc close ", RGB["dim"])

    def ask(self, label, done, initial=""):
        self.prompt = {"label": label, "buf": initial, "done": done}

    def board_key(self, ch):
        columns = self.columns()
        col = columns[self.bcol]["id"]
        cards = self.col_cards(col)
        card = self.selected_card()
        post = lambda op: self.conn.post("/board", op)  # noqa: E731
        if self.note_open:
            if ch == "n" and card:
                self.note_open = False
                self.ask("Note", lambda t: post({"op": "update", "id": card["id"], "note": t}), card.get("note", ""))
            elif ch in ("\r", "\x1b", "q"):
                self.note_open = False
            return
        if ch in ("[", "]") and len(self.boards()) > 1:   # switch boards
            self.bidx = (self.bidx + (1 if ch == "]" else -1)) % len(self.boards())
            self.bcol, self.bsel = 0, None
            return
        if ch in ("left", "h"):
            self.bcol, self.bsel = (self.bcol - 1) % len(columns), None
        elif ch in ("right", "l"):
            self.bcol, self.bsel = (self.bcol + 1) % len(columns), None
        elif ch in ("up", "k", "down", "j") and cards and card:
            i = cards.index(card) + (-1 if ch in ("up", "k") else 1)
            self.bsel = cards[max(0, min(len(cards) - 1, i))]["id"]
        elif ch in ("<", ",", ">", ".", "1", "2", "3", "4") and card:
            target = {"<": self.bcol - 1, ",": self.bcol - 1, ">": self.bcol + 1, ".": self.bcol + 1}.get(ch)
            target = int(ch) - 1 if target is None else target
            if 0 <= target < len(columns) and target != self.bcol:
                card["col"], card["order"] = columns[target]["id"], 1e9   # show it there at once
                post({"op": "move", "id": card["id"], "col": columns[target]["id"], "before": None, "board": self.cur_board()["id"]})
                self.bcol = target
        elif ch in ("a", "+", "n") and not (ch == "n" and card):
            self.ask(f"New card in {columns[self.bcol]['title']}",
                     lambda t: post({"op": "add", "title": t, "col": col, "board": self.cur_board()["id"]}))
        elif ch == "n" and card:
            self.ask("Note", lambda t: post({"op": "update", "id": card["id"], "note": t}), card.get("note", ""))
        elif ch == "e" and card:
            self.ask("Title", lambda t: t.strip() and post({"op": "update", "id": card["id"], "title": t}), card["title"])
        elif ch == "m" and card:
            post({"op": "update", "id": card["id"], "who": "" if card.get("who") == self.me else self.me})
        elif ch in ("d", "delete") and card:
            self.ask(f"Delete “{card['title'][:40]}”? y/n",
                     lambda t: post({"op": "delete", "id": card["id"]}) if t.strip().lower() in ("y", "yes", "д", "да") else None)
        elif ch == "\r" and card and self.pin_pick:
            post_pin = {"op": "add", "target": "last", "kind": "card", "card": card["id"]}
            self.conn.post("/pin", post_pin)
            self.set_view("chat")
        elif ch == "\r" and card:
            self.note_open = True

    # ---- files
    def render_files(self, g):
        cols, rows = g.cols, g.rows
        hints = "↑↓ pick · Enter download · s send a file · p paste · d delete · Tab whiteboard · Esc chat"
        g.put(0, 1, "📎 Files", "1;" + RGB["orange"])
        g.put(0, max(12, cols - len(hints) - 1), hints[: max(0, cols - 13)], RGB["dim"])
        files = self.files
        total = sum(f["size"] for f in files)
        g.put(2, 2, f"{len(files)} shared · {human(total)}" if files else "nothing shared yet · s sends a file, p pastes a copied picture",
              RGB["dim"])
        self.fsel = max(0, min(self.fsel, len(files) - 1))
        sel = files[self.fsel] if files else None
        preview_w = 0
        if sel and sel["name"].lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and cols >= 90:
            preview_w = min(58, cols // 2 - 4)
        list_w = cols - 4 - (preview_w + 3 if preview_w else 0)
        top, room_rows = 4, rows - 6
        start = max(0, self.fsel - room_rows + 1)
        for i, f in enumerate(files[start:start + room_rows]):
            idx, y = start + i, top + i
            here = idx == self.fsel
            pic = f["name"].lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            meta = f"{human(f['size']):>8}  {f['by']:<8} {ago(f['ts']):>4}"
            name = f["name"][: max(8, list_w - len(meta) - 6)]
            style = "1;" + RGB["orange"] if here else RGB["fg"]
            g.put(y, 2, ("❯ " if here else "  ") + ("🖼 " if pic else "· "), style)
            g.put(y, 6, name, "1" if here else RGB["fg"])
            g.put(y, 2 + list_w - len(meta), meta, RGB["dim"])
        if preview_w and sel:
            p = self.previews.get(sel["id"])
            if sel["id"] not in self.previews:
                self.previews[sel["id"]] = None
                self.fetch_preview(sel["id"])
            x0 = cols - preview_w - 2
            g.put(top - 1, x0, sel["name"][:preview_w], "1")
            if p and p.get("px"):
                w, h, px = p["w"], p["h"], bytes.fromhex(p["px"])
                for yy in range(0, min(h - 1, (rows - top - 3) * 2), 2):
                    for xx in range(min(w, preview_w)):
                        a, b = (yy * w + xx) * 3, ((yy + 1) * w + xx) * 3
                        g.put(top + yy // 2, x0 + xx, "▀", f"38;2;{px[a]};{px[a + 1]};{px[a + 2]};48;2;{px[b]};{px[b + 1]};{px[b + 2]}")
            else:
                g.put(top + 1, x0, "loading preview…", RGB["dim"])

    def fetch_preview(self, file_id):
        def work():
            try:
                url = f"{self.conn.base}/preview/{file_id}?t={urllib.parse.quote(self.conn.token)}"
                self.events.put({"t": "_preview", "id": file_id, "data": json.loads(OPENER.open(url, timeout=20).read())})
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=work, daemon=True).start()

    def files_key(self, ch):
        files, conn = self.files, self.conn
        sel = files[self.fsel] if files else None
        if ch in ("up", "k"):
            self.fsel = max(0, self.fsel - 1)
        elif ch in ("down", "j"):
            self.fsel = min(len(files) - 1, self.fsel + 1)
        elif ch == "\r" and sel:
            self.flash = (f"downloading {sel['name']} …", time.time())

            def work():
                try:
                    path, meta = download_shared(conn, sel["id"])
                    self.events.put({"t": "sys", "text": f"✓ saved {meta['name']} to {path}"})
                    self.flash = (f"✓ saved to {path}", time.time())
                except Exception as e:  # noqa: BLE001
                    self.flash = (f"couldn't download: {e}", time.time())
            threading.Thread(target=work, daemon=True).start()
        elif ch == "s":
            self.ask("Send which file (full path)", lambda t: t.strip() and self.local_file_command("/send " + t.strip(), conn))
        elif ch == "p":
            self.local_file_command("/paste", conn)
            self.flash = ("pasting what you copied …", time.time())
        elif ch in ("d", "delete") and sel and sel["by"] == self.me:
            self.ask(f"Delete {sel['name'][:40]} for everyone? y/n",
                     lambda t: conn.post("/files_delete", {"id": sel["id"]}) if t.strip().lower() in ("y", "yes", "д", "да") else None)
        elif ch == "r":
            self.fetch("files")

    # ---- whiteboard
    def wb_event(self, ev):
        wb, op, by = self.wb, ev.get("op"), ev.get("by")
        if op == "cursor" and by != self.me:
            wb["cursors"][by] = (ev["x"], ev["y"], time.time())
        elif op == "live" and by != self.me:
            wb["live"][by] = (ev["stroke"], time.time())
        elif op == "add":
            if not any(x["id"] == ev["stroke"]["id"] for x in wb["strokes"]):
                wb["strokes"].append(ev["stroke"])
            if by in wb["live"] and wb["live"][by][0]["id"] == ev["stroke"]["id"]:
                del wb["live"][by]
        elif op == "delete":
            ids = set(ev.get("ids", []))
            wb["strokes"] = [x for x in wb["strokes"] if x["id"] not in ids]
        elif op == "clear":
            wb["strokes"], self.mine = [], []

    def canvas_size(self):
        cols, rows = self.size
        return cols, rows - 2

    def fit_draw(self):
        w, h = self.canvas_size()
        strokes = self.sketch if self.sketch is not None else self.wb["strokes"]
        pts = [p for st in strokes for p in zip(st["pts"][0::2], st["pts"][1::2])]
        if not pts:
            k = 0.25
            self.dview = (-w * 2 / k / 2, -h * 4 / k / 2, k)
        else:
            x1, x2 = min(p[0] for p in pts), max(p[0] for p in pts)
            y1, y2 = min(p[1] for p in pts), max(p[1] for p in pts)
            k = min(w * 2 / max(1, (x2 - x1) * 1.15 + 40), h * 4 / max(1, (y2 - y1) * 1.15 + 40), 1.0)
            k = max(0.02, k)
            self.dview = ((x1 + x2) / 2 - w * 2 / k / 2, (y1 + y2) / 2 - h * 4 / k / 2, k)
        self.cross = (w // 2, h // 2)

    def cross_world(self):
        vx, vy, k = self.dview
        c, r = self.cross
        return vx + (c * 2 + 1) / k, vy + (r * 4 + 2) / k

    def render_draw(self, g):
        if self.dview is None:
            self.fit_draw()
        w, h = self.canvas_size()
        vx, vy, k = self.dview
        dots, color = bytearray(w * h), [""] * (w * h)
        texts = []

        def plot(px, py, style):
            if 0 <= px < w * 2 and 0 <= py < h * 4:
                i = (py >> 2) * w + (px >> 1)
                dots[i] |= BRAILLE[py & 3][px & 1]
                color[i] = style

        def seg(a, b, style):
            c = clip_line((a[0] - vx) * k, (a[1] - vy) * k, (b[0] - vx) * k, (b[1] - vy) * k, w * 2, h * 4)
            if not c:
                return
            x1, y1, x2, y2 = c
            steps = int(max(abs(x2 - x1), abs(y2 - y1))) + 1
            for t in range(steps + 1):
                plot(int(round(x1 + (x2 - x1) * t / steps)), int(round(y1 + (y2 - y1) * t / steps)), style)

        now = time.time()
        live = [st for st, t in self.wb["live"].values() if now - t < 5]
        shown = self.sketch if self.sketch is not None else self.wb["strokes"] + live
        for st in shown + ([self.pen] if self.pen else []):
            style, p = hex_rgb(st.get("color")), st["pts"]
            pts = list(zip(p[0::2], p[1::2]))
            if st["type"] == "pen":
                for a, b in zip(pts, pts[1:] or pts):
                    seg(a, b, style)
            elif st["type"] == "rect" and len(pts) == 2:
                (x1, y1), (x2, y2) = pts
                for a, b in (((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))):
                    seg(a, b, style)
            elif st["type"] == "arrow" and len(pts) == 2:
                (x1, y1), (x2, y2) = pts
                seg((x1, y1), (x2, y2), style)
                import math
                ang, head = math.atan2(y2 - y1, x2 - x1), 10 + st.get("size", 4) * 2.2
                for d in (-0.45, 0.45):
                    seg((x2, y2), (x2 - head * math.cos(ang + d), y2 - head * math.sin(ang + d)), style)
            elif st["type"] == "text":
                texts.append((pts[0], st.get("text", ""), style))
        for i, bits in enumerate(dots):
            if bits:
                g.put(1 + i // w, i % w, chr(0x2800 + bits), color[i])
        for (x, y), text, style in texts:
            c, r = int((x - vx) * k / 2), int((y - vy) * k / 4)
            for n, line in enumerate(text.split("\n")):
                if 0 <= r + n < h:
                    g.put(1 + r + n, c, line, "1;" + style)
        for name, (x, y, t) in list(self.wb["cursors"].items()):
            if now - t > 4:
                del self.wb["cursors"][name]
                continue
            c, r = int((x - vx) * k / 2), int((y - vy) * k / 4)
            if 0 <= r < h:
                g.put(1 + r, c, "◆ " + name, "1;" + self.person_style(name))
        if self.cross:
            c, r = self.cross
            g.put(1 + r, c, "✛" if not self.pen else "●", "1;" + hex_rgb(PEN_COLORS[self.pen_color]))
        pen = "drawing · Space stops" if self.pen else "Space draws · mouse drags draw"
        g.put(0, 1, "✎ Whiteboard", "1;" + RGB["orange"])
        g.put(0, 15, "●", hex_rgb(PEN_COLORS[self.pen_color]))
        hints = f"{pen} · arrows move · c color · t text · u undo · +/- zoom · f fit · Tab chat · Esc back"
        if self.sketch is not None:
            g.put(0, 1, "📌 Sketch   ", "1;" + RGB["orange"])
            hints = f"{pen} · arrows move · c color · u undo · Enter pins it onto Claude's last answer · Esc cancels"
        g.put(0, 17, hints[: g.cols - 18], RGB["dim"])

    def draw_key(self, ch):
        w, h = self.canvas_size()
        if self.dview is None:
            self.fit_draw()
        if isinstance(ch, tuple):
            return self.draw_mouse(*ch[1])
        vx, vy, k = self.dview
        moves = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
        if ch in moves:
            c, r = self.cross
            c, r = c + moves[ch][0] * 2, r + moves[ch][1]
            if not 0 <= c < w or not 0 <= r < h:   # past the edge: slide the board instead
                vx += (moves[ch][0] * w // 3 * 2) / k
                vy += (moves[ch][1] * h // 3 * 4) / k
                self.dview = (vx, vy, k)
                c, r = max(0, min(w - 1, c - moves[ch][0] * (w // 3))), max(0, min(h - 1, r - moves[ch][1] * (h // 3)))
            self.cross = (max(0, min(w - 1, c)), max(0, min(h - 1, r)))
            self.cross_moved()
        elif ch == " ":
            if self.pen:
                self.finish_pen()
            else:
                x, y = self.cross_world()
                self.pen = {"id": f"{self.me}-{int(time.time() * 1000):x}", "type": "pen",
                            "color": PEN_COLORS[self.pen_color], "size": 4, "pts": [round(x, 1), round(y, 1)]}
        elif ch == "c":
            self.pen_color = (self.pen_color + 1) % len(PEN_COLORS)
            if self.pen:
                self.pen["color"] = PEN_COLORS[self.pen_color]
        elif ch in ("+", "=", "-", "_"):
            cx, cy = self.cross_world()
            k2 = max(0.02, min(4.0, k * (1.4 if ch in ("+", "=") else 1 / 1.4)))
            c, r = self.cross
            self.dview = (cx - (c * 2 + 1) / k2, cy - (r * 4 + 2) / k2, k2)
        elif ch == "f":
            self.fit_draw()
        elif ch == "t":
            x, y = self.cross_world()
            color = PEN_COLORS[self.pen_color]
            self.ask("Text", lambda t: t.strip() and self.add_stroke(
                {"type": "text", "color": color, "size": 5, "pts": [round(x, 1), round(y, 1)], "text": t}))
        elif ch == "\r" and self.sketch is not None:
            if self.pen:
                self.finish_pen()
            if self.sketch:
                self.conn.post("/pin", {"op": "add", "target": "last", "kind": "sketch", "strokes": self.sketch})
            self.set_view("chat")
        elif ch == "u" and self.sketch is not None:
            if self.sketch:
                self.sketch.pop()
        elif ch == "u":
            while self.mine:
                sid = self.mine.pop()
                if any(x["id"] == sid for x in self.wb["strokes"]):
                    self.wb["strokes"] = [x for x in self.wb["strokes"] if x["id"] != sid]
                    self.conn.post("/wb", {"op": "delete", "ids": [sid]})
                    break

    def draw_mouse(self, button, x, y, pressed):
        w, h = self.canvas_size()
        c, r = max(0, min(w - 1, x - 1)), max(0, min(h - 1, y - 2))
        self.cross = (c, r)
        if button in (64, 65):   # wheel zooms
            return self.draw_key("+" if button == 64 else "-")
        if button & 3 == 0 and pressed and not button & 32:      # left button down: start a stroke
            if self.pen:
                self.finish_pen()
            self.draw_key(" ")
        elif button & 32 and self.pen:                           # dragging
            self.cross_moved()
        elif not pressed and self.pen:                           # released
            self.cross_moved()
            self.finish_pen()

    def cross_moved(self):
        x, y = self.cross_world()
        if self.sketch is not None:
            if self.pen:
                self.pen["pts"] += [round(x, 1), round(y, 1)]
            return
        if self.pen:
            self.pen["pts"] += [round(x, 1), round(y, 1)]
            if time.time() - self.last_cursor_post > 0.12:
                self.conn.post("/wb", {"op": "live", "stroke": self.pen})
        if time.time() - self.last_cursor_post > 0.12:
            self.last_cursor_post = time.time()
            self.conn.post("/wb", {"op": "cursor", "x": x, "y": y})

    def finish_pen(self):
        stroke, self.pen = self.pen, None
        if stroke:
            self.add_stroke(stroke)

    def add_stroke(self, stroke):
        stroke.setdefault("id", f"{self.me}-{int(time.time() * 1000):x}")
        stroke.setdefault("size", 4)
        if self.sketch is not None:   # drawing a sketch for a pin: stays here until Enter
            self.sketch.append(stroke)
            return
        self.wb["strokes"].append(stroke)
        self.mine.append(stroke["id"])
        self.conn.post("/wb", {"op": "add", "stroke": stroke})

    def view_key(self, ch):
        if self.prompt:
            p = self.prompt
            if ch == "\r":
                self.prompt = None
                p["done"](p["buf"])
            elif ch == "\x1b":
                self.prompt = None
            elif ch in ("\x08", "\x7f"):
                p["buf"] = p["buf"][:-1]
            elif isinstance(ch, str) and len(ch) == 1 and ch >= " ":
                p["buf"] += ch
            return
        if ch == "\t":
            self.set_view(VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)])
        elif ch in ("\x1b", "q") and not self.note_open:
            self.set_view("chat")
        elif self.view == "board":
            self.board_key(ch)
        elif self.view == "files":
            self.files_key(ch)
        else:
            self.draw_key(ch)

    # ---- parallel answers ---------------------------------------------------
    # Answers for different people stream at the same time. One streams into the
    # log; the others are held and printed when it ends. Your own answer always
    # streams: it takes over and the other one continues further down.
    def lane_open(self, lane):
        if lane not in self.busy:
            self.busy[lane] = {"since": time.time(), "note": ""}
        if lane == self.live or lane in self.held:
            return
        if self.live is None:
            self.live = lane
            self.claude_start(lane)
        elif owner(lane) == self.me and owner(self.live) != self.me:
            parked, self.live = self.live, lane
            self.newline_if_needed()
            self.log(f"{DIM}  … continues below when done{RESET}\n")
            self.held[parked] = [("continued",)]
            self.lane_order.insert(0, parked)
            self.claude_start(lane)
        else:
            self.held[lane] = []
            self.lane_order.append(lane)

    def lane_out(self, lane, item):
        self.lane_open(lane)
        if lane == self.live:
            if self.interrupted:   # say whose answer this is again
                self.interrupted = False
                self.claude_start(lane, continued=True)
            self.lane_print(item)
        else:
            self.held[lane].append(item)

    def lane_print(self, item):
        if item[0] == "text":
            self.stream(item[1])
        elif item[0] == "tool":
            self.show_tool(item[1], item[2], item[3] if len(item) > 3 else None)
        elif item[0] == "tool_done":
            self.newline_if_needed()
            self.show_tool_result(item[1], item[2], item[3])

    def lane_done(self, lane):
        self.busy.pop(lane, None)
        if lane == self.live:
            self.claude_end()
            self.live, self.interrupted = None, False
            self.next_lane()
        elif lane in self.held:
            self.held[lane].append(("end",))

    def next_lane(self):
        """The streaming answer ended: print the held ones; one still running keeps streaming."""
        while self.lane_order:
            lane = self.lane_order.pop(0)
            items = self.held.pop(lane)
            continued = bool(items) and items[0][0] == "continued"
            self.claude_start(lane, continued)
            for item in items:
                self.lane_print(item)
            if not (items and items[-1][0] == "end"):
                self.live = lane
                return
            self.claude_end()

    # ---- events -----------------------------------------------------------
    def handle(self, ev):
        t = ev.get("t")
        if t == "hello":
            self.connected = True
            self.notes.update(ev.get("notes") or {})
            self.locks = ev.get("locks") or []
            self.new_version(ev.get("chat_version", 0))
            self.me, self.colors, self.mode = ev["you"], ev["colors"], ev["mode"]
            st = ev.get("settings") or {}
            self.model = (st.get("model_label") or "default model") + (f" · {st['effort']}" if st.get("effort") else "") if st else ev.get("model", "")
            self.busy = {n: {"since": time.time() - secs, "note": ""} for n, secs in (ev.get("busy_secs") or {}).items()}
            self.live, self.held, self.lane_order, self.tool_ids = None, {}, [], []
            self.threads = ev.get("threads") or []
            self.usage = ev.get("usage")
            if self.thread not in [t["id"] for t in self.my_threads()]:
                self.thread = ""
            set_active_lanes(ev.get("busy_secs") or {})
            report_lease()   # the room forgets it when it restarts
            self.mute_approval = bool((ev.get("settings") or {}).get("mute_approval_sound"))
            ZONES[:] = ev.get("zones") or []
            self.log_text, self.at_line_start, self.header_buf = "", True, None
            self.resize(force=True)
        elif t == "_lost":
            self.connected = False
            self.last_error = ev.get("err", "")[:120]
        elif t == "msg":
            self.show_message(ev["from"], ev["text"] + "".join(f"  📎 {f.get('name', '')}" for f in ev.get("files") or []), ev.get("lane"))
            self.snips[ev.get("id")] = f"{ev['from']}: {ev['text']}"
        elif t == "batch":
            set_active_lanes([ev["lane"]], add=True)
            self.busy[ev["lane"]] = {"since": time.time(), "note": ""}
            self.lane_aid[ev["lane"]], self.answer_text[ev["lane"]] = ev.get("aid"), ""
            self.lane_open(ev["lane"])
        elif t == "delta":
            self.lane_out(ev["lane"], ("text", ev["text"]))
            self.answer_text[ev["lane"]] = self.answer_text.get(ev["lane"], "") + ev["text"]
            self.snips[self.lane_aid.get(ev["lane"])] = "Claude: " + self.answer_text[ev["lane"]]
            self.busy[ev["lane"]]["note"] = ""
        elif t == "tool":
            self.lane_out(ev["lane"], ("tool", ev["name"], ev["detail"], ev.get("tid")))
            self.busy[ev["lane"]]["note"] = ev["name"]
        elif t == "tool_done":
            self.lane_out(ev["lane"], ("tool_done", ev.get("ok"), ev.get("preview", ""), ev.get("lines", 0)))
            self.busy[ev["lane"]]["note"] = ""
        elif t == "_tool_full":
            self.show_tool_full(ev["n"], ev["tool"])
        elif t == "threads":
            self.threads = ev.get("threads") or []
            if ev.get("created") and ev.get("by") == self.me:
                self.thread = ev["created"]
                self.show_sys(f"you're in the thread “{self.thread_name(self.thread)}” now: it runs at the same time as "
                              "your others · /thread main goes back")
            elif self.thread and self.thread not in [t["id"] for t in self.my_threads()]:
                self.thread = ""
                self.show_sys("that thread is closed: back in Main")
        elif t == "status":
            if ev.get("lane") in self.busy:
                self.busy[ev["lane"]]["note"] = ev["text"]
        elif t == "done":
            set_active_lanes([ev["lane"]], add=False)   # its files are free for your other threads again
            threading.Thread(target=lease_turn_ended, args=(ev["lane"],), daemon=True).start()
            started = self.busy.get(ev["lane"], {}).get("since", time.time())
            self.lane_done(ev["lane"])
            if owner(ev["lane"]) == self.me and time.time() - started > 20:
                self.w("\a")
        elif t == "answer":
            self.snips[ev.get("id")] = "Claude: " + "".join(p[1] for p in ev["parts"] if p[0] == "text")
            self.claude_start(ev.get("lane"))
            for part in ev["parts"]:
                if part[0] == "text":
                    self.stream(part[1])
                else:
                    self.show_tool(part[1], part[2], part[3] if len(part) > 3 else None)
                    if len(part) > 6:
                        self.show_tool_result(part[4], part[6])
            self.claude_end()
        elif t == "sys":
            self.show_sys(ev["text"])
        elif t == "presence":
            self.online = ev["online"]
        elif t == "typing":
            self.typing[ev["from"]] = time.time()
        elif t == "model":
            self.model = ev["model"]
        elif t == "update_available":
            self.notes.update(ev.get("notes") or {})
            self.new_version(ev.get("chat", 0))
        elif t == "_preview":
            self.previews[ev["id"]] = ev["data"]
        elif t == "_loaded":
            if ev["what"] == "files":
                self.files, self.files_loaded = ev["data"], True
            elif ev["what"] == "board":
                self.board, self.board_loaded = ev["data"], True
            else:
                self.wb["strokes"], self.wb_loaded = ev["data"]["strokes"], True
                if self.view == "draw":
                    self.dview = None
        elif t == "files":
            self.files, self.files_loaded = ev["files"], True
        elif t == "locks":
            self.locks = ev["locks"]
        elif t == "settings":
            self.model = (ev.get("model_label") or "default model") + (f" · {ev['effort']}" if ev.get("effort") else "")
            self.mute_approval = bool(ev.get("mute_approval_sound"))
        elif t == "notify":
            self.add_notice(ev.get("title", ""), ev.get("body", ""), ev.get("kind", "info"))
        elif t == "zones":   # places in the map the threads depend on
            ZONES[:] = ev.get("zones") or []
        elif t == "lease_release":   # Done testing, clicked in your browser
            set_lease(None)
            self.show_sys("✓ playtest freed (Done testing in the browser): your other threads may use UEFN again")
        elif t == "release_locks":   # the "Release my locks" button in your browser
            threading.Thread(target=release_my_locks, args=(self.events,), daemon=True).start()
        elif t == "remote_update":   # the Update button in your browser
            if self.approvals or self.prompt:
                self.show_sys("an update was asked for in your browser: it runs after you answer")
                self.remote_update_pending = True
            else:
                self.want_update = True
        elif t == "board":
            self.board = ev["board"]
            if ev.get("by") != self.me and ev.get("what"):
                self.flash = (("✻ " if ev["by"] == "Claude" else "") + ev["what"], time.time())
        elif t == "timeline":   # someone else added a moment: one line, the page has the rest
            if ev.get("by") != self.me and ev.get("what") and ev.get("op") in ("add", "from_msg", "from_card"):
                self.show_sys("⏱ " + ev["what"] + " · /timeline")
        elif t == "poll":
            self.show_sys(self.poll_line(ev))
        elif t == "poll_update" and not ev.get("closed"):
            self.flash = ("📊 " + self.poll_line(ev, short=True), time.time())
        elif t == "pmsg":   # the people's own chat
            if not ev.get("replay"):
                who = "you" if ev.get("from") == self.me else ev.get("from", "")
                self.show_sys(f"💬 {who}: {ev.get('text') or ''}" + (f" [{ev.get('name')}]" if ev.get("file") else "")
                              + (f" ↪ {ev['ref'].get('who', '')}: “{ev['ref'].get('snip', '')[:120]}”" if ev.get("ref") else "")
                              + (f" {ev['url']}" if ev.get("url") else "") + "  (/p to answer)")
                if ev.get("from") != self.me:
                    self.w("\a")
        elif t == "sync":
            mine = (ev.get("sync") or {}).get(self.me) or {}
            if mine.get("behind"):
                self.flash = (f"⟳ your project copy is {mine['behind']} check-in(s) behind: Sync Latest in UEFN", time.time())
        elif t == "shot":   # /shot typed in this person's browser
            self.share_screenshot()
        elif t == "react":
            if ev.get("by") != self.me and ev.get("added"):
                self.flash = (f"{ev['by']} reacted {ev.get('e', '')}", time.time())
        elif t == "wb":
            self.wb_event(ev)
        elif t == "exec":
            self.exec_request(ev)
        elif t == "remote_answer":
            self.remote_answer(ev)
        elif t == "image":
            self.show_image(ev)
            self.snips[ev.get("id")] = f"picture {ev.get('name', '')}"
        elif t == "usage":
            self.usage = ev.get("usage")
        elif t == "del":   # printed lines can't be taken back: say what went
            what = " ".join(self.snips.pop(ev.get("id"), "").split())
            self.show_sys(f"🗑 {ev.get('by')} deleted a message" + (f" ({what[:80]})" if what else ""))
        elif t == "pin":
            if ev.get("op") == "add":
                self.show_pin(ev["pin"], ev.get("replay"))
        elif t == "_exec_done":
            mark = "✓" if ev["ok"] else "✗"
            first = ev["output"].strip().splitlines()[:1]
            self.show_sys(f"{mark} {ev['summary']}" + (f" → {first[0][:100]}" if first else ""))

    # ---- acting on this PC ------------------------------------------------
    def exec_request(self, ev):
        self.set_view("chat")   # an approval needs your eyes on the chat
        asked = ", ".join(ev.get("asked_by") or []) + (f" #{ev['thread']}" if ev.get("thread") else "")
        self.newline_if_needed()
        self.log(f"{BOLD}{CSI}33m⚠ Claude → your PC{RESET} (turn from {asked}): {summarize(ev['tool'], ev['args'])}\n")
        if ev.get("tool") == "undo_write" and ev.get("by_owner"):   # you clicked ↶ Undo yourself
            self.start_exec(ev)
            return
        asked_by = ev.get("asked_by") or []
        if self.auto and asked_by == [self.me]:
            self.start_exec(ev)
        else:
            if self.auto:
                self.show_sys("auto mode skipped: someone else also wrote in this turn, so you decide")
            ev["_deadline"] = time.time() + ev.get("timeout", 120)
            self.approvals.append(ev)
            if not self.mute_approval:
                self.w("\a")   # beep / flash the taskbar so nobody forgets to answer
            if len(self.approvals) == 1:
                self.approval_since = time.time()

    def expire_approvals(self):
        """Drop prompts the server already gave up on, so a late 'y' can't run them."""
        expired = [ev for ev in self.approvals if time.time() > ev["_deadline"]]
        for ev in expired:
            self.approvals.remove(ev)
            self.show_sys(f"✗ approval timed out, skipped: {summarize(ev['tool'], ev['args'])}")
            self.approval_since = time.time()
        return bool(expired)

    def answer_approval(self, ch):
        if time.time() - self.approval_since < 0.4:   # don't let in-flight typing approve by accident
            return
        if ch not in YES | NO | ALWAYS:
            return
        self.decide(self.approvals[0], ch)

    def remote_answer(self, ev):
        """y / n / a clicked in the browser tab of the same person."""
        for pending in self.approvals:
            if pending["id"] == ev.get("id"):
                self.decide(pending, ev.get("answer", ""), " (from the browser)")
                return

    def decide(self, ev, ch, via=""):
        if ch not in YES | NO | ALWAYS:
            return
        self.approvals.remove(ev)
        self.approval_since = time.time()
        if getattr(self, "remote_update_pending", False) and not self.approvals:
            self.remote_update_pending = False
            self.want_update = True
        if ch in NO:
            self.show_sys("✗ you denied it" + via)
            self.conn.post("/exec_result", {"id": ev["id"], "ok": False, "output": "the user denied this action"})
            return
        if ch in ALWAYS:
            self.auto = True
            self.show_sys(f"auto mode ON{via} (type /auto to turn it off)")
            pending, self.approvals = self.approvals, []
            for other in pending:
                self.start_exec(other)
        self.start_exec(ev)

    def start_exec(self, ev):
        self.conn.post("/exec_started", {"id": ev["id"]})

        def work():
            RUNNING[0] += 1
            try:
                ok, output = run_tool(ev["tool"], ev["args"], self.conn, ev.get("lane") or self.me, ev.get("thread", ""))
            finally:
                RUNNING[0] -= 1
            self.conn.post("/exec_result", {"id": ev["id"], "ok": ok, "output": output})
            self.events.put({"t": "_exec_done", "ok": ok, "output": output,
                             "summary": summarize(ev["tool"], ev["args"])})
        threading.Thread(target=work, daemon=True).start()

    # ---- input --------------------------------------------------------------
    def matches(self):
        """Commands that start with what's typed (or with what was typed before Tab cycling)."""
        base = (self.complete_base if self.complete_base is not None else self.buf).lower()
        if not base.startswith("/") or "\n" in base:
            return []
        return [c for c in COMMANDS if c[0].startswith(base)]

    def tab_complete(self, step=1):
        matches = self.matches()
        if not matches:
            return
        if self.complete_base is None:
            self.complete_base = self.buf
        else:
            self.sel = (self.sel + step) % len(matches)
        self.buf = matches[self.sel % len(matches)][0]
        self.cursor = len(self.buf)

    def key(self, ch, conn, more_waiting):
        if self.view != "chat":
            self.view_key(ch)
            return False
        if isinstance(ch, tuple):   # stray mouse event
            return False
        if self.approvals:
            self.answer_approval(ch)
            return False
        if ch not in ("\t", "up", "down"):
            self.complete_base, self.sel = None, 0
        if ch in ("\r", "\n") and not more_waiting:
            matches = self.matches()
            if matches and self.buf.strip().lower() not in {c for c, _ in COMMANDS}:
                self.buf = matches[self.sel % len(matches)][0]   # Enter on "/ne" runs /new
        if ch in ("\r", "\n"):
            if more_waiting:          # part of a multi-line paste
                self.insert("\n")
            elif self.local_file_command(self.buf.strip(), conn):
                self.buf, self.cursor = "", 0
            elif self.buf.strip().lower() in ("/board", "/draw", "/whiteboard"):
                view = "board" if self.buf.strip().lower() == "/board" else "draw"
                self.buf, self.cursor = "", 0
                self.set_view(view)
            elif self.buf.strip().lower() == "/web":
                import webbrowser
                url = f"{conn.base}/?t={urllib.parse.quote(conn.token)}"
                self.show_sys("opening the room in your browser (task board + whiteboard)" if webbrowser.open(url)
                              else f"open this in your browser: {url}")
                self.buf, self.cursor = "", 0
            elif self.buf.strip().lower() == "/update":
                self.want_update = True
                self.buf, self.cursor = "", 0
            elif self.buf.strip().lower() in ("/sound approvals off", "/sound approvals on"):
                off = self.buf.strip().lower().endswith("off")
                conn.post("/settings", {"mute_approval_sound": off})
                self.mute_approval = off
                self.show_sys("approval prompts are silent now" if off else "approval prompts beep again")
                self.buf, self.cursor = "", 0
            elif self.buf.strip().lower() == "/tested":
                set_lease(None)
                self.show_sys("✓ playtest freed: your other threads may use UEFN again")
                self.buf, self.cursor = "", 0
            elif self.buf.strip().lower() in ("/auto", "/auto on", "/auto off"):
                arg = self.buf.strip().lower()
                self.auto = (not self.auto) if arg == "/auto" else arg.endswith("on")
                self.show_sys("auto mode ON: Claude's actions on your PC run without asking" if self.auto
                              else "auto mode OFF: you approve each action on your PC")
                self.buf, self.cursor = "", 0
            elif self.buf.strip():
                conn.post("/say", {"text": self.buf, "thread": self.thread})
                self.buf, self.cursor = "", 0
        elif ch == "\x0f":   # Ctrl+O: the last tool call in full, like Claude Code
            self.expand_tool()
        elif ch in ("\x08", "\x7f"):
            if self.cursor:
                self.buf = self.buf[: self.cursor - 1] + self.buf[self.cursor:]
                self.cursor -= 1
        elif ch == "\x1b":
            if not self.buf and self.update_at:
                self.update_at, self.update_postponed = None, time.time() + 600
                self.show_sys("update postponed for 10 minutes (/update any time)")
            elif not self.buf and (self.thread or self.me) in self.busy:   # Esc twice stops Claude here
                if time.time() - self.esc_at < 2:
                    self.esc_at = 0
                    conn.post("/say", {"text": "/stop", "thread": self.thread})
                else:
                    self.esc_at = time.time()
                    self.show_sys("press Esc again to stop Claude")
            self.buf, self.cursor = "", 0
        elif ch in ("left", "right", "home", "end", "delete"):
            if ch == "left":
                self.cursor = max(0, self.cursor - 1)
            elif ch == "right":
                self.cursor = min(len(self.buf), self.cursor + 1)
            elif ch == "home":
                self.cursor = 0
            elif ch == "end":
                self.cursor = len(self.buf)
            else:
                self.buf = self.buf[: self.cursor] + self.buf[self.cursor + 1:]
        elif ch == "\t":
            if self.buf:
                self.tab_complete()
            else:
                self.set_view("board")
        elif ch in ("up", "down"):
            matches = self.matches()
            if matches:
                step = -1 if ch == "up" else 1
                if self.complete_base is None:
                    self.sel = (self.sel + step) % len(matches)
                else:
                    self.tab_complete(step)
        elif ch >= " ":
            self.insert(ch)
            return True   # counts as typing
        return False

    def local_file_command(self, text, conn):
        """/send PATH, /files, /get ID: file sharing from the terminal. True = handled."""
        low = text.lower()
        if low == "/shot":
            self.share_screenshot()
            return True
        if low == "/files":
            self.set_view("files")
            return True
        if low == "/locks":
            if not self.locks:
                self.show_sys("🔒 no Lore locks right now")
            for l in self.locks:
                self.show_sys(f"🔒 {l['file']} · {l['owner']}" + (f" (Epic {l['owner_epic']})" if l['owner'] != l['owner_epic'] else "")
                              + (f" · since {l['since']}" if l.get("since") else ""))
            return True
        m = re.fullmatch(r"/thread(?:s|\s+(.+))?", text.strip(), re.I)
        if m and not (m[1] or "").lower().split(" ")[0] in ("new", "rename", "close"):
            mine = self.my_threads()
            pick = (m[1] or "").strip()
            if not pick:
                names = ["Main"] + [t["name"] for t in mine]
                cur = 1 + next((i for i, t in enumerate(mine) if t["id"] == self.thread), -1)
                self.show_sys("your threads: " + " · ".join(f"{i}) {n}{' ◀ here' if i == cur else ''}" for i, n in enumerate(names))
                              + " · /thread N switches · /thread new NAME opens one")
            elif pick.lower() in ("main", "0"):
                self.thread = ""
                self.show_sys("back in your main thread")
            else:
                hit = mine[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(mine) else next(
                    (t for t in mine if t["name"].lower().startswith(pick.lower())), None)
                if hit:
                    self.thread = hit["id"]
                    self.show_sys(f"now in the thread “{hit['name']}”")
                else:
                    self.show_sys(f"no thread “{pick}” · /thread lists yours")
            return True
        m = re.fullmatch(r"/tools?(?:\s+\[?(\d+)\]?)?", low)
        if m:
            self.expand_tool(int(m[1]) if m[1] else None)
            return True
        if low == "/release":
            threading.Thread(target=release_my_locks, args=(self.events,), daemon=True).start()
            return True
        if low == "/notifications":
            for ts, text, _ in self.notices[-12:] or [(0, "no notifications yet", "")]:
                self.show_sys((time.strftime("%H:%M ", time.localtime(ts)) if ts else "") + text)
            return True

        if low == "/files-list":
            def work():
                try:
                    files = json.loads(OPENER.open(f"{conn.base}/files?t={urllib.parse.quote(conn.token)}", timeout=30).read())
                    lines = [f"[{f['id']}] {f['name']} · {f['size'] // 1024} KB · from {f['by']}" for f in files]
                    self.events.put({"t": "sys", "text": "shared files:\n" + "\n".join(lines) if lines else "no shared files yet"})
                except Exception as e:  # noqa: BLE001
                    self.events.put({"t": "sys", "text": f"couldn't list files: {e}"})
        elif low == "/paste":
            def work():
                try:
                    paths = clipboard_files()
                    if not paths:
                        self.events.put({"t": "sys", "text": "nothing to paste: copy a picture or a file first"})
                    for path in paths:
                        ok, msg = upload_file(conn, path)
                        if not ok:
                            self.events.put({"t": "sys", "text": f"not shared: {msg}"})
                except Exception as e:  # noqa: BLE001
                    self.events.put({"t": "sys", "text": f"couldn't paste: {e}"})
        elif low == "/pin" or low.startswith("/pin "):
            arg = text[4:].strip()
            if not arg:
                self.show_sys("pin onto Claude's last answer: /pin a note · /pin card · /pin sketch · /pin file <id>")
            elif arg.lower() == "card":
                self.pin_pick = True
                self.set_view("board")
            elif arg.lower() == "sketch":
                self.set_view("draw")
                self.sketch = []
                self.dview = None
            elif arg.lower().startswith("file "):
                conn.post("/pin", {"op": "add", "target": "last", "kind": "file", "file": arg[5:].strip()})
            else:
                conn.post("/pin", {"op": "add", "target": "last", "kind": "note", "text": arg})
            return True
        elif low.startswith("/send ") or low == "/send":
            path = text[5:].strip().strip('"')
            if not path:
                self.show_sys("usage: /send C:\\path\\to\\file  (shares it with everyone in the room)")
                return True

            def work():
                self.events.put({"t": "sys", "text": f"uploading {os.path.basename(path)} …"})
                try:
                    ok, msg = upload_file(conn, path)
                    if not ok:
                        self.events.put({"t": "sys", "text": f"not shared: {msg}"})
                except Exception as e:  # noqa: BLE001
                    self.events.put({"t": "sys", "text": f"not shared: {e}"})
        elif low.startswith("/get ") or low == "/get":
            file_id = text[4:].strip()
            if not file_id:
                self.show_sys("usage: /get <id>  (ids are in /files)")
                return True

            def work():
                try:
                    path, meta = download_shared(conn, file_id)
                    self.events.put({"t": "sys", "text": f"✓ saved {meta['name']} to {path}"})
                except Exception as e:  # noqa: BLE001
                    self.events.put({"t": "sys", "text": f"couldn't get it: {e}"})
        else:
            return False
        threading.Thread(target=work, daemon=True).start()
        return True

    def insert(self, s):
        self.buf = self.buf[: self.cursor] + s + self.buf[self.cursor:]
        self.cursor += len(s)


def summarize(tool, args):
    if tool == "run_command":
        return "run: " + str(args.get("command", "")).replace("\n", " ⏎ ")[:200]
    if tool == "uefn_call":
        return f"UEFN: {args.get('tool', '')} " + json.dumps(args.get("arguments") or {}, ensure_ascii=False)[:160]
    if tool == "uefn_list_tools":
        return "UEFN: list tools" + (f" ({args['name']})" if args.get("name") else "")
    if tool == "file_get":
        return f"save shared file {args.get('file_id', '')} to {args.get('dest') or 'Downloads'}"
    if tool == "file_put":
        return f"share this file with the room: {args.get('path', '')}"
    if tool == "undo_write":
        return f"undo Claude's change to {args.get('path', '')}"
    if tool == "screenshot":
        return "take a picture of your screen and share it in the room"
    verb = {"read_file": "read", "write_file": "write", "list_dir": "list"}.get(tool, tool)
    return f"{verb}: {args.get('path', '')}"


def resolve(path, base=HOME):
    path = os.path.expandvars(os.path.expanduser(str(path or ".")))
    return path if os.path.isabs(path) else os.path.join(base, path)


class UefnMcp:
    """Client for Epic's UEFN MCP (streamable HTTP) in the editor on this PC, so the room's
    Claude can drive UEFN here without Claude Code being installed."""
    URL = "http://127.0.0.1:8000/mcp"

    def __init__(self):
        self.session, self.next_id = None, 0

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.URL, data=json.dumps(msg).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=840) as r:
            self.session = r.headers.get("Mcp-Session-Id") or self.session
            ctype, raw = r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
        if notify:
            return None
        if "text/event-stream" in ctype:
            replies = [json.loads(line[5:]) for line in raw.splitlines() if line.startswith("data:") and line[5:].strip()]
            reply = next((m for m in replies if m.get("id") == msg["id"]), replies[-1] if replies else {})
        else:
            reply = json.loads(raw) if raw.strip() else {}
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", str(reply["error"])))
        return reply.get("result", {})

    def request(self, method, params):
        for attempt in (1, 2):
            try:
                if not self.session:
                    self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "claude-room", "version": str(VERSION)}})
                    self.rpc("notifications/initialized", notify=True)
                return self.rpc(method, params)
            except urllib.error.HTTPError as e:
                if attempt == 2 or e.code not in (400, 404):
                    raise
                self.session = None   # session expired (editor restarted): start a new one
            except urllib.error.URLError as e:
                raise RuntimeError("UEFN's MCP isn't answering on 127.0.0.1:8000. Is UEFN open with Python Editor "
                                   "Scripting and UEFN MCP Toolsets on in Project Settings?") from e

    def list_tools(self, name=""):
        tools, cursor = [], None
        while True:
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += result.get("tools", [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        if name:
            tool = next((t for t in tools if t.get("name") == name), None)
            return json.dumps(tool, ensure_ascii=False, indent=1) if tool else f"no tool named {name}"
        return f"{len(tools)} tools:\n" + "\n".join(
            f"- {t.get('name')}: {(t.get('description') or '').strip().splitlines()[0][:160] if t.get('description') else ''}"
            for t in tools)

    def call(self, tool, arguments):
        result = self.request("tools/call", {"name": tool, "arguments": arguments or {}})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(f"[{item.get('type')} content, {len(str(item.get('data', '')))} chars, not shown]")
        if result.get("structuredContent") and not parts:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return not result.get("isError"), "\n".join(parts) or "(no output)"


UEFN = UefnMcp()


def downloads_dir():
    path = os.path.join(HOME, "Downloads")
    return path if os.path.isdir(path) else HOME


def free_path(path):
    """path, or path with (1), (2)... so nothing gets overwritten."""
    stem, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path):
        path = f"{stem} ({n}){ext}"
        n += 1
    return path


def download_shared(conn, file_id, dest=""):
    files = json.loads(OPENER.open(f"{conn.base}/files?t={urllib.parse.quote(conn.token)}", timeout=30).read())
    meta = next((f for f in files if f["id"] == file_id or f["name"].lower() == str(file_id).lower()), None)
    if not meta:
        raise RuntimeError(f"no shared file {file_id!r}")
    dest = resolve(dest) if dest else downloads_dir()
    path = free_path(os.path.join(dest, meta["name"]) if os.path.isdir(dest) or not os.path.splitext(dest)[1] else dest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with OPENER.open(f"{conn.base}/file/{meta['id']}?t={urllib.parse.quote(conn.token)}", timeout=60) as r, \
            open(path + ".part", "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(path + ".part", path)
    return path, meta


def clipboard_files():
    """Files copied in Explorer, or a copied picture saved as a PNG (Windows)."""
    if os.name != "nt":
        raise RuntimeError("/paste works on Windows; elsewhere use /send PATH")
    out = os.path.join(os.environ.get("TEMP", HOME), f"pasted-{time.strftime('%Y%m%d-%H%M%S')}.png")
    script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
              "$f = [Windows.Forms.Clipboard]::GetFileDropList(); if ($f.Count) { $f; exit } "
              "$i = [Windows.Forms.Clipboard]::GetImage(); "
              f"if ($i) {{ $i.Save('{out}', [Drawing.Imaging.ImageFormat]::Png); '{out}' }}")
    p = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                       encoding="utf-8", errors="replace", timeout=30)
    return [line.strip() for line in p.stdout.splitlines() if line.strip() and os.path.isfile(line.strip())]


def take_screenshot():
    """The screen under the mouse as a JPEG (at most 1920 px wide) in the temp folder: (ok, path or why not)."""
    path = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp", time.strftime("screen-%Y%m%d-%H%M%S.jpg"))
    try:
        if os.name == "nt":
            ps = ("Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' "
                  "-Name U -Namespace W; [W.U]::SetProcessDPIAware() | Out-Null;"
                  "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                  "$b=[System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position).Bounds;"
                  "$src=New-Object Drawing.Bitmap $b.Width,$b.Height;$g=[Drawing.Graphics]::FromImage($src);"
                  "$g.CopyFromScreen($b.Left,$b.Top,0,0,$src.Size);"
                  "$k=[Math]::Min([double]1,[double]1920/$b.Width);$w=[int]($b.Width*$k);$h=[int]($b.Height*$k);"
                  "$dst=New-Object Drawing.Bitmap $w,$h;$g2=[Drawing.Graphics]::FromImage($dst);"
                  "$g2.InterpolationMode='HighQualityBicubic';$g2.DrawImage($src,0,0,$w,$h);"
                  "$enc=[Drawing.Imaging.ImageCodecInfo]::GetImageEncoders()|?{$_.MimeType -eq 'image/jpeg'};"
                  "$p=New-Object Drawing.Imaging.EncoderParameters 1;"
                  "$p.Param[0]=New-Object Drawing.Imaging.EncoderParameter([Drawing.Imaging.Encoder]::Quality,[long]85);"
                  "$dst.Save('" + path.replace("'", "''") + "',$enc,$p)")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                               timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif sys.platform == "darwin":
            r = subprocess.run(["screencapture", "-x", "-t", "jpg", path], capture_output=True, text=True, timeout=40)
        else:
            r = subprocess.run(["import", "-window", "root", path], capture_output=True, text=True, timeout=40)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, path
        return False, ((r.stderr or r.stdout or "").strip() or "no picture came out")[:300]
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def upload_file(conn, path):
    path = resolve(path)
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        req = urllib.request.Request(
            f"{conn.base}/upload?t={urllib.parse.quote(conn.token)}&name={urllib.parse.quote(os.path.basename(path))}",
            data=f, method="POST", headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
        try:
            reply = json.loads(OPENER.open(req, timeout=3600).read())
        except urllib.error.HTTPError as e:
            reply = json.loads(e.read() or b"{}")
    return bool(reply.get("ok")), reply.get("message", "")


# ---- Lore locks -------------------------------------------------------------------
# Anything the room's Claude changes inside a UEFN project first takes that file's Lore lock: the
# same server-side lock UEFN's Auto Checkout uses (and any Claude Code lock hook the project has,
# e.g. .claude/hooks/lore-lock.ps1). So two people's AIs can never edit the same file at once, and a
# file someone else is editing is refused with their name. Enforced here, not left to the AI.
VERSE_WRITES = re.compile(r"^(WriteFile|Replace|Delete|Move)$")
SHELL_WRITES = re.compile(r"(Set-Content|Add-Content|Out-File|Clear-Content|WriteAll\w+|AppendAll\w+|Copy-Item|Move-Item|"
                          r"Remove-Item|Rename-Item|New-Item|\bdel\b|\berase\b|\bcopy\b|\bmove\b|\bren\b|\brm\b|\bmv\b|\bcp\b|"
                          r"\btee\b|\bsed\s+-i|(?<![-=<>&2])>{1,2}(?!\s*(&|\$null)))", re.I)   # not 2>$null, 2>&1, ->, =>
LOCK_SKIP = re.compile(r"^(Saved|Intermediate|Binaries|DerivedDataCache|\.lore)/|held-locks\.txt$|\.local\.md$|^\.claude/changes/", re.I)
_cache = {}


def uefn_projects_dir():
    """Where UEFN keeps its projects: UEFN_PROJECTS_DIR, else the usual Documents folder."""
    return os.environ.get("UEFN_PROJECTS_DIR") or os.path.join(HOME, "Documents", "Fortnite Projects")


def lore_repo(path):
    """The UEFN project folder (it has a .lore folder) that contains `path`, or None."""
    p = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(p, ".lore")):
            return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _powershell(script, timeout=20):
    p = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    return p.stdout


def open_project():
    """The project folder UEFN has open right now (from the editor's command line); cached for a minute."""
    hit = _cache.get("project")
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    repo = None
    if os.name == "nt":
        out = _powershell("(Get-CimInstance Win32_Process -Filter \"Name like 'UnrealEditorFortnite%'\" | "
                          "Select-Object -First 1).CommandLine")
        m = re.search(r"([A-Za-z]:[\\/][^\"]+?\.uefnproject)", out or "")
        if m:
            repo = lore_repo(os.path.dirname(m.group(1)))
    if not repo:   # opened from UEFN's project list: the editor log says which one
        log = os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealEditorFortnite", "Saved", "Logs", "UnrealEditorFortnite.log")
        try:
            with open(log, "rb") as f:
                f.seek(max(0, os.path.getsize(log) - 8_000_000))
                text = f.read().decode("utf-8", "replace")
            opened = re.findall(r"Opening project '([^']+\.uefnproject)'", text)
            if opened:
                repo = lore_repo(os.path.dirname(opened[-1]))
        except OSError:
            pass
    if not repo:   # the only UEFN project in Documents
        docs = uefn_projects_dir()
        found = [r for r in (lore_repo(os.path.join(docs, d)) for d in (os.listdir(docs) if os.path.isdir(docs) else [])) if r]
        repo = found[0] if len(found) == 1 else None
    _cache["project"] = (repo, time.time())
    return repo


def find_lore():
    if "lore" in _cache:
        return _cache["lore"]
    found = None
    candidates = [os.environ.get("LORE_EXE", "")]
    manifests = r"C:\ProgramData\Epic\EpicGamesLauncher\Data\Manifests"
    if os.path.isdir(manifests):   # wherever the Epic launcher installed Fortnite (any drive)
        for item in os.listdir(manifests):
            try:
                with open(os.path.join(manifests, item), encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("AppName") == "Fortnite":
                    candidates.append(os.path.join(data["InstallLocation"], r"FortniteGame\Binaries\Win64\lore.exe"))
            except (OSError, ValueError, KeyError):
                pass
    candidates.append(r"C:\Program Files\Epic Games\Fortnite\FortniteGame\Binaries\Win64\lore.exe")
    for c in candidates:
        if c and os.path.isfile(c):
            found = c
            break
    _cache["lore"] = found
    return found


def lore_lock(repo, rel):
    """Take the Lore lock on one project file. None = it's ours now (or it's a new file); else who holds it."""
    exe = find_lore()
    if not exe:
        return "nobody can be checked: lore.exe wasn't found (is Fortnite/UEFN installed? set LORE_EXE)"
    run = lambda *a: subprocess.run([exe, "--repository", repo, "-P", "--non-interactive", *a], cwd=repo,  # noqa: E731
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=90)
    p = run("lock", "acquire", rel)
    out = (p.stdout or "") + (p.stderr or "")
    if re.search(r"Lock (acquired|already owned)", out):
        held = os.path.join(repo, ".claude", "hooks", "held-locks.txt")   # so lore-lock.ps1 release frees it later
        try:
            have = open(held, encoding="utf-8").read().split() if os.path.exists(held) else []
            if rel not in have:
                os.makedirs(os.path.dirname(held), exist_ok=True)
                with open(held, "a", encoding="utf-8") as f:
                    f.write(rel + "\n")
        except OSError:
            pass
        return None
    if re.search(r"Node not found|Invalid path|not found in repository", out):
        return None   # a brand-new file: nothing to lock yet
    st = run("lock", "status", rel)
    m = re.search(re.escape(rel) + r" by (.+?) on (.+)", (st.stdout or "") + (st.stderr or ""))
    return f"{m.group(1)} (since {m.group(2).strip()})" if m else f"unknown ({' '.join(out.split())[:160]})"


def lock_files(repo, paths):
    for path in paths:
        full = os.path.abspath(path)
        if not full.lower().startswith(repo.lower() + os.sep):
            continue
        rel = full[len(repo) + 1:].replace("\\", "/")
        if LOCK_SKIP.search(rel):
            continue
        owner = lore_lock(repo, rel)
        if owner:
            return (f"LOCKED: {rel} is being edited by {owner}. Their AI holds the Lore lock until they Check In in "
                    "UEFN and release it. Tell the person who asked, work on a different file meanwhile, and don't "
                    "work around the lock.")
    return None


def held_list(repo):
    path = os.path.join(repo, ".claude", "hooks", "held-locks.txt")
    try:
        return [l.strip() for l in open(path, encoding="utf-8") if l.strip()]
    except OSError:
        return []


def set_held(repo, rels):
    path = os.path.join(repo, ".claude", "hooks", "held-locks.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(r + "\n" for r in rels))


def lore_out(repo, *a):
    p = subprocess.run([find_lore(), "--repository", repo, "-P", "--non-interactive", *a], cwd=repo, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return ((p.stdout or "") + (p.stderr or "")).strip()


def epic_name():
    if _cache.get("epic"):
        return _cache["epic"]
    out = _powershell("(Get-CimInstance Win32_Process -Filter \"Name like 'UnrealEditorFortnite%'\" | "
                      "Select-Object -First 1).CommandLine") if os.name == "nt" else ""
    m = re.search(r'-epicusername="?([^"\s]+)', out or "")
    if m:
        _cache["epic"] = m.group(1)
    return m.group(1) if m else ""


def scan_locks(repo):
    """Lore locks on the project's Verse files and on what this PC took: {file: {owner, since}}."""
    rels = []
    for root, _dirs, files in os.walk(os.path.join(repo, "Content")):
        rels += [os.path.relpath(os.path.join(root, f), repo).replace("\\", "/") for f in files
                 if f.endswith(".verse") and not f.endswith(".digest.verse")]
    locks = {}
    for rel in sorted(set(rels + held_list(repo)))[:60]:
        m = re.search(re.escape(rel) + r" by (.+?) on ([^\n]+)", lore_out(repo, "lock", "status", rel))
        if m:
            locks[rel] = {"owner": m.group(1).strip(), "since": m.group(2).strip()}
    return locks


def main_signature(repo):
    """Signature of main's latest revision on the server ("" if unknown); cached for a minute."""
    hit = _cache.get("main_sig")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    m = re.search(r"Signature\s*:\s*([0-9a-f]{64})", lore_out(repo, "history", "1", "--branch", "main", "--remote"))
    _cache["main_sig"] = (time.time(), m.group(1) if m else "")
    return _cache["main_sig"][1]


def clean_files(repo, rels):
    """Which of `rels` are really checked in: no local changes AND the same as main's latest revision. UEFN's
    Automatic Backup commits work to the person's own branch (main-<id>), where `lore status` calls it clean
    although it never reached main; releasing then let the other person take the file and block this Check In.
    Anything unclear counts as changed, so a lock is never released by mistake."""
    if not rels:
        return []
    out = lore_out(repo, "status", "--scan", *rels)
    if "No tracked changes" in out:
        local = list(rels)
    elif "Tracked changes" in out:
        local = [r for r in rels if r not in out]
    else:
        return []
    sig = main_signature(repo) if local else ""
    if not sig:
        return []
    targets = os.path.join(tempfile.gettempdir(), f"claude-room-lock-check-{os.getpid()}.txt")
    try:
        with open(targets, "w", encoding="utf-8") as f:
            f.write("".join(r + "\n" for r in local))
        diff = lore_out(repo, "revision", "diff", sig, "--targets", targets)
    except (OSError, subprocess.SubprocessError):
        return []
    if re.search(r"\[Error\]|error:", diff, re.I):
        return []
    differ = {line[2:].strip() for line in diff.splitlines() if re.match(r"^[AMD] ", line)}
    return [r for r in local if r not in differ]


def release_my_locks(events, only_older_than=0.0, quiet=False):
    """Release this PC's Lore locks on files that are checked in (no pending changes). Keeps the rest."""
    repo, exe = open_project(), find_lore()
    if not repo or not exe:
        if not quiet:
            events.put({"t": "sys", "text": "🔒 can't release: no UEFN project open or lore.exe not found"})
        return []
    held = held_list(repo)
    first = _cache.setdefault("held_since", {})
    for rel in held:
        first.setdefault(rel, time.time())
    ready = [r for r in held if time.time() - first[r] >= only_older_than]
    clean = clean_files(repo, ready)
    released = [r for r in clean if re.search(r"released|not locked|not found|Invalid path", lore_out(repo, "lock", "release", r))]
    if released:
        set_held(repo, [r for r in held_list(repo) if r not in released])
        for r in released:
            first.pop(r, None)
    kept = [r for r in held if r not in released]
    if released:
        events.put({"t": "notify", "kind": "info", "title": "🔓 released " + ", ".join(released),
                    "body": "checked in, so the other person can edit them now"})
    elif not quiet:
        events.put({"t": "sys", "text": "🔒 nothing to release" + (f" (still not checked in: {', '.join(kept)})" if kept else "")})
    if kept and not quiet:
        events.put({"t": "sys", "text": "🔒 kept (changes not checked in yet): " + ", ".join(kept)})
    return released


APPS = {"uefn": ("unrealeditorfortnite",), "fortnite": ("fortniteclient-win64-shipping",)}


def running_apps():
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
        else:
            out = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=20).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return []
    return [app for app, names in APPS.items() if any(n in out for n in names)]


def idle_seconds():
    """Seconds since this PC's last key press or mouse move (Windows), else None."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        class LastInput(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return max(0, (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) // 1000
    except Exception:  # noqa: BLE001 - best effort
        return None


AUTO_FILE = os.path.join(HOME, ".claude-room", "auto.json")


def load_auto():
    """Auto mode as it was when this terminal last ran (an update or a restart keeps it)."""
    try:
        with open(AUTO_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("auto"))
    except (OSError, ValueError, AttributeError):
        return False


def save_auto(on):
    try:
        os.makedirs(os.path.dirname(AUTO_FILE), exist_ok=True)
        with open(AUTO_FILE, "w", encoding="utf-8") as f:
            json.dump({"auto": bool(on), "ts": time.time()}, f)
    except OSError:
        pass


def activity_reporter(conn, screen=None):
    """Every 30 s: UEFN / Fortnite running and how long since the last key press, for "who's doing what".
    Right away when auto mode is switched, so the browser doesn't ask about actions that run by themselves."""
    sent_auto, last = None, 0.0
    while not conn.closed:
        auto = bool(getattr(screen, "auto", False))
        if auto != sent_auto or time.time() - last >= 30:
            if sent_auto is not None and auto != sent_auto:
                save_auto(auto)   # /auto, the a answer or the browser's Auto: remembered for the next start
            last = time.time()
            try:
                lease_tick()
            except Exception:  # noqa: BLE001 - best effort
                pass
            try:
                conn.post("/activity", {"apps": running_apps(), "idle": idle_seconds(), "auto": auto})
                sent_auto = auto
            except Exception:  # noqa: BLE001 - best effort
                pass
        time.sleep(1)


def lock_reporter(conn, events):
    """Every ~45 s: tell the room which project files are locked, and free this PC's locks on files
    that have been checked in (held 2+ minutes, no pending changes), so nobody stays blocked."""
    while not conn.closed:
        try:
            repo = open_project()
            if repo and find_lore():
                release_my_locks(events, only_older_than=120, quiet=True)
                conn.post("/locks", {"project": os.path.basename(repo), "me_epic": epic_name(), "locks": scan_locks(repo)})
                state = lore_sync_state(repo, fresh=True)
                if state:
                    conn.post("/sync", {"project": os.path.basename(repo), "rev": state[0], "remote": state[1],
                                        "me_epic": epic_name(), "history": lore_history(repo, 6)})
        except Exception:  # noqa: BLE001 - best effort
            pass
        time.sleep(45)


# ---- never change the project from an old copy ------------------------------------------
# Each person has their own copy of the UEFN project; a change made on a copy that's behind the shared one
# could overwrite what the other person checked in. So every change waits until the copy is up to date.
SYNC_CACHE = {}
READ_UEFN = re.compile(r"^(get|list|find|describe|read|query|search|inspect|count|is|has|can|lookup|print|dump|validate|trace|raycast|line_trace|sweep|overlap)", re.I)


def lore_sync_state(repo, fresh=False):
    """(this copy's revision, the shared one's) of a UEFN project, or None if Lore can't tell; cached 20 s."""
    hit = SYNC_CACHE.get(repo)
    if hit and not fresh and time.time() - hit[0] < 20:
        return hit[1]
    try:
        out = lore_out(repo, "status")
    except Exception:  # noqa: BLE001 - can't tell
        out = ""
    m, r = re.search(r"\brevision (\d+)", out), re.search(r"Remote revision (\d+)", out)
    state = (int(m.group(1)), int(r.group(1))) if m and r else None
    SYNC_CACHE[repo] = (time.time(), state)
    return state


def lore_history(repo, count=8):
    """The shared project's latest check-ins: [{rev, creator, date, msg}]."""
    try:
        out = lore_out(repo, "--remote", "history", str(count))
    except Exception:  # noqa: BLE001
        return []
    items = []
    for block in re.split(r"\n(?=Revision\s*:)", out):
        rev = re.search(r"Revision\s*:\s*(\d+)", block)
        if not rev:
            continue
        creator = re.search(r"Creator\s*:\s*(.+)", block)
        date = re.search(r"Date\s*:\s*(.+)", block)
        msg = " ".join(line.strip() for line in block.splitlines() if line.startswith("    "))
        items.append({"rev": int(rev.group(1)), "creator": creator.group(1).strip() if creator else "",
                      "date": date.group(1).strip() if date else "", "msg": msg[:300]})
    return items


def sync_gate(tool, args):
    """None if the change may run; else why not (the copy is behind the shared project)."""
    if tool not in ("write_file", "uefn_call", "undo_write") or not find_lore():
        return None
    if tool == "uefn_call":
        a = args.get("arguments") or {}
        name = str(a.get("tool_name") or "").split(".")[-1]
        if args.get("tool") in ("list_toolsets", "describe_toolset") or READ_UEFN.match(name or str(args.get("tool", ""))):
            return None   # looking, not changing
        repo = open_project()
    else:
        targets = guard_targets(tool, args)
        if not isinstance(targets, tuple) or not targets[0]:
            return None
        repo = targets[0]
        rels = [os.path.relpath(f, repo).replace("\\", "/") for f in targets[1]]
        if rels and all(LOCK_SKIP.search(r) for r in rels):
            return None   # notes, logs and caches aren't shared
    if not repo:
        return None
    state = lore_sync_state(repo)
    if state and state[1] > state[0]:
        state = lore_sync_state(repo, fresh=True)   # it may have just been synced
    if not state or state[1] <= state[0]:
        return None
    news = lore_history(repo, min(5, state[1] - state[0]))
    behind = state[1] - state[0]
    return (f"OUT OF DATE: this copy of {os.path.basename(repo)} is at revision {state[0]}, the shared project is at {state[1]} "
            f"({behind} newer check-in{'s' if behind != 1 else ''}"
            + (": " + "; ".join(f"{h['rev']} by {h['creator']}: {h['msg'][:80]}" for h in news) if news else "")
            + "). Changing it now could overwrite the other person's work. Ask the person to click Sync Latest in UEFN "
              "(Revision Control), then try again. Don't work around this.")


def lore_guard(tool, args):
    """None if the action may run; else why not. Edits inside a UEFN project take the file's Lore lock first."""
    targets = guard_targets(tool, args)
    if isinstance(targets, str):
        return targets
    repo, files = targets
    return lock_files(repo, files) if repo and files else None


def guard_targets(tool, args):
    """(project, [files]) an edit names up front; (None, []) when it names none; a str = refused."""
    if tool == "write_file":
        path = resolve(args.get("path"))
        repo = lore_repo(path)
        return (repo, [path]) if repo else (None, [])
    if tool == "undo_write":
        meta = backup_meta(args.get("bid"))
        repo = lore_repo(meta["path"]) if meta else None
        return (repo, [meta["path"]]) if repo else (None, [])
    if tool == "uefn_call":
        a = args.get("arguments") or {}
        paths = []
        if args.get("tool") == "call_tool" and "verse" in str(a.get("toolset_name", "")).lower() \
                and VERSE_WRITES.match(str(a.get("tool_name", ""))):
            inner = a.get("arguments") or {}
            paths = [str(inner[k]) for k in ("path", "sourcePath", "destPath") if inner.get(k)]
            if not paths:
                return "this Verse write names no file, so its Lore lock can't be taken"
        else:   # a script that names its Verse files: lock them up front (anything else is caught afterwards)
            script = json.dumps(a)
            if re.search(r"Verse\w*\.?\W*(WriteFile|Replace|Delete|Move)", script):
                paths = re.findall(r"""\\?["'](?:path|sourcePath|destPath)\\?["']\s*:\s*\\?["']([^"'\\]+)""", script)
        if not paths:
            return None, []   # reads, scene and device tools: the editor's Auto Checkout covers assets
        repo = open_project()
        if not repo:
            return "couldn't tell which project UEFN has open, so the Lore lock can't be taken"
        files = []
        for p in paths:
            m = re.match(r"^/[0-9a-fA-F-]{36}(/.*)?$", p)   # Verse paths look like /<project guid>/Sub/file.verse
            files.append(os.path.join(repo, "Content", *(m.group(1) or "").split("/")) if m else resolve(p, repo))
        return repo, files
    return None, []


# ---- several threads of one person on this PC ------------------------------------------
# Actions that change things run one at a time (each one's before/after check must only see its own
# changes), and a file one thread changed stays off limits to the person's other threads until that
# thread's turn ends: Lore locks are per person, so they can't keep two threads of the same person apart.
PC_CHANGING = ("write_file", "run_command", "uefn_call", "undo_write")
PC_LOCK = threading.Lock()
CLAIMS_LOCK = threading.Lock()
CLAIMS = {}            # (project, file) -> (lane, thread name) that changed it in its running turn
ACTIVE_LANES = set()   # lanes whose turn is running (batch / done events)


def set_active_lanes(lanes, add=None):
    """add=None: replace with `lanes` (on connect); True / False: one turn started / ended."""
    with CLAIMS_LOCK:
        if add is None:
            ACTIVE_LANES.clear()
        for lane in lanes:
            if add is False:
                ACTIVE_LANES.discard(lane)
            else:
                ACTIVE_LANES.add(lane)
        for key in [k for k, v in CLAIMS.items() if v[0] not in ACTIVE_LANES]:
            del CLAIMS[key]


def claim_conflict(keys, lane):
    """(file, thread name) of the first of `keys` another running thread changed, else None."""
    with CLAIMS_LOCK:
        for key in keys:
            c = CLAIMS.get(key)
            if c and c[0] != lane and c[0] in ACTIVE_LANES:
                return key[1], c[1]
    return None


def claim(keys, lane, thread):
    if lane:
        with CLAIMS_LOCK:
            for key in keys:
                if not LOCK_SKIP.search(key[1]):
                    CLAIMS[key] = (lane, thread or "Main")


# ---- zones: places in the map a thread depends on (keep_clear) or works in (area / folder) ----------------
ZONES = []   # from the room (hello / zones events)
POINT_KEYS = re.compile(r"^(location|translation|position|newLocation|worldLocation|target|destination)$", re.I)


def placed_points(obj, out=None, key=""):
    """Every [x, y, z] a UEFN call names under a location-like key."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        if POINT_KEYS.match(key or "") and all(isinstance(obj.get(k), (int, float)) for k in ("x", "y", "z")):
            out.append([float(obj["x"]), float(obj["y"]), float(obj["z"])])
        for k, v in obj.items():
            placed_points(v, out, k)
    elif isinstance(obj, list):
        if POINT_KEYS.match(key or "") and len(obj) == 3 and all(isinstance(v, (int, float)) for v in obj):
            out.append([float(v) for v in obj])
        for v in obj:
            placed_points(v, out, key)
    elif isinstance(obj, str) and obj[:1] in "{[":
        try:
            placed_points(json.loads(obj), out, key)
        except ValueError:
            pass
    return out


def zone_label(z):
    return f"“{z.get('name', '')}” ({z.get('lane') or z.get('by', '')})"


def zone_gate(op, args, lane):
    """(refusal or None, warnings) for an editor change, from the zones it touches."""
    if op in (None, "read", "look"):
        return None, []
    a = args.get("arguments") or {}
    me = (lane or "").split("/")[0]
    warn = []
    for p in placed_points(a.get("arguments") or {}):
        for z in ZONES:
            if z.get("kind") == "folder" or z.get("lane") == lane:
                continue
            if all(z["min"][i] <= p[i] <= z["max"][i] for i in range(3)):
                if z.get("kind") == "keep_clear":
                    return (f"ZONE: ({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}) is inside the keep-clear zone {zone_label(z)}"
                            + (f": {z['note']}" if z.get("note") else "") + ". Nothing else may go there (it would break "
                            "that work, e.g. spawns or a camera shot). Pick another spot, or thread_note its owner if you "
                            "really need it."), []
                warn.append(f"({p[0]:.0f}, {p[1]:.0f}, {p[2]:.0f}) is in {zone_label(z)}'s area")
    text = json.dumps(a)
    for z in ZONES:
        if z.get("kind") == "folder" and z.get("lane") != lane and z.get("folder") and "/" + z["folder"].strip("/") + "/" in text:
            warn.append(f"{z['folder']} is {zone_label(z)}'s folder")
    return None, warn


# ---- the playtest log: errors every thread of this person should hear about at once ----------------
LOG_ISSUES = re.compile(r"TeleportTo error|VerseRuntimeError|Verse runtime error|Runtime error|LogVerse: Error|"
                        r"Script Stack|Assertion failed|Unhandled exception|Accessed None", re.I)


def editor_log_path():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealEditorFortnite", "Saved", "Logs", "UnrealEditorFortnite.log")


def log_watcher(conn):
    """Every 20 s: new error lines in UEFN's log (counted, deduplicated) go to the room."""
    path, pos, seen = editor_log_path(), None, {}
    while not conn.closed:
        try:
            size = os.path.getsize(path)
            if pos is None or size < pos:   # first look, or UEFN started a new log
                pos = size
            elif size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(min(size - pos, 4_000_000)).decode("utf-8", "replace")
                pos = size
                found = {}
                for line in chunk.splitlines():
                    if LOG_ISSUES.search(line):
                        msg = re.sub(r"^\[[^\]]*\]\[\s*\d+\]", "", line).strip()
                        key = re.sub(r"\d+", "#", msg)[:160]
                        found.setdefault(key, [msg, 0])[1] += 1
                fresh = [{"text": m[:300], "count": n} for k, (m, n) in found.items() if time.time() - seen.get(k, 0) > 600]
                for k in found:
                    seen[k] = time.time()
                if fresh:
                    conn.post("/pc_event", {"kind": "playtest_log", "items": fresh[:6]})
        except Exception:  # noqa: BLE001 - best effort
            pass
        time.sleep(20)


# ---- commands that act on the whole project wait for the person's other threads -------------------------
PROJECT_WIDE = [
    (re.compile(r"\blore(\.exe)?\b[^\n|;]*\b(commit|checkin|check-in|check_in|submit|share)\b|lore-share|check-in\.ps1", re.I),
     "Check In", "it would also check in their half-done changes"),
    (re.compile(r"\blore(\.exe)?\b[^\n|;]*\b(sync|update|pull|revert|reset)\b", re.I),
     "Sync Latest / revert", "it could replace the files they are changing under them"),
    (re.compile(r"(Stop-Process|taskkill|kill\b|Get-Process[^\n|]*\|\s*Stop)[^\n]*(UnrealEditorFortnite|FortniteClient|UEFN|Fortnite)", re.I),
     "closing UEFN / Fortnite", "it would cut off their editor calls and their playtest"),
]


def project_wide_gate(tool, args, lane):
    if tool != "run_command":
        return None
    command = str(args.get("command", ""))
    with CLAIMS_LOCK:
        others = sorted(l for l in ACTIVE_LANES if l != lane)
    if not others:
        return None
    for rx, what, why in PROJECT_WIDE:
        if rx.search(command):
            return (f"WAIT: {what} waits while your other threads are working ({', '.join(others)}): {why}. Do it when "
                    "they are done, or tell the person and let them decide.")
    return None


# ---- commands that only look: they don't wait behind another thread's long change ----------------
READ_CMD = re.compile(r"^\s*(cd|Set-Location|sl|Get-Content|gc|cat|type|Select-String|sls|Get-ChildItem|gci|dir|ls|Get-Item|gi|"
                      r"Test-Path|Resolve-Path|Split-Path|Join-Path|Get-FileHash|Measure-Object|measure|Select-Object|select|"
                      r"Sort-Object|sort|Group-Object|Where-Object|where|\?|Format-\w+|ft|fl|Out-String|Write-Output|echo|"
                      r"findstr|Get-Date|Get-Process|tasklist|ConvertFrom-Json|ConvertTo-Json|\$[\w:]+|['\"]|%\s*\{|"
                      r"ForEach-Object\s*\{|foreach\s*\()", re.I)
WRITE_HINT = re.compile(r">|(?<![\w.\\/-])(Set-(?!Location)\w+|Remove-\w+|New-\w+|Copy-\w+|Move-\w+|Rename-\w+|Out-File|"
                        r"Add-Content|Clear-\w+|Start-\w+|Stop-\w+|Invoke-\w+|Expand-Archive|Compress-Archive|"
                        r"Write-(?!Output)\w+|Import-\w+|Export-\w+|mkdir|md|rmdir|rm|del|erase|copy|move|ren|xcopy|robocopy|"
                        r"python\w*|py|pip|node|npm|npx|blender\w*|lore|git|curl|wget|iwr|irm|powershell|pwsh|cmd|reg|"
                        r"schtasks|sc|net|taskkill|msiexec|winget|choco)(?![\w.-])|\.(ps1|exe|bat|cmd)\b(?!\s*['\"])|"
                        r"\[(System\.)?IO\.(File|Directory)\]::(Write|Append|Delete|Copy|Move|Create|Replace)|"
                        r"\.(Delete|MoveTo|CopyTo|Create|Save|Write\w*|Kill)\(", re.I)


def read_only_command(command):
    """True only for a command made of reading cmdlets (anything that might change or start something: False)."""
    command = str(command or "")
    if not command.strip() or WRITE_HINT.search(command):
        return False
    return all(READ_CMD.match(s) for s in re.split(r"[;\n|]", command) if s.strip() and s.strip() not in ("}", "{"))


PC_HOLDER = {}       # the action holding PC_LOCK: {"thread", "what", "since"}
RUNNING = [0]        # actions running now (the terminal doesn't restart itself meanwhile)


# ---- the UEFN playtest belongs to one thread at a time ------------------------------------------
# The first thread that starts a session holds it; the person's other threads get HELD for session calls, builds,
# imports and editor changes until it is free: that thread calls StopSession, the person clicks Done testing (or
# /tested), or the session ends. Reads never wait. The room shows who holds it (POST /lease).
SESSION_START = re.compile(r"\b(StartSession|StartGame|PushChanges|LaunchSession)\b")
SESSION_STOP = re.compile(r"\b(StopSession|StopGame|EndSession)\b")
EDITOR_WIDE = re.compile(r"\b(BuildAll|BuildVerse|import_files?|delete_assets?|rename_assets?)\b")
SCRIPT_CHANGES = re.compile(r"\b(set_\w+|Set[A-Z]\w*|add_\w+|Add[A-Z]\w*|remove\w*|Remove\w*|save\w*|Save\w*|import\w*|"
                            r"Import\w*|delete\w*|Delete\w*|destroy\w*|spawn\w*|Spawn\w*|place\w*|Place\w*|write\w*|Write\w*|"
                            r"Replace\w*|move\w*|Move\w*|create\w*|Create\w*|duplicate\w*|Duplicate\w*|rename\w*|Rename\w*|"
                            r"modify\w*|Modify\w*|apply\w*|Apply\w*|attach\w*|edit\w*|Edit\w*|compile\w*|Compile\w*|Build\w*)\b")
LEASE_LOCK = threading.Lock()
LEASE = {}              # {"lane", "thread", "since", "state": "running" / "review", "touched", "had_session"} or empty
LEASE_CONN = [None]     # the room connection, to report the lease
_session_cache = [0.0, ("", "")]


def uefn_op(tool, args):
    """What a UEFN call does, for the lease: read / look (a script that only reads) / start / stop / saveall / wide /
    change; None for anything that isn't a UEFN call."""
    if tool != "uefn_call":
        return None
    a = args.get("arguments") or {}
    name = str(a.get("tool_name") or "").split(".")[-1]
    if args.get("tool") in ("list_toolsets", "describe_toolset") or READ_UEFN.match(name or str(args.get("tool", ""))):
        return "read"
    text = json.dumps(a) if name == "execute_tool_script" else name
    if SESSION_START.search(text):
        return "start"
    if SESSION_STOP.search(text):
        return "stop"
    if name == "save_assets" and not (a.get("arguments") or {}).get("asset_paths"):
        return "saveall"
    if name == "execute_tool_script" and re.search(r"save_assets\W+(?:asset_paths\W+)?\[\s*\]|save_dirty|SaveDirty|save_all|SaveAll", text):
        return "saveall"
    if EDITOR_WIDE.search(text):
        return "wide"
    if name == "execute_tool_script" and not SCRIPT_CHANGES.search(text.replace("execute_tool_script", "")):
        return "look"
    return "change"


def session_state(max_age=20):
    """(GetSessionStatus, GetGameState) of UEFN's playtest session, e.g. ("Connected", "Running"); cached briefly."""
    if time.time() - _session_cache[0] < max_age:
        return _session_cache[1]

    def ask(name):
        try:
            ok, out = UEFN.call("call_tool", {"toolset_name": "ValkyrieToolset.SessionToolset", "tool_name": name, "arguments": {}})
            m = re.search(r'"returnValue"\s*:\s*"(\w+)"', out or "")
            return m.group(1) if ok and m else ""
        except Exception:  # noqa: BLE001 - no editor, no session
            return ""
    status = ask("GetSessionStatus")
    state = (status, ask("GetGameState") if status == "Connected" else "")
    _session_cache[:] = [time.time(), state]
    return state


def report_lease():
    conn = LEASE_CONN[0]
    if conn:
        with LEASE_LOCK:
            lease = dict(LEASE) or None
        threading.Thread(target=lambda: conn.post("/lease", {"lease": lease}), daemon=True).start()


def set_lease(new):
    with LEASE_LOCK:
        LEASE.clear()
        LEASE.update(new or {})
    report_lease()


def held_text(lease):
    since = time.strftime("%H:%M", time.localtime(lease["since"]))
    how = "it is testing right now" if lease["state"] == "running" else "it left the session running for the person to look at"
    return (f"HELD: the UEFN playtest on this PC belongs to your other thread “{lease['thread']}” since {since} ({how}). Until "
            "it's free, don't start/stop sessions, push changes, build or change the editor: you'd break that test or ship "
            "your unfinished changes into it. Meanwhile do what doesn't touch the editor (read, write files, plan, prepare "
            "assets) or tell the person you're waiting. It frees when that thread calls StopSession, or when the person "
            "clicks Done testing in the room (/tested in chat.py).")


def lease_gate(op, lane, thread):
    """None if this UEFN call may run now; else why not. Takes the lease for a thread that starts a session."""
    if op in (None, "read", "look"):
        return None
    if op == "saveall":
        with CLAIMS_LOCK:
            others = sorted(l for l in ACTIVE_LANES if l != lane)
        if others:
            return ("SAVE-ALL REFUSED: other threads are working in this editor right now (" + ", ".join(others) + "); saving "
                    "everything would also save their half-done changes. Call save_assets with asset_paths = exactly the "
                    "assets you changed.")
        op = "wide"
    with LEASE_LOCK:
        lease = dict(LEASE)
    if lease and lease["lane"] != lane:
        if lease["state"] == "running" and lease["lane"] in ACTIVE_LANES:
            return held_text(lease)
        status, game = session_state()
        if status != "Connected":
            set_lease(None)   # that session is over
        elif game == "Running" or time.time() - lease.get("touched", lease["since"]) < 600:
            if game == "Running":
                with LEASE_LOCK:
                    LEASE["touched"] = time.time()
            return held_text(lease)
        else:
            set_lease(None)   # left connected but nobody played it for 10 minutes
    elif lease and lease["state"] == "review":   # its thread is back at it
        with LEASE_LOCK:
            LEASE["state"], LEASE["touched"] = "running", time.time()
        report_lease()
    elif not lease and op in ("start", "stop"):
        status, game = session_state()
        if status == "Connected" and game == "Running":
            return ("HELD: a playtest is running on this PC that none of your threads started: the person is probably "
                    "playing it. Don't start, stop or push into it; ask them first.")
    if op == "start":
        with LEASE_LOCK:
            mine = LEASE.get("lane") == lane
        if not mine:
            set_lease({"lane": lane, "thread": thread or "Main", "since": time.time(), "state": "running",
                       "touched": time.time(), "had_session": False})
    return None


def lease_after(op, args, lane, ok, output):
    """After a UEFN call: a failed start frees the lease, StopSession frees it, and Verse build errors in files
    another running thread is changing are labelled as theirs."""
    text = json.dumps(args.get("arguments") or {})
    with LEASE_LOCK:
        mine = LEASE.get("lane") == lane
        if mine and op == "start" and ok:
            LEASE["had_session"], LEASE["touched"] = True, time.time()
        free = mine and ((op == "start" and not ok and not LEASE.get("had_session"))
                         or (op == "stop" and ok and re.search(r"\b(StopSession|EndSession)\b", text)))
    if free:
        set_lease(None)
    _session_cache[0] = 0.0
    if op == "wide" and "BuildAll" in text and output:
        with CLAIMS_LOCK:
            theirs = {os.path.basename(k[1]): v[1] for k, v in CLAIMS.items() if v[0] != lane and v[0] in ACTIVE_LANES}
        hits = [f"{f} (thread “{t}”)" for f, t in theirs.items() if f in output]
        if hits:
            output += ("\n\n[room] errors in " + ", ".join(hits) + " come from your other thread changing those files right "
                       "now, not from you: don't fix them; thread_note that thread if they block you.")
    return output


def lease_turn_ended(lane):
    """The holder's turn ended: if its session still runs, the person is looking at it (review); else it's free."""
    with LEASE_LOCK:
        mine = LEASE.get("lane") == lane and LEASE.get("state") == "running"
    if not mine:
        return
    _session_cache[0] = 0.0
    status, _ = session_state()
    if status == "Connected":
        with LEASE_LOCK:
            LEASE["state"], LEASE["touched"] = "review", time.time()
        report_lease()
    else:
        set_lease(None)


def lease_tick():
    """Every 30 s: a session left for the person frees itself once it's gone or nobody played it for 10 minutes."""
    with LEASE_LOCK:
        lease = dict(LEASE)
    if not lease or lease["state"] != "review":
        return
    status, game = session_state(max_age=0)
    if status != "Connected" or (game != "Running" and time.time() - lease.get("touched", lease["since"]) > 600):
        set_lease(None)
    elif game == "Running":
        with LEASE_LOCK:
            LEASE["touched"] = time.time()


# ---- the guarantee: check what actually changed ------------------------------------------
CODE_FILES = re.compile(r"\.(verse|md|json|txt|ini|py|cfg|csv|ya?ml|toml|xml)$", re.I)
SNAP_SKIP_DIRS = re.compile(r"^(Saved|Intermediate|Binaries|DerivedDataCache|\.lore)$", re.I)
_content = {}   # (repo, file) -> (stat, bytes): what each code file held, to undo a change


def projects_in_scope(cwd=None):
    repos = {r for r in (open_project(), lore_repo(resolve(cwd or HOME))) if r}
    docs = uefn_projects_dir()
    if os.path.isdir(docs):
        repos |= {r for r in (lore_repo(os.path.join(docs, d)) for d in os.listdir(docs)) if r}
    return repos


def code_snapshot(repo, keep=True):
    """{file: (mtime, size)} of the project's code/text files (the editor's assets are Auto Checkout's job).
    With keep, remembers each file's bytes so a change can be undone."""
    snap = {}
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if not (root == repo and SNAP_SKIP_DIRS.match(d))]
        for f in files:
            if not CODE_FILES.search(f):
                continue
            full = os.path.join(root, f)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, repo).replace("\\", "/")
            snap[rel] = (st.st_mtime_ns, st.st_size)
            if keep and st.st_size <= 2_000_000 and _content.get((repo, rel), (None,))[0] != snap[rel]:
                try:
                    with open(full, "rb") as fh:
                        _content[(repo, rel)] = (snap[rel], fh.read())
                except OSError:
                    pass
    return snap


def synced_from_main(repo, rel):
    """True when `rel` on disk is exactly main's latest version: a Sync (or a reset) changed it, not anyone's edit."""
    sig = main_signature(repo)
    if not sig:
        return False
    out = lore_out(repo, "diff", "--source", sig, rel)   # main's file against the one on disk: empty when they match
    return not re.search(r"^(---|\+\+\+|@@)|\[Error\]|error:", out, re.M | re.I)


def enforce_effects(repo, before, lane=None, changed=None):
    """After a command: every changed code file must get its Lore lock; if someone else holds it (or the
    lock can't be checked), or the person's other thread is changing it, the change is undone.
    Returns what was undone; the files kept are added to `changed`."""
    after = code_snapshot(repo, keep=False)
    touched = sorted({r for r in after if before.get(r) != after[r]} | {r for r in before if r not in after})
    undone = []
    for rel in touched:
        if LOCK_SKIP.search(rel):
            continue
        busy = claim_conflict([(repo, rel)], lane) if lane else None
        owner = f"your other thread “{busy[1]}”" if busy else lore_lock(repo, rel)
        if not owner:
            if changed is not None:
                changed.append((repo, rel))
            continue
        if synced_from_main(repo, rel):   # a Sync Latest landed meanwhile: undoing it would revert the other person's work
            continue
        full = os.path.join(repo, *rel.split("/"))
        saved = _content.get((repo, rel))
        try:
            if rel in before and saved and saved[0] == before[rel]:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as fh:
                    fh.write(saved[1])
                os.utime(full, ns=(before[rel][0], before[rel][0]))
                undone.append(f"{rel} (held by {owner}): your change was undone")
            elif rel not in before and os.path.exists(full):
                os.remove(full)
                undone.append(f"{rel} (held by {owner}): the new file was removed")
            else:
                undone.append(f"{rel} (held by {owner}): changed, but couldn't be undone; revert it in UEFN")
        except OSError as e:
            undone.append(f"{rel} (held by {owner}): couldn't undo ({e}); revert it in UEFN")
    return undone


# ---- ↶ undo: what a write replaced is kept here, so its owner can put it back from the room ----
BACKUP_DIR = os.path.join(HOME, ".claude-room", "backups")


def backup_file(path):
    """Keep what `path` holds before Claude writes it; returns the backup id (None if it can't)."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        bid = time.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(3).hex()
        existed = os.path.isfile(path)
        if existed:
            if os.path.getsize(path) > 50 * 1024 * 1024:
                return None
            shutil.copyfile(path, os.path.join(BACKUP_DIR, bid + ".bak"))
        with open(os.path.join(BACKUP_DIR, bid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"path": path, "existed": existed, "ts": time.time()}, f)
        cutoff = time.time() - 14 * 86400   # two weeks of backups
        for name in os.listdir(BACKUP_DIR):
            p = os.path.join(BACKUP_DIR, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        return bid
    except OSError:
        return None


def backup_meta(bid):
    if not re.fullmatch(r"\d{14}-[0-9a-f]{6}", str(bid or "")):
        return None
    try:
        with open(os.path.join(BACKUP_DIR, bid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_tool(tool, args, conn=None, lane=None, thread=""):
    """Execute one approved action on this PC; returns (ok, text for Claude). Reads run side by side;
    changes wait for the person's other threads (see CLAIMS) and then run one at a time."""
    if tool not in PC_CHANGING:
        return run_tool_checked(tool, args, conn)
    op = uefn_op(tool, args)
    if op == "read" or (tool == "run_command" and read_only_command(args.get("command"))):
        return run_tool_unguarded(tool, args, conn)   # looking never waits for the other threads' changes
    held = project_wide_gate(tool, args, lane)
    if held:
        return False, held
    refused, zone_warn = zone_gate(op, args, lane)
    if refused:
        return False, refused
    held = lease_gate(op, lane, thread)
    if held:
        return False, held
    targets = guard_targets(tool, args)
    keys = [(targets[0], os.path.relpath(f, targets[0]).replace("\\", "/")) for f in targets[1]] \
        if isinstance(targets, tuple) and targets[0] else []
    deadline = time.time() + 300
    hit = claim_conflict(keys, lane) if lane else None
    while hit and time.time() < deadline:
        time.sleep(2)
        hit = claim_conflict(keys, lane)
    if hit:
        return False, (f"BUSY: {hit[0]} is being changed by your other thread “{hit[1]}” right now (waited 5 minutes). "
                       "Tell the person; work on something else or try again when that thread is done.")
    if not PC_LOCK.acquire(timeout=600):
        h = dict(PC_HOLDER)
        who = (f"your other thread “{h.get('thread')}” ({h.get('what')}, running {int((time.time() - h.get('since', time.time())) / 60)} min)"
               if h else "another thread's action")
        return False, (f"BUSY: {who} has held this PC's change line for 10 minutes; reading still works. Try again later "
                       "or thread_note that thread.")
    PC_HOLDER.update({"thread": thread or "Main", "what": summarize(tool, args)[:80], "since": time.time()})
    try:
        changed = []
        ok, output = run_tool_checked(tool, args, conn, lane, changed)
        claim(changed + (keys if ok else []), lane, thread)
        if op:
            output = lease_after(op, args, lane, ok, output)
        if zone_warn:
            output += ("\n\n[room] heads-up: " + "; ".join(zone_warn) + ". Tell that thread what you changed "
                       "(thread_note) so it isn't surprised.")
        return ok, output
    finally:
        PC_HOLDER.clear()
        PC_LOCK.release()


def run_tool_checked(tool, args, conn=None, lane=None, changed=None):
    try:
        blocked = sync_gate(tool, args) or lore_guard(tool, args)
        if blocked:
            m = re.match(r"LOCKED: (\S+) is being edited by (.+?)\. ", blocked)
            if m and conn:   # tell the people, not only Claude
                conn.post("/lock_blocked", {"file": m.group(1), "owner": m.group(2)})
            return False, blocked
        if tool in ("run_command", "uefn_call") and find_lore():
            repos = projects_in_scope(args.get("cwd") if tool == "run_command" else None)
            before = {r: code_snapshot(r) for r in repos}
            ok, output = run_tool_unguarded(tool, args, conn)
            undone = [u for r in repos for u in enforce_effects(r, before[r], lane, changed)]
            if undone:
                for u in undone:
                    m = re.match(r"(\S+) \(held by (.+?)\)", u)
                    if m and conn and "your other thread" not in u:   # a clash between your own threads isn't a Lore lock
                        conn.post("/lock_blocked", {"file": m.group(1), "owner": m.group(2)})
                return False, (("BUSY" if all("your other thread" in u for u in undone) else "LOCKED")
                               + ": this changed project files someone else is editing, so those changes were "
                               "undone:\n- " + "\n- ".join(undone) + "\nTell the person who asked; don't work around "
                               "the lock.\n\nOriginal output:\n" + output)
            return ok, output
        return run_tool_unguarded(tool, args, conn)
    except Exception as e:  # noqa: BLE001 - report any failure back to Claude
        return False, f"{type(e).__name__}: {e}"


def run_tool_unguarded(tool, args, conn=None):
    try:
        if tool == "uefn_list_tools":
            return True, UEFN.list_tools(str(args.get("name") or ""))[:30000]
        if tool == "uefn_call":
            ok, out = UEFN.call(str(args.get("tool", "")), args.get("arguments") or {})
            return ok, out if len(out) <= 30000 else out[:30000] + "\n…(cut at 30k chars)"
        if tool == "file_get":
            path, meta = download_shared(conn, str(args.get("file_id", "")), str(args.get("dest") or ""))
            return True, f"saved {meta['name']} ({meta['size']} bytes) to {path}"
        if tool == "file_put":
            ok, msg = upload_file(conn, str(args.get("path", "")))
            return ok, msg
        if tool == "run_command":
            command = str(args.get("command", ""))
            if os.name == "nt":
                shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
            else:
                shell = ["bash", "-lc", command]
            p = subprocess.run(shell, cwd=resolve(args.get("cwd") or HOME), capture_output=True,
                               stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace", timeout=300)
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if len(out) > 30000:
                out = "…(cut)…\n" + out[-30000:]
            return p.returncode == 0, f"exit code {p.returncode}\n{out}"
        if tool == "read_file":
            with open(resolve(args.get("path")), encoding="utf-8", errors="replace") as f:
                text = f.read(200_001)
            return True, text[:200_000] + ("\n…(file is longer, cut at 200k chars)" if len(text) > 200_000 else "")
        if tool == "write_file":
            path = resolve(args.get("path"))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            content = str(args.get("content", ""))
            bid = backup_file(path)   # what was there before, so the room can undo this
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return True, f"wrote {len(content)} chars to {path}" + (f" [undo:{bid}]" if bid else "")
        if tool == "screenshot":   # Claude looks at the screen: the picture goes into the room for everyone to see
            ok, path = take_screenshot()
            if not ok:
                return False, f"couldn't take a screenshot: {path}"
            ok, msg = upload_file(conn, path)
            try:
                os.remove(path)
            except OSError:
                pass
            return ok, (f"took a screenshot of the screen and {msg}: look at it with file_view" if ok else msg)
        if tool == "undo_write":   # ↶ Undo clicked by this PC's owner: the file goes back to the kept copy
            meta = backup_meta(args.get("bid"))
            if not meta:
                return False, "that backup is gone (they're kept two weeks)"
            if meta["existed"]:
                shutil.copyfile(os.path.join(BACKUP_DIR, args["bid"] + ".bak"), meta["path"])
                return True, f"restored {meta['path']} to how it was before Claude's change"
            if os.path.exists(meta["path"]):
                os.remove(meta["path"])
            return True, f"removed {meta['path']}: Claude had created it"
        if tool == "list_dir":
            path = resolve(args.get("path"))
            lines = []
            for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower()))[:500]:
                lines.append(f"{entry.name}/" if entry.is_dir() else f"{entry.name}  ({entry.stat().st_size} B)")
            return True, f"{path}\n" + ("\n".join(lines) or "(empty)")
        return False, f"unknown tool {tool}"
    except subprocess.TimeoutExpired:
        return False, "timed out after 5 minutes"
    except Exception as e:  # noqa: BLE001 - report any failure back to Claude
        return False, f"{type(e).__name__}: {e}"


def keys_windows(vt=False):
    import msvcrt
    if vt:   # the console sends VT sequences (needed for the mouse on the whiteboard)
        data = ""
        while msvcrt.kbhit():
            data += msvcrt.getwch()
        yield from parse_input(data)
        return
    special = {"K": "left", "M": "right", "G": "home", "O": "end", "S": "delete", "H": "up", "P": "down"}
    while msvcrt.kbhit():
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch = special.get(msvcrt.getwch(), "")
        yield ch, msvcrt.kbhit()


SEQS = {"\x1b[D": "left", "\x1b[C": "right", "\x1b[H": "home", "\x1b[F": "end", "\x1b[3~": "delete",
        "\x1b[A": "up", "\x1b[B": "down", "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
        "\x1b[1~": "home", "\x1b[4~": "end"}
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def parse_input(data):
    """Split raw terminal input into keys, named special keys and ("mouse", (button, x, y, pressed))."""
    i = 0
    while i < len(data):
        m = MOUSE.match(data, i)
        if m:
            yield ("mouse", (int(m[1]), int(m[2]), int(m[3]), m[4] == "M")), False
            i = m.end()
            continue
        for seq, name in SEQS.items():
            if data.startswith(seq, i):
                yield name, False
                i += len(seq)
                break
        else:
            yield data[i], i + 1 < len(data)
            i += 1


def keys_posix(fd, decoder):
    import select
    if not select.select([fd], [], [], 0)[0]:
        return
    yield from parse_input(decoder.decode(os.read(fd, 4096)))


def reopen_in_windows_terminal():
    """If we're in the old console window and Windows Terminal exists, move there. True = moved."""
    if os.name != "nt" or "--here" in sys.argv:
        return False
    import ctypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    cls = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, cls, 64)
    wt = shutil.which("wt")
    alias = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps", "wt.exe")
    if not wt and os.path.exists(alias):   # installed, but WindowsApps isn't on PATH
        wt = alias
    if cls.value != "ConsoleWindowClass" or not wt:   # already in Windows Terminal (or no WT)
        return False
    try:
        subprocess.Popen([wt, "-w", "new", "--title", "Claude Room", sys.executable,
                          os.path.abspath(__file__), *sys.argv[1:], "--here"])
    except OSError:
        return False
    print("Opened Claude Room in Windows Terminal (smoother). Add --here to stay in this window.")
    time.sleep(1.5)
    return True


def enable_vt():
    """Turn on ANSI colors; returns a short description of the terminal for lag reports."""
    if os.name != "nt":
        return os.environ.get("TERM", "?")
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # QuickEdit: a click in the old console window starts a selection and freezes all
    # output until a key is pressed, which looks like the chat lagging. Turn it off.
    stdin = kernel32.GetStdHandle(-10)
    kernel32.GetConsoleMode(stdin, ctypes.byref(mode))
    quick_edit = bool(mode.value & 0x0040)
    kernel32.SetConsoleMode(stdin, (mode.value & ~0x0040) | 0x0080)
    term = "Windows Terminal" if os.environ.get("WT_SESSION") else "old console window"
    return f"{term}, QuickEdit was {'on' if quick_edit else 'off'}"


INSTANCE = None


def claim_instance(base, token):
    """True if this is the only terminal for this person and room; else bring the open one to the front."""
    global INSTANCE
    INSTANCE = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        INSTANCE.bind(("127.0.0.1", 47000 + zlib.crc32(f"{base}|{token}".encode()) % 2000))
        INSTANCE.listen(1)
        return True
    except OSError:
        INSTANCE.close()
        INSTANCE = None
        focus_room_window()
        return False


def focus_room_window():
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    user32, found = ctypes.windll.user32, []
    own = ctypes.windll.kernel32.GetConsoleWindow()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value == "Claude Room" and hwnd != own and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(each, 0)
    for hwnd in found[:1]:
        user32.ShowWindow(hwnd, 9)   # restore if minimized
        user32.SetForegroundWindow(hwnd)


def register_link(conn):
    """clauderoom:// (the "Open terminal" button on the room page) starts this terminal, or brings it up."""
    if os.name != "nt":
        return
    import winreg
    target = os.path.join(ROOT, "room.bat" if conn.base.startswith("http://127.0.0.1") else "Claude Room.bat")
    if not os.path.exists(target):
        return
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\clauderoom")
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Claude Room")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        command = winreg.CreateKey(key, r"shell\open\command")
        winreg.SetValueEx(command, "", 0, winreg.REG_SZ, f'"{os.environ.get("COMSPEC", "cmd.exe")}" /c call "{target}"')
    except OSError:
        pass


def refresh_launcher(conn, events):
    """Re-download this person's launcher (.bat) next to chat.py and keep a "Claude Room" desktop
    shortcut on it, so changes to the launcher reach them too, not only changes to chat.py."""
    if os.name != "nt" or conn.base.startswith("http://127.0.0.1"):
        register_link(conn)
        return
    try:
        bat = OPENER.open(f"{conn.base}/join.bat?t={urllib.parse.quote(conn.token)}", timeout=20).read()
        path = os.path.join(ROOT, "Claude Room.bat")
        old = open(path, "rb").read() if os.path.exists(path) else b""
        if bat and bat != old:
            with open(path, "wb") as f:
                f.write(bat)
        ps = ("$d=[Environment]::GetFolderPath('Desktop'); $l=Join-Path $d 'Claude Room.lnk'; "
              "if (-not (Test-Path $l)) { $s=(New-Object -ComObject WScript.Shell).CreateShortcut($l); "
              f"$s.TargetPath='{path}'; $s.WorkingDirectory='{ROOT}'; $s.Save(); 'made' }}")
        made = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30).stdout
        if "made" in made:
            events.put({"t": "sys", "text": "made a “Claude Room” shortcut on your desktop: it always starts the newest version"})
    except Exception:  # noqa: BLE001 - nice to have
        pass
    register_link(conn)


def update_and_restart(conn):
    """Put the room's newest chat.py in place of this file and run it in this same window."""
    path = os.path.abspath(__file__)
    print("Updating chat.py ...", flush=True)
    try:
        new = OPENER.open(f"{conn.base}/chat.py", timeout=30).read()
        compile(new, path, "exec")   # never swap a working client for a broken download
        with open(path, "rb") as f:
            same = f.read() == new   # e.g. the host, whose chat.py is the one being served
        if not same:
            with open(path + ".new", "wb") as f:
                f.write(new)
            os.replace(path + ".new", path)
    except Exception as e:  # noqa: BLE001
        print(f"Update failed ({e}); starting this version again.", flush=True)
        time.sleep(2)
    conn.close()
    if INSTANCE:   # hand the one-window slot to the new version
        INSTANCE.close()
    # this process just waits for the new one, which owns the window (and Ctrl+C) from now on
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    args = sys.argv[1:] + ([] if "--here" in sys.argv else ["--here"])
    sys.exit(subprocess.call([sys.executable, path, *args]))


def main():
    if reopen_in_windows_terminal():
        return
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    base, token = parse_args([a for a in sys.argv[1:] if not a.startswith("--")])
    if not claim_instance(base, token):
        print("Claude Room is already open: brought it to the front.")
        time.sleep(1.2)
        return
    terminal = enable_vt()
    events = queue.Queue()
    conn = Connection(base, token, events, share_pc="--no-pc" not in flags)
    screen = Screen()
    screen.conn, screen.events, screen.auto = conn, events, "--auto" in flags or load_auto()
    screen.setup()

    posix_state = None
    if os.name != "nt":
        import codecs
        import termios
        import tty
        fd = sys.stdin.fileno()
        posix_state = (fd, termios.tcgetattr(fd))
        tty.setcbreak(fd)
        decoder = codecs.getincrementaldecoder("utf-8")()

    last_typing_post = 0.0
    last_tick = 0.0
    # lag report: once at start, then whenever the screen froze for more than 0.3 s
    # (the server writes it to room.log, so the host can see what is slow on this PC)
    threading.Thread(target=refresh_launcher, args=(conn, events), daemon=True).start()
    threading.Thread(target=lock_reporter, args=(conn, events), daemon=True).start()
    LEASE_CONN[0] = conn
    threading.Thread(target=log_watcher, args=(conn,), daemon=True).start()
    threading.Thread(target=activity_reporter, args=(conn, screen), daemon=True).start()
    conn.post("/diag", {"text": f"start: Python {platform.python_version()}, {platform.system()} "
                                f"{platform.release()}, {terminal}, {os.cpu_count()} CPUs"})
    stalls, last_report = [], time.time()
    typing_loops, keys_max, last_typing_report = [], 0, time.time()
    try:
        while True:
            loop_start = time.time()
            n_keys = n_events = 0
            dirty = False
            keys = keys_windows(screen.vt_mode is not None) if os.name == "nt" else keys_posix(posix_state[0], decoder)
            for ch, more in keys:
                if screen.key(ch, conn, more) and time.time() - last_typing_post > 0.8:
                    conn.post("/typing")
                    last_typing_post = time.time()
                n_keys += 1
                dirty = True
            while True:
                try:
                    screen.handle(events.get_nowait())
                except queue.Empty:
                    break
                n_events += 1
                dirty = True
            screen.resize()
            if screen.approvals and screen.expire_approvals():
                dirty = True
            if time.time() - last_tick > 0.5:   # keep timers and typing hints fresh
                last_tick, dirty = time.time(), True
            draw_start = time.time()
            if dirty:
                screen.draw_bottom()
            now = time.time()
            if n_keys:
                typing_loops.append(now - loop_start)
                keys_max = max(keys_max, n_keys)
            if typing_loops and now - last_typing_report > 30:
                ms = sorted(int(x * 1000) for x in typing_loops)
                conn.post("/diag", {"text": f"typing: {len(ms)} redraws, ms per keystroke p50 {ms[len(ms) // 2]} "
                                            f"p95 {ms[int(len(ms) * .95)]} max {ms[-1]}, up to {keys_max} keys "
                                            f"queued at once"})
                typing_loops, keys_max, last_typing_report = [], 0, now
            if now - loop_start > 0.3:
                stalls.append(f"{now - loop_start:.1f}s (drawing {now - draw_start:.1f}s, "
                              f"{n_keys} keys, {n_events} events)")
            if stalls and now - last_report > 20:
                conn.post("/diag", {"text": f"screen froze: {'; '.join(stalls[-5:])} · "
                                            f"recent POSTs {conn.post_ms[-5:]} ms"})
                stalls, last_report = [], now
            if n_keys:
                screen.last_key = time.time()
            idle = (not screen.buf and not screen.approvals and not screen.prompt and not screen.pen
                    and time.time() - screen.last_key > 20 and not RUNNING[0])   # never in the middle of an action
            if screen.latest > VERSION and idle and time.time() > screen.update_postponed and not screen.update_at:
                screen.update_at = time.time() + 10
            if screen.update_at and not idle:
                screen.update_at = None   # you started doing something: try again later
            if screen.update_at and time.time() >= screen.update_at:
                screen.want_update = True
            if screen.want_update:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        screen.restore()
        if posix_state:
            import termios
            termios.tcsetattr(posix_state[0], termios.TCSADRAIN, posix_state[1])
    if screen.want_update:
        update_and_restart(conn)


if __name__ == "__main__":
    main()

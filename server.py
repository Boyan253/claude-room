"""Claude Room server: several people chatting with Claude together.

Each person has their own thread ("lane") of Claude turns, so two people's
requests are worked on at the same time. Every lane starts from the shared
conversation and is told what the other lanes asked and answered, and everyone
sees everything. Claude has no tools on this PC; it acts on each person's own
PC through their chat.py (see room_mcp.py), and only for the person whose turn it is.

  python server.py                 run the room
  python server.py invite NAME     add NAME (if new) and print their join info
"""
import base64
import datetime
import glob
import hashlib
import io
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse
from room_access_lock import ai_paused, AI_PAUSE_MESSAGE

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.join(ROOT, "workspace")
CONFIG = os.path.join(ROOT, "room.json")
STATE = os.path.join(ROOT, "state.json")
HISTORY = os.path.join(ROOT, "history.jsonl")
CONNECT_LOG = os.path.join(ROOT, "room.log")
BOARD = os.path.join(ROOT, "board.json")
WHITEBOARD = os.path.join(ROOT, "whiteboard.json")
SNAPSHOT = os.path.join(ROOT, "whiteboard.png")   # drawn by a browser, so Claude can look at it
COLUMNS = [("ideas", "Ideas"), ("backlog", "Backlog"), ("todo", "To do"), ("doing", "Doing"), ("done", "Done")]
LABELS = ("", "red", "yellow", "green", "blue", "magenta", "cyan")
PRIORITIES = ("", "highest", "high", "medium", "low", "lowest")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "auto")
# words that mean real work (English, plus Bulgarian stems: add your own language's here)
AUTO_HEAVY = re.compile(r"implement|build|creat|make|add|fix|bug|debug|refactor|design|rewrite|optimi[sz]|crash|error|"
                        r"doesn'?t work|not working|why|verse|level|system|mechanic|направ|добав|оправ|поправ|бъг|грешк|"
                        r"не работи|защо|създа|напиш|промен|систем|механик|нив|карт|код", re.I)   # real work: Auto thinks on High
MODEL_INFO = {"fable": "the most capable", "opus": "deep, careful work", "sonnet": "fast and smart",
              "haiku": "quickest and lightest", "default": "Claude Code's default",
              "codex": "OpenAI Codex · through the host's Codex bridge"}
STROKE_TYPES = ("pen", "rect", "arrow", "text")
FILES_DIR = os.path.join(ROOT, "files")          # shared files, stored as <id><ext>
FILES_META = os.path.join(ROOT, "files.json")
MAX_FILE = 500 * 1024 * 1024
MAX_FILES_TOTAL = 6 * 1024 * 1024 * 1024   # 17.10: 3 GB filled up (playtest GIFs)
FEEDBACK = os.path.join(ROOT, "feedback.json")   # requests from the room's Claude to its developer
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PINS = os.path.join(ROOT, "pins.json")       # things pinned onto chat messages
PEOPLE_LOG = os.path.join(ROOT, "people.jsonl")   # the people's own chat (Claude doesn't read it)
TIMELINE = os.path.join(ROOT, "timeline.json")   # the project's story: moments with pictures, cards, sketches, notes
TL_KINDS = ("milestone", "feature", "fix", "bug", "idea", "decision", "playtest", "art")
OPERATOR_DIR = os.path.join(ROOT, "operator")   # control threads: requests for the host's Claude Code and its replies
ZONES = os.path.join(ROOT, "zones.json")          # places in the map a thread depends on or works in
ZONE_TTL = 12 * 3600   # a working area or folder lasts 12 h unless it is added again; keep-clear zones stay
NOTES = os.path.join(ROOT, "notes.json")          # each thread's goal and summary, when people were last here, secrets found
REACTIONS = os.path.join(ROOT, "reactions.json")  # message id -> {emoji: [people]}
POLLS = os.path.join(ROOT, "polls.json")
QUIET_DIR = os.path.join(ROOT, "quiet")           # summaries run here: no room rules to load
SECRETS = os.path.join(ROOT, "secrets.json")      # people's own Claude tokens / API keys: never sent to any page
REACTS = ("👍", "❤️", "😂", "🔥", "💀", "👀", "✅", "🤯")
SUMMARY_MODEL = "claude-haiku-5-5"
RECAP_AFTER = 3 * 3600                            # away this long: a "previously on" when you come back
TOOLS_LOG = os.path.join(ROOT, "tools.jsonl")   # each tool call's full input and output (click a tool line to see it)
TOOL_KEEP = 800
SECRET_RE = re.compile(r"(sk-ant-[\w-]{10,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}"
                       r"|tskey-[\w-]{10,}|xox[abpr]-[\w-]{10,}|AKIA[0-9A-Z]{16}|AIza[\w-]{30,})")
PIN_KINDS = ("note", "card", "file", "sketch")
ASPECTS = {"square": (1024, 1024, "1:1"), "wide": (1344, 768, "16:9"), "tall": (768, 1344, "9:16")}
try:
    from PIL import Image   # terminal previews and right-sized copies for Claude; optional
except ImportError:
    Image = None
COLORS = ["cyan", "yellow", "magenta", "green", "blue", "red"]

DEFAULT_CONFIG = {
    "port": 8765,
    "host": "alice",           # the person who runs the room (python chat.py on this PC joins as them)
    "people": {},
    "quiet_seconds": 2.0,     # send once nobody has typed or sent for this long
    "max_wait_seconds": 20,   # ...but never hold a batch longer than this
    "claude_args": [],        # e.g. ["--model", "sonnet"]
    "compact_at": 200000,     # tokens: a thread's conversation bigger than this is compacted (0 = never)
    "public_url": "",         # the room's public address, e.g. a Tailscale Funnel (see funnel.ps1) or a reverse proxy
    "model": "",              # everyone's default model ("" = Claude Code's default)
    "effort": "",             # everyone's default effort ("" = Claude Code's default)
    "prefs": {},              # person -> {"model", "effort"}: their own choice (/model, /effort, the picker)
    "approval_seconds": 120,  # how long someone has to answer y/n before the action is skipped
    "dev_token": "",          # lets the room's developer (whoever maintains this copy) answer requests, see dev.py
    "gemini_api_key": "",     # with a key, pictures come from Gemini ("nano banana") and can be edited
    "gemini_image_model": "gemini-2.5-flash-image",
    "meetings_dir": "",       # Meetings tab: folder of meeting transcripts, one .json (+ .md) each ("" = meetings/ here)
    "meetings_language": "en",   # language of meetings recorded in the room (a Deepgram code: en, de, es, fr, bg, multi, ...)
    "deepgram_api_key": "",   # live transcription of meetings recorded in the room ("" = DEEPGRAM_API_KEY / a .env file)
    "groq_api_key": "",       # meeting summaries ("" = GROQ_API_KEY / a .env file)
    "tenor_api_key": "",      # GIF search in the page ("" = TENOR_API_KEY)
    "codex_bridge": {},       # optional OpenAI Codex engine: {"url": "...", "key": "..."} of a Codex bridge, see README
    "epic_names": {},         # optional: Epic (UEFN) account name -> person, to name who checked in before their chat.py says
}
MODELS = {"fable": "claude-fable-5-1", "opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
          "haiku": "claude-haiku-5-5", "default": "", "codex": "codex"}


CODEX_DEFAULT_MODEL = "gpt-6-astra"
try:
    with open(os.path.join(ROOT, "codex_models.json"), encoding="utf-8") as _catalog_file:
        CODEX_MODELS = {m["id"]: m for m in json.load(_catalog_file)["result"]["data"]}
except (OSError, ValueError, KeyError):
    CODEX_MODELS = {CODEX_DEFAULT_MODEL: {"displayName": "GPT-6-Astra", "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": e} for e in ("low", "medium", "high", "xhigh", "max")]}}
MODELS.update({"codex/" + mid: "codex:" + mid for mid in CODEX_MODELS})
with open(os.path.join(ROOT, "gemini_models.json"), encoding="utf-8") as _gemini_catalog:
    GEMINI_MODELS = json.load(_gemini_catalog)
MODELS.update({info["key"]: "gemini:" + mid for mid, info in GEMINI_MODELS.items()})


def is_gemini_model(model):
    return str(model or "").startswith("gemini:") and str(model).split(":", 1)[1] in GEMINI_MODELS


def gemini_model_id(model):
    if not is_gemini_model(model):
        raise ValueError("Unknown Gemini model")
    return str(model).split(":", 1)[1]


def assistant_name(model):
    return "Gemini" if is_gemini_model(model) else "Codex" if is_codex_model(model) else "Claude"


def is_external_model(model):
    return is_codex_model(model) or is_gemini_model(model)

def is_codex_model(model):
    return model == "codex" or str(model or "").startswith("codex:")

def codex_model_id(model):
    return model.split(":", 1)[1] if str(model).startswith("codex:") else CODEX_DEFAULT_MODEL

def model_label(model_id):
    if is_gemini_model(model_id):
        return GEMINI_MODELS[gemini_model_id(model_id)]["label"]
    if is_codex_model(model_id):
        mid = codex_model_id(model_id)
        return "Codex · " + CODEX_MODELS.get(mid, {}).get("displayName", mid)
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)", model_id or "")
    return f"{m.group(1)} {m.group(2)}.{m.group(3)}" if m else (model_id or "default model")

CLAUDE_MD_VERSION = 25
CLAUDE_MD_END = "<!-- end room rules -->"
CLAUDE_MD = """<!-- room rules v25 -->
# Group chat room

You are in a shared chat room with several people, relayed through a small
server. Each person has their own thread: a turn holds the messages of one
person, tagged with their name,

    [alice]: ...

while the other people's requests are worked on at the same time in parallel
threads (other copies of you, with the same conversation up to the split).
Everyone sees every message and every answer.

Rules:
- Start your answer with a header line that is exactly `[for <name>]`.
- Do what the person in this turn asked. Don't do, redo or finish another
  person's task unless this person asks you to.
- Lines starting with `[room]` come from the server, not from a person:
  - `[room] PCs: ...` who has a PC connected (see below).
  - `[room] meanwhile ...` what the others asked and what their threads
    answered since your last turn. It's context: don't answer it here.
  - `[room] in progress ...` what the other threads are doing right now. Don't
    work on the same files at the same time; if this request clashes with one
    of those, say so instead.
- A person can run several threads of their own at once (shown as
  `alice · Inventory UI`): each is its own conversation with you, all running in
  parallel. The `user` of a PC tool is always the person (alice), never the
  thread. If an action comes back "BUSY: ... your other thread", that person's
  other thread is changing the same file right now: wait for it, or work on
  something else, and say so.
- Address people by name. Output is shown raw in a terminal: keep it
  chat-sized, light markdown only.

## Doing things on people's PCs
You have no access to the PC this room runs on. Your only hands are the room
tools (run_command, read_file, write_file, list_dir), and every call names the
person whose PC it runs on.
- Act only on the PC of the person whose turn it is (the server refuses any
  other PC). Never touch someone's PC because somebody else asked for it.
- The first line of each turn, `[room] PCs: ...`, says who has a PC connected,
  its OS and home folder. Someone shown without a PC (browser only) can't have
  things done on their machine: tell them to join with chat.py instead.
- The owner approves every action on their side. If they deny it, accept that.
- Windows commands run in PowerShell, others in bash.

## Task board and whiteboard
The room page has shared task boards (several, each with its own columns, by
default Ideas, Backlog, To do, Doing, Done; cards have #numbers, assignees,
labels, priorities) and
a whiteboard, live for everyone. Neither needs anyone's approval.
- Use the board_* tools when people agree on tasks, ask you to track or update
  something, or ask what's left. Short card titles, details in the note.
  Assign a card only when it's clearly someone's. board_update can move a card
  to another board/column or to a position (1 = top); board_manage creates,
  renames and deletes boards and columns when people ask.
- When someone mentions a drawing, a sketch or the whiteboard, look at it with
  whiteboard_view before answering.

## UEFN editor through the room (nobody needs Claude Code for this)
When a person's chat.py shares their PC, you can drive the UEFN editor open on
their PC through Epic's UEFN MCP there (127.0.0.1:8000): uefn_list_tools shows
what it offers (pass a name for one tool's full schema), uefn_call runs one.
Edits to a UEFN project take that file's Lore lock automatically (chat.py
enforces it): if an action comes back "LOCKED by X", the other person's AI is
editing that file. Tell the person who asked and do something else; never work
around it. run_command can't change project files (refused): use write_file or
the Verse MCP tools. After the person clicks Check In in UEFN, chat.py frees the
locks by itself (/release in chat.py frees them at once). Keep a
feature in its own .verse file when you can, so two people can work at once.
Same rules as any PC action: their turn, their PC, their approval. For a long
editor session, tell them /auto in chat.py saves answering y every time. If
UEFN isn't open or its MCP is off, say so (Project Settings: Python Editor
Scripting + UEFN MCP Toolsets).

## Files
People share files in the room (Files tab on the page, 📎 in the chat, /send
in chat.py). files_list shows them. file_get saves a shared file onto the PC
of the person whose turn it is; file_put shares a file from their PC. To get
a file from one person to another: the owner shares it in their turn, the
other saves it in theirs.

## Pictures
- image_generate makes a picture and posts it in the chat (and Files). Write
  the prompt in English: subject, setting, style, light, camera. aspect is
  square, wide or tall. edit_file_id changes an existing picture (only when
  the room has a Gemini key; otherwise say so).
- People paste (Ctrl+V) or drop pictures into the room. A `[room] X shared:`
  line lists what they shared since their last message: look at pictures with
  file_view before answering about them. People can also type /image.

## Pins
People pin notes, board cards, pictures/files and little sketches onto chat
messages, your answers included ("this part works", the card it finished, a
screenshot of the result). `[room] pinned onto chat messages` lines tell you
what was pinned since your last turn: treat them as their notes on the
conversation. pin_add pins something onto your current answer: the card you
just finished, the picture you made, a one-line "how to test this" note. Use
it when it will help them find things later, not on every answer.

## Timeline
The page has a Timeline tab: the project's story, moment by moment (milestone,
feature, fix, bug, idea, playtest, art), each with pictures, cards, sketches,
notes and files attached. People add moments from chat messages and cards, and
a card reaching Done adds itself. timeline_read shows it. When you finish a
fix or a feature, a playtest result comes in, or a level or milestone is
reached, add it with timeline_add: a short title, a line or two of details,
and the pictures that go with it (file_ids: screenshots people shared, before
and after for a fix, concept art) plus its card. timeline_update changes a
moment or adds pictures to it later. One moment per thing worth remembering,
not every small step.

## Thread goals, reactions, polls
- `[room] what X set this thread for:` is that thread's goal (its first message
  unless they changed it): keep working toward it.
- `[room] reactions to your answers` are quick feedback from the people (🔥
  great, 💀 broke something or scary, 👀 looking at it, ✅ done). Take them into
  account; don't comment on each one.
- poll_create asks everyone to choose (2 to 6 short options) when the people
  have to decide between options. `[room] polls` lines show the votes.
- "▶ continue" means they stopped your last answer and want you to carry on
  from where it was cut off.
- A message starting `[from the people chat]` forwards something the people
  said to each other (you don't see that chat otherwise): take it as context
  from the person who forwarded it.

## Undo, reminders, what people are doing
- `↶ X undid Claude's change to FILE` means that person put the file back the
  way it was before your write: don't write it again unless they ask.
- reminder_add sets a reminder when people ask to be reminded ("remind us at
  18:00 to playtest"): when (18:00, 30m, 2h, tomorrow 10:00), what, and who
  (all, me = the person whose turn it is, or a name).
- The `[room] PCs:` line says what each PC is doing right now (UEFN open,
  playtesting in Fortnite, away from the keyboard). Use it: don't drive the
  UEFN editor while they are playtesting, and say so instead.

## Threads working side by side
- `[room] threads:` lists what the other threads are doing (only when that
  changed since your last turn), and `[room] files the other threads are
  changing` names files you must leave alone right now: say so instead.
- threads_read shows every thread's goal, summary and state on demand.
  thread_note tells another thread something it needs (a file you changed that
  it uses, a decision, a question); if it is working, it gets the note at once.
  Use it when the threads' work touches; don't chat for nothing.
- A message marked `(added while you work)` came in during your task: fold it
  into what you're doing (change course if it asks), don't start over.

## How you keep track of work (the room enforces the first step)
- Before you change anything on a PC (write_file, or a uefn_call that changes
  the editor), this thread needs its task card. board_read: if the work is
  already a card, board_update it to Doing, assigned to the person; if not,
  board_add it in Doing. A plain title anyone gets at a glance ("The
  door opens when you walk up to it", not "Fix BP_Door trigger") and a
  short note: what's wrong or wanted, why, how to check it. The room pins the
  card to this thread for you. Without one, changes come back "TASK FIRST".
- While you work: thread_pin the pictures that show the problem or the result
  (screenshots, references), and a note for anything this thread must
  remember (a decision, a file, a rule the person gave you). thread_unpin what
  no longer matters. Pin when it will help later, not every step.
- When it works: board_update the card to Done. That puts it on the timeline
  with the pictures this thread pinned for it (the reply says the moment's id;
  timeline_update adds more). A new version of something gets its own moment:
  never retitle an older moment. Then say in one line how to test it.
- Questions and small talk: no card. Several small asks in one message: one
  card for the piece of work, not one per sentence.

## Several threads on one PC: take turns with UEFN
- The UEFN playtest belongs to one thread at a time. The first thread that
  calls StartSession / StartGame / PushChanges holds it. Until it is free, the
  person's other threads get "HELD" for session calls, BuildAll, imports and
  editor changes; reading, files outside the editor and planning still work.
- Holding it: test, look (screenshot), then StopSession so the others can go
  on. Leave it running only when the person must look at it, and say so: they
  free it with Done testing. Never stop, restart or push another thread's
  session.
- Never save_assets without asset_paths: list exactly what you changed. Saving
  everything also saves the other threads' half-done work.
- Build errors in files another thread is changing are theirs: don't touch
  those files; thread_note that thread if it blocks you.
- `[room] ... arrived while you work` at the end of a tool result is a message
  from the person or a note from another thread: read it before your next step.
- A message from a person that reaches you while you work ("(added while you
  work)" or the arrived-while-you-work lines): answer it at once in one short
  line before your next tool call ("Got it, checking the spawn now"), so they know
  it arrived; then go on. Notes from other threads need no reply unless asked.

## Project sync (Lore): never build on an old copy
- Each person has their own copy of the UEFN project. Before any change to it,
  chat.py checks the copy is up to date with the shared one; if it's behind,
  the change comes back "OUT OF DATE". Stop, tell the person to click Sync
  Latest in UEFN (Revision Control), and wait. Never work around it.
- `[room] project check-ins` lines say what was checked in (and by whom) since
  your last turn: read what changed before you edit related files, and build on
  it; don't redo or undo the other person's work. project_status shows the
  copies and recent check-ins on demand.
- When your change works (Verse builds, quick check done): save it
  (save_assets), then remind the person to Check In with a one-line
  description. Don't leave bigger changes un-checked-in.

## Looking at their screen
screenshot takes a picture of a person's screen (on their PC, with their
approval) and shares it in the chat; look at it with file_view. Use it to check
something visible you just changed in UEFN (lighting, placed actors, UI) before
calling it done, or when they say "look at this". Not for every step.

## Meetings
The page has a Meetings tab: the meetings the people recorded in the room or
with a notetaker app (automatic transcripts with speakers and times, a summary
with decisions and tasks, usually the audio).
`[room] meetings on record` lists them. When someone refers to what was said,
decided or asked in a meeting ("what did we say in the meeting about…", the
call, the stand-up), look it up before answering: meetings_search finds the moments,
meeting_read reads a summary or a stretch of transcript. `[room] from the
meetings` lines are moments the room found for the message: check them, don't
assume. Quote a moment as meeting:<id> @mm:ss so they can click it and hear
it. A message with `📝 Meeting … (meeting:<id> @mm:ss)` points at that meeting
and moment. Transcripts have recognition errors: go by the meaning, and say
when something isn't in the meetings instead of guessing.

## Places in the map: zones
Several threads (and several people) build in one map, so a thread can break
another's work just by putting something in the wrong place (a mesh whose
collision covers the spawn pads sends every player to the wrong place). So:
- zone_add a keep_clear zone for every spot your work depends on: spawn pads,
  teleport / arrival points, a prop the camera looks at, a camera path. Its
  box in UEFN centimetres (min/max x, y, z). Nobody else can then place or move
  anything into it (their change comes back "ZONE").
- zone_add an area zone for the part of the map you are building, and a folder
  zone for the Content folder that is yours. Others get a warning there; when
  you get one, tell the owner (thread_note) what you changed.
- zones_read before you place things; zone_check a box when in doubt. After
  importing a mesh, give it proper collision (not one convex hull over a big
  area) and check its bounds don't reach into someone's keep_clear zone.
- `[room] ... playtest log` lines are errors from the running playtest (failed
  teleports, Verse runtime errors): fix them if they come from your change.
- Check In, Sync Latest and closing UEFN wait until the person's other threads
  are done (they'd ship or lose their half-done work).

## Working as a team (you can split work yourself)
- A task with parts that can go side by side (a building's props and its doors;
  a model in Blender while the Verse is written): thread_spawn a sub-thread
  per part, each with a clear scope, its own place (zone / files) and how to
  check it. It starts at once with your card; its reports come back to you,
  and you steer it with thread_note. You stay the one who checks the parts
  fit, playtests the whole and moves the card to Done. thread_close a
  sub-thread when its part is done. Don't split small jobs: one part = one
  thread.
- Inside one turn you also have the Agent tool: subagents for parallel
  reading, searching and research (logs, many files, references). Changes on
  a PC go through you or a sub-thread, not through subagents.
- Keep your checklist with plan_update (todo / doing / done) for anything
  with more than 3 steps: the people see where you are without asking.
- lesson_add when you learn something every thread must know so it never
  happens again (one concrete line: "an imported mesh gets one convex hull
  by default: set complex collision before placing it near spawns").
- The room tidies up: a card that reaches Done is unpinned from every thread
  (its pictures go onto its timeline moment). Idle threads stay visible; after
  the idle period they get an archive warning. Only the owner confirms archiving,
  and can reopen it later. So pin what this work needs, and finish cards when they're done.

## Lessons the threads learned
@lessons.md

## House rules
The people's own rules for this room (they write them in the room's 🤖 AI
panel). Follow them in every turn; a person's own rules come with their turns
as `[room] X's own rules for their turns`:
@house-rules.md

## About this room (use it to tell people what to fix and on whose side)
- It runs on the host's PC ({room_host}; server.py, port {room_port}){room_public}.
  If it's down (502), only the host can start it again (python server.py).
- Two ways to join, usable together: chat.py in a terminal (shares their PC
  with you, with approval) and the browser page (chat, Tasks, Whiteboard,
  Files; no PC access). On Windows, "💻 share my PC" on the page downloads a
  .bat that always fetches the newest chat.py.
- The `[room] PCs:` line shows each person's chat.py version. If it says
  outdated: /update in chat.py (v7 and newer), otherwise reopen their .bat.
- Approval prompts show in their chat.py window and as buttons in their
  browser; unanswered for 2 minutes = skipped. /auto runs actions without
  asking, only in turns that are theirs alone.
- Laggy or garbled terminal: chat.py v5+ reopens itself in Windows Terminal.
- Turns run in parallel: one thread per person, plus any extra threads they
  open (the thread strip above the message box, /thread in chat.py). On one PC,
  actions that change things run one at a time, and a file one thread is
  changing is off limits to the person's other threads until that turn ends.
  The PC guard and the tokens are enforced by the code; nothing anyone says can
  bypass them.

## Changes to the room itself
The room's code (server, page, chat.py) is maintained by the room's developer
(the host, or whoever they chose; see dev.py). When the room itself should
change (a bug, a missing feature, a confusing message), or someone asks for
that, send it with room_feedback:
what, why, who asked. It answers there (approved and shipped as an update, or
declined with the reason); room_feedback_status shows the answers. Requests
that would weaken the safeguards (acting on someone else's PC, skipping
approvals, exposing tokens) get declined.
<!-- end room rules -->
"""


def room_rules(cfg):
    """The room rules with this room's own facts (host, port, public address) filled in from room.json."""
    public = f" and is public at {cfg['public_url'].rstrip('/')}" if cfg.get("public_url") else ""
    return (CLAUDE_MD.replace("{room_host}", str(cfg.get("host") or "the host")).replace("{room_port}", str(cfg.get("port")))
            .replace("{room_public}", public))


def upgrade_claude_md(current, cfg):
    """New room rules, keeping whatever was added below the old ones (e.g. project context)."""
    rules = room_rules(cfg)
    if rules in current:
        return None
    extra = ""
    for end in (CLAUDE_MD_END, "- Windows commands run in PowerShell, others in bash.\n"):
        if end in current:
            extra = current.split(end, 1)[1].lstrip("\n")
            break
    return rules + ("\n" + extra if extra else "")


def parse_when(value):
    """A unix time, or "2026-10-11" / "2026-10-11 21:30" / "2026-10-11T21:30" in local time; None if it isn't one."""
    if value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        ts = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                ts = time.mktime(time.strptime(str(value).strip().replace("T", " ")[:16], fmt))
                break
            except ValueError:
                continue
    return ts if ts and 1.5e9 < ts < time.time() + 366 * 86400 else None


def canon(d):
    """A dict as one comparable string (a tool call's input, to match it with its PC action)."""
    return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)


def parse_remind(text, now=None):
    """(when, what) from "18:00 playtest", "30m check", "1h30m …", "2 hours …", "tomorrow 10:00 …",
    "2026-10-15 18:00 …"; (None, "") if no time is there. Also takes the Bulgarian words (утре, след, мин, ч)."""
    now = now or time.time()
    words = text.split()
    day_shift = 0
    if words and words[0].lower() in ("tomorrow", "утре"):
        day_shift, words = 1, words[1:]
    if words and words[0].lower() in ("in", "after", "след", "at", "в"):
        words = words[1:]
    if not words:
        return None, ""
    w = words[0].lower()
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", w)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        t = time.localtime(now)
        at = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + day_shift, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return (at + 86400 if at <= now else at), " ".join(words[1:])
    if day_shift:
        return None, ""
    m = re.fullmatch(r"(?:(\d+)(?:h|ч))?(?:(\d+)(?:m|min|мин))?", w)
    if m and (m.group(1) or m.group(2)):
        return now + int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60, " ".join(words[1:])
    if w.isdigit() and len(words) > 1:
        unit = words[1].lower()
        if unit.startswith(("min", "мин")):
            return now + int(w) * 60, " ".join(words[2:])
        if unit.startswith(("h", "час", "ч")):
            return now + int(w) * 3600, " ".join(words[2:])
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w) and len(words) > 1:
        at = parse_when(f"{w} {words[1]}")
        if at:
            return at, " ".join(words[2:])
    return None, ""


def save_json_safely(path, data):
    """Write to a temp file first, so a crash mid-write never leaves a broken board."""
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def clean_text(value, limit):
    return str(value or "").strip()[:limit]


def clean_stroke(stroke, who):
    """A whiteboard item from a browser, checked: pen = points, rect/arrow = two corners, text = one point."""
    if not isinstance(stroke, dict) or stroke.get("type") not in STROKE_TYPES:
        return None
    pts = stroke.get("pts")
    if not isinstance(pts, list) or not pts or len(pts) > 8000 or len(pts) % 2:
        return None
    try:
        pts = [round(float(v), 1) for v in pts]
        size = min(60.0, max(1.0, float(stroke.get("size", 4))))
    except (TypeError, ValueError):
        return None
    if any(v != v or abs(v) > 1e7 for v in pts):   # NaN or absurd
        return None
    color = str(stroke.get("color", ""))
    out = {"id": clean_text(stroke.get("id"), 64) or secrets.token_hex(6), "type": stroke["type"],
           "color": color if re.fullmatch(r"#[0-9a-fA-F]{6}", color) else "#e8eaed", "size": size, "pts": pts,
           "by": who}
    if stroke["type"] == "text":
        out["text"] = str(stroke.get("text", ""))[:1000]
        if not out["text"].strip():
            return None
    return out


def image_preview(path, cols=48, rows=26):
    """A tiny RGB grid of a picture, for terminals to draw with half blocks (2 pixels per character)."""
    if not Image:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((cols, rows))
            return {"w": im.width, "h": im.height, "px": im.tobytes().hex()}
    except Exception:  # noqa: BLE001 - no preview is fine
        return None


POLLINATIONS_LOCK = threading.Lock()
POLLINATIONS_LAST = [0.0]


def pollinations_image(prompt, aspect):
    """Free, no key. Without an account it allows about one picture per 30-45 s (else HTTP 402),
    so requests queue here and wait their turn."""
    w, h, _ = ASPECTS.get(aspect, ASPECTS["square"])
    with POLLINATIONS_LOCK:
        for attempt in range(6):
            time.sleep(max(0.0, 35 - (time.time() - POLLINATIONS_LAST[0])))
            url = (f"https://image.pollinations.ai/prompt/{quote(prompt)}?width={w}&height={h}&nologo=true"
                   f"&model=flux&seed={secrets.randbelow(10 ** 6)}")
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "claude-room"}),
                                            timeout=150) as r:
                    data = r.read()
                POLLINATIONS_LAST[0] = time.time()
                break
            except urllib.error.HTTPError as e:
                POLLINATIONS_LAST[0] = time.time()
                if e.code not in (402, 429) or attempt == 5:
                    raise RuntimeError(f"the free image service said HTTP {e.code}") from e
    if not data.startswith(b"\xff\xd8") and not data.startswith(b"\x89PNG"):
        raise RuntimeError("the image service sent something that isn't a picture")
    return data, ".jpg" if data.startswith(b"\xff\xd8") else ".png"


def gemini_image(key, model, prompt, aspect, source=None):
    """Gemini image generation ("nano banana"); with `source` (bytes, mime) it edits that picture."""
    parts = [{"text": prompt}]
    if source:
        parts.append({"inlineData": {"mimeType": source[1], "data": base64.b64encode(source[0]).decode()}})
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                 "imageConfig": {"aspectRatio": ASPECTS.get(aspect, ASPECTS["square"])[2]}}}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            reply = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini said {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from e
    for cand in reply.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            data = part.get("inlineData") or part.get("inline_data")
            if data:
                raw = base64.b64decode(data["data"])
                return raw, ".png" if raw.startswith(b"\x89PNG") else ".jpg"
    raise RuntimeError("Gemini returned no picture: " + json.dumps(reply)[:300])


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


COMPACT_HOW = ("Keep what this room needs to carry on: who asked for what, decisions and their reasons, open tasks, "
               "file paths, UEFN actor/asset names and positions, what was tried and failed. Drop raw tool output.")


def last_context(session_id):
    """Tokens the last request of a saved Claude Code session carried (its conversation size), from its file."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    paths = glob.glob(os.path.join(base, "projects", "*", session_id + ".jsonl"))
    size = None
    for line in open(paths[0], encoding="utf-8", errors="replace") if paths else []:
        if '"usage"' not in line and "compact_boundary" not in line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
            meta = o.get("compactMetadata") or o.get("compact_metadata") or {}
            size = meta.get("postTokens") or meta.get("post_tokens") or 0
        u = (o.get("message") or {}).get("usage") if o.get("type") == "assistant" else None
        if u:
            size = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return size


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_EVERY = 150   # seconds between looks at the host's plan usage (and right after every turn)
USAGE_WARN = (80, 95)   # percent: everyone gets a notification when a limit crosses these


HOST_CLAUDE_CONFIG = ""   # room.json host_claude_config: the config dir of the personal account (host_oauth_token's login)
# The host's two Claude plans; the Usage panel switches between them (room.json host_account).
# personal = host_oauth_token (claude setup-token); team = the PC's own ~/.claude login (team plan).
HOST_ACCOUNTS = {"personal": "Personal", "team": "Team"}
TEAM_STOP_DEFAULT = 35   # percent: on the team plan, Claude stops once its week reaches this (room.json team_stop_percent)
TEAM_NEAR = 5            # percent below the stop line where the room looks at usage more often
TEAM_NEAR_EVERY = 30     # seconds between those looks


def credentials_path(account="personal"):
    if account == "team":
        base = os.path.join(os.path.expanduser("~"), ".claude")
    else:
        base = HOST_CLAUDE_CONFIG or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, ".credentials.json")


def iso_ts(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fetch_usage(account="personal"):
    """The host's Claude plan limits: the numbers Claude Code's /usage shows, read with Claude's own login."""
    with open(credentials_path(account), encoding="utf-8") as f:
        login = json.load(f).get("claudeAiOauth") or {}
    req = urllib.request.Request(USAGE_URL, headers={"Authorization": "Bearer " + login.get("accessToken", ""),
                                                     "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-room"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    limits = []
    raw = data.get("limits") or [{"kind": k, "percent": (data.get(w) or {}).get("utilization"),
                                  "resets_at": (data.get(w) or {}).get("resets_at")}
                                 for k, w in (("session", "five_hour"), ("weekly_all", "seven_day")) if data.get(w)]
    for lim in raw:
        if lim.get("percent") is None:
            continue
        kind = lim.get("kind", "")
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        label = {"session": "Session", "weekly_all": "Week"}.get(kind) or (f"Week ({model})" if model else kind)
        limits.append({"kind": kind if kind != "weekly_scoped" else "weekly_" + (model or "scoped").lower(),
                       "label": label, "model": model or "", "percent": round(float(lim["percent"])), "resets": iso_ts(lim.get("resets_at")),
                       "window": 5 * 3600 if kind == "session" else 7 * 86400})
    tier = str(login.get("rateLimitTier", ""))
    plan = re.search(r"max_(\d+x)", tier)
    return {"limits": limits, "plan": f"Max {plan.group(1)}" if plan else str(login.get("subscriptionType", "")).title()}


def until_text(ts):
    """'in 3 h 12 min (00:59)' / 'Fri 01:59 (in 5 d)'."""
    if not ts:
        return "?"
    left = max(0, ts - time.time())
    if left < 86400:
        h, m = int(left // 3600), int(left % 3600 // 60)
        return f"in {f'{h} h ' if h else ''}{m} min, at {time.strftime('%H:%M', time.localtime(ts))}"
    return f"{time.strftime('%a %H:%M', time.localtime(ts))}, in {left / 86400:.0f} d"


def clip(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def placeholder_token(token):
    """An empty or example token (room.example.json's "<token>"): replaced by a random one on start."""
    token = str(token or "").strip()
    return not token or token.startswith("<") or token.lower() in ("token", "changeme", "change-me")


def load_config():
    first_run = not os.path.exists(CONFIG)
    saved = {}
    if not first_run:
        try:
            with open(CONFIG, encoding="utf-8-sig") as f:   # -sig: a BOM from some editors is fine
                saved = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit(f"room.json can't be read ({e}); fix it or start from room.example.json")
    cfg = {**DEFAULT_CONFIG, **saved}
    cfg["people"] = {str(n).lower(): t for n, t in (cfg.get("people") or {}).items()}
    cfg["host"] = str(cfg.get("host") or "alice").lower()
    missing = [n for n in [cfg["host"]] if n not in cfg["people"]]
    fresh = [n for n, t in cfg["people"].items() if placeholder_token(t)]
    for name in missing + fresh:
        cfg["people"][name] = secrets.token_urlsafe(12)
    new_dev_token = placeholder_token(cfg.get("dev_token"))
    if new_dev_token:
        cfg["dev_token"] = secrets.token_urlsafe(18)
    if first_run or missing or fresh or new_dev_token:
        save_json(CONFIG, cfg)
    return cfg


def lan_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    # Tailscale (100.x) first: that is the address people outside the LAN usually use
    return sorted(ips, key=lambda ip: (not ip.startswith("100."), ip))


def join_info(cfg, name):
    token = cfg["people"][name]
    lines = [f"Join info for {name}:"]
    bases = [cfg["public_url"].rstrip("/")] if cfg.get("public_url") else []
    bases += [f"http://{ip}:{cfg['port']}" for ip in lan_ips()]
    for base in bases:
        lines.append(f"  browser : {base}/?t={token}")
        lines.append(f"  terminal: curl.exe -o chat.py {base}/chat.py  then  py chat.py {base} {token}")
    return "\n".join(lines)


MIN_PC_VERSION = 3   # oldest chat.py that can share a PC
MIN_CHANGE_VERSION = 14   # oldest chat.py allowed to change things on a PC (it enforces Lore locks)
RELEASE_NOTES = {   # shown with "update available" in the browser and the terminal
    56: "Gemini 4 Argon, 3.1 Pro, 3.8 Flash and 3.6 Flash are available alongside Claude and Codex, with model-specific thinking levels and the same room tools and PC permissions.",
    13: "Premium look, Trello-style board with #numbers, Files view in the terminal, the browser's Update button updates your terminal too.",
    14: "Lore locks are enforced for everything the room changes in your UEFN project; old terminals can only read.",
    15: "Notifications (🔔), open your terminal from the browser, live 🔒 lock list with Ask to release, locks free themselves after Check In, terminals update themselves when idle.",
    16: "Locks are checked by what a command actually changed: a change to a file someone else holds is undone.",
    17: "Pick your own model and effort (the pill by the message box), several boards with your own columns, card positions, priorities, cover pictures, Ctrl+K search.",
    18: "Tool calls are a tidy list now: click one to see its full input and output (terminal: Ctrl+O or /tool N). Claude's waiting line has fun words.",
    19: "Threads: open more than one conversation with Claude and run them at the same time (the strip above the message box, /thread in chat.py).",
    27: "💬 a chat just for the people (side panel; /p in the terminal), and the project is never changed from an old copy: Claude's changes wait until you Sync Latest, and every check-in is announced.",
    26: "📸 /shot puts a picture of your screen in the chat (and Claude can ask for one to check its work in UEFN), 🔎 Ctrl+K searches the whole chat, 🎙 talk instead of typing (browser), Auto effort (/effort auto): quick questions answer faster.",
    25: "↶ Undo under Claude's file changes (your PC keeps the old file), who's doing what next to each name (UEFN, playtesting, away), ★ saved messages (/saved), ⏰ reminders (/remind 18:00 playtest).",
    24: "Threads show what they're for and what they did (☰ Overview, /threads all) · reactions · polls (/poll, /vote) · a 'Previously on…' when you come back (/recap) · ▶ Continue after a Stop, also on another model (/continue opus).",
    23: "Timeline tab: the project's story with pictures, cards, sketches and notes. Add a moment from any chat message (📌 → Timeline) or card; cards reaching Done add themselves. /timeline in the terminal.",
    22: "Long conversations compact themselves (above 200k tokens; /compact does it now). Every request re-reads the whole conversation, so this saves a lot of usage. Sizes are in the usage panel.",
    21: "Claude usage in the top bar: how much of the session and the week is used, and when each resets (terminal: status line and /usage).",
    46: "A message you send while Claude works now shows between what it already said and what it says next, like in Claude Code; pictures show in their own thread too.",
    45: "/compact_at N (host) changes when long threads are compacted, without a restart.",
    44: "Approvals survive a terminal restart (they're asked again in the reopened window), and you can take a message back: 🗑 on your own message (or /unsend for your last one) also stops Claude working on it.",
    43: "The status line under the message box shows only your own working threads, with a tip about the room while one or none works.",
    42: "A playtest or editor change on one person's PC is only reported to that person's threads, so the other person's threads no longer think they must wait or stop a playtest.",
    41: "The Claude usage warning comes once per threshold (80%, 95%) and window instead of every few minutes.",
    40: "/operator turns a thread into a control thread answered by the host's own Claude Code (for things like reverting a check-in); /operator off switches it back.",
    39: "✅ for a finished task now sits on the AI's answer; mute the approval sound just for you (Settings, or /sound approvals off in chat.py); the host can clear the people chat (🧹).",
    38: "A thread's goal now comes from its card; the terminal never updates itself in the middle of an action, and commands that only read no longer wait behind another thread's long change.",
    37: "One compact row above the message box: a thread switcher with every thread (and how many work or finished), pins behind 📌, ⤢ for the full rows.",
    36: "Finished cards leave the pins; idle threads stay visible with an archive warning until their owner confirms.",
    35: "Threads work as a team: a thread can open its own sub-threads for parts of a task (you see them and can talk to them), get their reports back, keep a checklist, and save lessons every thread learns from.",
    33: "Zones: threads mark the spots they depend on (spawns, the key, camera marks) and nobody can build over them; the playtest log is watched; Check In, Sync and closing UEFN wait while other threads work.",
    32: "📝 Meetings tab: every recorded meeting with its summary, transcript and audio, new meetings recorded right in the room, drag a meeting or a line into the chat, and Claude looks up what was said in meetings by itself.",
    31: "Threads take turns with UEFN: one playtest at a time, the others wait (Done testing frees it). Codex hears your messages while it works. Work starts as a card on the board, pinned to its thread.",
    30: "Smoother chat: switching threads keeps your place, the Overview stops jumping to the top, answers don't flash while they stream, and auto mode no longer asks you in the browser.",
    20: "Delete messages (🗑 on hover: the host any, everyone else their own) and ■ Stop on Claude's running answer (Esc Esc on an empty line).",
}
CHANGING_TOOLS = ("write_file", "run_command", "uefn_call", "file_get")
READ_UEFN = re.compile(r"^(get|list|find|describe|read|query|search|inspect|count|is|has|can|lookup|print|dump|validate)", re.I)   # same as chat.py
ROOM_VERSION = 56   # shown on the page, next to the name and in Settings
BROWSER_ANSWER_VERSION = 5   # oldest chat.py that accepts y / n / a clicked in the browser
UPDATE_COMMAND_VERSION = 7   # oldest chat.py with /update


def describe(tool, args):
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
    if tool == "screenshot":
        return "take a picture of your screen and share it in the room"
    verb = {"read_file": "read", "write_file": "write", "list_dir": "list"}.get(tool, tool)
    return f"{verb}: {args.get('path', '')}"


def web_version():
    """Fingerprint of web.html, so open browser tabs notice a new page."""
    with open(os.path.join(ROOT, "web.html"), "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:10]


GOOD_CLIENT = {"mtime": None, "body": None, "version": 0}


def good_client():
    """chat.py as handed out: the file on disk if it compiles, else the last copy that did, so a
    half-finished edit never reaches anyone (their .bat downloads it on every start)."""
    path = os.path.join(ROOT, "chat.py")
    mtime = os.path.getmtime(path)
    if mtime != GOOD_CLIENT["mtime"]:
        with open(path, "rb") as f:
            body = f.read()
        try:
            compile(body, "chat.py", "exec")
        except (SyntaxError, ValueError) as e:
            print(f"chat.py doesn't compile ({e}); still serving the last good copy")
        else:
            m = re.search(rb"^VERSION = (\d+)", body, re.M)
            GOOD_CLIENT.update(body=body, version=int(m.group(1)) if m else 0)
        GOOD_CLIENT["mtime"] = mtime
    return GOOD_CLIENT["version"], GOOD_CLIENT["body"]


def client_version():
    """VERSION of the chat.py this server hands out; older clients can't share a PC."""
    return good_client()[0]


JOIN_BAT = r"""@echo off
chcp 65001 >nul
title Claude Room - {name}
set "ROOM={base}"
set "DIR=%USERPROFILE%\claude-room"
if not exist "%DIR%" mkdir "%DIR%"
echo Downloading the latest chat.py ...
curl.exe -sSf -o "%DIR%\chat.py" "%ROOM%/chat.py"
if errorlevel 1 (echo Download failed. & pause & exit /b 1)
set "PY=py"
where py >nul 2>nul || set "PY=python"
where %PY% >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ & pause & exit /b 1)
%PY% "%DIR%\chat.py" "%ROOM%" {token}
if errorlevel 1 pause
"""


def find_claude():
    path = shutil.which("claude")
    if not path:
        sys.exit("claude CLI not found on PATH")
    return path


class Room:
    def __init__(self, cfg):
        self.cfg = cfg
        self.claude = find_claude()
        self.agents = {}             # name -> {"q": queue, "info": {...}} for chat.py clients sharing their PC
        self.execs = {}              # id -> {"user", "lane", "started"/"done": Event, "result"}
        self.exec_tokens = {}        # secret handed to one running turn's room_mcp.py -> whose turn it is
        self.lock = threading.Condition()
        self.clients = []            # (name, queue)
        self.conn_info = {}          # queue -> {"kind": "chat.py"/"browser", "version": int}
        # one lane per person: their messages queue up for their own next turn
        self.pending = {}            # name -> [texts]
        self.first_pending = {}      # name -> when the oldest waiting text arrived
        self.last_activity = {}      # name -> last message or keystroke
        self.busy = {}               # name -> {"since": t, "texts": [...]} while their turn runs
        self.running_models = {}
        self.procs = {}              # name -> their running claude process
        self.state_lock = threading.Lock()
        self.generation = 0          # bumped by /new so running turns don't save old sessions
        self.state = self._load_state()
        self.history = self._load_history()
        self.history_lock = threading.Lock()
        self.deleted = []            # what was deleted from the chat: Claude hears about it on its next turn
        self.stopping = set()        # lanes /stop just killed: their exit isn't an error
        self.halted = set()          # lanes /stop hit: no tidy-up compaction after, the person wants to go on
        self.usage = None            # the host's plan usage: {"limits": [...], "plan", "ts", "error"}
        self.usage_sig = ""
        self.usage_by = {}           # host account -> its usage (both are watched; self.usage is the one in use)
        self.usage_kick = threading.Event()
        self.usage_warned = {}       # limit kind -> (highest warning sent, its window's reset time)
        self.board_lock = threading.Lock()
        self.board = load_json(BOARD, {"cards": []})
        numbered = False
        if "boards" not in self.board:   # one fixed board until now: it becomes the first of many
            self.board["boards"] = [{"id": "main", "name": "Tasks", "columns": [{"id": c, "title": t} for c, t in COLUMNS]}]
            numbered = True
        for card in self.board["cards"]:
            if "board" not in card:
                card["board"], numbered = "main", True
        for card in sorted(self.board["cards"], key=lambda c: c["ts"]):   # number cards made before numbers existed
            if "num" not in card:
                self.board["next"] = self.board.get("next", 1)
                card["num"] = self.board["next"]
                self.board["next"] += 1
                numbered = True
        if "next" not in self.board:
            self.board["next"] = max([c["num"] for c in self.board["cards"]], default=0) + 1
            numbered = True
        if numbered:
            save_json_safely(BOARD, self.board)
        self.wb_lock = threading.Lock()
        self.wb = load_json(WHITEBOARD, {"strokes": []})
        self.wb_changed = os.path.getmtime(WHITEBOARD) if os.path.exists(WHITEBOARD) else 0.0
        self.snapshot_at = os.path.getmtime(SNAPSHOT) if os.path.exists(SNAPSHOT) else 0.0
        self.files_lock = threading.Lock()
        self.files = load_json(FILES_META, [])
        self.feedback_lock = threading.Lock()
        self.feedback = load_json(FEEDBACK, [])
        self.attachments = {}        # person -> files they shared since their last turn (told to Claude)
        self.pins_lock = threading.Lock()
        self.pins = load_json(PINS, [])
        self.timeline_lock = threading.Lock()
        self.timeline = load_json(TIMELINE, {"entries": []})
        self.notes_lock = threading.Lock()   # notes.json and reactions.json
        self.notes = {"lanes": {}, "seen": {}, "eggs": {}, **load_json(NOTES, {})}
        self.reactions = load_json(REACTIONS, {})
        self.react_log = []          # recent reactions (Claude hears about the ones on its answers)
        self.polls_lock = threading.Lock()
        self.polls = load_json(POLLS, {})
        self.summarizing = set()
        self.turn_model = {}         # lane -> model for its next turn only (/continue opus)
        self.prev_seen = {}          # person -> when they were last here before this visit (for /recap)
        self.live_stdin = {}         # lane -> the running turn's stdin: messages sent meanwhile join that turn
        self.sync = {}               # person -> their project copy: {rev, remote, ts}
        self.sync_seen_rev = 0       # newest check-in announced
        self.checkins = []           # recent check-ins: {rev, who, msg, ts}
        self.epic_names = {str(k).lower(): str(v).lower() for k, v in (cfg.get("epic_names") or {}).items()}   # Epic name -> person
        self.pchat = []
        try:
            with open(PEOPLE_LOG, encoding="utf-8") as f:
                self.pchat = [json.loads(line) for line in f.readlines()[-300:] if line.strip()]
        except (OSError, ValueError):
            pass
        self.turn_effort = {}        # lane -> what Auto effort picked for the running turn
        self.last_effort = {}        # lane -> what it picked last time ("go on" keeps it)
        self.injected = {}           # lane -> messages that joined the running turn
        self.thread_notes = {}       # lane -> notes from other threads, for its next turn
        self.midturn = {}            # lane -> [(text, "msg" / "note", raw)] for a running Codex turn: rides on its next tool result
        self.leases = {}             # person -> who holds the UEFN playtest on their PC (reported by chat.py)
        self.feed_seen = {}          # lane -> when its running turn last heard what the other threads changed
        self.zones = load_json(ZONES, {"zones": []})
        self.zones_lock = threading.Lock()
        self.zones_seen = {}         # lane -> the zones it was last shown
        self.lease_waiters = {}      # person -> {lane: when it got HELD}: they carry on when UEFN is free
        self.wakes = {}              # (from lane, to lane) -> when one woke the other (a cap stops ping-pong)
        self.brief_seen = {}         # lane -> the threads directory it was last shown (sent again only when it changed)
        self.meet_lock = threading.RLock()   # the meetings folder (Meetings tab)
        self.meet_cache, self.meet_index, self.meet_root, self.meet_scanned = {}, {}, None, 0.0
        self.meet_seen = {}          # lane -> the meetings list it was last shown
        self.meet_summarizing = {}   # meeting id -> what its summary is at
        self.meet_relays = {}        # person -> live transcriptions passing through the room
        self.dg_grant_denied = 0.0   # the Deepgram key can't mint browser tokens until then: relay
        self.write_tids = {}         # (lane, write_file input) -> its tool line, so the PC's backup can be undone from there
        self.activity = {}           # person -> {"pc": {apps, idle, ts}, "web": {idle, ts}}
        self.activity_since = {}     # person -> ((state, away), since)
        for key, empty in (("stars", {}), ("reminders", []), ("undos", {}), ("rules", {})):
            self.notes.setdefault(key, empty)
        self.secrets_lock = threading.Lock()
        self.secrets = load_json(SECRETS, {})
        self.write_house_rules()
        self.current_aid = {}        # person -> id of the answer Claude is writing for them right now
        self.live_parts = {}         # lane -> that answer's parts so far (a page opened mid-turn gets them)
        self.lock_reports = {}       # person -> what their terminal last saw of the project's Lore locks
        self.tool_lock = threading.Lock()
        self.tools = self._load_tools()  # tool_use id -> {"name", "input", "output", "ok", "secs", ...}
        self.colors = {}
        for i, name in enumerate(cfg["people"]):
            self.colors[name] = COLORS[i % len(COLORS)]

    # ---- history / broadcast -------------------------------------------
    def _load_state(self):
        """state.json: "session_id" = the shared conversation every new lane is forked from,
        "lanes" = {name: {"session", "seen"}}, "base_ts" = when the shared one stopped."""
        state = load_json(STATE, {})
        state.setdefault("session_id", None)
        state.setdefault("lanes", {})
        if "base_ts" not in state:
            state["base_ts"] = time.time()
            save_json(STATE, state)
        return state

    def _load_history(self):
        events = []
        try:
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    # events from before ids existed get a stable one, so they can carry pins too
                    ev.setdefault("id", "h" + hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:8])
                    events.append(ev)
        except OSError:
            pass
        return events[-300:]

    def record(self, event):
        event = {**event, "ts": event.get("ts") or time.time(), "id": event.get("id") or "e" + secrets.token_hex(4)}
        with self.history_lock:
            self.history.append(event)
            self.history = self.history[-300:]
            with open(HISTORY, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def delete_msg(self, who, mid):
        """The host deletes any message or answer, everyone else only their own."""
        with self.history_lock:
            ev = next((e for e in self.history if e.get("id") == mid and e.get("t") in ("msg", "answer", "image", "poll")), None)
            if not ev:
                return False, ("Claude is still writing that answer: stop it first (■ Stop)" if mid in self.current_aid.values()
                               else "that message is gone or too old to delete")
            author = ev.get("assistant", "Claude") if ev["t"] == "answer" else ev.get("from") or ev.get("by")
            if who not in (author, self.cfg["host"]):
                return False, f"only {self.cfg['host']} (the host) can delete other people's messages"
            self.history = [e for e in self.history if e is not ev]
            keep = []
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    try:
                        old = json.loads(line)
                    except ValueError:
                        keep.append(line)
                        continue
                    if (old.get("id") or "h" + hashlib.sha1(line.strip().encode("utf-8")).hexdigest()[:8]) != mid:
                        keep.append(line)
            with open(HISTORY + ".tmp", "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(HISTORY + ".tmp", HISTORY)
        seen = True
        if ev["t"] == "msg":   # still waiting for its turn: Claude never sees it
            lane = ev.get("lane") or author
            with self.lock:
                queued = self.pending.get(lane, [])
                if ev["text"] in queued:
                    queued.remove(ev["text"])
                    seen = False
                    if not queued:
                        self.pending.pop(lane, None)
                        self.first_pending.pop(lane, None)
        if ev["t"] == "msg" and seen and who == author:   # Claude already has it: take it back from the running turn
            self.take_back(ev.get("lane") or author, ev.get("text") or "", who)
        with self.pins_lock:
            if any(p["target"] == mid for p in self.pins):
                self.pins = [p for p in self.pins if p["target"] != mid]
                save_json_safely(PINS, self.pins)
        tids = [p[3] for p in ev.get("parts", []) if p[0] == "tool" and len(p) > 3 and p[3]]
        if tids:
            with self.tool_lock:
                for tid in tids:
                    self.tools.pop(tid, None)
                try:
                    self._rewrite_tools(self.tools)
                except OSError:
                    pass
        if seen:
            if ev["t"] == "answer":
                text = "".join(p[1] for p in ev["parts"] if p[0] == "text")
                what = f"{who} deleted your answer to {self.lane_label(ev.get('lane')) or 'everyone'}: “{clip(text, 160)}”"
            else:
                text = ev.get("text") or ev.get("caption") or ev.get("name") or ""
                whose = "their own" if who == author else f"{author}'s"
                what = f"{who} deleted {whose} {'message' if ev['t'] == 'msg' else 'picture'}: “{clip(text, 160)}”"
            self.deleted = (self.deleted + [{"ts": time.time(), "text": what}])[-20:]
        self.broadcast({"t": "del", "id": mid, "by": who})
        return True, "deleted"

    def take_back(self, lane, text, who):
        """`who` deleted their own message that Claude already has: a running turn only that message started is
        stopped; a running turn that has other messages too is told to ignore it (a finished one hears it next turn)."""
        with self.lock:
            b = self.busy.get(lane)
            texts = list((b or {}).get("texts") or [])
            proc = self.procs.get(lane)
        if not b or b.get("compacting"):
            return
        if texts and all(t == text for t in texts) and proc and proc.poll() is None:
            with self.lock:
                self.halted.add(lane)
            for entry in list(self.execs.values()):
                if entry["lane"] == lane:
                    entry["result"] = (False, "stopped by the room")
                    entry["started"].set()
                    entry["done"].set()
            self.stopping.add(lane)
            kill_tree(proc)
            self.broadcast({"t": "sys", "text": f"↩ {who} took back their message, so {self.lane_label(lane)} stopped"}, keep=True)
        else:
            self.inject(lane, f"[room] {who} took back their message “{clip(text, 300)}”: ignore it and carry on with "
                              "the rest of the task.", kind="note")
            self.broadcast({"t": "sys", "text": f"↩ {who} took back a message; {self.lane_label(lane)} was told to ignore it"},
                           keep=True)

    def redeliver_execs(self, name):
        """Approvals waiting for `name` whose terminal went away (it restarted to update, or reconnected) go to the
        terminal connected now, so the y / n / a prompt isn't lost."""
        with self.lock:
            agent = self.agents.get(name)
            live = {id(q) for _, q in self.clients}
        if not agent:
            return
        for entry in list(self.execs.values()):
            if (entry["user"] != name or entry["started"].is_set() or not entry.get("msg")
                    or entry.get("q") is agent["q"] or id(entry.get("q")) in live):
                continue
            left = entry.get("until", 0) - time.time()
            if left < 3:
                continue
            entry["q"] = agent["q"]
            agent["q"].put({**entry["msg"], "timeout": left})

    def broadcast(self, event, keep=False):
        if keep:
            event = self.record(event)
        with self.lock:
            clients = list(self.clients)
        for _, q in clients:
            q.put(event)

    def send_to(self, name, event, kind=None):
        """Send to one person's open windows (only their browsers with kind="browser")."""
        with self.lock:
            targets = [q for n, q in self.clients
                       if n == name and (kind is None or self.conn_info.get(q, {}).get("kind") == kind)]
        for q in targets:
            q.put(event)

    def broadcast_web(self, event):
        """Only to browser tabs (whiteboard traffic means nothing to a terminal)."""
        with self.lock:
            targets = [q for _, q in self.clients if self.conn_info.get(q, {}).get("kind") == "browser"]
        for q in targets:
            q.put(event)

    def terminal_versions(self):
        """name -> newest chat.py version among that person's open terminals."""
        with self.lock:
            out = {}
            for n, q in self.clients:
                info = self.conn_info.get(q, {})
                if info.get("kind") == "chat.py":
                    out[n] = max(out.get(n, 0), info.get("version", 0))
            return out

    # ---- threads: more than one conversation per person, side by side ------
    def thread_name(self, lane):
        """"" for a person's main thread, else the name they gave it."""
        if "/" not in str(lane or ""):
            return ""
        with self.state_lock:
            return self.state.get("threads", {}).get(lane, {}).get("name", lane.split("/", 1)[1])

    def lane_label(self, lane):
        name = self.thread_name(lane)
        return f"{owner(lane)} · {name}" if name else str(lane or "")

    def threads_view(self):
        with self.state_lock:
            return [{"id": k, **v} for k, v in self.state.get("threads", {}).items()]

    def lane_of(self, name, thread):
        """Where `name`'s message goes: one of their own open threads, else their main one."""
        thread = str(thread or "")
        if thread and owner(thread) == name:
            with self.state_lock:
                t = self.state.get("threads", {}).get(thread)
            if t and not t.get("closed"):
                return thread
        return name

    def thread_op(self, name, body):
        op, lane = str(body.get("op", "")), str(body.get("thread") or "")
        if op == "create":
            title = clean_text(body.get("name"), 40) or "New thread"
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24] or secrets.token_hex(3)
            self.make_room_for_thread(name)   # at the limit, mark an idle candidate and let its owner choose
            with self.state_lock:
                threads = self.state.setdefault("threads", {})
                if sum(1 for t in threads.values() if t["owner"] == name and not t.get("closed")) >= 12:
                    return False, "you have 12 open threads: review an archive warning and confirm one if you want to make room"
                lane, n = f"{name}/{slug}", 2
                while lane in threads:
                    lane, n = f"{name}/{slug}-{n}", n + 1
                threads[lane] = {"owner": name, "name": title, "ts": time.time()}
                save_json(STATE, self.state)
            self.broadcast({"t": "threads", "threads": self.threads_view(), "created": lane, "by": name})
            self.broadcast({"t": "sys", "text": f"{name} opened the thread “{title}”"}, keep=True)
            return True, lane
        if op in ("goal", "summarize"):   # "" = the person's main conversation
            target = lane or name
            if op == "summarize":
                threading.Thread(target=self.summarize, args=(target,), daemon=True).start()
                return True, target
            if owner(target) != name:
                return False, "only its owner can say what a thread is for"
            self.set_lane_meta(target, goal=clean_text(body.get("goal"), 400), goal_auto=False)
            self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
            return True, target
        with self.state_lock:
            t = self.state.get("threads", {}).get(lane)
        if not t or t["owner"] != name:
            return False, "that's not one of your threads"
        if op == "rename":
            title = clean_text(body.get("name"), 40)
            if not title:
                return False, "a thread needs a name"
            with self.state_lock:
                old, t["name"] = t["name"], title
                save_json(STATE, self.state)
            self.broadcast({"t": "threads", "threads": self.threads_view()})
            self.broadcast({"t": "sys", "text": f"{name} renamed the thread “{old}” to “{title}”"}, keep=True)
            return True, lane
        if op in ("close", "reopen"):
            with self.lock:
                running = lane in self.busy or bool(self.pending.get(lane))
            if op == "close" and running:
                return False, "Claude is still working in that thread: wait for it or /stop it first"
            with self.state_lock:
                t["closed"] = op == "close"
                if op == "close":
                    t["archived"] = time.time()
                    t.pop("archive_warning", None)
                    t.pop("archive_reason", None)
                else:
                    t.pop("archived", None)
                    t.pop("archive_warning", None)
                    t.pop("archive_reason", None)
                save_json(STATE, self.state)
            self.broadcast({"t": "threads", "threads": self.threads_view()})
            self.broadcast({"t": "sys", "text": f"{name} {'closed' if op == 'close' else 'reopened'} the thread “{t['name']}”"},
                           keep=True)
            return True, lane
        return False, f"unknown thread op {op!r}"

    # ---- tidying: finished cards leave pins; idle threads get an archive warning ----
    def goal_from_card(self, lane):
        """A goal the room took from the thread's first message gives way to its open card's title."""
        m = self.lane_meta(lane)
        if m.get("goal") and not m.get("goal_auto", True):
            return False   # typed by hand: keep it
        cards = self.lane_cards(lane)
        if not cards:
            return False
        goal = f"#{cards[0].get('num')} {cards[0]['title']}"
        if goal != m.get("goal"):
            self.set_lane_meta(lane, goal=goal, goal_auto=True)
            return True
        return False

    def refresh_goals(self):
        changed = [lane for lane in list(self.lane_meta_all()) if self.goal_from_card(lane)]
        if changed:
            self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})

    def card_finished(self, who, card_id, moment):
        """A card reached Done: every thread unpins it; the pictures a thread pinned for it go onto its moment."""
        added, lanes = 0, []
        for lane, m in self.lane_meta_all().items():
            pins = m.get("pins") or []
            cp = next((p for p in pins if p.get("kind") == "card" and p.get("card") == card_id), None)
            if not cp:
                continue
            pics = [p for p in pins if p.get("kind") == "file" and p.get("ts", 0) >= cp.get("ts", 0)]
            if moment:
                for p in pics[:8]:
                    if p.get("file") and self.timeline_op(who, {"op": "attach", "id": moment, "kind": "file", "file": p["file"]})[0]:
                        added += 1
            gone = {cp["id"]} | ({p["id"] for p in pics} if moment else set())
            self.set_lane_meta(lane, pins=[p for p in pins if p.get("id") not in gone])
            lanes.append(self.lane_label(lane))
        if lanes:
            self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        return ((f" · {added} picture(s) from the threads went onto that moment" if added else "")
                + (f" · unpinned from {', '.join(lanes)}" if lanes else ""))

    def lane_last_ts(self, lane):
        ts = next((e.get("ts", 0) for e in reversed(self.history) if e.get("lane") == lane and e.get("t") in ("msg", "answer")), 0)
        with self.state_lock:
            t = self.state.get("threads", {}).get(lane) or {}
        return max(ts, t.get("ts", 0))

    def archivable(self, lane, idle):
        with self.lock:
            if lane in self.busy or self.pending.get(lane):
                return False
        return not self.lane_cards(lane) and time.time() - self.lane_last_ts(lane) >= idle

    def archive(self, lane, why):
        """Suggest archiving an idle thread without hiding it; the owner must confirm."""
        with self.state_lock:
            t = self.state.get("threads", {}).get(lane)
            if not t or t.get("closed") or t.get("archive_warning"):
                return False
            t["archive_warning"] = time.time()
            t["archive_reason"] = str(why)[:180]
            save_json(STATE, self.state)
        self.broadcast({"t": "threads", "threads": self.threads_view()})
        self.broadcast({"t": "sys", "text": f"⚠ # {t['name']} is idle and still open. Review the warning beside it if you want to archive it."}, keep=True)
        return True

    def make_room_for_thread(self, person):
        with self.state_lock:
            mine = [l for l, t in self.state.get("threads", {}).items() if t["owner"] == person and not t.get("closed")]
        if len(mine) < 12:
            return
        idle = sorted((self.lane_last_ts(l), l) for l in mine if self.archivable(l, 3600))
        if idle:
            self.archive(idle[0][1], "the oldest idle thread, to make room for a new one")

    def archiver(self):
        while True:
            time.sleep(600)
            try:
                with self.state_lock:
                    open_ = [l for l, t in self.state.get("threads", {}).items() if not t.get("closed")]
                for lane in open_:
                    sub = bool(self.lane_meta(lane).get("parent"))
                    if self.archivable(lane, 2 * 3600 if sub else 24 * 3600):
                        self.archive(lane, "its part is done and it was idle 2 h" if sub else "idle 24 h with no open card")
            except Exception as e:  # noqa: BLE001 - try again next round
                print("archiver:", e)

    # ---- teamwork: sub-threads a thread opens itself, its checklist, shared lessons ----
    def thread_spawn(self, who, lane, args):
        person = owner(lane)
        name = clean_text(args.get("name"), 40)
        task = clean_text(args.get("task"), 4000)
        if not name or not task:
            return False, "thread_spawn needs a short name and the task (what exactly, where, how to check it)"
        depth = int(self.lane_meta(lane).get("depth") or 0)
        if depth >= 2:
            return False, "a sub-thread of a sub-thread can't open more: do it yourself or tell your parent thread"
        kids = [l for l, m in self.lane_meta_all().items() if m.get("parent") == lane and not self.thread_closed(l)]
        if len(kids) >= 4:
            return False, "this thread already has 4 open sub-threads: close finished ones (thread_close) first"
        ok, child = self.thread_op(person, {"op": "create", "name": name})
        if not ok:
            return False, child
        self.set_lane_meta(child, parent=lane, depth=depth + 1, goal=clip(task, 400))
        cards = [c["id"] for c in self.lane_cards(lane)]
        ref = str(args.get("card") or "").strip()
        if ref:
            with self.board_lock:
                c = next((c for c in self.board["cards"] if c["id"] == ref or str(c.get("num")) == ref.lstrip("#")), None)
            if c:
                cards = [c["id"]]
        for cid in cards[:2]:
            self.thread_pin(who, child, {"kind": "card", "card": cid})
        text = (f"(room) A task from the thread {self.lane_label(lane)}, which opened this sub-thread: {task}\n"
                f"Work only on this. When you are done (or stuck), end your turn with a short report: what you did, "
                f"what's left, how to check it. The room hands it to {self.lane_label(lane)}.")
        with self.lock:
            self.pending.setdefault(child, []).append(text)
            self.first_pending.setdefault(child, time.time())
            self.lock.notify_all()
        self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        self.broadcast({"t": "sys", "text": f"🧵 {self.lane_label(lane)} opened # {name} for: {clip(task, 160)}"}, keep=True)
        return True, (f"opened {child} (“{name}”); it starts now" + (f" with card(s) {', '.join(cards[:2])} pinned" if cards else "")
                      + ". Its reports come back to you; thread_note it to steer it.")

    def thread_closed(self, lane):
        with self.state_lock:
            return bool((self.state.get("threads", {}).get(lane) or {}).get("closed"))

    def related(self, a, b):
        return self.lane_meta(a).get("parent") == b or self.lane_meta(b).get("parent") == a

    def wake(self, target, note, ask, from_lane):
        """Hand `note` to `target`: into its running turn, or (idle, within the budget) as a new turn."""
        if self.inject(target, note, kind="note"):
            return "while it works"
        with self.lock:
            if target in self.busy:
                self.thread_notes.setdefault(target, []).append(note)
                return "at its next turn"
            key = (from_lane, target)
            recent = [t for t in self.wakes.get(key, []) if time.time() - t < 3600]
            if len(recent) >= 6:   # they keep waking each other: stop the loop, the people can look
                self.thread_notes.setdefault(target, []).append(note)
                return "at its next turn (wake-up limit reached for the hour)"
            self.wakes[key] = recent + [time.time()]
            self.pending.setdefault(target, []).append(ask)
            self.first_pending.setdefault(target, time.time())
            self.lock.notify_all()
        return "now: it starts a turn for it"

    def report_to_parent(self, lane, parts):
        parent = self.lane_meta(lane).get("parent")
        if not parent or self.thread_closed(parent) and parent != owner(parent):
            return
        text = clip(re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in parts if p[0] == "text")).strip(), 1500)
        label = self.lane_label(lane)
        how = self.wake(parent, f"[room] your sub-thread {label} finished a turn: {text}",
                        f"(room) Your sub-thread {label} finished a turn and reports: {text}\n"
                        "Check it; give it the next step (thread_note), close it when done (thread_close), or carry on.", lane)
        self.broadcast({"t": "sys", "text": f"🧵 {label} → {self.lane_label(parent)}: report delivered {how}"})

    def plan_update(self, lane, args):
        items = []
        for it in (args.get("items") or [])[:20]:
            if isinstance(it, dict) and clean_text(it.get("text"), 160):
                st = it.get("status") if it.get("status") in ("todo", "doing", "done") else "todo"
                items.append({"text": clean_text(it.get("text"), 160), "status": st})
        self.set_lane_meta(lane, plan=items)
        self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        done = sum(1 for i in items if i["status"] == "done")
        return True, f"plan saved: {done}/{len(items)} done"

    def lesson_add(self, lane, text):
        text = clean_text(text, 400)
        if not text:
            return False, "the lesson is empty"
        path = os.path.join(WORKSPACE, "lessons.md")
        try:
            lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
        except OSError:
            lines = []
        head = [l for l in lines if not l.startswith("- ")] or ["# Lessons the room's threads learned (newest last)"]
        items = [l for l in lines if l.startswith("- ")]
        if any(text.lower() in l.lower() for l in items):
            return True, "already a lesson"
        items = (items + [f"- {time.strftime('%Y-%m-%d')} ({self.lane_label(lane)}): {text}"])[-120:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(head + items) + "\n")
        self.broadcast({"t": "sys", "text": f"📘 lesson from {self.lane_label(lane)}: {clip(text, 200)}"}, keep=True)
        return True, "saved: every thread reads it from now on"

    def lessons_text(self):
        try:
            return open(os.path.join(WORKSPACE, "lessons.md"), encoding="utf-8").read()
        except OSError:
            return ""

    # ---- threads: what each is for and what it has done; who was here when ----
    def lane_meta(self, lane):
        with self.notes_lock:
            return dict(self.notes["lanes"].get(lane, {}))

    def lane_meta_all(self):
        with self.notes_lock:
            return {k: {f: v[f] for f in ("goal", "summary", "sum_ts", "pins", "plan", "parent", "depth") if f in v}
                    for k, v in self.notes["lanes"].items()}

    def set_lane_meta(self, lane, **fields):
        with self.notes_lock:
            self.notes["lanes"].setdefault(lane, {}).update(fields)
            save_json_safely(NOTES, self.notes)

    def mark_seen(self, name, save=False):
        with self.notes_lock:
            self.notes["seen"][name] = time.time()
            if save:
                save_json_safely(NOTES, self.notes)

    def seen_at(self, name):
        with self.notes_lock:
            return self.notes["seen"].get(name)

    def codex_rules(self):
        with self.notes_lock:
            house = (self.notes["rules"].get("room") or {}).get("text", "")
        return ("Follow the room's rules below, using Codex as your identity. "
                "References to Claude's room tools also apply to you.\n" + room_rules(self.cfg) + "\nHouse rules:\n" + house
                + "\n\nLessons:\n" + self.lessons_text()
                + "\n\nProject context:\n" + self.codex_project_context())

    def turn_pref(self, lane):
        # a /continue may pick another model for one turn; Auto effort picks per turn
        model, effort = self.pref(lane)
        if effort == "auto":
            effort = self.auto_effort(lane)
        model = self.turn_model.get(lane) or model
        if is_gemini_model(model):
            info = GEMINI_MODELS[gemini_model_id(model)]
            if effort not in info["efforts"]:
                effort = info["default_effort"]
        return model, effort

    def threads_text(self, name, everyone=False):
        meta, rows = self.lane_meta_all(), []
        for person in (list(self.cfg["people"]) if everyone else [name]):
            lanes = [(person, "Main")] + [(t["id"], t["name"]) for t in self.threads_view() if t["owner"] == person and not t.get("closed")]
            for lane, label in lanes:
                m = meta.get(lane, {})
                rows.append(f"  # {person + ' · ' + label if everyone else label}"
                            + (f" · for: {clip(m['goal'], 120)}" if m.get("goal") else "")
                            + (f" · so far: {m['summary']}" if m.get("summary") else ""))
        return (("threads:" if everyone else "your threads:") + "\n" + "\n".join(rows)
                + "\n/thread new NAME opens one that runs at the same time · /threads all shows everyone's")

    def quick_claude(self, prompt, timeout=90):
        if ai_paused():
            return ""
        # one small question to a cheap model: no tools, no session, no room rules (summaries)
        os.makedirs(QUIET_DIR, exist_ok=True)
        try:
            r = subprocess.run([self.claude, "-p", "--model", SUMMARY_MODEL, "--strict-mcp-config", "--tools", "",
                                "--no-session-persistence", "--output-format", "json"],
                               input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=QUIET_DIR,
                               timeout=timeout, env={**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
            return str(json.loads(r.stdout).get("result") or "") if r.returncode == 0 else ""
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return ""

    def summarize(self, lane):
        if ai_paused():
            return
        with self.lock:
            if lane in self.summarizing:
                return
            self.summarizing.add(lane)
        try:
            meta, convo = self.lane_meta(lane), []
            for ev in list(self.history):
                if ev.get("t") == "msg" and (ev.get("lane") or ev.get("from")) == lane:
                    convo.append(f"[{ev['from']}]: {clip(ev['text'], 400)}")
                elif ev.get("t") == "answer" and ev.get("lane") == lane:
                    text = re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text"))
                    tools = [p[1].split(" · ")[-1] for p in ev["parts"] if p[0] == "tool"]
                    convo.append(f"[Claude]: {clip(text, 700)}" + (f" (tools: {', '.join(tools[:10])})" if tools else ""))
            if not convo:
                self.set_lane_meta(lane, dirty=False)
                return
            prompt = ("Summarize this thread of a team chat room (people building a project with Claude) for the people in it. "
                      "Write 2 short sentences in the language the people write in: what the thread has done so far, then "
                      "where it stands now (next step or what blocks it). Plain text, no preamble, at most 280 characters.\n\n"
                      + (f"Thread goal: {meta['goal']}\n" if meta.get("goal") else "")
                      + (f"Earlier summary: {meta['summary']}\n" if meta.get("summary") else "")
                      + "Latest messages:\n" + "\n".join(convo[-16:]))
            out = " ".join(self.quick_claude(prompt).split())
            self.set_lane_meta(lane, dirty=False, **({"summary": clip(out, 400), "sum_ts": time.time()} if out else {}))
            if out:
                self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        finally:
            with self.lock:
                self.summarizing.discard(lane)

    def watch_summaries(self):
        # a thread's summary is refreshed after its turns, at most every 10 minutes, never while it works
        with self.notes_lock:   # threads that already talked but have no summary yet get one in the first minutes
            for ev in list(self.history):
                lane = ev.get("lane") or ev.get("from") if ev.get("t") in ("msg", "answer") else None
                if lane and lane != "Claude" and not self.notes["lanes"].get(lane, {}).get("summary"):
                    self.notes["lanes"].setdefault(lane, {})["dirty"] = True
        while True:
            time.sleep(60)
            with self.notes_lock:
                due = [lane for lane, m in self.notes["lanes"].items() if m.get("dirty") and time.time() - m.get("sum_ts", 0) >= 600]
                save_json_safely(NOTES, self.notes)   # also keeps "last seen" on disk
            with self.lock:
                due = [lane for lane in due if lane not in self.busy]
            for lane in due[:4]:
                self.summarize(lane)

    # ---- reactions ------------------------------------------------------
    # ---- ↶ undo: chat.py keeps what a write replaced; its owner can put it back ----
    # ---- 🤖 AI settings: the people's own rules, and whose Claude account a person's turns use ----
    # ---- a running thread takes more input; threads know about each other -----------------
    # ---- Auto effort, search ----------------------------------------------------------------
    # ---- the project's copies (Lore): who is behind, what was checked in ----------------------
    def epic_person(self, epic):
        epic = str(epic or "").lower()
        with self.lock:
            known = dict(self.epic_names)
        known.update({r["me_epic"].lower(): n for n, r in self.lock_reports.items() if r.get("me_epic")})
        return known.get(epic)

    def sync_report(self, name, body):
        try:
            rev, remote = int(body.get("rev")), int(body.get("remote"))
        except (TypeError, ValueError):
            return
        if body.get("me_epic"):
            with self.lock:
                self.epic_names[str(body["me_epic"]).lower()] = name
        hist = [h for h in (body.get("history") or [])[:10] if isinstance(h, dict) and str(h.get("rev", "")).isdigit()]
        with self.lock:
            before = dict(self.sync.get(name) or {})
            self.sync[name] = {"rev": rev, "remote": remote, "ts": time.time(), "project": clean_text(body.get("project"), 80)}
            baseline = self.sync_seen_rev
            newest = max([int(h["rev"]) for h in hist] + [remote, baseline])
            self.sync_seen_rev = newest
        if baseline:   # the first report only sets where we are: old check-ins aren't news
            for h in sorted((h for h in hist if int(h["rev"]) > baseline), key=lambda h: int(h["rev"])):
                who = self.epic_person(h.get("creator")) or h.get("creator") or "someone"
                msg = clip(" ".join(str(h.get("msg", "")).split()), 200)
                with self.lock:
                    self.checkins = (self.checkins + [{"rev": int(h["rev"]), "who": who, "msg": msg, "ts": time.time()}])[-50:]
                self.broadcast({"t": "sys", "text": f"⬆ {who} checked in revision {h['rev']}: {msg or '(no description)'}"}, keep=True)
        if remote > rev and before.get("remote") != remote:
            self.notify(name, "sync", f"Sync Latest in UEFN: {remote - rev} new check-in{'s' if remote - rev != 1 else ''}",
                        "Until your copy is up to date, Claude can't change the project (so nobody's work gets overwritten).")
        if (before.get("rev"), before.get("remote")) != (rev, remote):
            self.broadcast({"t": "sync", "sync": self.sync_view()})

    def sync_view(self):
        with self.lock:
            return {n: {"rev": s["rev"], "remote": s["remote"], "behind": max(0, s["remote"] - s["rev"])}
                    for n, s in self.sync.items() if time.time() - s["ts"] < 240}

    def sync_lines(self, person, since):
        lines = []
        with self.lock:
            news = [c for c in self.checkins if c["ts"] > since]
        if news:
            lines.append("[room] project check-ins since your last turn (build on them, don't redo or undo them): "
                         + "; ".join(f"rev {c['rev']} by {c['who']}: {c['msg']}" for c in news[-8:]))
        s = self.sync_view().get(person)
        if s and s["behind"]:
            lines.append(f"[room] {person}'s copy of the project is {s['behind']} check-in(s) behind (revision {s['rev']}, the shared "
                         f"one is {s['remote']}): changes to the project are refused until {person} clicks Sync Latest in UEFN.")
        return lines

    # ---- the UEFN playtest belongs to one thread at a time (chat.py enforces it per PC; the room shows it) ----
    def lease_report(self, name, lease):
        lease = lease if isinstance(lease, dict) and lease.get("state") in ("running", "review") else None
        with self.lock:
            if lease:
                self.leases[name] = {"lane": str(lease.get("lane") or name), "thread": clip(str(lease.get("thread") or "Main"), 60),
                                     "since": float(lease.get("since") or time.time()), "state": lease["state"]}
            else:
                self.leases.pop(name, None)
            view = dict(self.leases)
            waiters = {} if lease else self.lease_waiters.pop(name, {})
        self.broadcast({"t": "leases", "leases": view})
        woken = []
        for lane, since in waiters.items():   # UEFN is free: the threads that had to wait carry on
            if time.time() - since > 3 * 3600 or lane in (self.leases.get(name) or {}).values():
                continue
            text = "(room) UEFN on this PC is free again (the playtest is over): carry on with your task where you stopped."
            if not self.inject(lane, "[room] " + text, kind="note"):
                with self.lock:
                    if lane in self.busy:
                        self.thread_notes.setdefault(lane, []).append("[room] " + text)
                    else:
                        self.pending.setdefault(lane, []).append(text)
                        self.first_pending.setdefault(lane, time.time())
                        self.lock.notify_all()
            woken.append(self.lane_label(lane))
        if woken:
            self.broadcast({"t": "sys", "text": "🎮 UEFN is free on " + name + "'s PC: " + ", ".join(woken) + " carry on"}, keep=True)

    def lease_lines(self, lane=None):
        """The playtests in progress; for a thread, only the one on its own person's PC (the hold is per PC)."""
        with self.lock:
            leases = dict(self.leases)
        if lane:
            leases = {n: l for n, l in leases.items() if n == owner(lane)}
        out = []
        for name, l in sorted(leases.items()):
            how = "testing now" if l["state"] == "running" else f"left running for {name} to look at"
            out.append(f"[room] {name}'s UEFN playtest belongs to the thread {self.lane_label(l['lane'])} since "
                       f"{time.strftime('%H:%M', time.localtime(l['since']))} ({how}): only that thread may start/stop sessions, "
                       f"push, BuildAll or change {name}'s editor until it calls StopSession or {name} clicks Done testing "
                       f"(only {name}'s threads wait: other people's PCs are not affected).")
        return out

    def with_midturn(self, lane, output):
        """Every tool result of a running turn brings what the other threads changed since its last one, and (for
        Codex, which has no input stream) the messages and thread notes that arrived meanwhile."""
        with self.lock:
            items = self.midturn.pop(lane, [])
            since = self.feed_seen.get(lane) or (self.busy.get(lane) or {}).get("since") or time.time()
            self.feed_seen[lane] = time.time()
        feed = self.feed_since(lane, since)
        out = str(output)
        if feed:
            out += "\n\n[room] meanwhile the other threads: " + "; ".join(feed)
        if items:
            out += "\n\n[room] arrived while you work (read it before your next step):\n" + "\n".join(c for c, _, _ in items)
        return out

    def feed_since(self, lane, since):
        """What the other threads changed (files, editor, playtests) since `since`, one short line each."""
        with self.tool_lock:
            tools = sorted((dict(t) for t in self.tools.values() if t["lane"] != lane and t.get("ok") is True
                            and t["ts"] + (t.get("secs") or 0) > since), key=lambda t: t["ts"])
        out, me = [], owner(lane)
        for t in tools:
            what = self.change_text(t)
            if what and owner(t["lane"]) != me:   # another person's editor and playtests are on their own PC
                if not what.startswith("wrote "):
                    continue
                what += f" (on {owner(t['lane'])}'s PC)"
            line = f"{self.lane_label(t['lane'])} {what}" if what else ""
            if line and line not in out:
                out.append(line)
        return out[-6:]

    def change_text(self, t):
        name = t["name"].split("· ")[-1]
        try:
            inp = json.loads(t["input"]) if isinstance(t["input"], str) else (t["input"] or {})
        except ValueError:
            inp = {}
        if not isinstance(inp, dict):
            return ""
        if name == "write_file":
            return "wrote " + re.split(r"[\\/]+", str(inp.get("path", "")))[-1]
        if name != "uefn_call" or inp.get("tool") != "call_tool":
            return ""
        a = inp.get("arguments") or {}
        op = str(a.get("tool_name") or "").split(".")[-1]
        if not op or READ_UEFN.match(op):
            return ""
        fixed = {"StartSession": "started a playtest", "StartGame": "started the game in its playtest",
                 "StopSession": "ended its playtest", "StopGame": "stopped the game in its playtest",
                 "PushChanges": "pushed changes into its playtest", "BuildAll": "built Verse"}
        if op in fixed:
            return fixed[op]
        inner = a.get("arguments") or {}
        bits = [re.split(r"[\\/.:]+", v)[-1] for k, v in inner.items() if isinstance(v, str) and re.search(r"path|actor|asset|name|file", k, re.I)]
        return f"changed the editor ({op}{': ' + clip(', '.join(b for b in bits if b), 70) if bits else ''})"

    def lane_cards(self, lane):
        """The open cards pinned to a thread: what it is working on."""
        ids = [p.get("card") for p in self.thread_pins(lane) if p.get("kind") == "card"]
        with self.board_lock:
            cards = {c["id"]: dict(c) for c in self.board["cards"] if c["id"] in ids and not self.card_done(c)}
        return [cards[i] for i in ids if i in cards]

    def codex_project_context(self):
        """The project part of workspace/CLAUDE.md with its @imports read in (Claude Code does that for Claude);
        the long change logs are named, not copied."""
        try:
            with open(os.path.join(WORKSPACE, "CLAUDE.md"), encoding="utf-8") as f:
                tail = f.read().split(CLAUDE_MD_END, 1)[1]
        except (OSError, IndexError):
            return ""
        out, budget = [], 45000
        for line in tail.splitlines():
            m = re.match(r"^@(\S+)$", line.strip())
            if not m:
                out.append(line)
                continue
            full = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(WORKSPACE, m.group(1))
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if "/changes/" in full.replace("\\", "/") or size > budget:
                out.append(f"(a long log: read it when you need it: {full})")
                continue
            with open(full, encoding="utf-8", errors="replace") as f:
                out.append(f"--- {m.group(1)} ---\n" + f.read())
            budget -= size
        return "\n".join(out).strip()

    def card_done(self, card):
        b = next((b for b in self.board["boards"] if b["id"] == card.get("board", "main")), None)
        col = next((c for c in b["columns"] if c["id"] == card["col"]), None) if b else None
        return bool(col) and (col["id"] == "done" or col["title"].strip().lower() == "done" or col is b["columns"][-1])

    def needs_card(self, lane, tool, args):
        """True when this change on a PC must wait: the thread has no open task card pinned to it."""
        if tool == "uefn_call":
            a = args.get("arguments") or {}
            name = str(a.get("tool_name") or "").split(".")[-1]
            if args.get("tool") in ("list_toolsets", "describe_toolset") or READ_UEFN.match(name or str(args.get("tool", ""))):
                return False
        elif tool != "write_file":
            return False
        ids = {p.get("card") for p in self.thread_pins(lane) if p.get("kind") == "card"}
        with self.board_lock:
            return not any(c["id"] in ids and not self.card_done(c) for c in self.board["cards"])

    def card_follow(self, lane, who, ok, msg, ref):
        """After the AI adds or moves a card: an open one is pinned to its thread; at Done, the pictures the thread
        pinned for it go onto the timeline moment."""
        if not ok:
            return ""
        m = re.search(r"\(id (\w+), #\d+\)", msg)
        ref = m.group(1) if m else str(ref or "").strip()
        with self.board_lock:
            card = next((dict(c) for c in self.board["cards"] if ref and (c["id"] == ref or str(c.get("num")) == ref.lstrip("#"))), None)
            done = bool(card) and self.card_done(card)
        if not card:
            return ""
        pins = self.thread_pins(lane)
        moment = re.search(r"moment (tl\w+)", msg)
        if done:
            if not moment:
                return ""
            since = next((p.get("ts", 0) for p in pins if p.get("kind") == "card" and p.get("card") == card["id"]), 0)
            files = [p["file"] for p in pins if p.get("kind") == "file" and p.get("file") and p.get("ts", 0) >= since][:8]
            added = sum(1 for f in files if self.timeline_op(who, {"op": "attach", "id": moment.group(1), "kind": "file", "file": f})[0])
            return f" · {added} picture(s) this thread pinned went onto that moment" if added else ""
        with self.lock:
            busy = set(self.busy)
        others = [l for l, m in self.lane_meta_all().items() if l != lane and l in busy
                  and any(p.get("kind") == "card" and p.get("card") == card["id"] for p in m.get("pins") or [])]
        warn = (" · ⚠ the running thread " + ", ".join(self.lane_label(l) for l in others) + " also works on this card: agree "
                "who does which part (thread_note) before you both change the same things") if others else ""
        if any(p.get("kind") == "card" and p.get("card") == card["id"] for p in pins):
            return warn
        pinned, _ = self.thread_pin(who, lane, {"kind": "card", "card": card["id"]})
        return (f" · the room pinned it to this thread ({self.lane_label(lane)})" if pinned else "") + warn

    # ---- zones: places in the map (and Content folders) a thread depends on or works in ----
    def zones_view(self):
        now = time.time()
        with self.zones_lock:
            keep = [z for z in self.zones["zones"] if z["kind"] == "keep_clear" or now - z["ts"] < ZONE_TTL]
            if len(keep) != len(self.zones["zones"]):
                self.zones["zones"] = keep
                save_json_safely(ZONES, self.zones)
            return [dict(z) for z in keep]

    def zone_op(self, who, lane, op):
        kind = str(op.get("op", ""))
        if kind == "add":
            zk = str(op.get("kind") or "keep_clear")
            if zk not in ("keep_clear", "area", "folder"):
                return False, "kind must be keep_clear, area or folder"
            name = clean_text(op.get("name"), 80)
            if not name:
                return False, "give the zone a name people understand (\"Arrival spot by the gate\")"
            z = {"id": "z" + secrets.token_hex(3), "name": name, "kind": zk, "lane": lane or "", "by": who,
                 "note": clean_text(op.get("note"), 300), "ts": time.time()}
            if zk == "folder":
                path = str(op.get("folder") or "").strip().strip("/")
                if not path:
                    return False, "a folder zone needs folder (e.g. Props/Kitchen)"
                z["folder"] = path
            else:
                try:
                    a = [float(v) for v in op.get("min")][:3]
                    b = [float(v) for v in op.get("max")][:3]
                    if len(a) == 2:
                        a.append(-1e6)
                    if len(b) == 2:
                        b.append(1e6)
                    if len(a) != 3 or len(b) != 3:
                        raise ValueError
                except (TypeError, ValueError):
                    return False, "min and max are [x, y, z] in UEFN centimetres (z may be left out)"
                z["min"], z["max"] = [min(p, q) for p, q in zip(a, b)], [max(p, q) for p, q in zip(a, b)]
            with self.zones_lock:
                same = [x for x in self.zones["zones"] if x["name"].lower() == name.lower() and x["lane"] == z["lane"]]
                self.zones["zones"] = [x for x in self.zones["zones"] if x not in same] + [z]
                save_json_safely(ZONES, self.zones)
            self.broadcast({"t": "zones", "zones": self.zones_view()})
            return True, f"zone {z['id']} “{name}” ({zk}) saved" + (" (replaced the old one)" if same else "")
        if kind == "remove":
            with self.zones_lock:
                z = next((x for x in self.zones["zones"] if x["id"] == op.get("id")), None)
                if not z:
                    return False, f"no zone {op.get('id')!r}"
                mine = lane == z["lane"] if lane else who in (z["by"], owner(z["lane"]), self.cfg["host"])
                if not mine:   # a thread removes only its own; a person their own, their threads' or (host) any
                    return False, f"only {self.lane_label(z['lane']) if z['lane'] else z['by']} can remove it"
                self.zones["zones"] = [x for x in self.zones["zones"] if x is not z]
                save_json_safely(ZONES, self.zones)
            self.broadcast({"t": "zones", "zones": self.zones_view()})
            return True, f"zone “{z['name']}” removed"
        return False, "op must be add or remove"

    def zone_line(self, z):
        who = self.lane_label(z["lane"]) if z["lane"] else z["by"]
        where = (f"folder {z['folder']}" if z["kind"] == "folder" else
                 "X {:.0f}..{:.0f} Y {:.0f}..{:.0f}".format(z["min"][0], z["max"][0], z["min"][1], z["max"][1])
                 + ("" if z["min"][2] <= -1e5 else " Z {:.0f}..{:.0f}".format(z["min"][2], z["max"][2])))
        return f"{z['id']} {z['kind']} “{z['name']}” ({who}): {where}" + (f" · {z['note']}" if z.get("note") else "")

    def zones_hit(self, a, b=None):
        b = b or a
        out = []
        for z in self.zones_view():
            if z["kind"] != "folder" and all(a[i] <= z["max"][i] and b[i] >= z["min"][i] for i in range(3)):
                out.append(z)
        return out

    def pc_event(self, name, body):
        """chat.py saw something every thread of this person should know now (the playtest log)."""
        items = [i for i in (body.get("items") or [])[:6] if isinstance(i, dict) and i.get("text")]
        if not items:
            return
        lines = []
        for i in items:
            text = clip(str(i["text"]), 260)
            m = re.search(r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?", text)
            hit = self.zones_hit([float(m.group(k)) for k in (1, 2, 3)]) if m else []
            lines.append(f"{int(i.get('count') or 1)}× {text}" + (" · in zone " + ", ".join(f"“{z['name']}” ({self.lane_label(z['lane']) if z['lane'] else z['by']})" for z in hit) if hit else ""))
        holder = self.leases.get(name, {}).get("lane")
        note = (f"[room] {name}'s playtest log" + (f" (session of {self.lane_label(holder)})" if holder else "")
                + ": " + " | ".join(lines) + " — if it's from your change, fix it; if not, leave it to its thread.")
        with self.lock:
            running = [l for l in self.busy if owner(l) == name]
        for lane in running:
            self.inject(lane, note, kind="note")
        self.broadcast({"t": "sys", "text": "🩺 " + name + "'s playtest: " + clip(" | ".join(lines), 400)}, keep=True)

    def project_text(self):
        view = self.sync_view()
        rows = [f"{n}: revision {s['rev']} of {s['remote']}" + (f" ({s['behind']} behind: needs Sync Latest)" if s["behind"] else " (up to date)")
                for n, s in view.items()] or ["no copy reported yet (needs chat.py with UEFN's project open)"]
        with self.lock:
            news = list(self.checkins[-10:])
        rows += [x.replace("[room] ", "") for x in self.lease_lines()]
        return ("Copies of the project:\n" + "\n".join("- " + r for r in rows)
                + ("\nRecent check-ins:\n" + "\n".join(f"- rev {c['rev']} by {c['who']}: {c['msg']}" for c in news) if news else ""))

    # ---- 💬 the people's own chat (Claude doesn't read it) -----------------------------------
    # ---- 📌 what a thread keeps in view: its card, pictures, notes (Claude reads them every turn) ----
    def thread_pins(self, lane):
        return list(self.lane_meta(lane).get("pins") or [])

    def thread_pin(self, who, lane, op):
        kind = op.get("kind")
        if kind == "msg":   # a chat message: its picture, or its text as a note
            ev = next((e for e in reversed(self.history) if e.get("id") == op.get("msg")), None)
            if not ev:
                return False, "that message is gone"
            if ev.get("t") == "image":
                kind, op = "file", {"file": ev["file_id"]}
            else:
                body = (re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text")) if ev.get("t") == "answer"
                        else ev.get("text") or ev.get("q") or "")
                author = ev.get("assistant", "Claude") if ev.get("t") == "answer" else ev.get("from") or ev.get("by", "")
                kind, op = "note", {"text": f"{author}: {clip(' '.join(body.split()), 500)}"}
        fields, err = self.pin_payload(who, {**op, "kind": kind}, preview=False)
        if err:
            return False, err
        pins = self.thread_pins(lane)
        if kind == "card" and any(p.get("card") == fields.get("card") for p in pins):
            return True, f"that card is already pinned to {self.lane_label(lane)}"
        pin = {"id": "tp" + secrets.token_hex(3), "kind": kind, "by": who, "ts": time.time(), **fields}
        self.set_lane_meta(lane, pins=(pins + [pin])[-12:])
        if kind == "card":
            self.goal_from_card(lane)
        self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        return True, f"pinned {self.pin_text(pin)} to {self.lane_label(lane)} (pin id {pin['id']})"

    def thread_unpin(self, who, lane, pid):
        pins = self.thread_pins(lane)
        keep = [p for p in pins if p["id"] != pid]
        if len(keep) == len(pins):
            return False, f"no pin {pid!r} in {self.lane_label(lane)}"
        self.set_lane_meta(lane, pins=keep)
        self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        return True, "unpinned"

    def thread_pins_line(self, lane):
        pins = self.thread_pins(lane)
        if not pins:
            return ""
        with self.board_lock:
            cards = {c["id"]: dict(c) for c in self.board["cards"]}
            cols = {(b["id"], col["id"]): col["title"] for b in self.board["boards"] for col in b["columns"]}
        bits = []
        for p in pins:
            if p["kind"] == "card":
                c = cards.get(p["card"])
                bits.append(f"card #{c.get('num')} “{c['title']}” [{c['id']}] in {cols.get((c.get('board'), c['col']), c['col'])}" if c
                            else f"card “{p.get('title', '')}” (deleted)")
            elif p["kind"] == "note":
                bits.append(f"note: {clip(p['text'], 240)}")
            elif p["kind"] == "file":
                bits.append(f"{'picture' if p.get('image') else 'file'} {p.get('name', '')} (file {p['file']})")
            else:
                bits.append("a sketch")
            bits[-1] += f" [pin {p['id']}]"
        return "[room] pinned in this thread (keep them in mind; thread_unpin what's no longer needed): " + " · ".join(bits)

    def gif_search(self, query):
        key = self.cfg.get("tenor_api_key") or os.environ.get("TENOR_API_KEY", "")
        if not key:
            return {"key": False, "items": []}
        url = ("https://tenor.googleapis.com/v2/search?q=" + quote(clean_text(query, 80) or "scary") + "&key=" + quote(key)
               + "&client_key=claude-room&limit=24&media_filter=tinygif,gif&contentfilter=medium")
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
        except Exception:  # noqa: BLE001 - no GIFs this time
            return {"key": True, "items": []}
        items = []
        for res in data.get("results", []):
            fm = res.get("media_formats") or {}
            if fm.get("tinygif"):
                items.append({"preview": fm["tinygif"]["url"], "url": (fm.get("gif") or fm["tinygif"])["url"]})
        return {"key": True, "items": items}

    def pmsg(self, name, text, file_id="", ref="", url=""):
        text = clean_text(text, 4000)
        meta = None
        if file_id:
            meta, _ = self.file_path(str(file_id))
        quoted = None
        if ref:   # a message from Claude's chat, shown as a card that jumps there
            ev0 = next((e for e in reversed(self.history) if e.get("id") == ref), None)
            if ev0:
                body = (re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev0["parts"] if p[0] == "text")) if ev0.get("t") == "answer"
                        else ev0.get("text") or ev0.get("q") or ev0.get("name", ""))
                quoted = {"id": ref, "who": ev0.get("assistant", "Claude") if ev0.get("t") == "answer" else ev0.get("from") or ev0.get("by", ""),
                          "snip": clip(" ".join(re.sub(r"[*`#]+", "", body).split()), 220)}
        url = str(url or "").strip()
        if url and not (re.match(r"^https://[^\s\"'<>]{8,500}$", url) and (re.search(r"\.(gif|png|jpe?g|webp)(\?|$)", url, re.I)
                        or re.match(r"^https://(media\d*\.giphy\.com|i\.giphy\.com|media\d*\.tenor\.com|i\.imgur\.com)/", url))):
            url = ""
        if not text and not meta and not quoted and not url:
            return False, "empty message"
        ev = {"t": "pmsg", "id": "pm" + secrets.token_hex(4), "from": name, "text": text, "ts": time.time(),
              **({"ref": quoted} if quoted else {}), **({"url": url} if url else {})}
        if meta:
            ev.update(file=meta["id"], name=meta["name"], image=os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT)
        with self.lock:
            self.pchat = (self.pchat + [ev])[-300:]
        with open(PEOPLE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.broadcast(ev)
        return True, ""

    def auto_effort(self, lane):
        """Medium for quick questions (answers come faster), High for real work; decided once per turn."""
        with self.lock:
            if lane in self.turn_effort:
                return self.turn_effort[lane]
            texts = list((self.busy.get(lane) or {}).get("texts") or [])
        text = " ".join(texts).strip()
        if AUTO_HEAVY.search(text) or len(text) > 350 or len(texts) > 2:
            effort = "high"
        elif len(text) <= 60 and self.last_effort.get(lane) == "high":   # "go on" after real work: keep thinking hard
            effort = "high"
        else:
            effort = "medium"
        with self.lock:
            self.turn_effort[lane] = effort
            self.last_effort[lane] = effort
        return effort

    def search(self, query, limit=40):
        """Messages and answers (the whole history, newest first) that contain every word of `query`."""
        words = [w for w in str(query or "").lower().split() if w][:6]
        if not words or len("".join(words)) < 2:
            return []
        try:
            with open(HISTORY, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        hits = []
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("t")
            if kind == "answer":
                text, who = re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev.get("parts", []) if p[0] == "text")), "Claude"
            elif kind == "msg":
                text, who = ev.get("text", ""), ev.get("from", "")
            elif kind == "image":
                text, who = f"{ev.get('caption') or ''} {ev.get('name', '')}", ev.get("from", "")
            elif kind == "poll":
                text, who = ev.get("q", ""), ev.get("by", "")
            else:
                continue
            text = re.sub(r"[*`#_]{1,3}", "", text)   # markdown marks make poor snippets
            low = text.lower()
            if all(w in low for w in words):
                start = max(0, low.find(words[0]) - 60)
                hits.append({"id": ev.get("id"), "who": who, "lane": ev.get("lane", ""), "ts": ev.get("ts", 0),
                             "snip": ("…" if start else "") + " ".join(text[start:start + 220].split())})
                if len(hits) >= limit:
                    break
        return hits

    def inject(self, lane, content, raw=None, kind="msg"):
        """Hand a message to the turn running in `lane` (it reads it at its next step). False if none is running."""
        with self.lock:
            pipe = self.live_stdin.get(lane)
            b = self.busy.get(lane)
            if not pipe and b and not b.get("compacting") and is_external_model(self.running_models.get(lane)):
                self.midturn.setdefault(lane, []).append((content, kind, content if raw is None else raw))
                return True   # Codex reads it with its next tool result (with_midturn)
            if not pipe or (b or {}).get("compacting"):
                return False
            try:
                pipe.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
                pipe.flush()
                return True
            except (OSError, ValueError):
                self.live_stdin.pop(lane, None)
                return False

    def end_input(self, lane, proc):
        with self.lock:
            if self.live_stdin.get(lane) is proc.stdin:
                del self.live_stdin[lane]
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def threads_brief(self, lane):
        """One line per thread that worked in the last day (not this one): status, goal, summary."""
        meta, now = self.lane_meta_all(), time.time()
        lanes = {e.get("lane") or e.get("from") for e in list(self.history)[-200:]
                 if e.get("t") in ("msg", "answer") and now - e.get("ts", 0) < 86400}
        with self.lock:
            busy = dict(self.busy)
        rows = []
        for other in sorted(x for x in lanes if x and x != lane and owner(x) in self.cfg["people"]):
            m, b = meta.get(other, {}), busy.get(other)
            state = f"working for {int(now - b['since'])}s" if b and not b.get("compacting") else "idle"
            cards = self.lane_cards(other)
            rows.append(f"{self.lane_label(other)} ({state})" + ("".join(f" on #{c.get('num')} “{clip(c['title'], 60)}”" for c in cards[:2]))
                        + (f" for: {clip(m['goal'], 90)}" if m.get("goal") else "")
                        + (f" · so far: {clip(m['summary'], 170)}" if m.get("summary") else ""))
        return rows

    def files_in_use(self, lane):
        """Files the other running threads changed (or are changing) in their current turns."""
        with self.lock:
            running = {other: b["since"] for other, b in self.busy.items() if other != lane and not b.get("compacting")}
        with self.tool_lock:
            tools = [t for t in self.tools.values() if t["lane"] in running and t["ts"] >= running[t["lane"]] and t.get("ok") is not False
                     and (t["name"].endswith("write_file") or t["name"].endswith("uefn_call")
                          and re.search(r'"tool_name":\s*"(WriteFile|Replace|Delete|Move)"', t["input"]))]
        out = []
        for t in tools:
            for path in re.findall(r'"(?:path|sourcePath|destPath)":\s*"([^"]+)"', t["input"]):
                base = re.split(r"[\\/]+", path)[-1]
                out.append(f"{base} ({self.lane_label(t['lane'])})")
        return sorted(set(out))[:15]

    def find_lane(self, ref):
        """A thread from what Claude calls it: its id (sam/ideas), "sam", "sam · ideas" or just "ideas"."""
        ref = str(ref or "").strip().lower().replace("·", "/").replace(" / ", "/").replace(" ", "")
        lanes = list(self.cfg["people"]) + [t["id"] for t in self.threads_view() if not t.get("closed")]
        for lane in lanes:
            if ref in (lane.lower(), self.lane_label(lane).lower().replace("·", "/").replace(" ", "")):
                return lane
        hits = [t["id"] for t in self.threads_view() if not t.get("closed") and ref in (t["name"].lower().replace(" ", ""), t["id"].split("/")[-1])]
        return hits[0] if len(hits) == 1 else None

    def thread_note(self, from_lane, to, text):
        target = self.find_lane(to)
        text = clean_text(text, 1500)
        if not target:
            return False, "no such thread · threads_read lists them (e.g. sam, sam/ideas)"
        if target == from_lane:
            return False, "that's this thread"
        if not text:
            return False, "the note is empty"
        note = f"[room] note from the thread {self.lane_label(from_lane)}: {text}"
        # parent <-> sub-thread, or two threads of the same person (e.g. "the session is free"): an idle one wakes up for it.
        # Not a Main conversation, a closed thread, or another person's thread (those get it at their next turn).
        same_person = "/" in target and owner(target) == owner(from_lane) and not self.thread_closed(target)
        if self.related(from_lane, target) or same_person:
            how = self.wake(target, note, f"(room) Message from {self.lane_label(from_lane)}: {text}", from_lane)
            self.broadcast({"t": "sys", "text": f"📨 {self.lane_label(from_lane)} → {self.lane_label(target)}: {clip(text, 200)}"})
            return True, f"delivered to {self.lane_label(target)} {how}"
        delivered = self.inject(target, note, kind="note")
        if not delivered:
            with self.lock:
                self.thread_notes.setdefault(target, []).append(note)
        self.broadcast({"t": "sys", "text": f"📨 {self.lane_label(from_lane)} → {self.lane_label(target)}: {clip(text, 200)}"})
        return True, (f"delivered to {self.lane_label(target)} while it works" if delivered
                      else f"{self.lane_label(target)} gets it at the start of its next turn")

    def threads_text_full(self, lane):
        rows = self.threads_brief(lane) or ["(no other thread worked in the last day)"]
        files = self.files_in_use(lane)
        return ("Threads (this one is " + self.lane_label(lane) + "):\n" + "\n".join("- " + r for r in rows)
                + ("\nFiles the running threads are changing: " + ", ".join(files) if files else "")
                + "".join("\n" + x.replace("[room] ", "") for x in self.lease_lines(lane)))

    def rules_view(self, name):
        with self.notes_lock:
            r = self.notes["rules"]
            return {"room": dict(r.get("room") or {}), "me": (r.get("people", {}).get(name) or {}).get("text", "")}

    def write_house_rules(self):
        # CLAUDE.md imports this file, so every turn (and every thread) follows the room's rules
        with self.notes_lock:
            room = (self.notes["rules"].get("room") or {}).get("text", "").strip()
        try:
            os.makedirs(WORKSPACE, exist_ok=True)
            with open(os.path.join(WORKSPACE, "house-rules.md"), "w", encoding="utf-8") as f:
                f.write((room or "(none yet)") + "\n")
        except OSError:
            pass

    def set_rules(self, name, scope, text):
        text = clean_text(text, 4000)
        with self.notes_lock:
            rules = self.notes["rules"]
            if scope == "room":
                rules["room"] = {"text": text, "by": name, "ts": time.time()}
            else:
                rules.setdefault("people", {})[name] = {"text": text, "ts": time.time()}
            save_json_safely(NOTES, self.notes)
        if scope == "room":
            self.write_house_rules()
            self.broadcast({"t": "rules", "room": self.rules_view(name)["room"]})
            self.broadcast({"t": "sys", "text": f"🤖 {name} changed the room's rules for Claude"
                                                + (f": {clip(' '.join(text.split()), 180)}" if text else " (now there are none)")}, keep=True)
        else:
            self.send_to(name, {"t": "rules", "me": text})
            self.send_to(name, {"t": "sys", "text": "🤖 your own rules are saved: Claude follows them in your turns"})
        return True, ""

    def own_rules(self, person):
        with self.notes_lock:
            return (self.notes["rules"].get("people", {}).get(person) or {}).get("text", "")

    def account_of(self, name):
        with self.secrets_lock:
            return dict(self.secrets.get(owner(name)) or {})

    def account_view(self, name):
        a = self.account_of(name)
        return {"mode": a.get("mode", "plan"), "hint": ("…" + a["secret"][-4:]) if a.get("secret") else "",
                "ok": a.get("ok"), "msg": a.get("msg", "")}

    def account_env(self, lane):
        # a person's turns run on their own Claude subscription or API key when they set one
        a = self.account_of(lane)
        if a.get("ok") is not True:   # untested or rejected: stay on the host's plan rather than break their turns
            return {}
        if a.get("mode") == "api" and a.get("secret"):
            return {"ANTHROPIC_API_KEY": a["secret"]}
        if a.get("mode") == "oauth" and a.get("secret"):
            return {"CLAUDE_CODE_OAUTH_TOKEN": a["secret"]}
        return {}

    def secret_values(self):
        with self.secrets_lock:
            return [a["secret"] for a in self.secrets.values() if a.get("secret")]

    def set_account(self, name, mode, secret):
        if ai_paused():
            return False, AI_PAUSE_MESSAGE
        if mode not in ("plan", "oauth", "api"):
            return False, "unknown account type"
        secret = str(secret or "").strip()
        if mode == "plan":
            with self.secrets_lock:
                self.secrets.pop(name, None)
                save_json_safely(SECRETS, self.secrets)
            self.send_to(name, {"t": "account", **self.account_view(name)})
            self.broadcast({"t": "sys", "text": f"🤖 {name}'s turns use {self.cfg['host']}'s Claude plan again"}, keep=True)
            return True, ""
        old = self.account_of(name)
        if not secret and old.get("mode") == mode:
            secret = old.get("secret", "")
        if mode == "api" and not secret.startswith("sk-ant-api"):
            return False, "an API key starts with sk-ant-api… (console.anthropic.com → API Keys)"
        if mode == "oauth" and not secret.startswith("sk-ant-oat"):
            return False, "the token from `claude setup-token` starts with sk-ant-oat…"
        with self.secrets_lock:
            self.secrets[name] = {"mode": mode, "secret": secret, "ok": None, "msg": "testing…", "ts": time.time()}
            save_json_safely(SECRETS, self.secrets)
        self.send_to(name, {"t": "account", **self.account_view(name)})
        threading.Thread(target=self.test_account, args=(name,), daemon=True).start()
        return True, ""

    def test_account(self, name):
        if ai_paused():
            return
        a = self.account_of(name)   # tried with exactly this secret (account_env only hands out tested ones)
        env = {"ANTHROPIC_API_KEY": a.get("secret", "")} if a.get("mode") == "api" else {"CLAUDE_CODE_OAUTH_TOKEN": a.get("secret", "")}
        ok, msg = False, "no answer"
        try:
            os.makedirs(QUIET_DIR, exist_ok=True)
            r = subprocess.run([self.claude, "-p", "--model", SUMMARY_MODEL, "--strict-mcp-config", "--tools", "",
                                "--no-session-persistence", "--output-format", "json"],
                               input="Reply with the single word: ok", capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=QUIET_DIR, timeout=90,
                               env={**{k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")},
                                    "ENABLE_CLAUDEAI_MCP_SERVERS": "false", **env})
            try:
                d = json.loads(r.stdout)
                ok = r.returncode == 0 and not d.get("is_error")
                msg = "works" if ok else str(d.get("result") or "rejected")
            except ValueError:
                msg = ((r.stdout or "") + (r.stderr or "")).strip() or f"exit code {r.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            msg = str(e)
        msg = clip(self.redact(msg), 200)
        with self.secrets_lock:
            if name in self.secrets:
                self.secrets[name].update(ok=ok, msg=msg)
                save_json_safely(SECRETS, self.secrets)
        self.send_to(name, {"t": "account", **self.account_view(name)})
        if ok:
            what = "an API key" if self.account_of(name).get("mode") == "api" else "their own Claude account"
            self.broadcast({"t": "sys", "text": f"🤖 {name}'s turns now run on {what}, not {self.cfg['host']}'s plan"}, keep=True)

    def remember_undo(self, tid, user, bid, path, lane):
        with self.notes_lock:
            undos = self.notes["undos"]
            undos[tid] = {"user": user, "bid": bid, "path": path, "lane": lane, "ts": time.time()}
            for k in sorted(undos, key=lambda k: undos[k]["ts"])[:-300]:
                del undos[k]
            save_json_safely(NOTES, self.notes)
        self.broadcast({"t": "undo_ready", "tid": tid, "user": user, "path": path})

    def undos_view(self):
        with self.notes_lock:
            return {k: {"user": u["user"], "path": u["path"], **({"done": True} if u.get("done") else {})}
                    for k, u in self.notes["undos"].items()}

    def undo(self, name, tid):
        with self.notes_lock:
            u = dict(self.notes["undos"].get(tid) or {})
        if not u:
            return False, "there's nothing to undo on that line"
        if u["user"] != name:
            return False, f"only {u['user']} can undo a change on their own PC"
        if u.get("done"):
            return False, "that change was already undone"
        threading.Thread(target=self._undo, args=(name, tid, u), daemon=True).start()
        return True, ""

    def _undo(self, name, tid, u):
        ok, out = self.exec([name], name, "undo_write", {"bid": u["bid"], "path": u["path"]}, by_owner=True)
        if not ok:
            self.send_to(name, {"t": "sys", "text": f"↶ couldn't undo: {clip(out, 300)}"})
            self.broadcast({"t": "undo_failed", "tid": tid})
            return
        with self.notes_lock:
            if tid in self.notes["undos"]:
                self.notes["undos"][tid]["done"] = True
                save_json_safely(NOTES, self.notes)
        self.broadcast({"t": "undo_done", "tid": tid})
        self.broadcast({"t": "sys", "text": f"↶ {name} undid Claude's change to {os.path.basename(u['path'])}: {clip(out, 200)}"},
                       keep=True)

    # ---- who's doing what: their PC (UEFN open, playtesting) and their keyboard ----
    def activity_report(self, name, body):
        src = "web" if body.get("web") else "pc"
        apps = [a for a in (body.get("apps") or []) if a in ("uefn", "fortnite")] if src == "pc" else []
        try:
            idle = max(0, int(body["idle"])) if body.get("idle") is not None else None
        except (TypeError, ValueError):
            idle = None
        before = self.activity_view()
        with self.lock:
            self.activity.setdefault(name, {})[src] = {"apps": apps, "idle": idle, "ts": time.time(),
                                                       **({"auto": bool(body["auto"])} if src == "pc" and "auto" in body else {})}
        after = self.activity_view()
        if after != before:
            self.broadcast({"t": "activity", "activity": after})

    def activity_of(self, name):
        with self.lock:
            a = dict(self.activity.get(name, {}))
        now = time.time()
        pc = a.get("pc") if a.get("pc") and now - a["pc"]["ts"] < 90 else None
        apps = pc["apps"] if pc else []
        idles = [x["idle"] for x in (pc, a.get("web")) if x and now - x["ts"] < 150 and x.get("idle") is not None]
        state = ("playtest" if "uefn" in apps and "fortnite" in apps else "fortnite" if "fortnite" in apps
                 else "uefn" if "uefn" in apps else "")
        return {"state": state, "away": bool(idles) and min(idles) >= 600}

    def activity_view(self):
        view = {}
        for name in self.cfg["people"]:
            a = self.activity_of(name)
            key = (a["state"], a["away"])
            with self.lock:
                old = self.activity_since.get(name)
                if not old or old[0] != key:
                    self.activity_since[name] = old = (key, time.time())
            if a["state"] or a["away"]:
                view[name] = {**a, "since": round(old[1])}
        return view

    def activity_text(self, name):
        a = self.activity_of(name)
        bits = [{"playtest": "playtesting in Fortnite", "fortnite": "in Fortnite", "uefn": "UEFN open"}.get(a["state"], ""),
                "away from the keyboard" if a["away"] else ""]
        bits = [b for b in bits if b]
        return f", now: {', '.join(bits)}" if bits else ""

    # ---- ★ saved messages (each person their own) ------------------------
    def stars_of(self, name):
        with self.notes_lock:
            return list(self.notes["stars"].get(name, []))

    def star(self, name, mid):
        ev = next((e for e in reversed(self.history) if e.get("id") == mid), None)
        with self.notes_lock:
            mine = self.notes["stars"].setdefault(name, [])
            have = next((s for s in mine if s["id"] == mid), None)
            if have:
                mine.remove(have)
            elif ev:
                if ev.get("t") == "answer":
                    text = re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text"))
                else:
                    text = ev.get("text") or (f"poll: {ev['q']}" if ev.get("q") else f"picture {ev.get('name', '')}")
                mine.append({"id": mid, "ts": time.time(), "who": ev.get("assistant", "Claude") if ev.get("t") == "answer" else ev.get("from") or ev.get("by", ""),
                             "text": clip(" ".join(text.split()), 300)})
                del mine[:-200]
            else:
                return False, "that message is gone"
            save_json_safely(NOTES, self.notes)
            now = list(mine)
        self.send_to(name, {"t": "stars", "stars": now})
        return True, ""

    def saved_text(self, name):
        mine = self.stars_of(name)
        if not mine:
            return "nothing saved yet: ☆ on a message in the browser saves it"
        return "★ saved:\n" + "\n".join(f"  {s['who']}, {time.strftime('%d.%m %H:%M', time.localtime(s['ts']))}: {clip(s['text'], 160)}"
                                        for s in mine[-15:])

    # ---- ⏰ reminders --------------------------------------------------------
    def reminder_add(self, who, spec, lane=None):
        words = spec.split()
        to = "all"
        if words:
            w = words[0].lstrip("@").lower()
            if w in ("me", "all", "everyone", "всички", "мен") or w in self.cfg["people"]:
                words.pop(0)
                to = "all" if w in ("all", "everyone", "всички") else [owner(lane or who)] if w in ("me", "мен") else [w]
        at, text = parse_remind(" ".join(words))
        text = clean_text(text, 300)
        if not at or not text:
            return False, ("how to: /remind 18:00 playtest · /remind 30m check the build · /remind me tomorrow 10:00 … · "
                           "/remind @sam 2h … · /reminders lists them")
        if at > time.time() + 31 * 86400:
            return False, "reminders go up to a month ahead"
        r = {"id": "r" + secrets.token_hex(3), "by": who, "to": to, "at": at, "text": text}
        with self.notes_lock:
            self.notes["reminders"].append(r)
            save_json_safely(NOTES, self.notes)
        line = (f"⏰ {who} set a reminder for {time.strftime('%a %H:%M', time.localtime(at))}"
                + (" (everyone)" if to == "all" else f" ({', '.join(to)})") + f": {text}")
        if to == "all":
            self.broadcast({"t": "sys", "text": line})
        else:
            for p in set(to) | ({who} if who in self.cfg["people"] else set()):
                self.send_to(p, {"t": "sys", "text": line})
        return True, line

    def reminders_for(self, name):
        with self.notes_lock:
            return sorted((r for r in self.notes["reminders"] if r["to"] == "all" or name in r["to"] or r["by"] == name),
                          key=lambda r: r["at"])

    def reminders_text(self, name):
        mine = self.reminders_for(name)
        if not mine:
            return "no reminders · /remind 18:00 playtest sets one"
        return "⏰ reminders:\n" + "\n".join(
            f"  {i}. {time.strftime('%a %d.%m %H:%M', time.localtime(r['at']))} · {r['text']} (by {r['by']}, "
            + ("everyone" if r["to"] == "all" else ", ".join(r["to"])) + ")" for i, r in enumerate(mine, 1)) + "\n/remind cancel N removes one"

    def reminder_cancel(self, name, which):
        mine = self.reminders_for(name)
        if not which.isdigit() or not 1 <= int(which) <= len(mine):
            return False, "which one? /reminders lists them, then /remind cancel N"
        r = mine[int(which) - 1]
        if name not in (r["by"], self.cfg["host"]) and r["by"] not in ("Claude", "Codex", "Gemini"):
            return False, f"only {r['by']} (or the host) can cancel that one"
        with self.notes_lock:
            self.notes["reminders"] = [x for x in self.notes["reminders"] if x["id"] != r["id"]]
            save_json_safely(NOTES, self.notes)
        return True, f"cancelled: {r['text']}"

    def watch_reminders(self):
        while True:
            time.sleep(15)
            now = time.time()
            with self.notes_lock:
                due = [r for r in self.notes["reminders"] if r["at"] <= now]
                if due:
                    self.notes["reminders"] = [r for r in self.notes["reminders"] if r["at"] > now]
                    save_json_safely(NOTES, self.notes)
            for r in due:
                late = now - r["at"] > 600   # the room was off when it was due
                text = f"⏰ {r['text']} (reminder from {r['by']}" + (", late: the room was off at the time" if late else "") + ")"
                people = list(self.cfg["people"]) if r["to"] == "all" else [p for p in r["to"] if p in self.cfg["people"]]
                if r["to"] == "all":
                    self.broadcast({"t": "sys", "text": text}, keep=True)
                else:
                    for p in people:
                        self.send_to(p, {"t": "sys", "text": text})
                for p in people:
                    self.notify(p, "reminder", f"⏰ {r['text']}", f"from {r['by']}")

    def mark_done(self, lane, texts, aid=None, who="Claude"):
        # the AI ticks ✅ on its own final answer for the task (only adds, never takes a tick away)
        mid = aid if aid and any(e.get("id") == aid for e in list(self.history)[-50:]) else None
        if not mid:
            return
        with self.notes_lock:
            names = self.reactions.setdefault(mid, {}).setdefault("✅", [])
            if who in names:
                return
            names.append(who)
            save_json_safely(REACTIONS, self.reactions)
            now = dict(self.reactions[mid])
        self.broadcast({"t": "react", "id": mid, "r": now, "by": who, "e": "✅", "added": True})

    def reactions_view(self):
        with self.notes_lock:
            return json.loads(json.dumps(self.reactions))

    def react(self, who, mid, emoji):
        if emoji not in REACTS:
            return False, "react with one of " + " ".join(REACTS)
        ev = next((x for x in reversed(self.history) if x.get("id") == mid), None)
        if not ev and mid not in self.current_aid.values():
            return False, "that message is gone"
        with self.notes_lock:
            r = self.reactions.setdefault(mid, {})
            names = r.setdefault(emoji, [])
            added = who not in names
            if added:
                names.append(who)
            else:
                names.remove(who)
            if not names:
                del r[emoji]
            if not r:
                del self.reactions[mid]
            save_json_safely(REACTIONS, self.reactions)
            now = dict(self.reactions.get(mid, {}))
        if added and ev:
            text = ("".join(p[1] for p in ev["parts"] if p[0] == "text") if ev.get("t") == "answer"
                    else ev.get("text") or ev.get("q") or ev.get("name", ""))
            text = re.sub(r"\[for [^\]]*\]\s*", "", text)
            self.react_log = (self.react_log + [{"ts": time.time(), "who": who, "e": emoji, "on_claude": ev.get("t") == "answer",
                                                 "snip": clip(" ".join(text.split()), 60)}])[-50:]
        self.broadcast({"t": "react", "id": mid, "r": now, "by": who, "e": emoji, "added": added})
        return True, ""

    # ---- polls ----------------------------------------------------------
    def poll_state(self, ev):
        if ev.get("t") != "poll":
            return {}
        with self.polls_lock:
            p = self.polls.get(ev.get("id"))
            return json.loads(json.dumps(p)) if p else {"gone": True}

    @staticmethod
    def poll_counts(p):
        return [sum(1 for v in p["votes"].values() if v == i) for i in range(len(p["opts"]))]

    def poll_text(self, p):
        return (f"“{p['q']}” (" + ", ".join(f"{o}: {c}" for o, c in zip(p["opts"], self.poll_counts(p)))
                + (", closed" if p["closed"] else ", open") + f", started by {p['by']})")

    def polls_since(self, since):
        with self.polls_lock:
            return [self.poll_text(p) for p in self.polls.values() if p.get("upd", 0) > since][-5:]

    def poll_create(self, who, question, options):
        q = clean_text(question, 200)
        opts = [clean_text(o, 80) for o in (options if isinstance(options, list) else [])[:6] if clean_text(o, 80)]
        if not q or len(opts) < 2:
            return False, "a poll needs a question and 2 to 6 options: /poll Which theme? | Desert | Jungle"
        pid = "q" + secrets.token_hex(4)
        poll = {"q": q, "opts": opts, "votes": {}, "by": who, "ts": time.time(), "upd": time.time(), "closed": False}
        with self.polls_lock:
            self.polls[pid] = poll
            save_json_safely(POLLS, self.polls)
        ev = self.record({"t": "poll", "id": pid, "by": who, "q": q})
        self.broadcast({**ev, **json.loads(json.dumps(poll))})
        return True, f"poll started (id {pid})"

    def poll_vote(self, who, pid, opt=None, close=False):
        with self.polls_lock:
            p = self.polls.get(pid)
            if not p:
                return False, "that poll is gone"
            if close:
                if who not in (p["by"], self.cfg["host"]) and p["by"] not in ("Claude", "Codex", "Gemini"):
                    return False, "only whoever started the poll (or the host) can close it"
                p["closed"] = True
            else:
                if p["closed"]:
                    return False, "that poll is closed"
                try:
                    i = int(opt)
                except (TypeError, ValueError):
                    return False, "vote with an option number"
                if not 0 <= i < len(p["opts"]):
                    return False, f"the poll has options 1 to {len(p['opts'])}"
                if p["votes"].get(who) == i:
                    del p["votes"][who]
                else:
                    p["votes"][who] = i
            p["upd"] = time.time()
            save_json_safely(POLLS, self.polls)
            poll = json.loads(json.dumps(p))
        self.broadcast({"t": "poll_update", "id": pid, **poll})
        if close:
            counts = self.poll_counts(poll)
            best = max(counts)
            won = [o for o, c in zip(poll["opts"], counts) if c == best and best]
            self.broadcast({"t": "sys", "text": f"📊 {who} closed the poll “{poll['q']}”: " + (
                f"{' / '.join(won)} won ({best} vote{'' if best == 1 else 's'})" if won else "no votes")}, keep=True)
        return True, ""

    # ---- "previously on": what happened while someone was away ---------
    def recap(self, name, since):
        lanes = {}
        for ev in list(self.history):
            if ev.get("ts", 0) <= since or ev.get("t") not in ("msg", "answer"):
                continue
            lane = ev.get("lane") or ev.get("from") or ""
            c = lanes.setdefault(lane, {"msgs": 0, "last": ""})
            if ev["t"] == "msg":
                c["msgs"] += 1
            else:
                text = re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text")).strip()
                c["last"] = clip(text.splitlines()[0] if text else "", 200)
        meta = self.lane_meta_all()
        threads = [{"lane": lane, "label": self.lane_label(lane), "msgs": c["msgs"],
                    "summary": meta.get(lane, {}).get("summary") or c["last"]} for lane, c in lanes.items() if lane]
        moments = [{"id": e["id"], "title": e["title"], "kind": e["kind"],
                    "pics": [a["file"] for a in e["att"] if a.get("image")][:1]}
                   for e in self.timeline_view() if e["ts"] > since][-10:]
        with self.lock:
            running = [self.lane_label(lane) for lane, b in self.busy.items() if not b.get("compacting")]
        with self.board_lock:
            boards = {b["id"]: b for b in self.board["boards"]}
            mine = []
            for c in self.board["cards"]:
                b = boards.get(c.get("board"))
                done = not b or c["col"] in ("done", b["columns"][-1]["id"])
                if c.get("who") == name and not done:
                    mine.append({"num": c.get("num"), "title": c["title"]})
        with self.polls_lock:
            polls = [{"id": pid, "q": p["q"]} for pid, p in self.polls.items() if not p["closed"] and name not in p["votes"]]
        return {"t": "recap", "since": since, "threads": threads, "moments": moments, "running": running, "mine": mine[:5], "polls": polls}

    def recap_text(self, r):
        lines = [f"📼 Previously on Claude Room (since {time.strftime('%a %H:%M', time.localtime(r['since']))}):"]
        lines += [f"  # {t['label']}: {t['summary'] or '…'} ({t['msgs']} messages)" for t in r["threads"]]
        if r["moments"]:
            lines.append("  timeline: " + " · ".join(m["title"] for m in r["moments"][-6:]))
        if r["running"]:
            lines.append("  running now: " + ", ".join(r["running"]))
        if r["mine"]:
            lines.append("  waiting for you: " + ", ".join(f"#{c['num']} {c['title']}" for c in r["mine"]))
        if r["polls"]:
            lines.append("  vote on: " + ", ".join(p["q"] for p in r["polls"]) + " (/vote N)")
        if len(lines) == 1:
            lines.append("  nothing happened")
        return "\n".join(lines)

    # ---- numbers for the credits, and secrets found ---------------------
    def stats(self):
        s = {"messages": 0, "answers": 0, "tool_calls": 0, "claude_secs": 0, "pictures": 0, "first": None, "by": {}}
        try:
            with open(HISTORY, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    s["first"] = s["first"] or ev.get("ts")
                    if ev.get("t") == "msg":
                        s["messages"] += 1
                        s["by"][ev.get("from", "?")] = s["by"].get(ev.get("from", "?"), 0) + 1
                    elif ev.get("t") == "answer":
                        s["answers"] += 1
                        s["tool_calls"] += sum(1 for p in ev.get("parts", []) if p[0] == "tool")
                        s["claude_secs"] += ev.get("secs") or 0
                    elif ev.get("t") == "image":
                        s["pictures"] += 1
        except OSError:
            pass
        tl = self.timeline_view()
        s.update(moments=len(tl), fixed=sum(1 for e in tl if e["kind"] == "fix"), version=ROOM_VERSION,
                 people=list(self.cfg["people"]), threads=len(self.threads_view()))
        return s

    def egg(self, who, what):
        lines = {"pages": f"🕯 {who} found all 8 pages. Something in the dark noticed.",
                 "tape": f"📼 {who} found an old VHS tape in the room."}
        if what not in lines:
            return
        with self.notes_lock:
            found = self.notes.setdefault("eggs", {}).setdefault(who, [])
            if what in found:
                return
            found.append(what)
            save_json_safely(NOTES, self.notes)
        self.broadcast({"t": "sys", "text": lines[what]}, keep=True)

    def pref(self, name):
        """(model id, effort) this person's turns run with (all their threads; a thread may have its own, e.g. for a model test)."""
        prefs = self.cfg.get("prefs", {})
        lane_pref = prefs.get(name, {}) if "/" in str(name or "") else {}
        if lane_pref.get("model") in MODELS.values():
            return lane_pref["model"], lane_pref.get("effort") or ""
        p = prefs.get(owner(name), {})
        return p.get("model") or self.cfg.get("model") or "", p.get("effort") or self.cfg.get("effort") or ""

    def efforts_for(self, model):
        if is_gemini_model(model):
            return tuple(GEMINI_MODELS[gemini_model_id(model)]["efforts"])
        if is_codex_model(model):
            info = CODEX_MODELS.get(codex_model_id(model), {})
            return tuple(e["reasoningEffort"] for e in info.get("supportedReasoningEfforts", []))
        return tuple(e for e in EFFORTS if e != "auto")

    def models_info(self):
        rows = []
        for key, mid in MODELS.items():
            codex = is_codex_model(mid)
            info = CODEX_MODELS.get(codex_model_id(mid), {}) if codex else {}
            gemini = GEMINI_MODELS.get(gemini_model_id(mid), {}) if is_gemini_model(mid) else {}
            rows.append({"key": key, "id": mid, "provider": "gemini" if is_gemini_model(mid) else "codex" if codex else "claude",
                         "label": info.get("displayName") if codex else model_label(mid) if mid else "Default",
                         "desc": gemini.get("description") or info.get("description", MODEL_INFO.get(key, "")),
                         "efforts": list(self.efforts_for(mid)),
                         "default_effort": gemini.get("default_effort") or info.get("defaultReasoningEffort", ""),
                         "default_model": CODEX_DEFAULT_MODEL if mid == "codex" else ""})
        return rows

    def set_pref(self, name, model=None, effort=None, mute_approval=None):
        p = self.cfg.setdefault("prefs", {}).setdefault(name, {})
        if mute_approval is not None and model is None and effort is None:   # just the sound: no "assistant is now" line
            p["mute_approval_sound"] = bool(mute_approval)
            save_json(CONFIG, self.cfg)
            self.send_to(name, {"t": "settings", **self.settings_view(name)})
            return
        model_selected = model is not None and model in MODELS.values()
        if model_selected:
            p["model"] = model
        allowed = self.efforts_for(self.pref(name)[0]) + ("", "auto")
        effort_selected = effort is not None and effort in allowed
        if effort_selected:
            p["effort"] = effort
        if p.get("effort", "") not in allowed:
            p["effort"] = ""
        if "/" not in name and (model_selected or effort_selected):
            # The visible picker applies to all of this person's threads.
            # Old per-thread test pins must not silently override a new choice.
            for lane, lane_pref in self.cfg["prefs"].items():
                if "/" in lane and owner(lane) == name:
                    lane_pref.pop("model", None)
                    lane_pref.pop("effort", None)
        save_json(CONFIG, self.cfg)
        m, e = self.pref(name)
        self.send_to(name, {"t": "settings", **self.settings_view(name)})
        self.broadcast({"t": "sys", "text": f"{name}'s assistant is now {model_label(m)}" + (f" · {e} effort" if e else "")})

    def settings_view(self, name):
        m, e = self.pref(name)
        return {"model": m, "model_label": model_label(m), "effort": e,
                "mute_approval_sound": bool(self.cfg.get("prefs", {}).get(name, {}).get("mute_approval_sound"))}

    def pchat_clear(self, name):
        """The host wipes the people chat; the old messages stay in a backup file on the host."""
        if name != self.cfg["host"]:
            return False, "only the host can clear the people chat"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            if os.path.exists(PEOPLE_LOG):
                shutil.copyfile(PEOPLE_LOG, os.path.join(ROOT, f"people.backup-{stamp}.jsonl"))
            open(PEOPLE_LOG, "w", encoding="utf-8").close()
        except OSError as e:
            return False, f"couldn't clear it: {e}"
        with self.lock:
            n = len(self.pchat)
            self.pchat = []
        self.broadcast({"t": "pchat_clear", "by": name})
        self.broadcast({"t": "sys", "text": f"🧹 {name} cleared the people chat ({n} messages; a backup is kept on the host)"}, keep=True)
        return True, "cleared"

    def pc_names(self):
        with self.lock:
            return sorted(self.agents)

    def online(self):
        with self.lock:
            return sorted({n for n, _ in self.clients})

    def connect(self, name, agent_info=None, kind="browser", version=0, update_url=""):
        q = queue.Queue()
        latest = client_version()
        with self.lock:
            busy = {n: int(time.time() - b["since"]) for n, b in self.busy.items()}
        with open(CONNECT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} connect {name} kind={kind} v={version} "
                    f"pc={'yes ' + str(agent_info) if agent_info else 'no'}\n")
        q.put({"t": "hello", "you": name, "host": self.cfg["host"], "colors": self.colors,
               "mode": "your PC shared" if agent_info else "no PC shared", "busy": list(busy), "busy_secs": busy,
               "model": model_label(self.cfg.get("model")), "chat_version": latest, "web_version": web_version(),
               "notes": {str(v): RELEASE_NOTES[v] for v in sorted(RELEASE_NOTES)[-4:]}, "locks": self.locks_view(),
               "settings": self.settings_view(name), "operator": self.operator_view(name), "ai_paused": ai_paused(),
               "models": self.models_info(), "efforts": list(EFFORTS), "threads": self.threads_view(), "usage": self.usage_view(),
               "room_version": ROOM_VERSION, "lane_meta": self.lane_meta_all(), "reacts": list(REACTS),
               "undos": self.undos_view(), "activity": self.activity_view(), "stars": self.stars_of(name),
               "rules": self.rules_view(name), "account": self.account_view(name), "sync": self.sync_view(),
               "leases": dict(self.leases), "zones": self.zones_view()})
        with self.pins_lock:
            pins_by = {}
            for pin in self.pins:
                pins_by.setdefault(pin["target"], []).append(pin)
        for ev in list(self.history):
            q.put({**ev, **self.poll_state(ev), "replay": True})
            for pin in pins_by.get(ev.get("id"), []):
                q.put({"t": "pin", "op": "add", "pin": pin, "replay": True})
        for lane, secs in busy.items():   # a turn still running: its answer so far, tool calls included
            aid = self.current_aid.get(lane)
            if not aid:
                continue
            model, effort = self.pref(lane)
            model = self.running_models.get(lane, model)
            q.put({"t": "batch", "names": [lane], "lane": lane, "aid": aid, "secs": secs, "replay": True,
                   "model": model_label(model) if model else "", "effort": effort, "assistant": assistant_name(model)})
            q.put({"t": "live", "lane": lane, "parts": json.loads(json.dumps(self.live_parts.get(lane, []))), "replay": True})
            for pin in pins_by.get(aid, []):
                q.put({"t": "pin", "op": "add", "pin": pin, "replay": True})
        q.put({"t": "reactions", "all": self.reactions_view()})
        with self.lock:
            pchat = list(self.pchat[-200:])
        q.put({"t": "pchat", "items": pchat})
        q.put({"t": "replay_end"})
        prev = self.seen_at(name)
        self.prev_seen[name] = prev
        if prev and time.time() - prev >= RECAP_AFTER:   # back after a while: what happened meanwhile
            r = self.recap(name, prev)
            if r["threads"] or r["moments"]:
                q.put(r if kind == "browser" else {"t": "sys", "text": self.recap_text(r)})
        if kind == "browser":   # approvals still waiting for this person show up in a newly opened tab too
            for entry in list(self.execs.values()):
                if entry["user"] == name and entry.get("ask") and not entry["started"].is_set():
                    q.put({**entry["ask"], "timeout": max(0.0, entry["ask"]["until"] - time.time())})
        if kind == "chat.py" and version < MIN_PC_VERSION:
            q.put({"t": "sys", "text": "your chat.py is outdated, so Claude can't do things on your PC. "
                                       f"Update it: curl.exe -o chat.py {update_url}/chat.py  (Mac/Linux: curl)  then run it again"})
        elif kind == "chat.py" and version < latest:
            q.put({"t": "sys", "text": "a newer chat.py is out: close this window and open your claude-room .bat again "
                                       f"(or: curl.exe -o chat.py {update_url}/chat.py)"})
        with self.lock:
            self.clients.append((name, q))
            self.conn_info[q] = {"kind": kind, "version": version, "agent": agent_info}
            if agent_info:
                self.agents[name] = {"q": q, "info": agent_info}
        if agent_info:
            self.redeliver_execs(name)
        self.broadcast({"t": "presence", "online": self.online(), "pcs": self.pc_names(), "versions": self.terminal_versions()})
        return q

    def disconnect(self, name, q):
        with self.lock:
            self.clients = [c for c in self.clients if c[1] is not q]
            self.conn_info.pop(q, None)
            if self.agents.get(name, {}).get("q") is q:
                del self.agents[name]
                # a second window of the same person closed: hand PC sharing back to one still open
                for other_name, other_q in reversed(self.clients):
                    info = self.conn_info.get(other_q, {}).get("agent")
                    if other_name == name and info:
                        self.agents[name] = {"q": other_q, "info": info}
                        break
        self.redeliver_execs(name)   # an approval this window had goes to the one still (or newly) open
        with open(CONNECT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} disconnect {name}\n")
        self.mark_seen(name, save=True)
        self.broadcast({"t": "presence", "online": self.online(), "pcs": self.pc_names(), "versions": self.terminal_versions()})

    def watch_files(self):
        """chat.py and web.html are served straight from disk, so publishing a new one needs
        no restart: tell everyone the moment it changes (a chat.py counts once VERSION goes up)."""
        chat, web = client_version(), web_version()
        while True:
            time.sleep(2)
            try:
                new_chat, new_web = client_version(), web_version()
            except OSError:   # caught mid-write
                continue
            if new_chat <= chat and new_web == web:
                continue
            chat, web = max(chat, new_chat), new_web
            self.broadcast({"t": "update_available", "chat": chat, "web": web,
                            "notes": {str(v): RELEASE_NOTES[v] for v in sorted(RELEASE_NOTES)[-4:]}})
            with self.lock:   # older chat.py only understand a plain note
                old = [q for _, q in self.clients if self.conn_info.get(q, {}).get("kind") == "chat.py"
                       and self.conn_info[q]["version"] < min(chat, UPDATE_COMMAND_VERSION)]
            for q in old:
                q.put({"t": "sys", "text": f"⬆ chat.py v{chat} is out: close this window and open your claude-room "
                                           ".bat again to get it"})

    # ---- the host's Claude plan usage (everyone in the room spends it) ----------
    def host_account(self):
        acct = self.cfg.get("host_account") or ("personal" if self.cfg.get("host_oauth_token") else "team")
        return acct if acct in HOST_ACCOUNTS and (acct == "team" or self.cfg.get("host_oauth_token")) else "team"

    def apply_host_account(self):
        """New Claude runs inherit this: the personal plan's token, or none (the PC's own team login)."""
        if self.host_account() == "personal":
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.cfg["host_oauth_token"]
        else:
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    def team_stop(self):
        try:
            return max(1, min(100, int(self.cfg.get("team_stop_percent") or TEAM_STOP_DEFAULT)))
        except (TypeError, ValueError):
            return TEAM_STOP_DEFAULT

    def team_blocked(self, lane):
        """On the team plan, past the stop line: its weekly limit, once it crossed it (empty = go on)."""
        if self.host_account() != "team" or self.account_env(lane):   # a person on their own account isn't spending it
            return []
        line = self.team_stop()
        return [l for l in (self.usage_by.get("team") or {}).get("limits", [])
                if l["kind"] == "weekly_all" and l["percent"] >= line and (not l.get("resets") or l["resets"] > time.time())]

    def team_near(self):
        """On the team plan and close to its stop line: worth looking at usage more often."""
        week = [l for l in (self.usage_by.get("team") or {}).get("limits", []) if l["kind"] == "weekly_all"]
        return self.host_account() == "team" and any(l["percent"] >= self.team_stop() - TEAM_NEAR for l in week)

    def stop_team_turns(self):
        """Past the team stop line, turns already running on it stop too (new ones don't start)."""
        with self.lock:
            procs = [(lane, p) for lane, p in self.procs.items() if p and p.poll() is None]
        hit = [(lane, p) for lane, p in procs if self.team_blocked(lane)]
        if not hit:
            return
        lanes = {lane for lane, _ in hit}
        with self.lock:
            self.halted.update(lanes)
        for entry in list(self.execs.values()):
            if entry["lane"] in lanes:
                entry["result"] = (False, "stopped by the room")
                entry["started"].set()
                entry["done"].set()
        for lane, proc in hit:
            self.stopping.add(lane)
            kill_tree(proc)
        self.broadcast({"t": "sys", "text": f"⏸ the team plan reached its stop line ({self.team_stop()}% of the week), "
                        f"so {', '.join(map(self.lane_label, sorted(lanes)))} stopped. {self.cfg['host']} can switch to "
                        "the personal plan in ◔ Usage."}, keep=True)

    def set_host_account(self, name, account=None, stop=None):
        if name != self.cfg["host"]:
            return False, f"only {self.cfg['host']} (the host) can change which plan the room uses"
        if account is not None:
            if account not in HOST_ACCOUNTS:
                return False, "unknown account"
            if account == "personal" and not self.cfg.get("host_oauth_token"):
                return False, "no personal token in room.json (host_oauth_token)"
            self.cfg["host_account"] = account
        if stop is not None:
            try:
                self.cfg["team_stop_percent"] = max(1, min(100, int(stop)))
            except (TypeError, ValueError):
                return False, "the stop line is a percent, 1-100"
        save_json(CONFIG, self.cfg)
        self.apply_host_account()
        self.usage = self.usage_by.get(self.host_account()) or self.usage
        if account is not None:   # a new stop line only redraws the panel; a switch is news for everyone
            self.usage_warned = {}
            self.broadcast({"t": "sys", "text": f"🔀 {name}: Claude turns run on {HOST_ACCOUNTS[self.host_account()]}"
                            + (f", stopping at {self.team_stop()}%" if self.host_account() == "team" else "")}, keep=True)
        self.push_usage()
        self.usage_kick.set()
        return True, ""

    def watch_usage(self):
        while True:
            for acct in HOST_ACCOUNTS:
                if acct == "personal" and not self.cfg.get("host_oauth_token"):
                    continue
                try:
                    fresh, error = fetch_usage(acct), ""
                except (OSError, ValueError, KeyError, TypeError) as e:   # offline, login expired (the next turn renews it), ...
                    fresh, error = None, (f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__)
                old = self.usage_by.get(acct) or {}
                if fresh:
                    self.usage_by[acct] = {**fresh, "ts": time.time(), "error": ""}
                elif old:
                    self.usage_by[acct] = {**old, "error": error}
                else:
                    self.usage_by[acct] = {"limits": [], "plan": "", "ts": 0, "error": error}
            self.usage = self.usage_by.get(self.host_account()) or {"limits": [], "plan": "", "ts": 0, "error": ""}
            sig = json.dumps({a: (u.get("limits"), u.get("error")) for a, u in self.usage_by.items()}, sort_keys=True)
            if sig != self.usage_sig:
                self.usage_sig = sig
                self.push_usage()
            if self.usage.get("limits") and not self.usage.get("error"):
                self.usage_warnings(self.usage["limits"])
            self.stop_team_turns()
            self.usage_kick.wait(TEAM_NEAR_EVERY if self.team_near() else USAGE_EVERY)
            self.usage_kick.clear()
            time.sleep(15)   # a burst of turns ending still means one look

    def usage_warnings(self, limits):
        for lim in limits:
            level = max([w for w in USAGE_WARN if lim["percent"] >= w], default=0)
            sent, window = self.usage_warned.get(lim["kind"], (0, None))
            try:   # to the minute: the API's reset time jitters by microseconds on every poll
                resets = round(float(lim.get("resets") or 0) / 60)
            except (TypeError, ValueError):
                resets = lim.get("resets")
            if window != resets:   # a new window: start over
                sent = 0
            self.usage_warned[lim["kind"]] = (max(sent, level), resets)
            if level > sent:
                for person in self.cfg["people"]:
                    self.notify(person, "usage", f"Claude usage: {lim['label'][:1].lower() + lim['label'][1:]} at {lim['percent']}%",
                                f"resets {until_text(lim['resets'])}")

    def too_big(self, lane):
        limit = self.cfg.get("compact_at") or 0
        with self.state_lock:
            info = self.state["lanes"].get(lane, {})
            ctx, failed = info.get("ctx") or 0, info.get("compact_failed") or 0
        return bool(limit) and ctx > limit and ctx > failed * 1.25   # a failed one is tried again once it grew

    def compact_lane(self, lane, how=COMPACT_HOW):
        """Claude Code's /compact on a thread's own session: a summary replaces the long history.
        Every request re-reads the whole conversation, so this is what keeps usage down."""
        if ai_paused() or self.team_blocked(lane):
            return "stopped"
        with self.state_lock:
            generation = self.generation
            info = self.state["lanes"].get(lane, {})
            sid, before = info.get("session"), info.get("ctx") or 0
        if not sid:
            return "nothing"
        label = self.lane_label(lane)
        self.broadcast({"t": "sys", "text": f"🗜 compacting {label}'s conversation ({before // 1000}k tokens)…"})
        model, _ = self.pref(lane)
        env = {**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false", **self.account_env(lane)}
        meta = None
        for use_model in ([model, None] if model else [None]):   # the picked model may have a smaller window
            args = [self.claude, "-p", "--output-format", "stream-json", "--verbose", "--strict-mcp-config", "--resume", sid]
            args += ["--model", use_model] if use_model else []
            try:
                proc = subprocess.Popen(args, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace", bufsize=1)
            except OSError:
                break
            with self.lock:
                self.procs[lane] = proc
            proc.stdin.write("/compact " + how)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "system" and o.get("subtype") == "compact_boundary":
                    meta = o.get("compact_metadata") or {}
            proc.wait()
            with self.lock:
                if self.procs.get(lane) is proc:
                    del self.procs[lane]
            if lane in self.stopping:
                self.stopping.discard(lane)
                return "stopped"
            if meta is not None:
                break
        if meta is None:
            with self.state_lock:
                if lane in self.state["lanes"]:
                    self.state["lanes"][lane]["compact_failed"] = before
                    save_json(STATE, self.state)
            self.broadcast({"t": "sys", "text": f"🗜 couldn't compact {label}'s conversation (it carries on as it was)"}, keep=True)
            return "failed"
        pre, post = meta.get("pre_tokens") or before, meta.get("post_tokens") or 0
        with self.state_lock:
            if generation == self.generation and lane in self.state["lanes"]:
                self.state["lanes"][lane].update(ctx=post, compacted=time.time(), compact_failed=0)
                save_json(STATE, self.state)
        self.broadcast({"t": "sys", "text": f"🗜 compacted {label}'s conversation: {pre // 1000}k → {max(1, post // 1000)}k tokens "
                                            "(Claude keeps a summary of the rest)"}, keep=True)
        self.push_usage()
        return "done"

    def measure_contexts(self):
        """Sizes of saved conversations from before sizes were tracked (read once at startup)."""
        with self.state_lock:
            todo = [(k, v["session"]) for k, v in self.state["lanes"].items() if v.get("session") and not v.get("ctx")]
        for lane, sid in todo:
            try:
                size = last_context(sid)
            except OSError:
                continue
            with self.state_lock:
                if size and self.state["lanes"].get(lane, {}).get("session") == sid:
                    self.state["lanes"][lane]["ctx"] = size
                    save_json(STATE, self.state)
        self.push_usage()

    def contexts_view(self):
        with self.state_lock:
            lanes = {k: dict(v) for k, v in self.state["lanes"].items() if v.get("ctx") and v.get("session")}
            closed = {k for k, t in self.state.get("threads", {}).items() if t.get("closed")}
        return [{"lane": k, "label": self.lane_label(k), "ctx": v["ctx"], "compacted": v.get("compacted")}
                for k, v in sorted(lanes.items()) if k not in closed]

    def usage_view(self):
        accounts = [{"id": a, "label": HOST_ACCOUNTS[a], **{k: (self.usage_by.get(a) or {}).get(k) for k in ("limits", "plan", "ts", "error")}}
                    for a in HOST_ACCOUNTS if a == "team" or self.cfg.get("host_oauth_token")]
        return {**(self.usage or {}), "contexts": self.contexts_view(), "compact_at": self.cfg.get("compact_at") or 0,
                "account": self.host_account(), "accounts": accounts, "team_stop": self.team_stop()}

    def push_usage(self):
        self.broadcast({"t": "usage", "usage": self.usage_view(), "ai_paused": ai_paused()})

    def usage_line(self, long=False):
        u = self.usage or {}
        if not u.get("limits"):
            return "Claude usage: not known yet" + (f" ({u['error']})" if u.get("error") else "")
        if not long:
            return ", ".join(f"{l['label'][:1].lower() + l['label'][1:]} {l['percent']}%" for l in u["limits"][:2])
        rows = [f"{l['label'][:1].lower() + l['label'][1:]} {l['percent']}% (resets {until_text(l['resets'])})" for l in u["limits"]]
        age = int(time.time() - u["ts"])
        return (f"Claude usage, {self.cfg['host']}'s plan{' (' + u['plan'] + ')' if u.get('plan') else ''} shared by everyone here: "
                + " · ".join(rows) + f" · checked {age // 60} min ago" + (f", last check failed: {u['error']}" if u.get("error") else ""))

    # ---- incoming -------------------------------------------------------
    def say(self, name, text, thread=None, files=None):
        text = text.strip()
        attached = []   # files sent with this message (uploaded first, without a chat line of their own)
        for fid in (files if isinstance(files, list) else [])[:10]:
            meta, _ = self.file_path(str(fid))
            if meta:
                attached.append({"id": meta["id"], "name": meta["name"], "image": os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT})
        if not text and not attached:
            return
        lane = self.lane_of(name, thread)
        cleared_archive_warning = False
        if "/" in lane:
            with self.state_lock:
                t = self.state.get("threads", {}).get(lane)
                if t and t.get("archive_warning"):
                    t.pop("archive_warning", None)
                    t.pop("archive_reason", None)
                    save_json(STATE, self.state)
                    cleared_archive_warning = True
            if cleared_archive_warning:
                self.broadcast({"t": "threads", "threads": self.threads_view()})
        if text.startswith("/") and not attached and self.command(name, text, lane):
            return
        said = text or ("(see the attached " + ("picture" if all(a["image"] for a in attached) else "file") + ("s)" if len(attached) > 1 else ")"))
        refs = (" (attached: " + ", ".join(f"{a['name']} = file {a['id']}" + (", a picture: file_view shows it" if a["image"] else "")
                                            for a in attached) + ")") if attached else ""
        self.mark_seen(name)
        if "/" in lane and text and not self.lane_meta(lane).get("goal"):   # a thread's first message is its goal until someone edits it
            self.set_lane_meta(lane, goal=clip(text, 400), goal_auto=True)
            self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
        joined = False if ai_paused() else self.inject(lane, f"[{name}] (added while you work): {said}{refs}", raw=said + refs)
        cut = {}
        if joined:   # where the answer stands now: the page shows what Claude says next under this message
            with self.lock:
                parts_now = self.live_parts.get(lane)
                if parts_now is not None and self.current_aid.get(lane):
                    cut = {"aid": self.current_aid[lane], "at": len(parts_now)}
                    parts_now.append(["text", ""])   # the next words start a new part
        self.broadcast({"t": "msg", "from": name, "text": text, "ts": time.time(), "lane": lane, **({"joined": True} if joined else {}),
                        **cut, **({"files": attached} if attached else {})}, keep=True)
        if ai_paused():
            self.send_to(name, {"t": "sys", "text": AI_PAUSE_MESSAGE})
            return  # Keep the human message, without queuing work to run after unlock.
        if joined:   # it went straight into the task running in this thread
            with self.lock:
                self.injected.setdefault(lane, []).append(said)
                self.last_activity[name] = time.time()
            return
        if attached:   # Claude hears about them in this message's turn
            with self.lock:
                self.attachments.setdefault(name, []).extend(attached)
        with self.lock:
            self.pending.setdefault(lane, []).append(said)
            self.first_pending.setdefault(lane, time.time())
            self.last_activity[name] = time.time()
            self.lock.notify_all()
            compacting = (self.busy.get(lane) or {}).get("compacting")
        if compacting:
            self.send_to(name, {"t": "sys", "text": "Claude is compacting this thread's conversation first (can take a few"
                                                    " minutes): your message goes right after · /stop cancels both"})

    def keystroke(self, name):
        with self.lock:
            self.last_activity[name] = time.time()
        self.broadcast({"t": "typing", "from": name})

    def command(self, name, text, lane=None):
        cmd = text.split()[0].lower()
        if ai_paused() and cmd in ("/continue", "/compact", "/image"):
            self.send_to(name, {"t": "sys", "text": AI_PAUSE_MESSAGE})
            return True
        if cmd == "/new":
            with self.state_lock:
                self.generation += 1
                self.state = {"session_id": None, "lanes": {}, "base_ts": time.time(), "threads": self.state.get("threads", {})}
                save_json(STATE, self.state)
            self.broadcast({"t": "sys", "text": f"{name} started a fresh conversation"}, keep=True)
        elif cmd == "/operator":   # this thread is answered by the host's own Claude Code ("/operator off": the room's AI again)
            target = lane or name
            off = len(text.split()) > 1 and text.split()[1].lower() in ("off", "no", "0")
            self.set_lane_meta(target, engine=None if off else "operator")
            self.broadcast({"t": "lane_meta", "meta": self.lane_meta_all()})
            self.broadcast({"t": "sys", "text": (f"{self.lane_label(target)} is back to the room's AI" if off else
                            f"🛠 {self.lane_label(target)} is now a control thread: the host's Claude Code answers here (/operator off undoes it)")},
                           keep=True)
        elif cmd == "/compact_at":   # the host changes when long threads are compacted (tokens; 0 = never)
            words = text.split()
            if name != self.cfg["host"]:
                self.send_to(name, {"t": "sys", "text": f"only {self.cfg['host']} (the host) can change that"})
            elif len(words) < 2 or not words[1].isdigit():
                self.send_to(name, {"t": "sys", "text": f"threads compact above {self.cfg.get('compact_at') or 0} tokens · /compact_at 120000"})
            else:
                self.cfg["compact_at"] = int(words[1])
                save_json(CONFIG, self.cfg)
                self.broadcast({"t": "sys", "text": f"{name}: long threads now compact above {int(words[1]):,} tokens"}, keep=True)
        elif cmd == "/unsend":   # take back your last message in this thread (Claude drops it too)
            target = lane or name
            with self.history_lock:
                mine = next((e for e in reversed(self.history) if e.get("t") == "msg" and e.get("from") == name
                             and (e.get("lane") or e.get("from")) == target), None)
            if not mine or time.time() - mine.get("ts", 0) > 900:
                self.send_to(name, {"t": "sys", "text": "nothing of yours to take back in this thread (the last 15 minutes)"})
            else:
                ok, msg = self.delete_msg(name, mine["id"])
                self.send_to(name, {"t": "sys", "text": f"↩ took back “{clip(mine.get('text') or '', 80)}”" if ok else msg})
        elif cmd == "/stop":
            # "/stop" = this thread's task, "/stop sam" = all of sam's, "/stop all" = every task
            words = text.split()
            arg = words[1].lower() if len(words) > 1 else (lane or name)
            with self.lock:   # running turns, and messages still waiting for theirs
                active = set(self.busy) | {l for l, queued in self.pending.items() if queued}
                lanes = list(active) if arg == "all" else ([l for l in active if owner(l) == arg] or [arg])
                procs = [(lane, self.procs.get(lane)) for lane in lanes]
                dropped = [l for l in lanes if self.pending.pop(l, None)]
                self.halted.update(lanes)
                for l in lanes:
                    self.first_pending.pop(l, None)
            for entry in list(self.execs.values()):
                if entry["lane"] in lanes:
                    entry["result"] = (False, "stopped by the room")
                    entry["started"].set()
                    entry["done"].set()
            stopped = [lane for lane, proc in procs if proc and proc.poll() is None]
            for lane, proc in procs:
                if proc and proc.poll() is None:
                    self.stopping.add(lane)
                    kill_tree(proc)
            waiting = [l for l in dropped if l not in stopped]
            if stopped or waiting:
                what = [f"stopped the assistant's task for {', '.join(map(self.lane_label, stopped))}"] if stopped else []
                what += [f"cancelled what was still waiting for {', '.join(map(self.lane_label, waiting))}"] if waiting else []
                self.broadcast({"t": "sys", "text": f"{name} " + " and ".join(what)
                                + (" · ▶ Continue under the answer (or /continue, /continue opus) carries on" if stopped else "")}, keep=True)
            else:
                self.send_to(name, {"t": "sys", "text": f"nothing running for {arg}" + (
                    "" if arg == "all" else " · /stop all stops every task")})
        elif cmd in ("/model", "/effort"):
            words = text.split()
            arg = words[1].lower() if len(words) > 1 else ""
            model, effort = self.pref(name)
            if not arg:
                self.send_to(name, {"t": "sys", "text": f"you use {model_label(model)}" + (f" · {effort} effort" if effort else "")
                                                        + f" · /model {'|'.join(MODELS)} · /effort {'|'.join(self.efforts_for(model) + ('auto',))}|default"})
            elif cmd == "/model" and arg not in MODELS:
                self.send_to(name, {"t": "sys", "text": f"unknown model '{arg}' · options: {', '.join(MODELS)}"})
            elif cmd == "/effort" and arg not in self.efforts_for(model) + ("auto", "default"):
                self.send_to(name, {"t": "sys", "text": f"unknown effort '{arg}' · options: {', '.join(self.efforts_for(model) + ('auto',))}, default"})
            elif cmd == "/model":
                self.set_pref(name, model=MODELS[arg])
            else:
                self.set_pref(name, effort="" if arg == "default" else arg)
        elif cmd == "/image":
            prompt = text[len("/image"):].strip()
            if not prompt:
                self.send_to(name, {"t": "sys", "text": "usage: /image a lighthouse on a cliff at dusk, concept art"})
                return True
            self.broadcast({"t": "sys", "text": f"🎨 {name} is making a picture: {clip(prompt, 120)}"})

            def work():
                ok, msg = self.generate_image(name, prompt, "wide" if " wide" in f" {prompt.lower()}" else "square", lane=lane or name)
                if not ok:
                    self.send_to(name, {"t": "sys", "text": msg})
            threading.Thread(target=work, daemon=True).start()
        elif cmd in ("/thread", "/threads"):
            # new / rename / close here; switching threads is the client's (it picks where messages go)
            words = text.split(None, 2)
            sub = words[1].lower() if len(words) > 1 and cmd == "/thread" else ""
            rest = words[2] if len(words) > 2 else ""
            ok, msg = True, ""
            if sub == "new":
                ok, msg = self.thread_op(name, {"op": "create", "name": rest})
            elif sub in ("rename", "close"):
                ok, msg = self.thread_op(name, {"op": sub, "thread": lane, "name": rest})
                if not ok and lane == name:
                    msg = f"/thread {sub} works inside one of your threads, not in Main"
            else:
                ok, msg = False, self.threads_text(name, everyone=text.split()[1:2] == ["all"])
            if not ok:
                self.send_to(name, {"t": "sys", "text": msg})
        elif cmd == "/board":
            self.send_to(name, {"t": "sys", "text": self.board_text() + "\n(open the room in a browser to drag cards"
                                                    " and draw: /web in chat.py)"})
        elif cmd == "/who":
            self.broadcast({"t": "sys", "text": self.pcs_line().replace("[room] PCs: ", "online: ")})
        elif cmd == "/compact":   # this thread's conversation, now ("/compact keep X" adds what to keep)
            target, how = lane or name, text[len("/compact"):].strip()
            if is_gemini_model(self.turn_pref(target)[0]):
                self.send_to(name, {"t": "sys", "text": "Gemini keeps its own bounded conversation context. Earlier room messages remain available through conversation_read; Claude and Codex sessions are kept separately."})
                return True
            if is_codex_model(self.turn_pref(target)[0]):
                self.send_to(name, {"t": "sys", "text": "Codex manages its own context automatically. Claude history is kept separately."})
                return True
            with self.state_lock:
                known = self.state["lanes"].get(target, {}).get("session")
            with self.lock:
                free = target not in self.busy
                if free and known:
                    self.busy[target] = {"since": time.time(), "texts": [], "compacting": True}
            if not known:
                self.send_to(name, {"t": "sys", "text": "nothing to compact yet in this thread"})
            elif not free:
                self.send_to(name, {"t": "sys", "text": "Claude is busy in this thread: /compact after its answer (or ■ Stop it)"})
            else:
                def run():
                    try:
                        self.compact_lane(target, (how + ". " if how else "") + COMPACT_HOW)
                    finally:
                        with self.lock:
                            self.busy.pop(target, None)
                            self.lock.notify_all()
                threading.Thread(target=run, daemon=True).start()
        elif cmd == "/p":   # the people's own chat, from a terminal
            ok, msg = self.pmsg(name, text[len("/p"):].strip())
            if not ok:
                self.send_to(name, {"t": "sys", "text": "/p hello: a message for the people only (Claude doesn't read it)"})
        elif cmd in ("/sync", "/project"):
            self.send_to(name, {"t": "sys", "text": self.project_text()})
        elif cmd == "/shot":   # a picture of this person's screen, taken by their chat.py, into the chat
            with self.lock:
                agent = self.agents.get(name)
            if agent:
                agent["q"].put({"t": "shot"})
            else:
                self.send_to(name, {"t": "sys", "text": "📸 /shot needs chat.py running on your PC (it takes the picture)"})
        elif cmd == "/remind":
            rest = text[len("/remind"):].strip()
            if rest.lower().startswith("cancel"):
                self.send_to(name, {"t": "sys", "text": self.reminder_cancel(name, rest[len("cancel"):].strip())[1]})
            elif not rest:
                self.send_to(name, {"t": "sys", "text": self.reminders_text(name)})
            else:
                ok, msg = self.reminder_add(name, rest, lane)
                if not ok:
                    self.send_to(name, {"t": "sys", "text": msg})
        elif cmd == "/reminders":
            self.send_to(name, {"t": "sys", "text": self.reminders_text(name)})
        elif cmd == "/saved":
            self.send_to(name, {"t": "sys", "text": self.saved_text(name)})
        elif cmd == "/poll":
            parts = [x.strip() for x in text[len("/poll"):].split("|")]
            ok, msg = self.poll_create(name, parts[0], parts[1:])
            if not ok:
                self.send_to(name, {"t": "sys", "text": msg})
        elif cmd == "/vote":
            words = text.split()
            with self.polls_lock:
                open_polls = sorted((p["ts"], pid) for pid, p in self.polls.items() if not p["closed"])
            if not open_polls:
                self.send_to(name, {"t": "sys", "text": "no open poll · /poll Question | option | option starts one"})
            else:
                n = words[1] if len(words) > 1 else ""
                ok, msg = self.poll_vote(name, open_polls[-1][1], int(n) - 1 if n.isdigit() else None)
                if not ok:
                    self.send_to(name, {"t": "sys", "text": msg})
        elif cmd == "/recap":
            words = text.split()
            hours = float(words[1]) if len(words) > 1 and words[1].replace(".", "", 1).isdigit() else 0
            since = time.time() - hours * 3600 if hours else (self.prev_seen.get(name) or time.time() - 86400)
            r = self.recap(name, since)
            self.send_to(name, r, kind="browser")
            self.send_to(name, {"t": "sys", "text": self.recap_text(r)}, kind="chat.py")
        elif cmd == "/continue":   # after a Stop: carry on, optionally on another model for this one turn
            target, words = lane or name, text.split()
            pick = words[1].lower() if len(words) > 1 else ""
            if pick and (pick not in MODELS or not MODELS[pick]):
                self.send_to(name, {"t": "sys", "text": "pick a model: /continue " + " | ".join(k for k, v in MODELS.items() if v)})
                return True
            with self.lock:
                if target in self.busy or self.pending.get(target):
                    self.send_to(name, {"t": "sys", "text": "Claude is already working in this thread"})
                    return True
                if pick:
                    self.turn_model[target] = MODELS[pick]
            label = model_label(MODELS[pick]) if pick else ""
            self.broadcast({"t": "msg", "from": name, "text": "▶ continue" + (f" (with {label})" if label else ""),
                            "ts": time.time(), "lane": target}, keep=True)
            with self.lock:
                self.pending.setdefault(target, []).append(
                    "▶ continue: you were stopped in the middle of your last answer. Carry on from where it was cut off.")
                self.first_pending.setdefault(target, time.time())
                self.last_activity[name] = time.time()
                self.lock.notify_all()
        elif cmd == "/usage":
            self.send_to(name, {"t": "sys", "text": self.usage_line(long=True)})
        elif cmd == "/timeline":
            self.send_to(name, {"t": "sys", "text": self.timeline_text(25) + "\n(the Timeline tab on the page shows the pictures)"})
        elif cmd == "/help":
            self.broadcast({"t": "sys", "text": "/new fresh conversation · /stop stop your task (/stop all: every task)"
                                                " · /who online · /usage Claude limits · /compact shrink this thread · /board tasks · /timeline the story so far"
                                                " · /poll vote · /recap what you missed · /continue after a Stop · /threads all · /remind 18:00 … · /saved · /image picture"
                                                " · /model switch model"
                                                " · /auto (chat.py) run actions on your PC without asking"})
        else:
            return False
        return True

    # ---- lanes: one Claude turn per person, running side by side ------
    def worker(self):
        quiet = self.cfg["quiet_seconds"]
        max_wait = self.cfg["max_wait_seconds"]
        while True:
            with self.lock:
                if ai_paused():
                    self.lock.wait(1)
                    continue
                now = time.time()
                # a person's turn starts once they've stopped typing for a moment
                # (so a few quick messages go together) and their previous turn is over
                ready = [n for n, texts in self.pending.items() if texts and n not in self.busy and (
                    now - self.last_activity.get(owner(n), 0) >= quiet or now - self.first_pending.get(n, now) >= max_wait)]
                if not ready:
                    self.lock.wait(0.2 if any(self.pending.values()) else None)
                    continue
                for name in ready:
                    texts = self.pending.pop(name)
                    self.first_pending.pop(name, None)
                    self.halted.discard(name)
                    self.busy[name] = {"since": now, "texts": texts}
                    threading.Thread(target=self.run_lane, args=(name, texts), daemon=True).start()

    def run_lane(self, lane, texts):
        parts = []
        t0 = time.time()
        aid = "a" + secrets.token_hex(4)
        self.current_aid[lane] = aid
        is_external = is_external_model(self.turn_pref(lane)[0])
        assistant = assistant_name(self.turn_pref(lane)[0])
        self.running_models[lane] = self.turn_pref(lane)[0]
        self.feed_seen[lane] = time.time()   # its prompt says what the others did so far
        try:
            model, effort = self.turn_pref(lane)
            self.broadcast({"t": "batch", "names": [lane], "lane": lane, "aid": aid,
                            "model": model_label(model) if model else "", "effort": effort, "auto": self.pref(lane)[1] == "auto",
                            "assistant": assistant})
            if not is_external and self.too_big(lane):
                self.broadcast({"t": "status", "text": "compacting the conversation first", "lane": lane})
                if self.compact_lane(lane) == "stopped":
                    return
            prompt = self.build_prompt(lane, texts)
            for _ in range(3):
                parts, stale = self.ask_claude(lane, prompt)
                if not stale:
                    break
                with self.state_lock:   # saved session is gone: fall back to the shared one, then a fresh one
                    if self.state["lanes"].get(lane, {}).pop("session", None) is None:
                        self.state["session_id"] = None
                    save_json(STATE, self.state)
            with self.lock:
                asked = texts + self.injected.pop(lane, [])
            if parts:
                self.record({"t": "answer", "parts": parts, "lane": lane, "asked": asked, "id": aid, "secs": round(time.time() - t0),
                             "stopped": lane in self.halted, "assistant": assistant})
                if lane not in self.halted and not (is_gemini_model(self.running_models.get(lane)) and
                        self.state['lanes'].get(lane, {}).get('gemini_failed')):
                    self.mark_done(lane, asked, aid, assistant)
                    self.report_to_parent(lane, parts)
        finally:
            self.usage_kick.set()
            self.current_aid.pop(lane, None)
            self.live_parts.pop(lane, None)
            with self.lock:
                self.injected.pop(lane, None)
                for content, kind, raw in self.midturn.pop(lane, []):   # never reached Codex (no tool call after it): next turn
                    (self.thread_notes if kind == "note" else self.pending).setdefault(lane, []).append(raw)
                    if kind != "note":
                        self.first_pending.setdefault(lane, time.time())
            self.turn_model.pop(lane, None)
            with self.lock:
                self.turn_effort.pop(lane, None)
            self.set_lane_meta(lane, dirty=True)
            self.broadcast({"t": "done", "lane": lane, "secs": round(time.time() - t0), "asked": texts, "stopped": lane in self.halted, "assistant": assistant})
            try:
                with self.lock:
                    halted = lane in self.halted
                    self.halted.discard(lane)
                if not is_external and not halted and self.too_big(lane):   # the lane stays busy meanwhile: new messages wait for the summary
                    with self.lock:
                        if lane in self.busy:
                            self.busy[lane]["compacting"] = True
                    self.compact_lane(lane)
                else:
                    self.push_usage()
            finally:
                with self.lock:
                    self.busy.pop(lane, None)
                    self.running_models.pop(lane, None)
                    self.lock.notify_all()

    def build_prompt(self, lane, texts):
        with self.state_lock:
            seen = self.state["lanes"].setdefault(lane, {})
            since = seen.get("seen", self.state["base_ts"])
            seen["seen"] = time.time()
            save_json(STATE, self.state)
        lines = [self.pcs_line()]
        if not is_external_model(self.running_models.get(lane)):
            with self.state_lock:
                claude_seen = self.state["lanes"].get(lane, {}).get("claude_seen", 0)
            exchanges = []
            for ev in list(self.history):
                if (ev.get("t") == "answer" and ev.get("lane") == lane
                        and ev.get("assistant") in ("Codex", "Gemini") and ev.get("ts", 0) > claude_seen):
                    exchanges.append({"asked": ev.get("asked", []), ev.get("assistant"):
                        "".join(p[1] for p in ev.get("parts", []) if p[0] == "text")[:4000]})
            if exchanges:
                lines.append("[room] Recent exchanges with other assistants in this thread (context only, already handled): "
                             + json.dumps(exchanges[-8:], ensure_ascii=False))
        high = [l for l in (self.usage or {}).get("limits", []) if l["percent"] >= USAGE_WARN[0]]
        if high and not is_external_model(self.running_models.get(lane)):
            lines.append(f"[room] {self.cfg['host']}'s Claude plan is running low ("
                         + ", ".join(f"{l['label'][:1].lower() + l['label'][1:]} {l['percent']}% used, resets {until_text(l['resets'])}" for l in high)
                         + "): keep turns lean, and say so before starting a big job.")
        meanwhile = self.meanwhile(lane, since)
        if meanwhile:
            lines.append("[room] meanwhile in the other threads, already handled there (context only, do NOT answer):")
            lines += meanwhile
        with self.lock:
            busy, pending = dict(self.busy), dict(self.pending)
        others = [f'{self.lane_label(n)}: "{clip(" / ".join(b["texts"]), 200)}" (working on it, {int(time.time() - b["since"])}s)'
                  for n, b in busy.items() if n != lane and not b.get("compacting")]
        others += [f'{self.lane_label(n)}: "{clip(" / ".join(queued), 200)}" (queued)'
                   for n, queued in pending.items() if n != lane and queued]
        if others:
            lines.append("[room] other threads are handling these right now (NOT for you, don't answer or do them): "
                         + "; ".join(others))
        new_pins = [p for p in self.pins_view() if p["ts"] > since and p["by"] not in ("Claude", "Codex", "Gemini")]
        if new_pins:
            lines.append("[room] pinned onto chat messages since your last turn:")
            lines += [f"  {p['by']} pinned {self.pin_text(p)} onto {self.target_text(p['target'])}" for p in new_pins[-15:]]
        gone = [d["text"] for d in self.deleted if d["ts"] > since]
        if gone:
            lines.append("[room] deleted from the chat since your last turn (withdrawn: don't act on it or bring it up unless asked):")
            lines += [f"  {g}" for g in gone[-10:]]
        person, thread = owner(lane), self.thread_name(lane)
        with self.lock:
            attached = self.attachments.pop(person, [])
        if attached:
            lines.append(f"[room] {person} shared since their last message: " + ", ".join(
                f"{a['name']} (file {a['id']}{', a picture' if a['image'] else ''})" for a in attached)
                + ". file_view shows it.")
        try:
            lines += self.meeting_lines(lane, texts)
        except Exception as e:   # the meetings never stop a turn
            print(f"meetings: {e}", flush=True)
        own = self.own_rules(person)
        if own:
            lines.append(f"[room] {person}'s own rules for their turns: {clip(own, 2000)}")
        goal = self.lane_meta(lane).get("goal")
        if goal:
            lines.append(f"[room] what {person} set this thread for: {clip(goal, 400)}")
        pinned = self.thread_pins_line(lane)
        if pinned:
            lines.append(pinned)
        reacts = [r for r in self.react_log if r["ts"] > since and r["on_claude"]]
        if reacts:
            lines.append("[room] reactions to your answers since your last turn: "
                         + "; ".join(f"{r['who']} {r['e']} on “{r['snip']}”" for r in reacts[-8:]))
        polls = self.polls_since(since)
        if polls:
            lines.append("[room] polls since your last turn: " + "; ".join(polls))
        brief = self.threads_brief(lane)
        if brief and "\n".join(brief) != self.brief_seen.get(lane):   # only when it changed: the last one is in the conversation
            self.brief_seen[lane] = "\n".join(brief)
            lines.append("[room] threads: " + " | ".join(brief))
        files = self.files_in_use(lane)
        if files:
            lines.append("[room] files the other threads are changing right now (leave them alone; say so if you need one): "
                         + ", ".join(files))
        lines += self.lease_lines(lane)
        zl = [self.zone_line(z) for z in self.zones_view()]
        if zl and "\n".join(zl) != self.zones_seen.get(lane):   # only when they changed
            self.zones_seen[lane] = "\n".join(zl)
            lines.append("[room] zones (keep_clear = nobody else may put anything there; zones_read / zone_add): " + " | ".join(zl))
        with self.lock:
            notes = self.thread_notes.pop(lane, [])
        lines += notes
        lines += self.sync_lines(person, since)
        where = f", in their thread “{thread}” (PC tools still take user {person})" if thread else ""
        lines.append(f"[room] this turn is {person}'s{where}. Answer only this:")
        lines += [f"[{person}]: {t}" for t in texts]
        return "\n".join(lines)

    def meanwhile(self, lane, since):
        """Finished exchanges of the other lanes after `since`: question + shortened answer.
        Requests still running or queued are listed apart, so they can't pass for this turn's."""
        lines = []
        for ev in list(self.history):
            if ev.get("ts", 0) <= since:
                continue
            if ev["t"] == "answer" and ev.get("lane") != lane:
                who = self.lane_label(ev.get("lane")) or "everyone"
                for asked in ev.get("asked", []):
                    lines.append(f"  [{who}]: {clip(asked, 260)}")
                text = re.sub(r"\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text"))
                tools = [p[1].split(" · ")[-1] for p in ev["parts"] if p[0] == "tool"]
                used = f" (tools: {', '.join(sorted(set(tools))[:6])})" if tools else ""
                lines.append(f"  [{ev.get('assistant', 'Claude')} → {who}]: {clip(' '.join(text.split()), 420)}{used}")
            elif ev["t"] == "sys":
                lines.append(f"  [room]: {clip(ev['text'], 160)}")
        while len(lines) > 1 and sum(len(x) for x in lines) > 4000:   # the newest matter most
            lines.pop(0)
        return lines[-30:]

    # ---- tool calls: the full text behind each tool line --------------------
    def _load_tools(self):
        tools, lines = {}, 0
        try:
            with open(TOOLS_LOG, encoding="utf-8") as f:
                for line in f:
                    lines += 1
                    try:
                        t = json.loads(line)
                        tools[t["id"]] = t
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        tools = dict(list(tools.items())[-TOOL_KEEP:])
        if lines > TOOL_KEEP * 2:
            self._rewrite_tools(tools)
        return tools

    @staticmethod
    def _rewrite_tools(tools):
        tmp = TOOLS_LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for t in tools.values():
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
        os.replace(tmp, TOOLS_LOG)

    def redact(self, text):
        """Tool text is shown to everyone in the room: keys and room tokens never are."""
        if not text:
            return text
        with self.lock:
            own = list(self.exec_tokens)
        for secret in [*self.cfg["people"].values(), self.cfg.get("dev_token", ""), self.cfg.get("host_oauth_token", ""), (self.cfg.get("codex_bridge") or {}).get("key", ""), (self.cfg.get("gemini_bridge") or {}).get("key", ""), *own, *self.secret_values()]:
            if secret and len(secret) >= 12:
                text = text.replace(secret, "[hidden]")
        return SECRET_RE.sub("[hidden key]", text)

    def tool_start(self, tid, lane, name, inp):
        if name.endswith("· write_file"):   # the PC answers with [undo:<backup>]: matched back to this line
            self.write_tids[(lane, canon(inp))] = tid
            for k in list(self.write_tids)[:-200]:
                del self.write_tids[k]
        try:
            text = json.dumps(inp, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(inp)
        with self.tool_lock:
            self.tools[tid] = {"id": tid, "lane": lane, "name": name, "input": self.redact(clip_long(text, 12000)),
                               "output": None, "ok": None, "secs": None, "ts": time.time()}

    def tool_finish(self, tid, ok, output):
        """The result arrived (ok True/False) or the turn ended without one (ok None). Saved once."""
        with self.tool_lock:
            t = self.tools.get(tid)
            if not t or t.get("saved"):
                return t
            t.update(ok=ok, output=self.redact(clip_long(output, 16000)) if output is not None else None,
                     secs=round(time.time() - t["ts"], 1), saved=True)
            try:
                with open(TOOLS_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({k: v for k, v in t.items() if k != "saved"}, ensure_ascii=False) + "\n")
            except OSError:
                pass
            while len(self.tools) > TOOL_KEEP:
                self.tools.pop(next(iter(self.tools)))
            return dict(t)

    def tool_view(self, tid):
        with self.tool_lock:
            t = self.tools.get(tid)
            return {k: v for k, v in t.items() if k != "saved"} if t else None

    # ---- task board -----------------------------------------------------
    def _board(self, ref):
        """A board by id or name (any case); no ref = the first board."""
        boards = self.board["boards"]
        ref = str(ref or "").strip().lower()
        if not ref:
            return boards[0]
        return next((b for b in boards if b["id"].lower() == ref or b["name"].lower() == ref), None)

    @staticmethod
    def _col(board, ref, default=True):
        """A column of `board` by id or title; no ref = "To do" if there is one, else the first column."""
        ref = str(ref or "").strip().lower()
        if not ref:
            return next((c for c in board["columns"] if c["id"] == "todo"), board["columns"][0]) if default else None
        return next((c for c in board["columns"] if c["id"].lower() == ref or c["title"].lower() == ref), None)

    def board_view(self):
        with self.board_lock:
            boards = json.loads(json.dumps(self.board["boards"]))
            cards = sorted(self.board["cards"], key=lambda c: (c.get("board", ""), c["col"], c["order"]))
        return {"boards": boards, "columns": boards[0]["columns"], "cards": cards}

    def board_text(self):
        view = self.board_view()
        lines = []
        for b in view["boards"]:
            lines.append(f"== board “{b['name']}” (id {b['id']})")
            for col in b["columns"]:
                cards = [c for c in view["cards"] if c.get("board") == b["id"] and c["col"] == col["id"]]
                lines.append(f"{col['title']} [{col['id']}] ({len(cards)})" + (":" if cards else ""))
                for c in cards:
                    extra = ((f" · for {c['who']}" if c["who"] else "") + (f" · {c['priority']} priority" if c.get("priority") else "")
                             + (f" · note: {clip(c['note'], 160)}" if c["note"] else ""))
                    lines.append(f"  #{c.get('num', '?')} [{c['id']}] {c['title']}{extra}")
        return "\n".join(lines)

    def assignee(self, value):
        value = str(value or "").strip().lower()
        return value if value in self.cfg["people"] or value == "claude" else ""

    def board_op(self, who, op):
        """One change to the boards, from a browser, a terminal or Claude. Returns (ok, message)."""
        kind = str(op.get("op", ""))
        with self.board_lock:
            was = {c["id"]: (c.get("board"), c["col"]) for c in self.board["cards"]}
            ok, what, card = self._board_op(who, kind, op)
            if ok:
                save_json_safely(BOARD, self.board)
            done = False   # same rule as the page: the "done" column, one titled Done, or the board's last column
            if ok and card and kind in ("update", "move") and was.get(card["id"]) != (card.get("board"), card["col"]):
                b = next((b for b in self.board["boards"] if b["id"] == card.get("board")), None)
                col = next((c for c in b["columns"] if c["id"] == card["col"]), None) if b else None
                done = bool(col) and (col["id"] == "done" or col["title"].strip().lower() == "done" or col is b["columns"][-1])
        if ok:
            self.broadcast({"t": "board", "board": self.board_view(), "by": who, "what": what, "op": kind,
                            "card": card["id"] if card else None})
        if done:
            moment = re.search(r"\(id (tl\w+)\)", self.timeline_op(who, {"op": "from_card", "card": card["id"], "auto": True})[1] or "")
            if moment:
                what += f" · it's on the timeline as moment {moment.group(1)} (timeline_update adds pictures)"
            what += self.card_finished(who, card["id"], moment.group(1) if moment else None)
        return ok, what + (f" (id {card['id']}, #{card['num']})" if ok and kind == "add" and card else "")

    def _board_op(self, who, kind, op):
        b_all, cards = self.board["boards"], self.board["cards"]
        ref = str(op.get("id", "")).strip()
        card = next((c for c in cards if c["id"] == ref or (ref.lstrip("#").isdigit() and c.get("num") == int(ref.lstrip("#")))),
                    None) if ref else None
        # boards
        if kind == "board_create":
            name = clean_text(op.get("name"), 60) or "New board"
            b_all.append({"id": "b" + secrets.token_hex(3), "name": name, "columns": [{"id": c, "title": t} for c, t in COLUMNS]})
            return True, f"{who} made the board “{name}”", None
        board = self._board(op.get("board"))
        if op.get("board") and not board:
            return False, f"no board {op.get('board')!r} (board_read lists them)", None
        if kind == "board_rename":
            name, old = clean_text(op.get("name"), 60), board["name"]
            if not name:
                return False, "the board needs a name", None
            board["name"] = name
            return True, f"{who} renamed the board “{old}” to “{name}”", None
        if kind == "board_delete":
            if len(b_all) == 1:
                return False, "the last board can't be deleted", None
            gone = [c for c in cards if c.get("board") == board["id"]]
            b_all.remove(board)
            self.board["cards"] = [c for c in cards if c.get("board") != board["id"]]
            return True, f"{who} deleted the board “{board['name']}”" + (f" and its {len(gone)} cards" if gone else ""), None
        if kind == "board_move":
            try:
                at = max(0, min(len(b_all) - 1, int(op.get("index", 0))))
            except (TypeError, ValueError):
                return False, "position must be a number", None
            b_all.remove(board)
            b_all.insert(at, board)
            return True, f"{who} moved the board “{board['name']}” to place {at + 1}", None
        # columns
        if kind == "col_add":
            title = clean_text(op.get("title"), 40) or "New column"
            after = self._col(board, op.get("after"), default=False)
            at = board["columns"].index(after) + 1 if after else len(board["columns"])
            board["columns"].insert(at, {"id": "c" + secrets.token_hex(3), "title": title})
            return True, f"{who} added the column “{title}” to “{board['name']}”", None
        if kind in ("col_rename", "col_delete", "col_move"):
            col = self._col(board, op.get("col"), default=False)
            if not col:
                return False, f"no column {op.get('col')!r} on “{board['name']}”", None
            if kind == "col_rename":
                title, old = clean_text(op.get("title"), 40), col["title"]
                if not title:
                    return False, "the column needs a name", None
                col["title"] = title
                return True, f"{who} renamed the column “{old}” to “{title}”", None
            if kind == "col_delete":
                if len(board["columns"]) == 1:
                    return False, "a board needs at least one column", None
                board["columns"].remove(col)
                moved = [c for c in cards if c.get("board") == board["id"] and c["col"] == col["id"]]
                for c in moved:
                    self._place(c, board["id"], board["columns"][0]["id"], None)
                return True, (f"{who} deleted the column “{col['title']}”" +
                              (f" ({len(moved)} cards went to “{board['columns'][0]['title']}”)" if moved else "")), None
            try:
                at = max(0, min(len(board["columns"]) - 1, int(op.get("index", 0))))
            except (TypeError, ValueError):
                return False, "position must be a number", None
            board["columns"].remove(col)
            board["columns"].insert(at, col)
            return True, f"{who} moved the column “{col['title']}” to place {at + 1}", None
        # cards
        if kind == "add":
            title = clean_text(op.get("title"), 300)
            if not title:
                return False, "a card needs a title", None
            col = self._col(board, op.get("col"))
            if not col:
                return False, f"no column {op.get('col')!r} on “{board['name']}”", None
            card = {"id": secrets.token_hex(3), "board": board["id"], "col": col["id"], "title": title,
                    "note": clean_text(op.get("note"), 4000), "who": self.assignee(op.get("who")),
                    "color": op.get("color") if op.get("color") in LABELS else "",
                    "priority": op.get("priority") if op.get("priority") in PRIORITIES else "", "cover": "",
                    "by": who, "ts": time.time(), "order": 1e9, "num": self.board.get("next", 1)}
            self.board["next"] = card["num"] + 1
            cards.append(card)
            self._place(card, board["id"], col["id"], None, op.get("index"))
            return True, f"{who} added #{card['num']} “{title}” to {board['name']} › {col['title']}", card
        if card is None:
            return False, f"no card {op.get('id')!r} (board_read shows ids and #numbers)", None
        if kind == "delete":
            cards.remove(card)
            return True, f"{who} deleted #{card['num']} “{card['title']}”", card
        changed = []
        if kind == "update":
            if "title" in op and clean_text(op["title"], 300):
                card["title"] = clean_text(op["title"], 300)
                changed.append("title")
            if "note" in op:
                card["note"] = clean_text(op["note"], 4000)
                changed.append("note")
            if "who" in op:
                card["who"] = self.assignee(op["who"])
                changed.append("assignee")
            if "color" in op and op["color"] in LABELS:
                card["color"] = op["color"]
                changed.append("label")
            if "priority" in op and op["priority"] in PRIORITIES:
                card["priority"] = op["priority"]
                changed.append("priority")
            if "cover" in op:
                meta, _ = self.file_path(str(op["cover"])) if op["cover"] else (None, None)
                if op["cover"] and not (meta and os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT):
                    return False, "the cover must be a picture shared in the room", None
                card["cover"] = meta["id"] if meta else ""
                changed.append("cover")
        if kind in ("update", "move"):
            target = self._board(op.get("board")) if op.get("board") else next((b for b in b_all if b["id"] == card.get("board")), b_all[0])
            col = self._col(target, op.get("col"), default=False) if op.get("col") else None
            if op.get("col") and not col:
                return False, f"no column {op.get('col')!r} on “{target['name']}”", None
            if not col and target["id"] != card.get("board"):
                col = self._col(target, None)
            index = None
            if op.get("position") not in (None, ""):
                try:
                    index = int(op["position"]) - 1
                except (TypeError, ValueError):
                    return False, "position must be a number (1 = top)", None
            elif op.get("index") not in (None, ""):
                index = op.get("index")
            if col or index is not None or op.get("before"):
                was = (card.get("board"), card["col"])
                col = col or self._col(target, card["col"], default=False) or self._col(target, None)
                self._place(card, target["id"], col["id"], op.get("before"), index)
                if was != (target["id"], col["id"]):
                    changed.append(f"moved to {target['name']} › {col['title']}" if target["id"] != was[0] else f"moved to {col['title']}")
                elif index is not None:
                    changed.append(f"now #{index + 1} in {col['title']}")
        if kind == "move" and not changed:
            return True, "", card
        if kind not in ("update", "move"):
            return False, f"unknown board op {kind!r}", None
        return True, f"{who} updated #{card['num']} “{card['title']}” ({', '.join(changed) or 'no change'})", card

    def _place(self, card, board_id, col_id, before_id, index=None):
        """Put `card` in a column, before card `before_id`, at position `index` (0 = top) or at the end."""
        others = sorted((c for c in self.board["cards"] if c.get("board") == board_id and c["col"] == col_id and c is not card),
                        key=lambda c: c["order"])
        if index not in (None, ""):
            at = max(0, min(len(others), int(index)))
        else:
            at = next((i for i, c in enumerate(others) if c["id"] == before_id), len(others))
        others.insert(at, card)
        card["board"], card["col"] = board_id, col_id
        for i, c in enumerate(others):
            c["order"] = i

    # ---- whiteboard -------------------------------------------------------
    def wb_op(self, who, op):
        kind = op.get("op")
        if kind == "cursor":   # where people point: live only, never stored
            try:
                self.broadcast({"t": "wb", "op": "cursor", "by": who,
                                    "x": round(float(op.get("x")), 1), "y": round(float(op.get("y")), 1)})
            except (TypeError, ValueError):
                pass
            return
        if kind == "live":     # a stroke still being drawn
            stroke = clean_stroke(op.get("stroke"), who)
            if stroke:
                self.broadcast({"t": "wb", "op": "live", "by": who, "stroke": stroke})
            return
        with self.wb_lock:
            strokes = self.wb["strokes"]
            if kind == "add":
                stroke = clean_stroke(op.get("stroke"), who)
                if not stroke or len(strokes) >= 6000:
                    return
                strokes.append(stroke)
                event = {"t": "wb", "op": "add", "by": who, "stroke": stroke}
            elif kind == "delete":
                ids = {str(i) for i in (op.get("ids") or [])[:1000]}
                self.wb["strokes"] = [x for x in strokes if x["id"] not in ids]
                event = {"t": "wb", "op": "delete", "by": who, "ids": sorted(ids)}
            elif kind == "clear":
                self.wb["strokes"] = []
                event = {"t": "wb", "op": "clear", "by": who}
                if os.path.exists(SNAPSHOT):
                    os.remove(SNAPSHOT)
            else:
                return
            self.wb_changed = time.time()
            save_json_safely(WHITEBOARD, self.wb)
        self.broadcast(event)

    def wb_snapshot(self, png_b64):
        try:
            data = base64.b64decode(png_b64, validate=True)
        except (ValueError, TypeError):
            return
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 8_000_000:
            with open(SNAPSHOT + ".tmp", "wb") as f:
                f.write(data)
            os.replace(SNAPSHOT + ".tmp", SNAPSHOT)
            self.snapshot_at = time.time()

    def whiteboard_view(self):
        with self.wb_lock:
            strokes = list(self.wb["strokes"])
        if not strokes:
            return True, "The whiteboard is empty.", None
        texts = [x["text"] for x in strokes if x["type"] == "text"]
        summary = (f"{len(strokes)} items on the whiteboard, drawn by {', '.join(sorted({x['by'] for x in strokes}))}."
                   + (" Text on it: " + " | ".join(clip(t, 200) for t in texts[:40]) if texts else ""))
        if not os.path.exists(SNAPSHOT):
            return True, summary + " (No picture yet: an open room page draws one after the next change.)", None
        with open(SNAPSHOT, "rb") as f:
            image = base64.b64encode(f.read()).decode("ascii")
        if self.snapshot_at < self.wb_changed - 1:
            summary += " (The picture may miss the newest strokes.)"
        return True, summary, image

    # ---- shared files -----------------------------------------------------
    def files_view(self):
        with self.files_lock:
            return list(self.files)

    def files_text(self):
        files = self.files_view()
        if not files:
            return "No shared files."
        return "\n".join(f"[{f['id']}] {f['name']} ({human_size(f['size'])}, from {f['by']}, "
                         f"{time.strftime('%d.%m %H:%M', time.localtime(f['ts']))})" for f in files)

    def upload_lane(self, who):
        """The thread whose running file_put sent this upload (chat.py doesn't say which), else None."""
        for entry in list(self.execs.values()):
            if (entry["user"] == who and (entry.get("msg") or {}).get("tool") == "file_put" and entry["started"].is_set()
                    and not entry["done"].is_set()):
                return entry["lane"]
        return None

    def add_file(self, who, name, stream, length, caption="", announce=True, attach=True, lane=None):
        """Store an upload streamed from the request body; returns (ok, message)."""
        name = os.path.basename(str(name).replace("\\", "/")).strip()[:200] or "file"
        if length > MAX_FILE:
            return False, f"too big: {human_size(length)} (max {human_size(MAX_FILE)})"
        if sum(f["size"] for f in self.files_view()) + length > MAX_FILES_TOTAL:
            return False, "the room's file storage is full: delete some files first"
        os.makedirs(FILES_DIR, exist_ok=True)
        file_id = secrets.token_hex(4)
        ext = os.path.splitext(name)[1][:12] if re.fullmatch(r"\.[\w-]{1,11}", os.path.splitext(name)[1] or "") else ""
        path = os.path.join(FILES_DIR, file_id + ext)
        left = length
        with open(path + ".part", "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(path + ".part")
            return False, "upload interrupted"
        os.replace(path + ".part", path)
        meta = {"id": file_id, "name": name, "size": length, "by": who, "ts": time.time(), "disk": file_id + ext}
        with self.files_lock:
            self.files.insert(0, meta)
            save_json_safely(FILES_META, self.files)
        self.broadcast({"t": "files", "files": self.files_view()})
        is_image = ext.lower() in IMAGE_EXT
        if not announce:
            pass
        elif is_image:   # pictures show up in the chat itself
            self.broadcast({"t": "image", "from": who, "file_id": file_id, "name": name, "caption": caption,
                            "size": length, "preview": image_preview(path), **({"lane": lane} if lane else {})}, keep=True)
        else:
            self.broadcast({"t": "sys", "text": f"📎 {who} shared {name} ({human_size(length)}): Files tab, "
                                                f"or /get {file_id} in chat.py"}, keep=True)
        if attach and who in self.cfg["people"]:   # a staged file is told to Claude with the message it goes out with
            with self.lock:
                self.attachments.setdefault(who, []).append({"id": file_id, "name": name, "image": is_image})
        return True, f"shared as {file_id}"

    def generate_image(self, who, prompt, aspect="square", source_id="", lane=None):
        """Make a picture and post it in the chat: Gemini ("nano banana") with a key, else Pollinations."""
        if ai_paused():
            return False, AI_PAUSE_MESSAGE
        prompt = clean_text(prompt, 2000)
        if not prompt:
            return False, "describe the picture"
        key = self.cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        try:
            if key:
                source = None
                if source_id:
                    meta, path = self.file_path(source_id)
                    if not meta:
                        return False, f"no shared file {source_id!r} to edit"
                    with open(path, "rb") as f:
                        source = (f.read(), mimetypes.guess_type(meta["name"])[0] or "image/png")
                data, ext = gemini_image(key, self.cfg.get("gemini_image_model") or DEFAULT_CONFIG["gemini_image_model"],
                                         prompt, aspect, source)
            elif source_id:
                return False, "editing a picture needs a Gemini key (gemini_api_key in room.json); new pictures work without"
            else:
                data, ext = pollinations_image(prompt, aspect)
        except Exception as e:  # noqa: BLE001
            return False, f"picture failed: {e}"
        name = (re.sub(r"[^\w-]+", "-", prompt.lower())[:40].strip("-") or "picture") + ext
        ok, msg = self.add_file(who, name, io.BytesIO(data), len(data), caption=prompt, lane=lane)
        return ok, f"posted in the chat and in Files ({msg}, made with {'Gemini' if key else 'Pollinations'})"

    def file_view(self, file_id):
        """A shared file for Claude to look at: pictures as an image (made smaller), text as text."""
        meta, path = self.file_path(str(file_id))
        if not meta or not os.path.exists(path):
            return False, f"no shared file {file_id!r} (files_list shows them)", None
        label = f"{meta['name']} ({human_size(meta['size'])}, shared by {meta['by']})"
        if os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT:
            if not Image:
                return False, label + ": can't open pictures here (Pillow isn't installed)", None
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((1568, 1568))
                out = io.BytesIO()
                im.save(out, "PNG")
            return True, label, base64.b64encode(out.getvalue()).decode()
        if meta["size"] <= 200_000:
            with open(path, "rb") as f:
                raw = f.read()
            try:
                return True, f"{label}:\n{raw.decode('utf-8')}", None
            except UnicodeDecodeError:
                pass
        return True, f"{label}: a binary file, can't show it here. file_get saves it onto someone's PC.", None

    def file_path(self, file_id):
        meta = next((f for f in self.files_view() if f["id"] == file_id), None)
        return (meta, os.path.join(FILES_DIR, meta["disk"])) if meta else (None, None)

    def delete_file(self, who, file_id):
        meta, path = self.file_path(file_id)
        if not meta or who not in (meta["by"], self.cfg["host"]):
            return
        with self.files_lock:
            self.files = [f for f in self.files if f["id"] != file_id]
            save_json_safely(FILES_META, self.files)
        try:
            os.remove(path)
        except OSError:
            pass
        self.broadcast({"t": "files", "files": self.files_view()})

    # ---- pins: notes, cards, files and sketches pinned onto chat messages --
    def pins_view(self):
        with self.pins_lock:
            return list(self.pins)

    def resolve_target(self, who, target):
        """A message id, or "last" = the newest answer for `who` (else the newest answer at all)."""
        ids = {e.get("id") for e in self.history} | set(self.current_aid.values())
        if target and target != "last":
            return target if target in ids else None
        if who in self.current_aid:
            return self.current_aid[who]
        answers = [e for e in self.history if e.get("t") == "answer" and e.get("id")]
        mine = [e for e in answers if e.get("lane") == who]
        return (mine or answers)[-1]["id"] if (mine or answers) else None

    def target_text(self, target):
        ev = next((e for e in reversed(self.history) if e.get("id") == target), None)
        if target in self.current_aid.values():
            return "your current answer"
        if not ev:
            return "an older message"
        if ev["t"] == "answer":
            text = "".join(p[1] for p in ev["parts"] if p[0] == "text")
            return f"Claude's answer to {ev.get('lane') or 'everyone'} “{clip(text, 70)}”"
        if ev["t"] == "msg":
            return f"{ev['from']}'s message “{clip(ev['text'], 70)}”"
        if ev["t"] == "image":
            return f"the picture {ev.get('name', '')}"
        return f"the note “{clip(ev.get('text', ''), 70)}”"

    def pin_text(self, pin):
        if pin["kind"] == "note":
            return f"a note “{clip(pin['text'], 200)}”"
        if pin["kind"] == "card":
            return f"the card “{pin.get('title', '')}” [{pin['card']}]"
        if pin["kind"] == "file":
            return f"the {'picture' if pin.get('image') else 'file'} {pin.get('name', '')} (file {pin['file']})"
        return f"a sketch ({len(pin['sketch']['strokes'])} strokes)"

    def pin_payload(self, who, op, preview=True):
        """What a pin (or a timeline attachment) carries: (fields, error)."""
        kind = op.get("kind")
        if kind == "note":
            text = clean_text(op.get("text"), 600)
            return ({"text": text}, None) if text else (None, "the note is empty")
        if kind == "card":
            with self.board_lock:
                card = next((c for c in self.board["cards"] if c["id"] == op.get("card")), None)
            if not card:
                return None, f"no card {op.get('card')!r} (board_read shows the ids)"
            return {"card": card["id"], "title": card["title"]}, None
        if kind == "file":
            meta, path = self.file_path(str(op.get("file", "")))
            if not meta:
                return None, f"no shared file {op.get('file')!r}"
            image = os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT
            fields = {"file": meta["id"], "name": meta["name"], "image": image}
            if image and preview:
                fields["preview"] = image_preview(path, 28, 14)
            return fields, None
        if kind == "sketch":
            strokes = [st for st in (clean_stroke(x, who) for x in (op.get("strokes") or [])[:300]) if st]
            return ({"sketch": {"strokes": strokes}}, None) if strokes else (None, "the sketch is empty")
        return None, f"can't pin a {kind!r}"

    def pin_op(self, who, op):
        if str(op.get("target", "")).startswith("tl"):   # aimed at a timeline moment
            return self.timeline_op(who, {**op, "op": "attach", "id": op.get("target")})
        if op.get("op") == "delete" and self.timeline_entry_of(op.get("id")):
            return self.timeline_op(who, {"op": "detach", "att": op.get("id")})
        if op.get("op") == "delete":
            with self.pins_lock:
                pin = next((p for p in self.pins if p["id"] == op.get("id")), None)
                if not pin or who not in (pin["by"], self.cfg["host"]):
                    return False, "only whoever pinned it (or the host) can remove it"
                self.pins.remove(pin)
                save_json_safely(PINS, self.pins)
            self.broadcast({"t": "pin", "op": "delete", "id": pin["id"]})
            return True, "removed"
        target = self.resolve_target(who, op.get("target"))
        if not target:
            return False, "nothing to pin onto yet"
        fields, err = self.pin_payload(who, op)
        if err:
            return False, err
        pin = {"id": "p" + secrets.token_hex(4), "target": target, "kind": op.get("kind"), "by": who, "ts": time.time(), **fields}
        with self.pins_lock:
            self.pins.append(pin)
            save_json_safely(PINS, self.pins)
        self.broadcast({"t": "pin", "op": "add", "pin": pin})
        return True, f"pinned {self.pin_text(pin)} onto {self.target_text(target)}"

    # ---- timeline: the project's story, moment by moment --------------
    def timeline_view(self):
        with self.timeline_lock:
            return sorted(json.loads(json.dumps(self.timeline["entries"])), key=lambda e: e["at"])

    def timeline_entry_of(self, att_id):
        with self.timeline_lock:
            return next((e["id"] for e in self.timeline["entries"] if any(a["id"] == att_id for a in e["att"])), None)

    def timeline_text(self, last=40):
        entries = self.timeline_view()
        if not entries:
            return "The timeline is empty."
        lines = []
        for e in entries[-last:]:
            att = ", ".join(self.pin_text(a) for a in e["att"])
            lines.append(f"{time.strftime('%d.%m %H:%M', time.localtime(e['at']))} [{e['kind']}] {e['title']} (by {e['by']}, id {e['id']})"
                         + (f": {clip(e['text'], 200)}" if e["text"] else "") + (f" · attached: {att}" if att else ""))
        return f"{len(entries)} moments on the timeline" + (f", the last {last}" if len(entries) > last else "") + ":\n" + "\n".join(lines)

    def timeline_from_msg(self, msg_id):
        """(title, text, kind, at, author, attachments) for a moment made from a chat message, or None."""
        ev = next((e for e in reversed(self.history) if e.get("id") == msg_id), None)
        if not ev or ev.get("t") not in ("msg", "answer", "image"):
            return None
        if ev["t"] == "answer":
            text = re.sub(r"^\s*\[for [^\]]*\]\s*", "", "".join(p[1] for p in ev["parts"] if p[0] == "text"))
        else:
            text = ev.get("caption", "") if ev["t"] == "image" else ev.get("text", "")
        lines = [re.sub(r"^[#>*\-\s`]+|[*`]+", "", line).strip() for line in text.strip().splitlines()]
        lines = [line for line in lines if line]
        title = clip(lines[0], 90) if lines else ev.get("name") or "From the chat"
        low = text.lower()
        if len(lines) == 1 and title == lines[0]:   # a one-liner: the title says it all
            text = ""
        fixed = any(w in low for w in ("fixed", "fix:", "оправ", "поправ", "фикс"))
        buggy = any(w in low for w in ("bug", "бъг", "broken", "doesn't work", "не работи", "не се"))
        kind = ("art" if ev["t"] == "image" else "fix" if ev["t"] == "answer" and fixed else "bug" if buggy
                else "feature" if ev["t"] == "answer" else "idea")
        att = ([{"kind": "file", "file": ev["file_id"]}] if ev["t"] == "image" else []) + [{"kind": "file", "file": f["id"]} for f in ev.get("files") or []]
        with self.pins_lock:
            att += [dict(p) for p in self.pins if p["target"] == msg_id]
        return title, clip(text.strip(), 1200), kind, ev.get("ts") or time.time(), ev.get("from") or "Claude", att

    def timeline_items(self, who, wanted, strict):
        """Attachments ready to store: pins copied as they are, files/cards checked. strict = a bad one is an error."""
        items = []
        for a in wanted:
            if a.get("id"):   # a pin copied from a chat message
                items.append({**{k: v for k, v in a.items() if k not in ("target", "preview")}, "id": "p" + secrets.token_hex(4)})
                continue
            fields, err = self.pin_payload(who, a, preview=False)
            if err:
                if strict:
                    return None, err
                continue
            items.append({"id": "p" + secrets.token_hex(4), "kind": a["kind"], "by": who, "ts": time.time(), **fields})
        return items, None

    def timeline_op(self, who, op):
        """One change to the timeline, from a browser, a terminal or Claude. Returns (ok, message)."""
        kind = str(op.get("op", ""))
        if kind == "whiteboard":   # the whiteboard as it is now, as a picture on a moment
            if not os.path.exists(SNAPSHOT):
                return False, "the whiteboard has no picture yet: draw something with the page open"
            with open(SNAPSHOT, "rb") as f:
                ok, msg = self.add_file(who, time.strftime("whiteboard-%Y-%m-%d-%H%M.png"), f, os.path.getsize(SNAPSHOT), announce=False)
            if not ok:
                return False, msg
            return self.timeline_op(who, {"op": "attach", "id": op.get("id"), "kind": "file", "file": msg.split()[-1]})
        entry_id, what = op.get("id"), ""
        if kind in ("add", "from_msg", "from_card", "from_pmsg"):
            src, src_who, title, text, guess, at, wanted = "", "", "", "", "feature", time.time(), []
            if kind == "from_msg":
                made = self.timeline_from_msg(op.get("msg"))
                if not made:
                    return False, "that message isn't in the chat any more"
                title, text, guess, at, src_who, wanted = made
                src = op.get("msg")
            elif kind == "from_pmsg":
                with self.lock:
                    m = next((dict(x) for x in self.pchat if x.get("id") == op.get("pmsg")), None)
                if not m:
                    return False, "that message is gone"
                text = m.get("text", "")
                title = clip(text.strip().splitlines()[0], 90) if text.strip() else (m.get("name") or "From the people's chat")
                text = "" if text.strip() == title else text
                guess, at, src_who = "idea", m.get("ts") or time.time(), m.get("from", "")
                wanted = [{"kind": "file", "file": m["file"]}] if m.get("file") else []
            elif kind == "from_card":
                with self.board_lock:
                    card = next((dict(c) for c in self.board["cards"] if c["id"] == op.get("card")), None)
                if not card:
                    return False, f"no card {op.get('card')!r}"
                if op.get("auto"):   # a card going back and forth to Done is one moment
                    with self.timeline_lock:
                        if any(e.get("from_card") == card["id"] and time.time() - e["ts"] < 6 * 3600 for e in self.timeline["entries"]):
                            return True, ""
                title, text = card["title"], card.get("note", "")
                low = (title + " " + text).lower()
                guess = "fix" if card.get("color") == "red" or any(w in low for w in ("bug", "fix", "бъг", "оправ")) else "feature"
                wanted = [{"kind": "card", "card": card["id"]}] + ([{"kind": "file", "file": card["cover"]}] if card.get("cover") else [])
            else:
                src = clip(str(op.get("src") or ""), 20)
                wanted = [{"kind": "file", "file": str(f)} for f in (op.get("files") or [])[:12]]
                wanted += [{"kind": "card", "card": str(op["card"])}] if op.get("card") else []
            title = clean_text(op.get("title"), 140) or title
            if not title:
                return False, "a moment needs a title"
            items, err = self.timeline_items(who, wanted, strict=kind == "add")
            if err:
                return False, err
            entry = {"id": "tl" + secrets.token_hex(4), "at": parse_when(op.get("at")) or at,
                     "kind": op.get("kind") if op.get("kind") in TL_KINDS else guess, "title": title,
                     "text": clean_text(op["text"], 4000) if "text" in op else text, "by": who, "ts": time.time(),
                     "src": src, "src_who": src_who, "att": items, **({"auto": True} if op.get("auto") else {}),
                     **({"from_card": op.get("card")} if kind == "from_card" else {})}
            with self.timeline_lock:
                self.timeline["entries"].append(entry)
                save_json_safely(TIMELINE, self.timeline)
            entry_id, what = entry["id"], f"{who} added “{title}” to the timeline"
        else:
            new_items = []
            if kind in ("attach", "update"):
                wanted = ([{k: op.get(k) for k in ("kind", "text", "card", "file", "strokes")}] if kind == "attach"
                          else [{"kind": "file", "file": str(f)} for f in (op.get("files") or [])[:12]])
                new_items, err = self.timeline_items(who, wanted, strict=True)
                if err:
                    return False, err
            with self.timeline_lock:
                entries = self.timeline["entries"]
                if kind == "detach":
                    entry = next((e for e in entries if any(a["id"] == op.get("att") for a in e["att"])), None)
                else:
                    entry = next((e for e in entries if e["id"] == entry_id), None)
                if not entry:
                    return False, f"no moment {entry_id!r} on the timeline (timeline_read shows the ids)"
                entry_id = entry["id"]
                if kind == "update":
                    changed = []
                    if clean_text(op.get("title"), 140):
                        entry["title"] = clean_text(op["title"], 140)
                        changed.append("title")
                    if "text" in op:
                        entry["text"] = clean_text(op["text"], 4000)
                        changed.append("details")
                    if op.get("kind") in TL_KINDS and op["kind"] != entry["kind"]:
                        entry["kind"] = op["kind"]
                        changed.append(f"now a {op['kind']}")
                    if parse_when(op.get("at")):
                        entry["at"] = parse_when(op["at"])
                        changed.append("time")
                    if new_items:
                        entry["att"] += new_items
                        changed.append(f"{len(new_items)} attached")
                    what = f"{who} edited “{entry['title']}” on the timeline ({', '.join(changed) or 'no change'})"
                elif kind == "attach":
                    entry["att"] += new_items
                    what = f"{who} attached {self.pin_text(new_items[0])} to “{entry['title']}”"
                elif kind == "detach":
                    att = next(a for a in entry["att"] if a["id"] == op.get("att"))
                    if who not in (att["by"], entry["by"], self.cfg["host"]):
                        return False, "only whoever attached it (or the host) can remove it"
                    entry["att"].remove(att)
                    what = f"{who} removed {self.pin_text(att)} from “{entry['title']}”"
                elif kind == "delete":
                    if who not in (entry["by"], self.cfg["host"]) and not (entry.get("auto") and who in self.cfg["people"]):
                        return False, "only whoever added it (or the host) can delete it"
                    entries.remove(entry)
                    what = f"{who} took “{entry['title']}” off the timeline"
                else:
                    return False, f"unknown timeline op {kind!r}"
                save_json_safely(TIMELINE, self.timeline)
        self.broadcast({"t": "timeline", "entries": self.timeline_view(), "by": who, "what": what, "op": kind, "id": entry_id})
        return True, what + (f" (id {entry_id})" if kind in ("add", "from_msg", "from_card", "from_pmsg") else "")

    # ---- Lore locks: every terminal reports what it sees; the room shows the union live ----
    def locks_view(self):
        now = time.time()
        epic_to_person = {r["me_epic"].lower(): n for n, r in self.lock_reports.items() if r.get("me_epic")}
        merged = {}
        for reporter, rep_ in self.lock_reports.items():
            if now - rep_["ts"] > 180:   # that terminal went quiet
                continue
            for rel, info in rep_["locks"].items():
                owner_epic = str(info.get("owner", ""))
                merged[rel] = {"file": rel, "owner_epic": owner_epic, "since": info.get("since", ""),
                               "owner": epic_to_person.get(owner_epic.lower(), owner_epic), "project": rep_.get("project", "")}
        return sorted(merged.values(), key=lambda x: x["file"])

    def report_locks(self, name, body):
        locks = body.get("locks") if isinstance(body.get("locks"), dict) else {}
        before = self.locks_view()
        self.lock_reports[name] = {"ts": time.time(), "project": clean_text(body.get("project"), 80),
                                   "me_epic": clean_text(body.get("me_epic"), 80),
                                   "locks": {clean_text(k, 300): {"owner": clean_text(v.get("owner"), 80),
                                                                  "since": clean_text(v.get("since"), 80)}
                                             for k, v in list(locks.items())[:300] if isinstance(v, dict)}}
        after = self.locks_view()
        if after != before:
            self.broadcast({"t": "locks", "locks": after})

    def notify(self, to, kind, title, body="", **extra):
        """A notification for one person (their browser bell + toast, their terminal's status line)."""
        self.send_to(to, {"t": "notify", "kind": kind, "title": title, "body": body, "ts": time.time(),
                          "id": "n" + secrets.token_hex(4), **extra})

    def lock_request(self, name, rel):
        holder = next((l["owner"] for l in self.locks_view() if l["file"] == rel), None)
        if holder in self.cfg["people"] and holder != name:
            self.notify(holder, "lock", f"{name} wants to edit {rel}",
                        "You hold its Lore lock. When you're done: Check In in UEFN, then release your locks (🔒 menu or /release).",
                        file=rel)
            self.notify(name, "info", f"Asked {holder} to release {rel}")
        else:
            self.notify(name, "info", f"Nobody in the room holds {rel} right now" if not holder else f"{rel} is held by {holder} (outside the room)")

    def lock_blocked(self, name, rel, owner):
        self.notify(name, "lock", f"Blocked: {rel} is locked by {owner}",
                    "Claude couldn't edit it. Ask them to release it, or work on another file.", file=rel)
        holder = next((l["owner"] for l in self.locks_view() if l["file"] == rel), None)
        if holder in self.cfg["people"] and holder != name:
            self.notify(holder, "lock", f"{name}'s Claude tried to edit {rel}", "You hold its lock: Check In and release when you can.",
                        file=rel)

    # ---- requests from the room's Claude to the room's developer ----------
    def feedback_add(self, lane, args):
        kind = args.get("kind") if args.get("kind") in ("bug", "feature", "question") else "feature"
        title = clean_text(args.get("title"), 200)
        if not title:
            return False, "give the request a short title"
        item = {"id": secrets.token_hex(3), "ts": time.time(), "lane": lane, "kind": kind, "title": title,
                "details": clean_text(args.get("details"), 6000), "status": "new", "reply": ""}
        with self.feedback_lock:
            self.feedback.append(item)
            save_json_safely(FEEDBACK, self.feedback)
        self.broadcast({"t": "sys", "text": f"📮 sent to the room's developer ({kind} #{item['id']}): {title}"}, keep=True)
        return True, (f"Sent as #{item['id']}. The room's developer (see dev.py) reviews it and answers: "
                      "approved and shipped as an update, or declined with the reason. room_feedback_status shows it.")

    def feedback_text(self):
        with self.feedback_lock:
            items = self.feedback[-15:]
        if not items:
            return "No requests sent yet."
        return "\n".join(f"#{i['id']} [{i['status']}] {i['kind']}: {i['title']} (from {i['lane']}'s turn)"
                         + (f"\n    developer: {i['reply']}" if i["reply"] else "") for i in items)

    def dev_reply(self, item_id, status, message):
        with self.feedback_lock:
            item = next((i for i in self.feedback if i["id"] == item_id), None)
            if not item:
                return False
            item["status"], item["reply"] = clean_text(status, 30) or item["status"], clean_text(message, 4000)
            save_json_safely(FEEDBACK, self.feedback)
        self.broadcast({"t": "sys", "text": f"🛠 room developer on #{item_id} “{item['title']}”: {item['status']}"
                                            + (f": {item['reply']}" if item["reply"] else "")}, keep=True)
        return True

    # ---- tools for Claude that live in the room itself (no PC, no approval) ----
    # ---- meetings (Meetings tab): recorded in the room, or by a notetaker app writing the same format -------
    def meetings_dir(self):
        return str(self.cfg.get("meetings_dir") or "").strip() or MEETINGS_DIR

    def meeting_key(self, kind):
        """deepgram / groq: room.json, else the environment, else a .env next to the meetings folder. Never leaves the server."""
        env = kind.upper() + "_API_KEY"
        value = str(self.cfg.get(kind + "_api_key") or "").strip() or os.environ.get(env, "").strip()
        if value:
            return value
        root = self.meetings_dir()
        for path in [os.path.join(os.path.dirname(os.path.abspath(root)), ".env")] if root else []:
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        found = re.match(rf"\s*{env}\s*=\s*(.*?)\s*$", line)
                        if found and found.group(1).strip("'\""):
                            return found.group(1).strip("'\"")
            except OSError:
                pass
        return ""

    def meetings_scan(self, force=False):
        """id -> {"path", "dir", "m", "low"}: every *.json with segments under the meetings folder (cached by mtime)."""
        root = self.meetings_dir()
        with self.meet_lock:
            if not force and root == self.meet_root and time.time() - self.meet_scanned < 3:
                return self.meet_index
            index, seen = {}, set()
            if root and os.path.isdir(root):
                base = os.path.abspath(root).rstrip("\\/").count(os.sep)
                for dirpath, dirs, names in os.walk(root):
                    dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "node_modules"
                                     and os.path.abspath(dirpath).count(os.sep) - base < 4)
                    for fn in sorted(names):
                        if not fn.endswith(".json") or not MEETING_ID.fullmatch(fn[:-5]) or fn[:-5] in index:
                            continue
                        path = os.path.join(dirpath, fn)
                        try:
                            st = os.stat(path)
                        except OSError:
                            continue
                        seen.add(path)
                        cached = self.meet_cache.get(path)
                        if not cached or cached[0] != (st.st_mtime_ns, st.st_size):
                            m = load_json(path, None)
                            entry = None
                            if isinstance(m, dict) and isinstance(m.get("segments"), list):
                                segs = meeting_segments(m)
                                entry = {"path": path, "dir": dirpath, "m": m,
                                         "low": [str(s.get("text", "")).lower() for s in segs],
                                         "sum": [l.strip() for l in str(m.get("summary") or "").split("\n") if len(l.strip()) > 3]}
                            cached = self.meet_cache[path] = ((st.st_mtime_ns, st.st_size), entry)
                        if cached[1]:
                            index[fn[:-5]] = cached[1]
            for path in [p for p in self.meet_cache if p not in seen]:
                del self.meet_cache[path]
            self.meet_index, self.meet_root, self.meet_scanned = index, root, time.time()
            return index

    def meeting_entry(self, mid):
        mid = str(mid or "").strip()
        index = self.meetings_scan()
        if mid in ("latest", "newest", "last") and index:
            return max(index.values(), key=lambda e: str(e["m"].get("startedAt") or ""))
        return index.get(mid) if MEETING_ID.fullmatch(mid) else None

    def meeting_audio_path(self, entry):
        name = str(entry["m"].get("audioFile") or "")
        name = name if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}\.webm", name) else os.path.basename(entry["path"])[:-5] + ".webm"
        path = os.path.join(entry["dir"], name)
        return path if os.path.isfile(path) else ""

    def meeting_resume(self, m, n=240):
        s = m.get("summary") or ""
        text = summary_section(s, "Summary") or "\n".join(l for l in s.split("\n") if l.strip() and not l.startswith("#"))
        return clip(" ".join(text.split()), n)

    def meetings_list(self):
        out = []
        for mid, e in self.meetings_scan().items():
            m, segs = e["m"], meeting_segments(e["m"])
            out.append({"id": mid, "title": clip(str(m.get("title") or mid), 200), "startedAt": str(m.get("startedAt") or ""),
                        "durationSec": m.get("durationSec") or (segs[-1].get("start", 0) if segs else 0),
                        "audio": bool(self.meeting_audio_path(e)), "recordedBy": str(m.get("recordedBy") or ""),
                        "summary": self.meeting_resume(m), "lines": len(segs), "summarizing": self.meet_summarizing.get(mid, "")})
        out.sort(key=lambda x: x["startedAt"], reverse=True)
        return out

    def meeting_view(self, mid):
        e = self.meeting_entry(mid)
        if not e:
            return None
        m = e["m"]
        keep = ("id", "title", "startedAt", "endedAt", "durationSec", "sources", "model", "speakerNames", "segments", "suggestions",
                "liveNotes", "summary", "recordedBy", "recordedIn", "importedFromMarkdown")
        view = {k: m.get(k) for k in keep if k in m}
        view.update(id=os.path.basename(e["path"])[:-5], audio=bool(self.meeting_audio_path(e)),
                    summarizing=self.meet_summarizing.get(os.path.basename(e["path"])[:-5], ""))
        return view

    def meetings_hits(self, query, limit=10, per_meeting=4):
        """Best matching moments (words that start with the query's stems; rare words weigh more) and summary lines."""
        terms = meet_terms(query)
        index = self.meetings_scan()
        if not terms or not index:
            return [], terms
        pats = {t: re.compile(r"(?<!\w)" + re.escape(t)) for t in terms}
        found = {mid: [{t for t in terms if pats[t].search(low)} for low in e["low"]] for mid, e in index.items()}
        total = sum(len(f) for f in found.values()) or 1
        weight = {t: math.log(1 + total / (1 + sum(1 for f in found.values() for x in f if t in x))) for t in terms}
        hits = []
        for mid, e in index.items():
            seg = found[mid]
            for i, own in enumerate(seg):
                if not own:
                    continue
                near = {t for j in (i - 1, i + 1) if 0 <= j < len(seg) for t in seg[j]} - own
                hits.append({"id": mid, "i": i, "score": sum(weight[t] for t in own) + 0.35 * sum(weight[t] for t in near), "kind": "line"})
            for line in e["sum"]:
                own = {t for t in terms if pats[t].search(line.lower())}
                if own:   # a little below a transcript line with the same words: people want the moment
                    hits.append({"id": mid, "line": line, "score": sum(weight[t] for t in own) - 0.1, "kind": "summary"})
        hits.sort(key=lambda h: str(index[h["id"]]["m"].get("startedAt") or ""), reverse=True)   # ties: the newer meeting first
        hits.sort(key=lambda h: -h["score"])
        picked, per, taken = [], {}, set()
        for h in hits:
            cap = (h["id"], h["kind"])
            if per.get(cap, 0) >= (per_meeting if h["kind"] == "line" else min(2, per_meeting)):
                continue
            if h["kind"] == "line" and ((h["id"], h["i"] - 1) in taken or (h["id"], h["i"] + 1) in taken):
                continue
            per[cap] = per.get(cap, 0) + 1
            if h["kind"] == "line":
                taken.add((h["id"], h["i"]))
            picked.append(h)
            if len(picked) >= limit:
                break
        out = []
        for h in picked:
            m = index[h["id"]]["m"]
            row = {"id": h["id"], "title": clip(str(m.get("title") or h["id"]), 120), "startedAt": str(m.get("startedAt") or ""),
                   "day": meeting_day(m), "score": round(h["score"], 2), "kind": h["kind"]}
            if h["kind"] == "line":
                segs = meeting_segments(m)
                s = segs[h["i"]]
                ctx = lambda j: f"{speaker_label(m, segs[j].get('speaker'))}: {clip(str(segs[j].get('text', '')), 160)}" if 0 <= j < len(segs) else ""
                row.update(at=meeting_num(s.get("start")), speaker=speaker_label(m, s.get("speaker")), text=str(s.get("text", "")),
                           before=ctx(h["i"] - 1), after=ctx(h["i"] + 1))
            else:
                row.update(at=-1, text=h["line"])
            out.append(row)
        return out, terms

    def meetings_search_text(self, query, limit=10):
        items = self.meetings_list()
        if not items:
            return "No meetings on record (the Meetings tab is empty)."
        if not str(query or "").strip():
            return "Meetings, newest first:\n" + "\n".join(
                f"- {meeting_day(i, True)} “{i['title']}” ({fmt_mmss(i['durationSec'])}, meeting:{i['id']}): {i['summary']}" for i in items[:30])
        hits, terms = self.meetings_hits(query, limit=max(1, min(int(limit or 10), 30)))
        if not hits:
            return (f"Nothing in the meetings matches {query!r} (searched: {', '.join(terms) or 'no content words'}). Try other "
                    "words (in the meeting's language; transcripts have recognition errors), or meeting_read a summary.")
        groups = {}
        for h in hits:
            groups.setdefault(h["id"], []).append(h)
        out = []
        for mid, rows in groups.items():
            out.append(f"{meeting_day(rows[0], True)} “{rows[0]['title']}” (meeting:{mid})")
            for h in sorted(rows, key=lambda r: r["at"]):
                if h["kind"] == "summary":
                    out.append(f"  (summary) {clip(h['text'], 300)}")
                else:
                    out.append(f"  @{fmt_mmss(h['at'])} {h['speaker']}: {clip(h['text'], 400)}"
                               + (f"\n      (before: {h['before']})" if h["before"] else "") + (f"\n      (after: {h['after']})" if h["after"] else ""))
        return cap_text("\n".join(out), 7000) + "\n(meeting_read part=transcript from=mm:ss reads around a moment; cite it as meeting:<id> @mm:ss)"

    def meeting_read(self, mid, part="summary", start=None, end=None, cap=12000):
        e = self.meeting_entry(mid)
        if not e:
            known = ", ".join(f"{i['id']} “{clip(i['title'], 40)}”" for i in self.meetings_list()[:8])
            return False, f"No meeting {mid!r}. Meetings: {known or 'none'} (meetings_search finds them)."
        m, real = e["m"], os.path.basename(e["path"])[:-5]
        head = f"{meeting_day(m, True)} “{m.get('title') or real}” ({fmt_mmss(m.get('durationSec') or 0)}, meeting:{real})"
        if part == "notes":
            sugg = [s for s in m.get("suggestions") or [] if isinstance(s, dict) and s.get("text")]
            body = (f"Live notes:\n{m.get('liveNotes')}\n\n" if m.get("liveNotes") else "") + "\n\n".join(
                f"[{fmt_mmss(s.get('at'))}] {KIND_MD.get(s.get('kind'), '')}: {s['text']}" for s in sugg)
            return True, cap_text(f"{head}\n\n{body.strip() or '(no notes or suggestions in this meeting)'}", cap)
        if part != "transcript":
            speakers = sorted({speaker_label(m, s.get("speaker")) for s in meeting_segments(m)})
            return True, cap_text(f"{head}\nSpeakers: {', '.join(speakers) or 'none'}\n\n{m.get('summary') or '(no summary yet)'}", cap)
        lo, hi = parse_mmss(start) if start else None, parse_mmss(end) if end else None
        segs = [s for s in meeting_segments(m) if (lo is None or meeting_num(s.get("start")) >= lo - 1)
                and (hi is None or meeting_num(s.get("start")) <= hi)]
        lines, size = [], len(head)
        for s in segs:
            line = f"[{fmt_mmss(s.get('start'))}] {speaker_label(m, s.get('speaker'))}: {s.get('text', '')}"
            if size + len(line) > cap:
                lines.append(f"… cut here: read on with from={fmt_mmss(s.get('start'))}")
                break
            lines.append(line)
            size += len(line) + 1
        return True, f"{head}\n\n" + ("\n".join(lines) or "(no lines in that stretch)")

    def meeting_lines(self, lane, texts):
        """[room] lines about the meetings for this turn: the list (when it changed), meetings the message points at,
        and moments found for a message that talks about a meeting."""
        index = self.meetings_scan()
        if not index:
            return []
        out, items = [], self.meetings_list()
        sig = "|".join(f"{i['id']}:{i['title']}" for i in items[:10])
        if self.meet_seen.get(lane) != sig:
            self.meet_seen[lane] = sig
            out.append("[room] meetings on record (meetings_search / meeting_read): " + ", ".join(
                f"{meeting_day(i)} “{clip(i['title'], 70)}”" for i in items[:8]) + (f", and {len(items) - 8} older" if len(items) > 8 else ""))
        text = "\n".join(str(t) for t in texts)
        done = set()
        for mid, at in MEETING_REF.findall(text):
            e = index.get(mid)
            if not e or (mid, at) in done or len(done) >= 3:
                continue
            done.add((mid, at))
            m = e["m"]
            out.append(f"[room] the message points at the meeting {meeting_day(m, True)} “{m.get('title') or mid}” (meeting:{mid})"
                       + (f" at {at}" if at else "") + f"; its summary: {clip(' '.join((m.get('summary') or '(none yet)').split()), 2500)}")
            if at:
                t = parse_mmss(at) or 0
                near = [s for s in meeting_segments(m) if t - 40 <= meeting_num(s.get("start")) <= t + 60]
                if near:
                    out.append(f"[room] what was said around {at} there: " + clip(" | ".join(
                        f"{fmt_mmss(s.get('start'))} {speaker_label(m, s.get('speaker'))}: {s.get('text', '')}" for s in near), 1800))
        rest = MEETING_REF.sub(" ", MEETING_DRAG.sub(" ", text))
        if MEETING_CUES.search(rest):
            hits, _ = self.meetings_hits(rest, limit=3, per_meeting=2)
            hits = [h for h in hits if h["kind"] == "line"] or hits
            if hits:
                out.append("[room] from the meetings (found for this message; check with meetings_search / meeting_read): " + " | ".join(
                    f"{h['day']} “{clip(h['title'], 50)}” (meeting:{h['id']}" + (f" @{fmt_mmss(h['at'])}) {h['speaker']}: " if h["at"] >= 0 else ") summary: ")
                    + clip(h["text"], 220) for h in hits[:3]))
        return out

    def meeting_token(self):
        """For the page's live transcription: a short-lived Deepgram token if the key may mint one, else the room's relay."""
        key = self.meeting_key("deepgram")
        if not key:
            return {"ok": False, "error": "The room has no Deepgram key: put deepgram_api_key in room.json (or DEEPGRAM_API_KEY in the environment)."}
        if time.time() > self.dg_grant_denied:
            req = urllib.request.Request(f"https://{DG_HOST}/v1/auth/grant", data=b'{"ttl_seconds": 60}', method="POST",
                                         headers={"Authorization": f"Token {key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                if data.get("access_token"):
                    return {"ok": True, "mode": "direct", "token": data["access_token"], "expires_in": data.get("expires_in", 60)}
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):   # this key can't mint tokens (a Member key): the room relays instead
                    self.dg_grant_denied = time.time() + 3600
            except (OSError, ValueError):
                pass
        return {"ok": True, "mode": "relay"}

    def meeting_save(self, who, body):
        """A meeting recorded in the page: stored as .json + .md in the meetings folder (the audio comes next)."""
        root = self.meetings_dir()
        src = body.get("meeting") if isinstance(body.get("meeting"), dict) else {}
        if not root:
            return False, "no meetings folder (meetings_dir in room.json)"
        segs = []
        for s in (src.get("segments") or [])[:30000]:
            if not isinstance(s, dict) or not str(s.get("text") or "").strip():
                continue
            spk = str(s.get("speaker") or "s0")
            segs.append({"id": len(segs) + 1, "ch": 1 if s.get("ch") == 1 else 0, "speaker": spk if re.fullmatch(r"me|s\d{1,2}", spk) else "s0",
                         "text": clean_text(s.get("text"), 5000), "start": round(max(0.0, meeting_num(s.get("start"))), 3)})
        segs.sort(key=lambda s: s["start"])   # the two channels finish their lines out of order
        for i, s in enumerate(segs):
            s["id"] = i + 1
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            started = datetime.datetime.fromisoformat(str(src.get("startedAt") or "").replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
        except ValueError:
            started = now
        dur = max(0, min(int(meeting_num(src.get("durationSec"))), 86400))
        mid = str(src.get("id") or "")
        if not MEETING_ID.fullmatch(mid) or len(mid) > 40:
            mid = started.strftime("%Y-%m-%dT%H-%M-%S")
        index = self.meetings_scan(force=True)
        base, n = mid, 1
        while mid in index and not (index[mid]["m"].get("recordedIn") == "room" and index[mid]["m"].get("recordedBy") == who):
            n += 1
            mid = f"{base}-{n}"
        title = clean_text(src.get("title"), 120) or "Meeting " + started.astimezone().strftime("%d %b, %H:%M")
        iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03}Z"
        meeting = {"id": mid, "title": title, "startedAt": iso(started), "endedAt": iso(started + datetime.timedelta(seconds=dur)),
                   "durationSec": dur, "sources": {"mic": bool((src.get("sources") or {}).get("mic")), "system": bool((src.get("sources") or {}).get("system"))},
                   "segments": segs, "speakerNames": {"me": who} if any(s["speaker"] == "me" for s in segs) else {},
                   "suggestions": [], "model": clean_text(src.get("model"), 40) or "nova-3", "recordedBy": who, "recordedIn": "room"}
        if src.get("titleByUser"):
            meeting["titleByUser"] = True
        old = index.get(mid)
        if old:   # the page saving again (a retry): keep what the room added
            for k in ("summary", "audioFile") + (() if meeting.get("titleByUser") else ("title",)):
                if old["m"].get(k):
                    meeting[k] = old["m"][k]
        self.meeting_write(os.path.join(old["dir"] if old else root, mid + ".json"), meeting)
        self.broadcast({"t": "meetings", "id": mid, "by": who, "state": "saved"})
        if not meeting.get("summary"):
            threading.Thread(target=self.meeting_summarize, args=(mid, who), daemon=True).start()
        return True, mid

    def meeting_write(self, path, m):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self.meet_lock:
            with open(path, "w", encoding="utf-8", newline="\n") as f:   # like the app: LF, 2-space JSON
                json.dump(m, f, indent=2, ensure_ascii=False)
            with open(path[:-5] + ".md", "w", encoding="utf-8", newline="\n") as f:
                f.write(meeting_md(m))
            self.meet_scanned = 0

    def meeting_update(self, mid, change):
        e = self.meetings_scan(force=True).get(mid)
        if not e:
            return None
        with self.meet_lock:
            m = load_json(e["path"], None)
        if not isinstance(m, dict):
            return None
        change(m)
        self.meeting_write(e["path"], m)
        return m

    def meeting_audio_upload(self, who, mid, stream, length, ms):
        e = self.meetings_scan(force=True).get(mid) if MEETING_ID.fullmatch(mid or "") else None
        if not e or e["m"].get("recordedIn") != "room" or e["m"].get("recordedBy") != who:
            return False, "not a meeting you recorded in the room"
        if self.meeting_audio_path(e) and e["m"].get("audioFile"):
            return False, "this meeting already has its audio"
        if not 0 < length <= MAX_MEETING_AUDIO:
            return False, f"audio must be 1 byte to {human_size(MAX_MEETING_AUDIO)}"
        os.makedirs(MEETING_UPLOADS, exist_ok=True)
        tmp = os.path.join(MEETING_UPLOADS, f"{mid}-{secrets.token_hex(3)}.webm")
        left = length
        with open(tmp, "wb") as f:
            while left > 0:
                chunk = stream.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            os.remove(tmp)
            return False, "upload interrupted"
        try:
            fixed = ebml_fix_duration(tmp, ms or (e["m"].get("durationSec") or 0) * 1000)
        except OSError:
            fixed = False
        shutil.move(tmp, os.path.join(e["dir"], mid + ".webm"))
        self.meeting_update(mid, lambda m: m.__setitem__("audioFile", mid + ".webm"))
        self.broadcast({"t": "meetings", "id": mid, "by": who, "state": "audio"})
        return True, f"audio saved ({human_size(length)}{', seekable' if fixed else ''})"

    def meeting_digest(self, exclude, limit=1200):
        out = ""
        for i in self.meetings_list():
            if i["id"] == exclude:
                continue
            entry = f"- {meeting_day(i, True)} — {i['title']}: {clip(i['summary'], 220)}\n"
            if len(out) + len(entry) > limit:
                break
            out += entry
        return out.strip()

    def meeting_summarize(self, mid, who, announce=True):
        """Title + summary (SUMMARY_SYSTEM, in the meetings' language) from Groq; long meetings go in parts (free-tier limits)."""
        if ai_paused():
            self.broadcast({"t": "meetings", "id": mid, "by": who, "state": "summary", "error": AI_PAUSE_MESSAGE})
            return
        with self.lock:
            if mid in self.meet_summarizing:
                return
            self.meet_summarizing[mid] = "summarizing…"
        error, title = "", mid
        try:
            e = self.meeting_entry(mid)
            m = dict(e["m"]) if e else {}
            title = m.get("title") or mid
            segs = meeting_segments(m)
            if not segs:
                summary = f"# {title}\n\n## Summary\nNo speech was recognised in this recording."
            else:
                key = self.meeting_key("groq")
                if not key:
                    raise RuntimeError("no Groq key (groq_api_key in room.json)")
                speakers = ", ".join(dict.fromkeys(speaker_label(m, s.get("speaker")) for s in segs))
                me = speaker_label(m, "me")
                dur = meeting_num(m.get("durationSec"))
                header = (f"Date: {meeting_date_text(m)}\nDuration: {fmt_mmss(dur)} ({'h:mm:ss' if dur >= 3600 else 'mm:ss'})\nSpeakers: {speakers}\n"
                          f"(\"{me}\" is the person who recorded the meeting.)")
                digest = self.meeting_digest(mid)
                lang = meeting_language(self.cfg)[1]
                chunk_system = CHUNK_SYSTEM.replace("{language}", lang)
                system = SUMMARY_SYSTEM.replace("{language}", lang) + (
                    "\n\n" + SUMMARY_LINKS + "\n\nPAST MEETINGS (memory, newest first):\n" + digest if digest else "")
                room_for = int((GROQ_TPM - 3000) * GROQ_CPT) - len(system) - len(header) - 120   # what fits next to the answer
                text = transcript_text(m)
                if len(text) <= room_for:
                    body = f"Transcript:\n\n{text}"
                else:   # map: short notes per part (each call within the free tier's tokens a minute), then the summary
                    size = int((GROQ_TPM - 1300) * GROQ_CPT) - len(chunk_system) - len(header) - 160
                    parts, cur = [], ""
                    for line in text.split("\n"):
                        if cur and len(cur) + len(line) > size:
                            parts.append(cur)
                            cur = ""
                        cur += clip(line, 2000) + "\n"
                    parts = (parts + [cur])[:24]
                    per = max(350, room_for // len(parts) - 40)
                    notes = []
                    for i, part in enumerate(parts):
                        self.meet_summarizing[mid] = f"summarizing part {i + 1}/{len(parts)}…"
                        self.broadcast({"t": "meetings", "id": mid, "state": "progress"})
                        got = groq_chat(key, chunk_system.replace("{points}", str(max(3, per // 150))),
                                        f"{header}\n\nPart {i + 1} of {len(parts)}:\n\n{part}", 1300)
                        notes.append(f"Part {i + 1}:\n" + cap_text(got, per))
                    body = "The transcript is long, so it comes as notes per part (with moments [mm:ss]):\n\n" + "\n\n".join(notes)
                    self.meet_summarizing[mid] = "writing the summary…"
                    self.broadcast({"t": "meetings", "id": mid, "state": "progress"})
                summary = groq_chat(key, system, f"{header}\n\n{body}", 3000)

            def apply(mm):
                mm["summary"] = summary
                found = re.search(r"^#\s+(.+)$", summary, re.M)
                if found and not mm.get("titleByUser"):
                    mm["title"] = found.group(1).strip()[:120]
            saved = self.meeting_update(mid, apply)
            title = (saved or {}).get("title") or title
        except Exception as ex:   # the meeting is saved either way; the summary can be retried from the page
            error = clip(str(ex), 300)
            with open(CONNECT_LOG, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} meeting summary {mid} failed: {error}\n")
        finally:
            with self.lock:
                self.meet_summarizing.pop(mid, None)
        self.broadcast({"t": "meetings", "id": mid, "by": who, "state": "summary", **({"error": error} if error else {})})
        if announce:
            self.broadcast({"t": "sys", "text": f"📝 {who} saved the meeting “{title}” (Meetings tab)", "meeting": mid}, keep=True)

    def room_tool(self, lane, tool, args):
        who = assistant_name(self.running_models.get(lane))
        if tool == "conversation_read":
            from codex_provider import conversation_read
            return conversation_read(self, lane, args)
        if tool == "claude_session_read":
            from codex_provider import claude_session_read
            return claude_session_read(self, lane, args)
        if tool == "files_list":
            return True, self.files_text(), None
        if tool == "file_view":
            return self.file_view(args.get("file_id", ""))
        if tool == "pin_add":
            target = args.get("target") or self.current_aid.get(lane) or "last"
            ok, msg = self.pin_op(who, {"op": "add", "target": target, "kind": args.get("kind"),
                                             "text": args.get("text"), "card": args.get("card_id"),
                                             "file": args.get("file_id")})
            return ok, msg, None
        if tool == "image_generate":
            ok, msg = self.generate_image(who, args.get("prompt", ""), args.get("aspect", "square"),
                                          str(args.get("edit_file_id") or ""), lane=lane)
            return ok, msg, None
        if tool == "room_feedback":
            ok, msg = self.feedback_add(lane, args)
            return ok, msg, None
        if tool == "room_feedback_status":
            return True, self.feedback_text(), None
        if tool == "board_read":
            return True, self.board_text(), None
        if tool == "board_add":
            ok, msg = self.board_op(who, {"op": "add", "title": args.get("title"), "col": args.get("column"),
                                          "board": args.get("board"), "who": args.get("assignee"), "note": args.get("note"),
                                          "color": args.get("label", ""), "priority": args.get("priority", "")})
            return ok, msg + self.card_follow(lane, who, ok, msg, ""), None
        if tool == "board_update":
            op = {"op": "update", "id": args.get("id")}
            for key, field in (("title", "title"), ("note", "note"), ("assignee", "who"), ("label", "color"),
                               ("column", "col"), ("board", "board"), ("priority", "priority"), ("position", "position")):
                if key in args:
                    op[field] = args[key]
            ok, msg = self.board_op(who, op)
            return ok, msg + self.card_follow(lane, who, ok, msg, args.get("id")), None
        if tool == "board_manage":
            ops = {"create_board": "board_create", "rename_board": "board_rename", "delete_board": "board_delete",
                   "add_column": "col_add", "rename_column": "col_rename", "delete_column": "col_delete"}
            kind = ops.get(str(args.get("op", "")))
            if not kind:
                return False, f"op must be one of {', '.join(ops)}", None
            ok, msg = self.board_op(who, {"op": kind, "board": args.get("board"), "name": args.get("name"),
                                          "title": args.get("name"), "col": args.get("column")})
            return ok, msg, None
        if tool == "board_delete":
            ok, msg = self.board_op(who, {"op": "delete", "id": args.get("id")})
            return ok, msg, None
        if tool == "thread_pin":
            ok, msg = self.thread_pin(who, lane, {"kind": args.get("kind"), "text": args.get("text"),
                                                       "card": args.get("card_id"), "file": args.get("file_id")})
            return ok, msg, None
        if tool == "thread_unpin":
            ok, msg = self.thread_unpin(who, lane, str(args.get("id", "")))
            return ok, msg, None
        if tool == "thread_spawn":
            ok, msg = self.thread_spawn(who, lane, args)
            return ok, msg, None
        if tool == "thread_close":
            target = self.find_lane(args.get("thread"))
            if not target or self.lane_meta(target).get("parent") != lane:
                return False, "you can only close your own sub-threads (threads_read lists them)", None
            ok, msg = self.thread_op(owner(lane), {"op": "close", "thread": target})
            return ok, ("closed " + self.lane_label(target)) if ok else msg, None
        if tool == "plan_update":
            ok, msg = self.plan_update(lane, args)
            return ok, msg, None
        if tool == "lesson_add":
            ok, msg = self.lesson_add(lane, args.get("text"))
            return ok, msg, None
        if tool == "zone_add":
            ok, msg = self.zone_op(who, lane, {**args, "op": "add"})
            return ok, msg, None
        if tool == "zone_remove":
            ok, msg = self.zone_op(who, lane, {"op": "remove", "id": args.get("id")})
            return ok, msg, None
        if tool == "zones_read":
            return True, "\n".join(self.zone_line(z) for z in self.zones_view()) or "no zones yet", None
        if tool == "zone_check":
            try:
                a = [float(v) for v in (args.get("min") or args.get("point"))][:3]
                b = [float(v) for v in (args.get("max") or args.get("point"))][:3]
            except (TypeError, ValueError):
                return False, "min/max (or point) are [x, y, z]", None
            hit = self.zones_hit([min(p, q) for p, q in zip(a, b)], [max(p, q) for p, q in zip(a, b)])
            return True, ("overlaps: " + " | ".join(self.zone_line(z) for z in hit)) if hit else "free: no zone there", None
        if tool == "project_status":
            return True, self.project_text(), None
        if tool == "threads_read":
            return True, self.threads_text_full(lane), None
        if tool == "thread_note":
            ok, msg = self.thread_note(lane, args.get("to"), args.get("text"))
            return ok, msg, None
        if tool == "reminder_add":
            to = str(args.get("to") or "all").lower()
            ok, msg = self.reminder_add(who, f"{to} {args.get('when', '')} {args.get('text', '')}", lane)
            return ok, msg, None
        if tool == "poll_create":
            ok, msg = self.poll_create(who, args.get("question"), args.get("options") or [])
            return ok, msg, None
        if tool == "whiteboard_view":
            return self.whiteboard_view()
        if tool == "meetings_search":
            return True, self.meetings_search_text(str(args.get("query") or ""), args.get("limit") or 10), None
        if tool == "meeting_read":
            part = str(args.get("part") or "summary")
            ok, text = self.meeting_read(str(args.get("id") or ""), part if part in ("summary", "transcript", "notes") else "summary",
                                         args.get("from"), args.get("to"))
            return ok, text, None
        if tool == "timeline_read":
            return True, self.timeline_text(), None
        if tool == "timeline_add":
            ok, msg = self.timeline_op(who, {"op": "add", "title": args.get("title"), "kind": args.get("kind"),
                                             "text": args.get("details", ""), "at": args.get("when"),
                                             "files": args.get("file_ids") or [], "card": args.get("card_id"),
                                             "src": self.current_aid.get(lane, "")})
            return ok, msg, None
        if tool == "timeline_update":
            op = {"op": "update", "id": args.get("id"), "files": args.get("add_file_ids") or []}
            for key, field in (("title", "title"), ("kind", "kind"), ("details", "text"), ("when", "at")):
                if key in args:
                    op[field] = args[key]
            ok, msg = self.timeline_op(who, op)
            return ok, msg, None
        return False, f"unknown room tool {tool!r}", None

    def lane_for_token(self, token):
        with self.lock:
            tokens = list(self.exec_tokens.items())
        for secret, lane in tokens:
            if secrets.compare_digest(secret, token):
                return lane
        return None

    # ---- acting on people's PCs ----------------------------------------
    def pcs_line(self):
        latest = client_version()
        with self.lock:
            agents = dict(self.agents)
            kinds = {}
            for name, q in self.clients:
                kinds.setdefault(name, []).append(self.conn_info.get(q, {"kind": "browser", "version": 0}))
        bits = []
        for name in sorted(set(kinds) | set(agents)):
            info = agents.get(name, {}).get("info")
            conns = kinds.get(name, [])
            if info:
                with self.lock:
                    ver = self.conn_info.get(agents[name]["q"], {}).get("version", 0)
                old = f", OUTDATED: newest is v{latest}" if ver < latest else ""
                bits.append(f"{name} = {info['os']} (user {info['user']}, home {info['home']}, chat.py v{ver}{old})"
                            + self.activity_text(name))
            elif any(c["kind"] == "chat.py" and c["version"] < MIN_PC_VERSION for c in conns):
                bits.append(f"{name} = connected with an OUTDATED chat.py that can't share a PC "
                            "(they must re-download chat.py from the room link)")
            elif any(c["kind"] == "chat.py" for c in conns):
                bits.append(f"{name} = chat.py with PC sharing turned off (--no-pc)")
            else:
                bits.append(f"{name} = browser only, no PC connected")
        return "[room] PCs: " + (" · ".join(bits) or "none")

    def exec(self, askers, user, tool, args, by_owner=False):
        """Run a tool call on `user`'s own PC through their chat.py; blocks until they answer.
        `askers` = whose turn made the call (decided by the server, not by Claude)."""
        lane = askers[0]
        askers = [owner(a) for a in askers]   # a thread acts for its person
        if not user and len(set(askers)) == 1:
            user = askers[0]   # a call without "user" (e.g. nested inside arguments) can only mean the asker's own PC
        if user not in askers:
            return False, (f"refused: this is {', '.join(askers)}'s turn and {user or 'nobody'} didn't ask for it. "
                           "Only act on the PC of the person whose turn it is.")
        with self.lock:
            agent = self.agents.get(user)
        if not agent:
            return False, f"{user} has no PC connected (browser only or offline). They need to join with chat.py."
        with self.lock:
            agent_version = self.conn_info.get(agent["q"], {}).get("version", 0)
        if tool in CHANGING_TOOLS and agent_version < MIN_CHANGE_VERSION:
            return False, (f"refused: {user}'s chat.py is v{agent_version}; changing things on a PC needs v{MIN_CHANGE_VERSION}+ "
                           "(it takes Lore locks so two people never overwrite each other). Ask them to type /update "
                           "in their chat.py window, then try again. Reading files still works.")
        if not by_owner and self.needs_card(lane, tool, args):
            return False, ("TASK FIRST: nothing was changed. This thread has no open task card yet. Before changing anything: "
                           "board_read; if this work is already a card, board_update it to Doing (assignee " + user + "); if not, "
                           "board_add it in Doing with a plain title anyone understands and a short note (what's wrong or "
                           "wanted, why, how to check it). The room pins the card to this thread by itself; then run this again. "
                           "Looking around (reads) needs no card.")
        exec_id = secrets.token_hex(6)
        approval = self.cfg["approval_seconds"]
        msg = {"t": "exec", "id": exec_id, "tool": tool, "args": args, "asked_by": askers,
               "timeout": approval, "lane": lane, "thread": self.thread_name(lane), "by_owner": by_owner}
        entry = {"user": user, "lane": lane, "started": threading.Event(), "done": threading.Event(), "result": None,
                 "msg": msg, "q": agent["q"], "until": time.time() + approval}   # q / msg: to ask again if that terminal goes
        self.execs[exec_id] = entry
        agent["q"].put(msg)
        with self.lock:
            auto = (self.activity.get(user, {}).get("pc") or {}).get("auto")   # None: their chat.py doesn't say
        grace = 0 if by_owner else 8 if auto and askers == [user] else 2.5 if auto is None else 0
        if grace and entry["started"].wait(grace):
            by_owner = True   # auto mode already started it: nobody needs asking
        if not by_owner:   # the same y / n / a prompt in their browser tabs; a click is passed on to their chat.py
            with self.lock:
                remote_ok = self.conn_info.get(agent["q"], {}).get("version", 0) >= BROWSER_ANSWER_VERSION
            entry["ask"] = {"t": "approval", "id": exec_id, "summary": describe(tool, args), "asked_by": askers,
                            "timeout": approval, "until": time.time() + approval, "remote_ok": remote_ok}
            self.send_to(user, entry["ask"], kind="browser")
            self.broadcast({"t": "status", "text": f"waiting for {user} to approve on their PC", "lane": lane})
        # "started" is set when they approve (or answer at all); only that part is time-limited
        answered = entry["started"].wait(approval)
        self.send_to(user, {"t": "approval_done", "id": exec_id}, kind="browser")
        if not answered:
            entry["result"] = (False, f"{user} did not answer the approval prompt within {approval} seconds, "
                                      "so it was not run. Carry on without it; they can ask again.")
            self.broadcast({"t": "sys", "text": f"{user} didn't answer the approval in {approval}s, so it was skipped"},
                           keep=True)
        else:
            if not entry["done"].is_set():
                self.broadcast({"t": "status", "text": f"running on {user}'s PC", "lane": lane})
            if not entry["done"].wait(880):   # UEFN builds can take a while
                entry["result"] = (False, f"no result from {user}'s PC within 880 seconds")
        self.execs.pop(exec_id, None)
        result = entry["result"]
        if result and not result[0] and str(result[1]).startswith("HELD:"):
            with self.lock:
                self.lease_waiters.setdefault(user, {})[lane] = time.time()
        m = re.search(r" \[undo:(\d{14}-[0-9a-f]{6})\]$", result[1]) if tool == "write_file" and result and result[0] else None
        if m:   # chat.py kept the old file: its tool line gets ↶ Undo
            result = (True, result[1][:m.start()])
            tid = self.write_tids.pop((lane, canon(args)), None)
            if tid:
                self.remember_undo(tid, user, m.group(1), str(args.get("path", "")), lane)
        return result

    def remote_answer(self, name, exec_id, answer):
        """y / n / a clicked in someone's browser: their chat.py runs or denies it, exactly like a key press."""
        entry = self.execs.get(exec_id)
        with self.lock:
            agent = self.agents.get(name)
        if entry and entry["user"] == name and agent and answer in ("y", "n", "a") and not entry["started"].is_set():
            agent["q"].put({"t": "remote_answer", "id": exec_id, "answer": answer})

    def exec_started(self, name, exec_id):
        entry = self.execs.get(exec_id)
        if entry and entry["user"] == name:
            entry["started"].set()

    def exec_result(self, name, exec_id, ok, output):
        entry = self.execs.get(exec_id)
        if entry and entry["user"] == name:
            entry["result"] = (bool(ok), str(output))
            entry["started"].set()
            entry["done"].set()

    # ---- control threads: answered by the host's own Claude Code session (operator/inbox -> operator/outbox) ----
    def operator_view(self, name):
        path = os.path.join(OPERATOR_DIR, "routing.json")
        config = load_json(path, {})
        if name != config.get("lane") or not config.get("enabled"):
            return {"available": False}
        mode = config.get("mode", "auto")
        limited = any(float(l.get("percent") or 0) >= 100 and float(l.get("resets") or 0) > time.time()
                      for l in (self.usage or {}).get("limits", []))
        active = ("codex" if limited else "claude") if mode == "auto" else mode
        return {"available": True, "mode": mode, "active": active, "lane": name,
                "scope": "Main", "busy": name in self.busy}

    def set_operator(self, name, mode):
        if not self.operator_view(name).get("available"):
            return False, "This controller belongs to its room owner."
        if mode not in ("auto", "claude", "codex"):
            return False, "Choose Auto, Claude or Codex."
        with self.notes_lock:
            path = os.path.join(OPERATOR_DIR, "routing.json")
            config = load_json(path, {})
            config["mode"] = mode
            save_json_safely(path, config)
            self.notes["lanes"].setdefault(name, {})["engine"] = "operator"
            save_json_safely(NOTES, self.notes)
        state = self.operator_view(name)
        self.send_to(name, {"t": "operator", "operator": state})
        return True, state

    def ask_operator(self, lane, prompt):
        if ai_paused():
            return []
        aid = self.current_aid.get(lane) or ("a" + secrets.token_hex(4))
        with self.lock:
            texts = list((self.busy.get(lane) or {}).get("texts") or [])
        job = {"id": aid, "lane": lane, "person": owner(lane), "thread": self.thread_name(lane) or "Main",
               "texts": texts, "ts": time.time(), "context": clip(prompt, 6000)}
        inbox, outbox = os.path.join(OPERATOR_DIR, "inbox"), os.path.join(OPERATOR_DIR, "outbox")
        os.makedirs(inbox, exist_ok=True)
        os.makedirs(outbox, exist_ok=True)
        # Reserve the controller before making the request visible to either
        # watcher, so a manual choice cannot launch both assistants.
        if self.operator_view(owner(lane)).get("available"):
            from operator_routing import claim_for
            chosen = self.operator_view(owner(lane))["active"]
            if not claim_for(job, chosen, {"usage": self.usage or {}}):
                return [["text", "This controller request is already claimed; it was not started again."]]
        with open(os.path.join(inbox, aid + ".json"), "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        self.broadcast({"t": "status", "text": "waiting for the Main controller", "lane": lane})
        reply_path, deadline = os.path.join(outbox, aid + ".json"), time.time() + 45 * 60
        while time.time() < deadline:
            if lane in self.halted:
                return [["text", "(stopped before the controller answered)"]]
            if os.path.exists(reply_path):
                try:
                    with open(reply_path, encoding="utf-8") as f:
                        text = str(json.load(f).get("text") or "").strip()
                except (OSError, ValueError):
                    time.sleep(1)
                    continue
                if text:
                    self.broadcast({"t": "delta", "text": text, "lane": lane})
                    return [["text", text]]
            time.sleep(1)
        return [["text", "The controller didn't answer within 45 minutes; ask again, or switch back with /operator off."]]

    def ask_claude(self, lane, prompt):
        if ai_paused():
            return [], False
        # a fresh secret per turn tells the server whose turn a PC action comes from
        if self.lane_meta(lane).get("engine") == "operator":   # a control thread: the host's Claude Code answers
            return self.ask_operator(lane, prompt), False
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.exec_tokens[token] = lane
        try:
            if is_gemini_model(self.running_models.get(lane, self.turn_pref(lane)[0])):
                from gemini_provider import ask_gemini
                return ask_gemini(self, lane, prompt, token)
            if is_codex_model(self.running_models.get(lane, self.turn_pref(lane)[0])):
                from codex_provider import ask_codex
                return ask_codex(self, lane, prompt, token)
            return self._ask_claude(lane, prompt, token)
        finally:
            with self.lock:
                self.exec_tokens.pop(token, None)
                self.procs.pop(lane, None)

    def _ask_claude(self, lane, prompt, token):
        if ai_paused():
            return [], False
        over = self.team_blocked(lane)
        if over:
            text = (f"⏸ Not started: the room is on the team plan and it reached the stop line ({self.team_stop()}%): "
                    + ", ".join(f"{l['label'].lower()} {l['percent']}%, resets {until_text(l['resets'])}" for l in over)
                    + f". {self.cfg['host']} can switch to the personal plan or move the line in ◔ Usage.")
            self.broadcast({"t": "delta", "text": text, "lane": lane})
            return [["text", text]], False
        mcp = {"mcpServers": {"room": {
            "command": sys.executable, "args": [os.path.join(ROOT, "room_mcp.py")],
            "env": {"ROOM_URL": f"http://127.0.0.1:{self.cfg['port']}", "ROOM_EXEC_TOKEN": token}}}}
        # Built-in tools limited to web search/fetch: nothing runs on this PC.
        args = [self.claude, "-p", "--output-format", "stream-json", "--verbose",
                "--input-format", "stream-json", "--include-partial-messages", "--strict-mcp-config", "--mcp-config", json.dumps(mcp),
                "--tools", "WebSearch,WebFetch,Agent", "--permission-mode", "default",
                "--allowedTools", "mcp__room", "WebSearch", "WebFetch", "Agent"]
        with self.state_lock:
            generation = self.generation
            own = self.state["lanes"].get(lane, {}).get("session")
            shared = self.state.get("session_id")
        with self.state_lock:
            main = self.state["lanes"].get(owner(lane), {}).get("session") if lane != owner(lane) else None
        if own:
            args += ["--resume", own]
        elif main:     # a new thread: branch off the person's main conversation, so it knows what they did
            args += ["--resume", main, "--fork-session"]
        elif shared:   # this person's first turn: branch off the shared conversation
            args += ["--resume", shared, "--fork-session"]
        model, effort = self.turn_pref(lane)   # each person picks their own model and effort (/continue: one turn's own)
        model = self.running_models.get(lane, model)
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
        args += self.cfg["claude_args"]
        env = {**os.environ, "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "MCP_TOOL_TIMEOUT": "900000", **self.account_env(lane)}

        parts = []   # ["text", str] / ["tool", name, detail, tool_id, ok, secs, preview]
        self.live_parts[lane] = parts
        ctx = None   # tokens the conversation had at the turn's last request
        turn_tools = []
        result = {}
        try:
            proc = subprocess.Popen(
                args, cwd=WORKSPACE, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            self.broadcast({"t": "sys", "text": f"could not start claude: {e}"}, keep=True)
            return parts, False
        with self.lock:
            self.procs[lane] = proc
        stderr_lines = []
        threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()
        proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
        proc.stdin.flush()
        with self.lock:
            self.live_stdin[lane] = proc.stdin   # until its result: messages sent meanwhile join this turn

        for line in proc.stdout:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            t = o.get("type")
            if t == "system" and o.get("subtype") == "init":
                with self.state_lock:
                    if generation == self.generation:
                        self.state["lanes"].setdefault(lane, {})["session"] = o.get("session_id")
                        save_json(STATE, self.state)
            elif t == "stream_event":
                ev = o.get("event", {})
                if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    text = ev["delta"]["text"]
                    if parts and parts[-1][0] == "text":
                        parts[-1][1] += text
                    else:
                        parts.append(["text", text])
                    self.broadcast({"t": "delta", "text": text, "lane": lane})
                elif ev.get("type") == "content_block_start" and ev.get("content_block", {}).get("type") == "thinking":
                    self.broadcast({"t": "status", "text": "thinking", "lane": lane})
            elif t == "assistant" and not o.get("parent_tool_use_id"):
                u = o.get("message", {}).get("usage") or {}
                if u.get("input_tokens") is not None:
                    ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                for block in o.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        name, inp = block["name"], block.get("input", {})
                        detail = tool_detail(inp)
                        if name == "mcp__room__uefn_call":
                            a = inp.get("arguments") or {}
                            inner = a.get("arguments") if isinstance(a.get("arguments"), dict) else {}
                            detail = " ".join(x for x in (str(inp.get("tool", "")),
                                                          ".".join(str(a[k]) for k in ("toolset_name", "tool_name") if a.get(k)),
                                                          str(inner.get("path") or inner.get("actor") or inner.get("name") or "")[:60]) if x)
                        if name.startswith("mcp__room__"):
                            short = name[len("mcp__room__"):]
                            name = f"{inp.get('user', '?')}'s PC · {short}" if "user" in inp else f"room · {short}"
                        tid = str(block.get("id") or "t" + secrets.token_hex(6))
                        self.tool_start(tid, lane, name, inp)
                        turn_tools.append(tid)
                        parts.append(["tool", name, detail, tid, None, None, ""])
                        self.broadcast({"t": "tool", "name": name, "detail": detail, "tid": tid, "lane": lane})
            elif t == "user" and not o.get("parent_tool_use_id"):
                content = o.get("message", {}).get("content")
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tid, ok = str(block.get("tool_use_id", "")), not block.get("is_error")
                    done = self.tool_finish(tid, ok, result_text(block.get("content")))
                    if not done:
                        continue
                    preview, lines = tool_preview(done["output"])
                    for p in parts:
                        if p[0] == "tool" and len(p) > 3 and p[3] == tid:
                            p[4:7] = [ok, done["secs"], preview]
                    self.broadcast({"t": "tool_done", "tid": tid, "ok": ok, "secs": done["secs"],
                                    "preview": preview, "lines": lines, "lane": lane})
            elif t == "result":
                result = o
                self.end_input(lane, proc)   # no more input: the process ends after this answer
        self.end_input(lane, proc)
        proc.wait()
        rc = proc.returncode
        if ctx:
            with self.state_lock:
                if generation == self.generation and lane in self.state["lanes"]:
                    self.state["lanes"][lane]["ctx"] = ctx
                    save_json(STATE, self.state)
        for tid in turn_tools:   # stopped before a result came: still keep what it was asked to do
            self.tool_finish(tid, None, None)

        if lane in self.stopping:   # /stop killed it: what came so far is the answer, no error line
            self.stopping.discard(lane)
            return parts, False
        err = "".join(stderr_lines).strip()
        if "No conversation found" in err and not parts:
            return parts, True
        if rc == 0 and not result.get("is_error"):
            with self.state_lock:
                if generation == self.generation:
                    self.state["lanes"].setdefault(lane, {})["claude_seen"] = time.time()
                    save_json(STATE, self.state)
        text = result.get("result")
        if result.get("is_error") or (rc and not parts):
            reason = text or err[-400:] or result.get("subtype") or f"exit code {rc}"
            self.broadcast({"t": "sys", "text": f"claude error ({lane}): {reason}"}, keep=True)
        elif text and not any(p[0] == "text" for p in parts):
            parts.append(["text", text])
            self.broadcast({"t": "delta", "text": text, "lane": lane})
        return parts, False


def clip_long(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n] + f"\n… ({len(text) - n:,} more characters not kept)"


def result_text(content):
    """A tool_result's content: a string, or blocks of text/pictures."""
    if isinstance(content, str):
        return content
    out = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text", "")))
        elif isinstance(b, dict):
            out.append(f"[{b.get('type', 'content')}]")
    return "\n".join(out)


def tool_preview(output):
    """First meaningful line of a tool's output and how many lines it has."""
    lines = [l.strip() for l in (output or "").splitlines() if l.strip()]
    if not lines:
        return "(no output)" if output is not None else "", 0
    first = lines[0]
    return (first[:140] + ("…" if len(first) > 140 else "")), len(lines)


def owner(lane):
    """A lane is a person ("alice") or one of their threads ("alice/inventory-ui")."""
    return str(lane or "").split("/", 1)[0]


def tool_detail(inp):
    for key in ("file_path", "command", "pattern", "url", "query", "path", "title", "id", "description"):
        if key in inp:
            value = str(inp[key]).replace("\n", " ")
            return value[:100] + ("…" if len(value) > 100 else "")
    return ""


def kill_tree(proc):
    if hasattr(proc, "cancel"):
        proc.cancel()
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.kill()


# ---- meetings: transcripts (Deepgram live speech-to-text) and summaries (Groq) ----------------------------
MEETINGS_DIR = os.path.join(ROOT, "meetings")           # the default meetings folder (meetings_dir in room.json)
LANGUAGES = {"en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese",
             "nl": "Dutch", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
             "cs": "Czech", "ro": "Romanian", "bg": "Bulgarian", "ru": "Russian", "uk": "Ukrainian", "el": "Greek",
             "tr": "Turkish", "hi": "Hindi", "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
MEETING_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")          # fullmatch: a meeting's file name without .json
MEETING_REF = re.compile(r"\bmeeting:([A-Za-z0-9_-]{6,80})(?:\s*@\s*(\d{1,2}:\d{2}(?::\d{2})?))?")
MEETING_DRAG = re.compile(r"📝 Meeting[^\n]*?\(meeting:[A-Za-z0-9_-]+(?:\s*@\s*[\d:]+)?\)(?:\s*—[^\n„]*„[^“\n]*“)?")
# words that mean "in a meeting" (Bulgarian and English; add your language's)
MEETING_CUES = re.compile(r"\b(?:срещ(?:а|ата|и|ите)|мийтинг\w*|оперативк\w*|транскрипт\w*|говорихме|казахме|обсъждахме|"
                          r"обсъдихме|решихме|meetings?|transcripts?|we (?:said|discussed|decided|talked))\b", re.I)
MEETING_UPLOADS = os.path.join(ROOT, "meeting_uploads")   # audio lands here first, then moves next to its meeting
MAX_MEETING_AUDIO = 400 * 1024 * 1024
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_GATE = {"until": 0.0, "lock": threading.Lock()}       # Groq's free tier: 8000 tokens a minute
GROQ_TPM, GROQ_CPT = 7800, 2.2                              # tokens a request may use; characters a token (cautious: non-English text)
DG_HOST = "api.deepgram.com"
DG_PARAMS = ("model", "language", "encoding", "sample_rate", "channels", "multichannel", "interim_results", "punctuate",
             "endpointing", "utterance_end_ms", "vad_events", "diarize", "smart_format")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
KIND_MD = {"auto": "Automatic", "post": "After the meeting", "manual": "On request"}
# words the meeting search skips (Bulgarian and English; add your language's)
MEET_STOP = set("""как какво кога къде защо дали това този тази тези които който която което беше бяха има няма ние вие още само или
ако при след преди много мисля среща срещата срещи срещите казахме говорихме обсъждахме обсъдихме решихме минал миналата предишна
предишната нещо така също обаче тогава сега със без под над оперативка оперативката мийтинг мийтинга транскрипт транскрипта там тук
нали добре значи the and for what meeting meetings transcript did said say about with that this was were from have when where which
who how """.split())

SUMMARY_SYSTEM = """You take notes from meetings (like a meeting notetaker). You get an automatic transcript of a meeting held in {language}; it has recognition errors, so go by the meaning, not the literal words.

Write the notes in {language}, in Markdown, in exactly this structure:

# <a short, specific title for the meeting, up to 8 words>

## Summary
3-5 sentences: what the meeting was about and where it ended up.

## Key topics
For each topic: a line **Topic** and under it 1-3 points with the specifics (numbers, names, deadlines).

## Decisions
- …

## Tasks
- [ ] **Who:** what (deadline, if one was mentioned)

## Open questions
- …

## Next steps
- …

Rules: leave out a section with nothing in it (the Summary always stays). Don't invent anything that wasn't said. Use the speakers' names as given. You may point at a moment of the meeting with [mm:ss]. If the transcript is too short or empty, say so in the Summary in one sentence."""
SUMMARY_LINKS = ("If PAST MEETINGS has related meetings (the same project or topic), add a last section \"## Link to earlier "
                 "meetings\" with 1-3 points: what is new, changed or still open compared with them (with the date). If none "
                 "are related, leave that section out.")
CHUNK_SYSTEM = """You get one part of an automatic transcript of a meeting held in {language} (with recognition errors: go by the meaning). Write dense notes in {language} for this part: what is discussed, specifics (numbers, names, deadlines), decisions, tasks (who does what), open questions. Every point with its moment [mm:ss]. Short points only, no intro or conclusion, at most {points} points."""


def meeting_language(cfg):
    """(Deepgram language code, its name for the summary prompts) from room.json's meetings_language."""
    code = str(cfg.get("meetings_language") or "en").strip() or "en"
    return code, LANGUAGES.get(code.split("-")[0].lower(), "the language the meeting was held in")


def fmt_mmss(sec):
    s = max(0, int(meeting_num(sec)))
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60:02}:{s % 60:02}"


def cap_text(text, n):   # like clip(), but keeps the lines
    text = str(text)
    return text if len(text) <= n else text[:n].rstrip() + "…"


def meeting_num(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def parse_mmss(text):
    try:
        parts = [float(p) for p in str(text).strip().split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts[:3]:
        total = total * 60 + p
    return total


def meeting_dt(m):
    try:
        return datetime.datetime.fromisoformat(str(m.get("startedAt") or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def meeting_day(m, year=False):   # "13 Oct" / "13 Oct 2026"
    d = meeting_dt(m)
    return (f"{d.day} {MONTHS[d.month - 1]}" + (f" {d.year}" if year else "")) if d else "?"


def meeting_date_text(m):   # "2026-10-13 12:12:15", local time
    d = meeting_dt(m)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def speaker_label(m, key):
    names = m.get("speakerNames") if isinstance(m.get("speakerNames"), dict) else {}
    if names.get(key):
        return str(names[key])
    if key == "me":
        return "Me"
    try:
        return f"Speaker {int(str(key)[1:]) + 1}"
    except ValueError:
        return "Speaker ?"


def meeting_segments(m):
    return [s for s in m.get("segments") or [] if isinstance(s, dict)]


def transcript_text(m, segs=None):
    return "\n".join(f"[{fmt_mmss(s.get('start'))}] {speaker_label(m, s.get('speaker'))}: {s.get('text', '')}"
                     for s in (meeting_segments(m) if segs is None else segs))


def summary_section(summary, heading):
    found = re.search(r"^##\s+" + heading + r"\s*\n([\s\S]*?)(?=^##\s|^---|\Z)", summary or "", re.M)
    return found.group(1).strip() if found else ""


def meeting_md(m):
    """A readable .md copy of the meeting, saved next to its .json."""
    parts = [(m.get("summary") or "").strip() or f"# {m.get('title', '')}", "",
             f"_{meeting_date_text(m)} · {fmt_mmss(m.get('durationSec') or 0)}_", ""]
    sugg = [s for s in m.get("suggestions") or [] if isinstance(s, dict) and s.get("text")]
    if sugg:
        parts += ["---", "", "## The assistant's notes and suggestions", ""]
        for s in sugg:
            parts += [f"**[{fmt_mmss(s.get('at'))}] {KIND_MD.get(s.get('kind'), 'On request')}**", "", s["text"], ""]
    parts += ["---", "", "## Transcript", ""]
    parts += [f"**[{fmt_mmss(s.get('start'))}] {speaker_label(m, s.get('speaker'))}:** {s.get('text', '')}  " for s in meeting_segments(m)]
    return "\n".join(parts)


def meet_terms(query):
    """Search stems: content words of 3+ letters, cut to 4-7 letters (so word endings don't matter)."""
    out = []
    for word in re.findall(r"[^\W_]+", str(query or "").lower()):
        if len(word) < 3 or word in MEET_STOP:
            continue
        stem = word if len(word) <= 4 else word[:min(7, max(4, len(word) - 2))]
        if stem not in out:
            out.append(stem)
    return out[:12]


def groq_chat(key, system, user, max_tokens=3000):
    """One Groq chat completion (OpenAI-compatible). Waits out the free tier's tokens-a-minute limit."""
    body = json.dumps({"model": GROQ_MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       "max_completion_tokens": max_tokens, "temperature": 0.3, "reasoning_effort": "low"}).encode("utf-8")
    need = (len(system) + len(user)) / GROQ_CPT + max_tokens
    if need > GROQ_TPM + 150:
        raise RuntimeError(f"request too big for Groq's 8000 tokens a minute ({int(need)})")
    for attempt in range(6):
        with GROQ_GATE["lock"]:
            wait = GROQ_GATE["until"] - time.time()
        if wait > 0:
            time.sleep(min(wait, 65))
        req = urllib.request.Request(GROQ_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "claude-room"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
                left, reset = r.headers.get("x-ratelimit-remaining-tokens"), r.headers.get("x-ratelimit-reset-tokens") or ""
            if left is not None and left.isdigit() and int(left) < 7000:   # the next big call would wait anyway
                secs = sum(float(n) * (60 if u == "m" else 1) for n, u in re.findall(r"([\d.]+)(m|s)", reset)) or 20
                with GROQ_GATE["lock"]:
                    GROQ_GATE["until"] = max(GROQ_GATE["until"], time.time() + min(secs, 62) + 0.5)
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
            raise RuntimeError("Groq returned no text")
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace").replace(key, "…")
            if e.code in (429, 500, 502, 503) and attempt < 5:
                retry = e.headers.get("retry-after") or ""
                time.sleep(min(float(retry) if retry.replace(".", "", 1).isdigit() else 15 * (attempt + 1), 65))
                continue
            raise RuntimeError(f"Groq {e.code}: {detail}")
        except (OSError, ValueError, KeyError, IndexError) as e:
            if attempt < 2:
                time.sleep(5)
                continue
            raise RuntimeError(f"Groq: {e}")
    raise RuntimeError("Groq kept refusing (rate limit)")


# ---- a tiny WebSocket relay: the browser streams PCM to the room, the room to Deepgram (the key stays here)
def ws_mask(data, key):
    if not data:
        return data
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes((key * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")


def ws_frame(op, data, mask=False):
    n, bit = len(data), 0x80 if mask else 0
    head = bytes([0x80 | op]) + (bytes([bit | n]) if n < 126 else bytes([bit | 126]) + struct.pack(">H", n)
                                 if n < 65536 else bytes([bit | 127]) + struct.pack(">Q", n))
    if mask:
        key = os.urandom(4)
        return head + key + ws_mask(data, key)
    return head + data


def ws_read(f, limit=8 << 20):
    """One whole message: (opcode, payload), fragments joined; None when the connection ends."""
    op0, buf = None, b""
    while True:
        head = f.read(2)
        if len(head) < 2:
            return None
        fin, op, n = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", f.read(8))[0]
        if n + len(buf) > limit:
            return None
        key = f.read(4) if head[1] & 0x80 else b""
        data = f.read(n) if n else b""
        if len(data) < n:
            return None
        if key:
            data = ws_mask(data, key)
        if op >= 8:   # control frames may come between fragments
            return op, data
        if op:
            op0 = op
        buf += data
        if fin:
            return op0 or 2, buf


class DeepgramError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def deepgram_open(key, query, timeout=12):
    """WebSocket to Deepgram's live endpoint, authenticated with the room's key. Returns (socket, reader)."""
    raw = socket.create_connection((DG_HOST, 443), timeout=timeout)
    sock = None
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=DG_HOST)
        nonce = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /v1/listen?{query} HTTP/1.1\r\nHost: {DG_HOST}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Token {key}\r\n"
                      "User-Agent: claude-room\r\n\r\n").encode("utf-8"))
        f = sock.makefile("rb")
        status = f.readline().decode("latin-1").split()
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        headers = {}
        while True:
            line = f.readline().decode("latin-1")
            if line in ("\r\n", "\n", ""):
                break
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        if code != 101:
            body = b""
            try:
                sock.settimeout(3)
                body = f.read1(4000)
            except OSError:
                pass
            raise DeepgramError(code, (headers.get("dg-error") or body.decode("utf-8", "replace").strip() or f"HTTP {code}")[:300])
        sock.settimeout(None)
        return sock, f
    except BaseException:
        (sock or raw).close()
        raise


def meeting_relay(handler, room, name):
    """GET /meeting_ws: a WebSocket from the page, passed through to Deepgram both ways. The page speaks Deepgram's
    protocol; the room adds the key, and says {"type": "RelayReady"} (or RelayError + close 4000+status) first."""
    nonce = handler.headers.get("Sec-WebSocket-Key", "")
    if handler.headers.get("Upgrade", "").lower() != "websocket" or not nonce:
        handler.send_error(400, "WebSocket only")
        return
    with room.lock:
        busy = room.meet_relays.get(name, 0) >= 2
        if not busy:
            room.meet_relays[name] = room.meet_relays.get(name, 0) + 1
    if busy:
        handler.send_error(429, "already recording twice")
        return
    upstream = None
    try:
        query = parse_qs(urlparse(handler.path).query)
        params = {k: query[k][0] for k in DG_PARAMS if k in query and re.fullmatch(r"[\w.-]{1,24}", query[k][0])}
        key, err = room.meeting_key("deepgram"), None
        if not key:
            err = (0, "the room has no Deepgram key")
        else:
            try:
                upstream = deepgram_open(key, urlencode(params))
            except DeepgramError as e:
                err = (e.status, f"Deepgram {e.status}: {e}")
            except OSError as e:
                err = (0, f"can't reach Deepgram: {e}")
        accept = base64.b64encode(hashlib.sha1((nonce + WS_GUID).encode()).digest()).decode()
        handler.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                             f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        handler.close_connection = True
        down_lock, up_lock = threading.Lock(), threading.Lock()

        def to_page(op, data):
            with down_lock:
                handler.wfile.write(ws_frame(op, data))

        if err:
            to_page(1, json.dumps({"type": "RelayError", "status": err[0], "message": err[1]}).encode("utf-8"))
            to_page(8, struct.pack(">H", 4000 + err[0] if 0 < err[0] < 1000 else 4000) + err[1][:100].encode("utf-8", "ignore")[:100])
            return
        sock, reader = upstream
        started = time.time()

        def to_deepgram(op, data):
            with up_lock:
                sock.sendall(ws_frame(op, data, mask=True))

        def downstream():   # Deepgram -> page
            try:
                while True:
                    msg = ws_read(reader)
                    if msg is None:
                        break
                    op, data = msg
                    if op == 9:
                        to_deepgram(10, data)
                    elif op == 8:
                        to_page(8, data[:125])
                        break
                    elif op != 10:
                        to_page(op, data)
            except OSError:
                pass
            finally:
                try:
                    handler.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        pump = threading.Thread(target=downstream, daemon=True)
        to_page(1, b'{"type":"RelayReady"}')
        pump.start()
        try:   # page -> Deepgram
            while True:
                msg = ws_read(handler.rfile)
                if msg is None:
                    break
                op, data = msg
                if op == 9:
                    to_page(10, data)
                elif op == 8:
                    to_deepgram(8, data[:125])
                    break
                elif op != 10:
                    to_deepgram(op, data)
        except OSError:
            pass
        pump.join(5)
        with open(CONNECT_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} meeting relay {name}: {int(time.time() - started)}s\n")
    except OSError:
        pass
    finally:
        if upstream:
            try:
                upstream[0].close()
            except OSError:
                pass
        with room.lock:
            room.meet_relays[name] = max(0, room.meet_relays.get(name, 1) - 1)


def ebml_with_duration(head, ms):
    """head = a webm's first bytes. MediaRecorder writes no Duration, so players can't show a timeline or seek well:
    returns ("fixed", head with a Duration in Segment > Info), ("has", head) or ("bad", head) (like fix-webm-duration)."""
    def vint(pos, marker=False):
        first = head[pos]
        n = 1
        while n <= 8 and not first & (0x80 >> (n - 1)):
            n += 1
        if n > 8:
            raise ValueError("bad EBML")
        value = first if marker else first & (0xFF >> n)
        for b in head[pos + 1:pos + n]:
            value = (value << 8) | b
        return value, n

    try:
        ident, n = vint(0, True)
        size, sn = vint(n)
        if ident != 0x1A45DFA3:
            return "bad", head
        pos = n + sn + size
        ident, n = vint(pos, True)
        if ident != 0x18538067:
            return "bad", head
        seg_size_at, (seg_size, seg_sn) = pos + n, vint(pos + n)
        pos = seg_size_at + seg_sn
        while pos < len(head) - 12:
            ident, n = vint(pos, True)
            size, sn = vint(pos + n)
            if ident == 0x1549A966:   # Info
                data_at, end = pos + n + sn, pos + n + sn + size
                if end > len(head):
                    return "bad", head
                scale, p = 1000000, data_at
                while p < end:
                    cid, cn = vint(p, True)
                    csize, csn = vint(p + cn)
                    if cid == 0x2AD7B1:
                        scale = int.from_bytes(head[p + cn + csn:p + cn + csn + csize], "big") or 1000000
                    if cid == 0x4489:
                        return "has", head
                    p += cn + csn + csize
                if size + 11 >= (1 << (7 * sn)) - 1 or not ms or ms <= 0:
                    return "bad", head
                size_bytes = ((size + 11) | (1 << (7 * sn))).to_bytes(sn, "big")
                patched = bytearray(head[:pos + n]) + size_bytes + head[data_at:end] + b"\x44\x89\x88" + struct.pack(">d", float(ms) * 1e6 / scale) + head[end:]
                if seg_size != (1 << (7 * seg_sn)) - 1:   # a known Segment size grows too
                    patched[seg_size_at:seg_size_at + seg_sn] = ((seg_size + 11) | (1 << (7 * seg_sn))).to_bytes(seg_sn, "big")
                return "fixed", bytes(patched)
            if size == (1 << (7 * sn)) - 1:   # unknown size before Info
                return "bad", head
            pos += n + sn + size
    except (ValueError, IndexError, struct.error):
        pass
    return "bad", head


def ebml_fix_duration(path, ms):
    with open(path, "rb") as f:
        head = f.read(1 << 16)
        state, patched = ebml_with_duration(head, ms)
        if state != "fixed":
            return state == "has"
        rest = f.read()
    with open(path + ".fix", "wb") as f:
        f.write(patched)
        f.write(rest)
    os.replace(path + ".fix", path)
    return True


def make_handler(room):
    files = {"/": ("web.html", "text/html; charset=utf-8"),
             "/chat.py": ("chat.py", "text/x-python; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def who(self):
            token = parse_qs(urlparse(self.path).query).get("t", [""])[0]
            for name, tok in room.cfg["people"].items():
                if secrets.compare_digest(tok, token):
                    return name
            self.send_error(403, "bad token")
            return None

        def do_GET(self):
            path = urlparse(self.path).path
            if path in files:
                filename, ctype = files[path]
                if filename == "chat.py" and good_client()[1]:
                    body = good_client()[1]
                else:
                    with open(os.path.join(ROOT, filename), "rb") as f:
                        body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/operator":
                name = self.who()
                if name:
                    self.send_json(room.operator_view(name))
            elif path == "/files":
                if self.who():
                    self.send_json(room.files_view())
            elif path.startswith("/file/"):
                if self.who():
                    meta, fpath = room.file_path(path[len("/file/"):])
                    if not meta or not os.path.exists(fpath):
                        self.send_error(404)
                        return
                    # ?inline=1 shows pictures in the page; only plain image types (an SVG or HTML
                    # file could carry scripts), everything else is always a download
                    ctype = mimetypes.guess_type(meta["name"])[0] or ""
                    inline = parse_qs(urlparse(self.path).query).get("inline") == ["1"] and ctype in (
                        "image/png", "image/jpeg", "image/gif", "image/webp")
                    self.send_response(200)
                    self.send_header("Content-Type", ctype if inline else "application/octet-stream")
                    self.send_header("Content-Length", str(os.path.getsize(fpath)))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Disposition",
                                     f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(meta['name'])}")
                    self.end_headers()
                    with open(fpath, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 1 << 20)
            elif path in ("/meetings", "/meetings_search") or path.startswith(("/meeting/", "/meeting_audio/")):
                if self.who():
                    self.meetings_get(path)
            elif path == "/meeting_ws":   # live transcription: the page's audio to Deepgram through the room
                name = self.who()
                if name:
                    meeting_relay(self, room, name)
            elif path == "/search":   # Ctrl+K: the whole chat history
                if self.who():
                    self.send_json(room.search(parse_qs(urlparse(self.path).query).get("q", [""])[0]))
            elif path == "/gifs":   # GIF search for the people's chat
                if self.who():
                    self.send_json(room.gif_search(parse_qs(urlparse(self.path).query).get("q", [""])[0]))
            elif path == "/stats":   # numbers for the credits
                if self.who():
                    self.send_json(room.stats())
            elif path == "/pins":
                if self.who():
                    self.send_json(room.pins_view())
            elif path.startswith("/tool/"):   # the full input and output behind one tool line
                if self.who():
                    tool = room.tool_view(path[len("/tool/"):])
                    if tool:
                        self.send_json(tool)
                    else:
                        self.send_error(404)
            elif path.startswith("/preview/"):   # a small RGB grid of a shared picture, for terminals
                if self.who():
                    meta, fpath = room.file_path(path[len("/preview/"):])
                    ok = meta and os.path.splitext(meta["name"])[1].lower() in IMAGE_EXT
                    self.send_json(image_preview(fpath, 56, 40) if ok else None)
            elif path in ("/board", "/wb", "/timeline"):
                if self.who():
                    data = (room.board_view() if path == "/board" else room.timeline_view() if path == "/timeline"
                            else {"strokes": list(room.wb["strokes"])})
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "/join.bat":
                name = self.who()
                if name:
                    base = (room.cfg.get("public_url") or f"http://{self.headers.get('Host', '')}").rstrip("/")
                    bat = JOIN_BAT.format(name=name, base=base, token=room.cfg["people"][name])
                    body = bat.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", f'attachment; filename="claude-room-{name}.bat"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif path == "/events":
                name = self.who()
                if name:
                    query = parse_qs(urlparse(self.path).query)
                    agent = None
                    if query.get("agent") == ["1"]:
                        agent = {k: query.get(k, ["?"])[0][:200] for k in ("os", "user", "home")}
                    version = int(query.get("v", ["0"])[0] or 0) if query.get("v", ["0"])[0].isdigit() else 0
                    is_script = version or "Python-urllib" in self.headers.get("User-Agent", "")
                    update_url = (room.cfg.get("public_url") or f"http://{self.headers.get('Host', '')}").rstrip("/")
                    self.stream(name, agent, "chat.py" if is_script else "browser", version, update_url)
            else:
                self.send_error(404)

        def meetings_get(self, path):
            query = parse_qs(urlparse(self.path).query)
            if path == "/meetings":
                self.send_json({"meetings": room.meetings_list(), "folder": bool(room.meetings_dir()),
                                "language": meeting_language(room.cfg)[0],
                                "record": bool(room.meeting_key("deepgram")), "summary": bool(room.meeting_key("groq"))})
                return
            if path == "/meetings_search":
                hits, terms = room.meetings_hits(query.get("q", [""])[0][:300], limit=40, per_meeting=12)
                self.send_json({"hits": hits, "terms": terms})
                return
            mid = path.split("/", 2)[2]
            if path.startswith("/meeting/"):
                view = room.meeting_view(mid) if MEETING_ID.fullmatch(mid) else None
                if view:
                    self.send_json(view)
                else:
                    self.send_error(404)
                return
            entry = room.meeting_entry(mid) if MEETING_ID.fullmatch(mid) else None
            fpath = room.meeting_audio_path(entry) if entry else ""
            if not fpath:
                self.send_error(404)
                return
            with open(fpath, "rb") as f:
                head = f.read(1 << 16)
            state, served = ebml_with_duration(head, meeting_num(entry["m"].get("durationSec")) * 1000)
            shift = len(served) - len(head) if state == "fixed" else 0   # the header got a Duration: later bytes move
            total = os.path.getsize(fpath) + shift
            start, end = 0, total - 1
            ranged = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if ranged and (ranged.group(1) or ranged.group(2)):
                if ranged.group(1):
                    start, end = int(ranged.group(1)), min(int(ranged.group(2) or total - 1), total - 1)
                else:
                    start = max(0, total - int(ranged.group(2)))
                if start >= total or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            partial = bool(ranged and (ranged.group(1) or ranged.group(2)))
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "audio/webm")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                left = end - start + 1
                if shift and start < len(served):
                    chunk = served[start:min(len(served), end + 1)]
                    self.wfile.write(chunk)
                    start, left = start + len(chunk), left - len(chunk)
                with open(fpath, "rb") as f:
                    f.seek(start - shift)
                    while left > 0:
                        chunk = f.read(min(1 << 18, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except OSError:
                pass   # the player hung up to ask for another range

        def stream(self, name, agent, kind, version, update_url):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = room.connect(name, agent, kind, version, update_url)
            from room_idle_shutdown import register_client
            register_client(room, q, parse_qs(urlparse(self.path).query).get("power_sid", [""])[0])
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        data = "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
                    except queue.Empty:
                        data = ": ping\n\n"
                    self.wfile.write(data.encode("utf-8"))
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                room.disconnect(name, q)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/upload":   # raw file body, streamed to disk (bigger than the JSON limit below)
                name = self.who()
                if not name:
                    return
                query = parse_qs(urlparse(self.path).query)
                filename = query.get("name", ["file"])[0]
                thread = query.get("thread", [""])[0]
                ok, msg = room.add_file(name, filename, self.rfile, int(self.headers.get("Content-Length") or 0),
                                        announce=query.get("quiet") != ["1"] and query.get("stage") != ["1"],
                                        attach=query.get("stage") != ["1"],
                                        lane=room.lane_of(name, thread) if thread else room.upload_lane(name))
                self.send_json({"ok": ok, "message": msg}, 200 if ok else 400)
                return
            if path == "/dev_reply":   # the room's developer answering a request (dev.py)
                if not room.cfg.get("dev_token") or not secrets.compare_digest(
                        self.headers.get("X-Room-Dev", ""), room.cfg["dev_token"]):
                    self.send_error(403, "bad dev token")
                    return
                body = self.body()
                ok = room.dev_reply(str(body.get("id", "")), str(body.get("status", "")), str(body.get("message", "")))
                self.send_json({"ok": ok}, 200 if ok else 404)
                return
            if path.startswith("/meeting_audio/"):   # a recorded meeting's audio, streamed to disk
                name = self.who()
                if not name:
                    return
                query = parse_qs(urlparse(self.path).query)
                ms = query.get("ms", ["0"])[0]
                ok, msg = room.meeting_audio_upload(name, path[len("/meeting_audio/"):], self.rfile,
                                                    int(self.headers.get("Content-Length") or 0), int(ms) if ms.isdigit() else 0)
                self.send_json({"ok": ok, "message": msg}, 200 if ok else 400)
                return
            if int(self.headers.get("Content-Length") or 0) > 12_000_000:
                self.send_error(413, "too big")
                return
            if path == "/room_tool":
                # board / whiteboard tools from room_mcp.py, authenticated like /exec
                lane = room.lane_for_token(self.headers.get("X-Room-Exec", ""))
                if not lane:
                    self.send_error(403, "bad exec token")
                    return
                body = self.body()
                ok, output, image = room.room_tool(lane, str(body.get("tool", "")), body.get("args") or {})
                output = room.with_midturn(lane, output)
                data = json.dumps({"ok": ok, "output": output, "image": image}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/exec":
                # from room_mcp.py, authenticated by the per-turn secret, which also says whose turn it is
                lane = room.lane_for_token(self.headers.get("X-Room-Exec", ""))
                if not lane:
                    self.send_error(403, "bad exec token")
                    return
                body = self.body()
                ok, output = room.exec([lane], str(body.get("user", "")).lower(), str(body.get("tool", "")),
                                       body.get("args") or {})
                output = room.with_midturn(lane, output)
                data = json.dumps({"ok": ok, "output": output}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            name = self.who()
            if not name:
                return
            body = self.body()
            if path == "/operator":
                ok, result = room.set_operator(name, body.get("mode"))
                self.send_json({"ok": True, "operator": result} if ok else {"ok": False, "error": result},
                               200 if ok else (403 if not room.operator_view(name).get("available") else 400))
                return
            if path in ("/meeting_save", "/meeting_token", "/meeting_summarize"):
                if path == "/meeting_token":
                    self.send_json(room.meeting_token())
                elif path == "/meeting_save":
                    ok, msg = room.meeting_save(name, body)
                    self.send_json({"ok": ok, "id": msg} if ok else {"ok": False, "error": msg}, 200 if ok else 400)
                else:   # try the summary again (meetings recorded in the room only)
                    entry = room.meeting_entry(str(body.get("id", "")))
                    ok = bool(entry and entry["m"].get("recordedIn") == "room")
                    if ok:
                        threading.Thread(target=room.meeting_summarize, args=(os.path.basename(entry["path"])[:-5], name, False), daemon=True).start()
                    self.send_json({"ok": ok}, 200 if ok else 400)
                return
            if path == "/say":
                room.say(name, str(body.get("text", ""))[:20000], body.get("thread"), body.get("files"))
            elif path == "/thread":   # create / rename / close one of your threads
                ok, msg = room.thread_op(name, body)
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/typing":
                room.keystroke(name)
            elif path == "/diag":
                with open(CONNECT_LOG, "a", encoding="utf-8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} diag {name}: {str(body.get('text', ''))[:500]}\n")
            elif path == "/board":
                room.board_op(name, body)
            elif path == "/host_account":   # the Usage panel: which of the host's plans Claude runs on, and the team stop line
                ok, msg = room.set_host_account(name, body.get("account"), body.get("stop"))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/settings":   # the model / effort picker
                room.set_pref(name, model=body.get("model"), effort=body.get("effort"), mute_approval=body.get("mute_approval_sound"))
            elif path == "/locks":
                room.report_locks(name, body)
            elif path == "/lock_request":
                room.lock_request(name, clean_text(body.get("file"), 300))
            elif path == "/lock_blocked":
                room.lock_blocked(name, clean_text(body.get("file"), 300), clean_text(body.get("owner"), 120))
            elif path == "/release_my_locks":   # from the browser: your terminal releases what it can
                room.send_to(name, {"t": "release_locks"}, kind="chat.py")
            elif path == "/update_my_terminal":   # the browser's Update button updates your chat.py too
                room.send_to(name, {"t": "remote_update"}, kind="chat.py")
            elif path == "/pin":
                ok, msg = room.pin_op(name, body)
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/timeline":
                ok, msg = room.timeline_op(name, body)
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/delete":   # a chat message or answer (the host: any, others: their own)
                ok, msg = room.delete_msg(name, str(body.get("id", "")))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/thread_pin":   # 📌 pin onto / off one of your threads
                lane = room.lane_of(name, body.get("thread"))
                ok, msg = (room.thread_unpin(name, lane, str(body.get("id", ""))) if body.get("op") == "remove"
                           else room.thread_pin(name, lane, body))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/pchat_clear":   # 🧹 in the people chat (host only)
                ok, msg = room.pchat_clear(name)
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/pmsg":
                ok, msg = room.pmsg(name, body.get("text"), body.get("file") or "", str(body.get("ref") or ""), body.get("url") or "")
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/sync":   # chat.py: where this person's copy of the project is
                room.sync_report(name, body)
            elif path in ("/rules", "/account"):
                if path == "/rules":
                    ok, msg = room.set_rules(name, "room" if body.get("scope") == "room" else "me", body.get("text"))
                else:
                    ok, msg = room.set_account(name, str(body.get("mode", "")), body.get("secret"))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
                    room.send_to(name, {"t": "account_error", "msg": msg})
            elif path == "/undo":
                ok, msg = room.undo(name, str(body.get("tid", "")))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
                    room.send_to(name, {"t": "undo_failed", "tid": str(body.get("tid", ""))})
            elif path == "/activity":
                room.activity_report(name, body)
            elif path == "/zone":   # the page: remove a zone (or add one by hand)
                ok, msg = room.zone_op(name, "", body)
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/pc_event":   # chat.py: the playtest log shows errors
                room.pc_event(name, body)
            elif path == "/lease":   # chat.py: who holds the UEFN playtest on this PC
                room.lease_report(name, body.get("lease"))
            elif path == "/lease_release":   # the browser's Done testing: your chat.py frees it
                if not room.send_to(name, {"t": "lease_release"}, kind="chat.py"):
                    room.lease_report(name, None)
            elif path == "/star":
                ok, msg = room.star(name, str(body.get("id", "")))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path in ("/react", "/poll", "/vote"):
                if path == "/react":
                    ok, msg = room.react(name, str(body.get("id", "")), str(body.get("e", "")))
                elif path == "/poll":
                    ok, msg = room.poll_create(name, body.get("q"), body.get("opts") or [])
                else:
                    ok, msg = room.poll_vote(name, str(body.get("id", "")), body.get("opt"), bool(body.get("close")))
                if not ok:
                    room.send_to(name, {"t": "sys", "text": msg})
            elif path == "/egg":
                room.egg(name, str(body.get("what", "")))
            elif path == "/files_delete":
                room.delete_file(name, str(body.get("id", "")))
            elif path == "/wb":
                room.wb_op(name, body)
            elif path == "/wb_snapshot":
                room.wb_snapshot(str(body.get("png", "")))
            elif path == "/approve":
                room.remote_answer(name, str(body.get("id", "")), str(body.get("answer", "")))
            elif path == "/exec_started":
                room.exec_started(name, str(body.get("id", "")))
            elif path == "/exec_result":
                room.exec_result(name, str(body.get("id", "")), body.get("ok"), body.get("output", ""))
            self.send_response(204)
            self.end_headers()

        def send_json(self, data, code=200):
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

    return Handler


def main():
    from room_access_lock import enforce_startup
    enforce_startup()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    os.makedirs(WORKSPACE, exist_ok=True)
    cfg = load_config()
    os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)   # Room.apply_host_account sets it from the Usage panel's choice
    global HOST_CLAUDE_CONFIG
    HOST_CLAUDE_CONFIG = cfg.get("host_claude_config") or ""
    argv = sys.argv[1:]
    if argv[:1] == ["invite"] and len(argv) > 1:
        name = argv[1].lower()
        if name not in cfg["people"]:
            cfg["people"][name] = secrets.token_urlsafe(12)
            save_json(CONFIG, cfg)
        print(join_info(cfg, name))
        return

    claude_md = os.path.join(WORKSPACE, "CLAUDE.md")
    current = open(claude_md, encoding="utf-8").read() if os.path.exists(claude_md) else ""
    upgraded = upgrade_claude_md(current, cfg)
    if upgraded is not None:
        with open(claude_md, "w", encoding="utf-8") as f:
            f.write(upgraded)
    for fn, start in (("lessons.md", "# Lessons the room's threads learned (newest last)\n"), ("house-rules.md", "(none yet)\n")):
        if not os.path.exists(os.path.join(WORKSPACE, fn)):   # CLAUDE.md imports both
            with open(os.path.join(WORKSPACE, fn), "w", encoding="utf-8") as f:
                f.write(start)

    room = Room(cfg)
    room.apply_host_account()
    from room_idle_shutdown import attach
    attach(room, ROOT)
    threading.Thread(target=room.worker, daemon=True).start()
    threading.Thread(target=room.archiver, daemon=True).start()
    threading.Thread(target=room.refresh_goals, daemon=True).start()
    threading.Thread(target=room.watch_files, daemon=True).start()
    threading.Thread(target=room.watch_usage, daemon=True).start()
    threading.Thread(target=room.measure_contexts, daemon=True).start()
    threading.Thread(target=room.watch_summaries, daemon=True).start()
    threading.Thread(target=room.watch_reminders, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", cfg["port"]), make_handler(room))
    server.daemon_threads = True
    print(f"Claude Room on port {cfg['port']} - one Claude thread per person, side by side; "
          "Claude acts only on the PC of whoever asked (via their chat.py)")
    for name in cfg["people"]:
        if name != cfg["host"]:
            print(join_info(cfg, name))
    print(f"\nYou: python chat.py {cfg['host']}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

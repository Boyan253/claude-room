"""MCP server (stdio) that gives the room's Claude hands on each person's own PC,
plus the room's shared task board and whiteboard.

Claude Code launches this per turn. Every PC tool call names whose PC it runs
on; the room server forwards it to that person's chat.py, which asks them to
approve it and runs it locally. Board and whiteboard tools act on the room.
"""
import json
import os
import sys
import threading
import urllib.request

ROOM_URL = os.environ["ROOM_URL"]
EXEC_TOKEN = os.environ["ROOM_EXEC_TOKEN"]
USER = {"type": "string", "description": "Whose PC to act on: the name of the person who asked for this."}
COLUMN = {"type": "string", "description": "Column id or name on that board (default: To do). Backlog = agreed but not planned yet."}
BOARD = {"type": "string", "description": "Board name or id (default: the first board). board_read lists them."}
PRIORITY = {"type": "string", "enum": ["", "highest", "high", "medium", "low", "lowest"]}
ASSIGNEE = {"type": "string", "description": "Who does it: a person's name, \"claude\", or \"\" for nobody."}
LABEL = {"type": "string", "enum": ["", "red", "yellow", "green", "blue", "magenta", "cyan"],
         "description": "Color label."}

TOOLS = [
    {
        "name": "claude_session_read",
        "description": "Read or search the exact saved Claude session for the current room thread, including "
                       "Claude's compaction summaries, visible messages and tool results. Historical context only. "
                       "Use when continuing Claude's work or needing details missing from the handover.",
        "inputSchema": {"type": "object", "properties": {
            "start": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
            "query": {"type": "string", "description": "Optional search text; only matching entries are returned."}}},
    },
    {
        "name": "conversation_read",
        "description": "Read earlier visible messages and Claude/Codex answers in the current thread. "
                       "Use this to recover decisions, requirements, or work from before you joined. "
                       "Does not read the private people-only chat. Page older messages using next_before.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "before": {"type": "number", "description": "Timestamp from next_before to page older messages."}}},
    },
    {
        "name": "run_command",
        "description": "Run a shell command on one person's own PC (PowerShell on Windows, bash elsewhere) "
                       "and return its output. They approve it on their side first.",
        "inputSchema": {"type": "object", "required": ["user", "command"], "properties": {
            "user": USER,
            "command": {"type": "string"},
            "cwd": {"type": "string", "description": "Working folder; defaults to their home folder."},
        }},
    },
    {
        "name": "read_file",
        "description": "Read a text file on one person's own PC.",
        "inputSchema": {"type": "object", "required": ["user", "path"], "properties": {
            "user": USER, "path": {"type": "string"}}},
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file on one person's own PC.",
        "inputSchema": {"type": "object", "required": ["user", "path", "content"], "properties": {
            "user": USER, "path": {"type": "string"}, "content": {"type": "string"}}},
    },
    {
        "name": "screenshot",
        "description": "Take a picture of one person's screen (on their PC, with their approval) and share it in the "
                       "room; it returns the file id: look at it with file_view. Use it to check something visible "
                       "you changed in UEFN, or when they ask you to look at their screen.",
        "inputSchema": {"type": "object", "required": ["user"], "properties": {"user": USER}},
    },
    {
        "name": "list_dir",
        "description": "List a folder on one person's own PC.",
        "inputSchema": {"type": "object", "required": ["user", "path"], "properties": {
            "user": USER, "path": {"type": "string"}}},
    },
    {
        "name": "uefn_list_tools",
        "description": "List what Epic's UEFN MCP offers in the UEFN editor open on one person's PC (through their "
                       "chat.py, with their approval). Pass `name` to get one tool's full input schema.",
        "inputSchema": {"type": "object", "required": ["user"], "properties": {
            "user": USER, "name": {"type": "string", "description": "A tool name, for its full schema."}}},
    },
    {
        "name": "uefn_call",
        "description": "Call one tool of Epic's UEFN MCP in the UEFN editor open on one person's PC (through their "
                       "chat.py, with their approval). Same tools as Claude Code's unreal-mcp server.",
        "inputSchema": {"type": "object", "required": ["user", "tool"], "properties": {
            "user": USER, "tool": {"type": "string", "description": "Tool name from uefn_list_tools."},
            "arguments": {"type": "object", "description": "The tool's arguments."}}},
    },
    {
        "name": "files_list",
        "description": "List the files people shared in the room (id, name, size, who shared it).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "file_get",
        "description": "Save a file shared in the room onto one person's PC (their approval). "
                       "dest = folder or full path; default is their Downloads folder.",
        "inputSchema": {"type": "object", "required": ["user", "file_id"], "properties": {
            "user": USER, "file_id": {"type": "string"}, "dest": {"type": "string"}}},
    },
    {
        "name": "file_put",
        "description": "Share a file from one person's PC with the room (their approval), so others can get it.",
        "inputSchema": {"type": "object", "required": ["user", "path"], "properties": {
            "user": USER, "path": {"type": "string"}}},
    },
    {
        "name": "image_generate",
        "description": "Make a picture from a text prompt and post it in the room's chat (and Files). "
                       "Write the prompt in English: subject, setting, style, light, camera.",
        "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string"},
            "aspect": {"type": "string", "enum": ["square", "wide", "tall"]},
            "edit_file_id": {"type": "string", "description": "Change this shared picture instead of starting fresh "
                                                              "(needs the room's Gemini key)."}}},
    },
    {
        "name": "file_view",
        "description": "Look at a file shared in the room: pictures come back as an image, text files as text.",
        "inputSchema": {"type": "object", "required": ["file_id"], "properties": {"file_id": {"type": "string"}}},
    },
    {
        "name": "pin_add",
        "description": "Pin something onto your current answer in the chat (people see it under the answer, in "
                       "the browser and the terminal): a short note, a board card, or a shared picture/file.",
        "inputSchema": {"type": "object", "required": ["kind"], "properties": {
            "kind": {"type": "string", "enum": ["note", "card", "file"]},
            "text": {"type": "string", "description": "For a note: one or two lines."},
            "card_id": {"type": "string"}, "file_id": {"type": "string"}}},
    },
    {
        "name": "room_feedback",
        "description": "Send a request to the room's developer (whoever maintains this room's server, page and "
                       "chat.py, see dev.py): a bug, a missing feature or a question about the room itself.",
        "inputSchema": {"type": "object", "required": ["title", "details"], "properties": {
            "kind": {"type": "string", "enum": ["bug", "feature", "question"]},
            "title": {"type": "string", "description": "One line."},
            "details": {"type": "string", "description": "What happens or what's wanted, why, who asked, any error text."},
        }},
    },
    {
        "name": "room_feedback_status",
        "description": "See the requests sent to the room's developer and its answers.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "board_read",
        "description": "Show the room's shared task board: every card with its id, column, assignee and note.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "board_add",
        "description": "Add a card to the shared task board (everyone sees it live).",
        "inputSchema": {"type": "object", "required": ["title"], "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "column": COLUMN, "board": BOARD,
            "assignee": ASSIGNEE,
            "note": {"type": "string", "description": "Details, steps, links."},
            "label": LABEL, "priority": PRIORITY,
        }},
    },
    {
        "name": "board_update",
        "description": "Change a card on the task board: move it (column), rename it, reassign it, edit its note "
                       "or label. Only the fields you pass change.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string", "description": "Card id or number (#7) from board_read."},
            "title": {"type": "string"}, "column": COLUMN, "board": BOARD, "assignee": ASSIGNEE,
            "note": {"type": "string"}, "label": LABEL, "priority": PRIORITY,
            "position": {"type": "integer", "description": "Place in its column, 1 = top."},
        }},
    },
    {
        "name": "board_manage",
        "description": "Create, rename or delete a board, or add, rename or delete a column on a board.",
        "inputSchema": {"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": ["create_board", "rename_board", "delete_board", "add_column", "rename_column", "delete_column"]},
            "board": BOARD, "column": COLUMN,
            "name": {"type": "string", "description": "New name (board or column)."}}},
    },
    {
        "name": "board_delete",
        "description": "Delete a card from the task board.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "thread_pin",
        "description": "Pin something to this thread so it stays in view (people see it above the message box, you "
                       "get it every turn): the card you're working on, a picture showing the problem or the result, "
                       "or a note this thread must remember (a decision, a file, a rule the person gave).",
        "inputSchema": {"type": "object", "required": ["kind"], "properties": {
            "kind": {"type": "string", "enum": ["card", "file", "note"]},
            "card_id": {"type": "string"}, "file_id": {"type": "string"},
            "text": {"type": "string", "description": "For a note: one or two lines."}}},
    },
    {
        "name": "thread_unpin",
        "description": "Take a pin off this thread (its [pin …] id from the pinned line) when it no longer matters.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "project_status",
        "description": "The UEFN project's copies (Lore): each person's revision vs the shared one (who must Sync "
                       "Latest) and the recent check-ins with who made them.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "threads_read",
        "description": "What every thread in the room is doing: its goal, a summary of what it did, whether it's "
                       "working right now, and which files the running threads are changing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "thread_note",
        "description": "Tell another thread something it needs: a file you changed that it uses, a decision, a "
                       "question. If it is working, it gets the note at once; otherwise at its next turn.",
        "inputSchema": {"type": "object", "required": ["to", "text"], "properties": {
            "to": {"type": "string", "description": "The thread: sam, sam/ideas, alice/level-3 (threads_read lists them)."},
            "text": {"type": "string", "description": "Short and concrete."}}},
    },
    {
        "name": "reminder_add",
        "description": "Set a reminder when people ask to be reminded of something: it pops up for them (sound, "
                       "notification, a line in the chat) at that time.",
        "inputSchema": {"type": "object", "required": ["when", "text"], "properties": {
            "when": {"type": "string", "description": "'18:00', '30m', '2h', '1h30m', 'tomorrow 10:00' or 'YYYY-MM-DD HH:MM' (local time)."},
            "text": {"type": "string", "description": "What to remind them of, short."},
            "to": {"type": "string", "description": "'all' (default), 'me' (the person whose turn it is) or a person's name."}}},
    },
    {
        "name": "poll_create",
        "description": "Ask everyone in the room to vote (shows as a poll in the chat): use it when the people have to "
                       "choose between options. Votes come back in later turns as [room] polls lines.",
        "inputSchema": {"type": "object", "required": ["question", "options"], "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2 to 6 short options."}}},
    },
    {
        "name": "timeline_read",
        "description": "Show the project's timeline: every moment (milestone, feature, fix, bug, idea, playtest, art) "
                       "with its time, id and what's attached.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "timeline_add",
        "description": "Add a moment to the project's timeline (the Timeline tab): a fix or feature you finished, a "
                       "playtest result, a milestone or level reached, concept art. Attach its pictures (file ids "
                       "from files_list: screenshots, before/after) and its card. One moment per thing worth "
                       "remembering, not every small step.",
        "inputSchema": {"type": "object", "required": ["title", "kind"], "properties": {
            "title": {"type": "string", "description": "Short, e.g. 'Door opens when you walk up to it'."},
            "kind": {"type": "string", "enum": ["milestone", "feature", "fix", "bug", "idea", "decision", "playtest", "art"]},
            "details": {"type": "string", "description": "A line or two: what changed, how to test it."},
            "file_ids": {"type": "array", "items": {"type": "string"},
                         "description": "Shared pictures/files to attach; for a fix, before first then after."},
            "card_id": {"type": "string", "description": "The board card it belongs to."},
            "when": {"type": "string", "description": "Only if it happened earlier: 'YYYY-MM-DD HH:MM' (local time)."}}},
    },
    {
        "name": "timeline_update",
        "description": "Change a moment on the timeline (ids from timeline_read): title, kind (e.g. a bug that is "
                       "now fixed), details, time, or attach more pictures.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string"}, "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["milestone", "feature", "fix", "bug", "idea", "decision", "playtest", "art"]},
            "details": {"type": "string"}, "when": {"type": "string"},
            "add_file_ids": {"type": "array", "items": {"type": "string"}}}},
    },
    {
        "name": "thread_spawn",
        "description": "Open a sub-thread of this thread for one part of the task: it starts at once (same person, your "
                       "card pinned), works side by side with you, and its reports come back to you. Give it a clear scope, "
                       "its place (zone/files) and how to check it. Steer it with thread_note; thread_close it when done.",
        "inputSchema": {"type": "object", "required": ["name", "task"], "properties": {
            "name": {"type": "string", "description": "Short, e.g. 'Shop doors'"},
            "task": {"type": "string", "description": "What exactly, where, what not to touch, how to check it"},
            "card": {"type": "string", "description": "Card id or #number to pin instead of yours (optional)"}}},
    },
    {
        "name": "thread_close",
        "description": "Close one of your sub-threads when its part is done.",
        "inputSchema": {"type": "object", "required": ["thread"], "properties": {"thread": {"type": "string"}}},
    },
    {
        "name": "plan_update",
        "description": "Your checklist for this task (replaces the old one): the people see it in the Overview.",
        "inputSchema": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
            "type": "object", "required": ["text"], "properties": {
                "text": {"type": "string"}, "status": {"type": "string", "enum": ["todo", "doing", "done"]}}}}}},
    },
    {
        "name": "lesson_add",
        "description": "Save a lesson every thread must know from now on (one concrete line about a mistake not to repeat).",
        "inputSchema": {"type": "object", "required": ["text"], "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "zone_add",
        "description": "Mark a place in the UEFN map (box in centimetres) or a Content folder: keep_clear = a spot your work "
                       "depends on (spawn pads, arrival/teleport point, a prop the camera looks at): nobody else may put or move "
                       "anything into it; area = the part of the map you are building; folder = your Content folder. Same name "
                       "again replaces it.",
        "inputSchema": {"type": "object", "required": ["name", "kind"], "properties": {
            "name": {"type": "string"}, "kind": {"type": "string", "enum": ["keep_clear", "area", "folder"]},
            "min": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] lowest corner (z optional)"},
            "max": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] highest corner (z optional)"},
            "folder": {"type": "string", "description": "for kind folder: e.g. Props/Kitchen"},
            "note": {"type": "string"}}},
    },
    {
        "name": "zone_remove",
        "description": "Remove a zone of yours (its id from zones_read) when you no longer depend on it.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "zones_read",
        "description": "All zones in the map: keep-clear spots, working areas and folders, with their owners.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "zone_check",
        "description": "Is this box (or point) in someone's zone? Use before placing or after importing something big.",
        "inputSchema": {"type": "object", "properties": {
            "min": {"type": "array", "items": {"type": "number"}}, "max": {"type": "array", "items": {"type": "number"}},
            "point": {"type": "array", "items": {"type": "number"}}}},
    },
    {
        "name": "meetings_search",
        "description": "Search what was said in the people's recorded meetings (the room's Meetings tab: automatic "
                       "transcripts with speakers and times, plus each meeting's summary). Use it whenever someone "
                       "refers to what was said, decided or asked in a meeting (\"what did we say about…\", the call, "
                       "the stand-up) before answering. Give content words in the meeting's language, e.g. "
                       "\"release date\" or \"menu colors\". Returns the best matching moments: meeting date, "
                       "title and id, @mm:ss, speaker, the line with the lines around it, and matching summary lines. "
                       "An empty query lists all meetings with a one-line summary.",
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer", "description": "max moments (default 10, up to 30)"}}},
    },
    {
        "name": "meeting_read",
        "description": "Read one recorded meeting (id from meetings_search, a [room] meetings line or a meeting:<id> "
                       "reference; \"latest\" = the newest). part \"summary\" (default: title, speakers, decisions, tasks, "
                       "open questions), \"transcript\" (lines \"[mm:ss] Speaker: text\"; narrow it with from / to as mm:ss) "
                       "or \"notes\" (the notetaker's live notes and suggestions). Long parts stop at ~12k characters: "
                       "go on with from. Cite moments as meeting:<id> @mm:ss so people can click them.",
        "inputSchema": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string"}, "part": {"type": "string", "enum": ["summary", "transcript", "notes"]},
            "from": {"type": "string", "description": "mm:ss"}, "to": {"type": "string", "description": "mm:ss"}}},
    },
    {
        "name": "whiteboard_view",
        "description": "Look at the room's shared whiteboard: returns a picture of everything drawn on it, "
                       "plus any text written there.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]
ROOM_TOOLS = {"claude_session_read", "conversation_read", "board_read", "board_add", "board_update", "board_delete", "board_manage", "whiteboard_view", "files_list",
              "room_feedback", "room_feedback_status", "image_generate", "file_view", "pin_add",
              "timeline_read", "timeline_add", "timeline_update", "poll_create", "reminder_add", "threads_read", "thread_note",
              "project_status", "thread_pin", "thread_unpin",
              "meetings_search", "meeting_read", "zone_add", "zone_remove", "zones_read", "zone_check",
              "thread_spawn", "thread_close", "plan_update", "lesson_add"}

write_lock = threading.Lock()


def send(msg):
    with write_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def call_room(tool, args):
    """Returns (ok, text, png_base64_or_None)."""
    path = "/room_tool" if tool in ROOM_TOOLS else "/exec"
    body = json.dumps({"user": args.get("user", ""), "tool": tool, "args": args}).encode("utf-8")
    req = urllib.request.Request(f"{ROOM_URL}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Room-Exec": EXEC_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            res = json.loads(r.read())
        return bool(res.get("ok")), str(res.get("output", "")), res.get("image")
    except Exception as e:  # noqa: BLE001 - report any failure back to Claude
        return False, f"room server error: {e}", None


def handle_call(msg_id, params):
    ok, output, image = call_room(params.get("name", ""), params.get("arguments") or {})
    content = [{"type": "text", "text": output}]
    if image:
        content.append({"type": "image", "data": image, "mimeType": "image/png"})
    send({"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": not ok}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue  # notifications need no reply
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "room", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            threading.Thread(target=handle_call, args=(msg_id, msg.get("params") or {}), daemon=True).start()
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()

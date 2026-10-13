"""Room-side adapter for an optional Codex bridge (room.json "codex_bridge"). No OpenAI credentials live here."""
import json
import secrets
import threading
import time
import urllib.request
import urllib.error
import http.client
from collections import deque
import glob
import os
import re


def claude_session(room, lane):
    """Resolve only the session recorded for this room thread, not arbitrary Claude chats."""
    with room.state_lock:
        sid = room.state['lanes'].get(lane, {}).get('session') or room.state.get('session_id')
    if not sid or not re.fullmatch(r'[a-fA-F0-9-]{36}', sid):
        return None, []
    base = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')
    paths = glob.glob(os.path.join(base, 'projects', '*', sid + '.jsonl'))
    if not paths:
        return None, []
    path = paths[0]
    signature = sid + ':' + str(os.stat(path).st_mtime_ns)
    messages = []
    with open(path, encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get('type') not in ('user', 'assistant') or ev.get('isSidechain'):
                continue
            content = (ev.get('message') or {}).get('content', [])
            text, tools = [], []
            if isinstance(content, str):
                text = [content]
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'text':
                        text.append(block.get('text', ''))
                    elif block.get('type') == 'tool_use':
                        tools.append({'name': block.get('name'), 'input': block.get('input')})
                    elif block.get('type') == 'tool_result':
                        result = block.get('content', '')
                        if isinstance(result, list):
                            result = '\n'.join(b.get('text', '') for b in result if isinstance(b, dict))
                        text.append('[Tool result] ' + str(result)[:6000])
            if not text and not tools:
                continue
            messages.append({'index': len(messages), 'role': ev['type'], 'ts': ev.get('timestamp'),
                             'summary': bool(ev.get('isCompactSummary')), 'text': '\n'.join(text), 'tools': tools})
    return signature, messages


def claude_handover(room, lane):
    signature, messages = claude_session(room, lane)
    if not signature:
        return None, None
    with room.state_lock:
        previous = room.state['lanes'].get(lane, {}).get('codex_claude_source')
    if signature == previous:
        return signature, None
    summaries = [i for i, msg in enumerate(messages) if msg['summary']]
    start = summaries[-1] if summaries else 0
    summary = messages[start] if summaries else None
    recent = messages[start + 1:] if summary else messages
    # Preserve Claude's own compaction in full; bound subsequent raw tool output.
    selected, size = [], 0
    for msg in reversed(recent):
        item = {**msg, 'text': msg['text'][:8000], 'tools': msg['tools'][:10]}
        length = len(json.dumps(item))
        if selected and size + length > 65000:
            break
        selected.append(item)
        size += length
    data = {'source': signature.split(':')[0], 'lane': lane, 'summary': summary,
            'recent': list(reversed(selected)), 'total_entries': len(messages),
            'note': 'Historical context, not new requests. Use claude_session_read for older entries or details.'}
    return signature, json.loads(room.redact(json.dumps(data)))


def claude_session_read(room, lane, args):
    signature, messages = claude_session(room, lane)
    if not signature:
        return True, 'No saved Claude session is available for this thread.', None
    try:
        start = max(0, int(args.get('start', 0)))
        limit = max(1, min(30, int(args.get('limit', 10))))
    except (ValueError, TypeError):
        return False, 'start and limit must be integers.', None
    query = str(args.get('query', '')).casefold()
    candidates = [m for m in messages if m['index'] >= start and
                  (not query or query in json.dumps(m, ensure_ascii=False).casefold())]
    rows = [{**m, 'text': m['text'][:16000]} for m in candidates[:limit]]
    while len(rows) > 1 and len(json.dumps(rows)) > 60000:
        rows.pop()
    return True, room.redact(json.dumps({'source': signature.split(':')[0], 'total_entries': len(messages),
        'messages': rows, 'next_start': rows[-1]['index'] + 1 if rows and len(candidates) > len(rows) else None},
        ensure_ascii=False)), None


def conversation_read(room, lane, args):
    """Read the visible thread archive, never the private people-only chat."""
    from server import HISTORY
    try:
        limit = max(1, min(50, int(args.get('limit', 20))))
        before = float(args.get('before') or float('inf'))
    except (ValueError, TypeError):
        return False, 'limit must be a number from 1 to 50; before must be a timestamp.', None
    rows = deque(maxlen=limit + 1)
    try:
        with open(HISTORY, encoding='utf-8') as stream:
            for line in stream:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get('lane') != lane or ev.get('ts', 0) >= before or ev.get('t') not in ('msg', 'answer'):
                    continue
                if ev['t'] == 'msg':
                    row = {'id': ev.get('id'), 'ts': ev.get('ts'), 'speaker': ev.get('from'), 'text': ev.get('text', '')}
                else:
                    row = {'id': ev.get('id'), 'ts': ev.get('ts'), 'speaker': ev.get('assistant', 'Claude'),
                           'asked': ev.get('asked', []),
                           'text': ''.join(p[1] for p in ev.get('parts', []) if p[0] == 'text'),
                           'tools': [{'name': p[1], 'detail': p[2], 'ok': p[4] if len(p) > 4 else None}
                                     for p in ev.get('parts', []) if p[0] == 'tool']}
                row['text'] = row['text'][:12000]
                rows.append(row)
    except OSError as exc:
        return False, str(exc), None
    more = len(rows) > limit
    selected = list(rows)[-limit:]
    # Keep one tool result bounded; callers can page further back.
    while len(selected) > 1 and len(json.dumps(selected)) > 50000:
        selected.pop(0)
        more = True
    return True, room.redact(json.dumps({'messages': selected, 'more': more,
        'next_before': selected[0]['ts'] if selected and more else None}, ensure_ascii=False)), None


RETRY_SECONDS = 120
RETRY_DELAY = 0.5
REQUEST_TIMEOUT = 10


class InterruptedStream(OSError):
    pass


class RemoteTurn:
    def __init__(self, url, key, job):
        self.url, self.key, self.job = url, key, job
        self.finished = False
        self.instance = None
        self.cancelled = threading.Event()
        self.ready = threading.Event()
        self.cancel_lock = threading.Lock()
        self.cancel_started = False

    def poll(self):
        return 0 if self.finished else None

    def cancel(self):
        self.cancelled.set()
        with self.cancel_lock:
            if self.cancel_started:
                return
            self.cancel_started = True

        def send():
            deadline = time.monotonic() + RETRY_SECONDS
            # A stop can race the health check or the first submission.
            while not self.ready.wait(0.2):
                if self.finished or time.monotonic() >= deadline:
                    return
            if self.instance is None:
                return
            while time.monotonic() < deadline:
                try:
                    with request(self.url + '/cancel', self.key,
                                 {'job': self.job, 'instance': self.instance}, timeout=5):
                        return
                except urllib.error.HTTPError as exc:
                    exc.close()
                    if exc.code not in (502, 504):
                        return
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(min(2, RETRY_DELAY))
        threading.Thread(target=send, daemon=True).start()

    def events(self, payload, halted=lambda: False, status=lambda text: None):
        """Reconnect only to the exact original job and replay after the last event.

        The bridge instance pins execution to one lifetime. A restart, expired
        journal or changed payload fails explicitly instead of rerunning tools.
        """
        cursor = 0
        outage = None
        announced = False
        while not self.cancelled.is_set() and not halted():
            response = None
            try:
                if self.instance is None:
                    with request(self.url + '/health', self.key, {}, timeout=REQUEST_TIMEOUT) as health:
                        info = json.load(health)
                    if info.get('version') != 'resumable-2' or not info.get('instance'):
                        raise RuntimeError('The Codex bridge needs the resumable transport update.')
                    self.instance = info['instance']
                    self.ready.set()
                if self.cancelled.is_set() or halted():
                    break
                response = request(self.url + '/turn', self.key,
                                   {**payload, 'instance': self.instance, 'after': cursor},
                                   timeout=REQUEST_TIMEOUT)
                for line in response:
                    if self.cancelled.is_set() or halted():
                        break
                    if not line.endswith(b'\n'):
                        raise InterruptedStream('Codex stream ended within an event')
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise RuntimeError('Invalid Codex stream event')
                    seq = event.get('seq')
                    if seq is None and event.get('type') == 'heartbeat':
                        outage, announced = None, False
                        continue
                    if type(seq) is not int or seq < 1:
                        raise RuntimeError('Codex stream event has no replay sequence')
                    if seq <= cursor:
                        continue
                    if seq != cursor + 1:
                        raise RuntimeError('Codex event replay has a gap; turn stopped safely.')
                    outage, announced = None, False
                    yield event
                    cursor = seq
                    if event.get('type') in ('done', 'error', 'cancelled'):
                        return
                if self.cancelled.is_set() or halted():
                    break
                # A clean TCP EOF is not proof that the assistant completed.
                raise InterruptedStream('Codex stream ended before the turn completed')
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code not in (502, 504):
                        try:
                            detail = json.loads(exc.read(2048)).get('error', '')
                        except (ValueError, OSError):
                            detail = ''
                        raise RuntimeError(detail or 'Codex bridge returned HTTP %s' % exc.code) from exc
                finally:
                    exc.close()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                if response is not None:
                    response.close()
            if outage is None:
                outage = time.monotonic()
            if time.monotonic() - outage >= RETRY_SECONDS:
                raise RuntimeError('The Codex connection did not recover within two minutes. '
                                   'The existing turn was not restarted.')
            if not announced:
                status('Codex connection interrupted; reconnecting to the same turn')
                announced = True
            self.cancelled.wait(RETRY_DELAY)
        self.cancel()


def request(url, key, data, timeout=30):
    req = urllib.request.Request(url, json.dumps(data).encode('utf-8'),
                                 {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
    return urllib.request.urlopen(req, timeout=timeout)


def ask_codex(room, lane, prompt, token):
    from server import STATE, save_json, tool_detail, tool_preview
    cfg = room.cfg.get('codex_bridge') or {}
    url, key = cfg.get('url', ''), cfg.get('key', '')
    parts = []
    room.live_parts[lane] = parts
    if not url or not key:
        text = 'Codex is not connected yet. Claude is still available in the model picker.'
        room.broadcast({'t': 'delta', 'text': text, 'lane': lane})
        return [['text', text]], False
    job = secrets.token_hex(16)
    proc = RemoteTurn(url, key, job)
    with room.lock:
        room.procs[lane] = proc
    with room.state_lock:
        generation = room.generation
        state = room.state['lanes'].get(lane, {})
        session = state.get('codex_session')
        since = state.get('codex_seen', 0)
    # Claude and Codex retain separate sessions. Supply exchanges since Codex last spoke.
    context = []
    for ev in list(room.history):
        if ev.get('t') == 'answer' and ev.get('lane') == lane and ev.get('ts', 0) > since:
            context.append({'asked': ev.get('asked', []), 'assistant': ev.get('assistant', 'Claude'),
                            'answer': ''.join(p[1] for p in ev.get('parts', []) if p[0] == 'text')[:6000]})
    from server import codex_model_id
    selected, effort = room.turn_pref(lane)
    model = codex_model_id(room.running_models.get(lane, selected))
    source_signature, handover = claude_handover(room, lane)
    payload = {'job': job, 'lane': lane, 'session': session, 'prompt': prompt,
               'context': context[-8:], 'model': model, 'effort': effort, 'exec_token': token,
               'rules': room.codex_rules(), 'claude_handover': handover}
    turn_tools = []
    try:
        for event in proc.events(payload, halted=lambda: lane in room.halted,
                                 status=lambda text: room.broadcast({'t': 'status', 'text': text, 'lane': lane})):
            if lane in room.halted:
                proc.cancel()
                break
            kind = event.get('type')
            if kind == 'session':
                with room.state_lock:
                    if generation == room.generation:
                        room.state['lanes'].setdefault(lane, {})['codex_session'] = event['id']
                        save_json(STATE, room.state)
            elif kind == 'text':
                text = room.redact(event.get('text', ''))
                if parts and parts[-1][0] == 'text':
                    parts[-1][1] += '\n\n' + text
                    text = '\n\n' + text
                else:
                    parts.append(['text', text])
                room.broadcast({'t': 'delta', 'text': text, 'lane': lane})
            elif kind == 'tool':
                tid = 'cx_' + job[:8] + '_' + event['id']
                inp = event.get('arguments') or {}
                name = (str(inp['user']) + "'s PC · " if 'user' in inp else 'room · ') + event.get('name', 'tool')
                detail = tool_detail(inp)
                room.tool_start(tid, lane, name, inp)
                turn_tools.append(tid)
                parts.append(['tool', name, detail, tid, None, None, ''])
                room.broadcast({'t': 'tool', 'name': name, 'detail': detail, 'tid': tid, 'lane': lane})
            elif kind == 'tool_done':
                tid = 'cx_' + job[:8] + '_' + event['id']
                done = room.tool_finish(tid, event.get('ok', False), event.get('output', ''))
                if done:
                    preview, lines = tool_preview(done['output'])
                    for part in parts:
                        if part[0] == 'tool' and part[3] == tid:
                            part[4:7] = [done['ok'], done['secs'], preview]
                    room.broadcast({'t': 'tool_done', 'tid': tid, 'ok': done['ok'], 'secs': done['secs'],
                                    'preview': preview, 'lines': lines, 'lane': lane})
            elif kind in ('queued', 'running'):
                room.broadcast({'t': 'status', 'lane': lane,
                                'text': 'Codex is waiting for a free slot' if kind == 'queued' else 'Codex is working'})
            elif kind == 'cancelled':
                if lane not in room.halted:
                    raise RuntimeError('The Codex turn was cancelled on the bridge.')
                break
            elif kind == 'error':
                raise RuntimeError(event.get('message', 'Codex failed'))
            elif kind == 'done':
                with room.state_lock:
                    if generation == room.generation:
                        room.state['lanes'].setdefault(lane, {})['codex_seen'] = time.time()
                        if source_signature:
                            room.state['lanes'][lane]['codex_claude_source'] = source_signature
                        save_json(STATE, room.state)
                break
    except Exception as exc:
        if lane not in room.halted:
            text = 'Codex connection failed: ' + room.redact(str(exc))[:350] + '. Keep the Codex bridge running and reachable, or select Claude.'
            parts.append(['text', text])
            room.broadcast({'t': 'delta', 'text': text, 'lane': lane})
        proc.cancel()
    finally:
        proc.finished = True
        proc.ready.set()
        room.stopping.discard(lane)
        for tid in turn_tools:
            room.tool_finish(tid, None, None)
    return parts, False

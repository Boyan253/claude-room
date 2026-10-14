"""Gemini room agents. Google credentials stay in the owner's ADC gateway.

Every tool uses the same authenticated room endpoints as room_mcp.py. Model
responses (including opaque thought signatures) are checkpointed verbatim;
an interrupted action is never automatically executed again.
"""
import ast
import copy
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MODEL = 'gemini-4-argon'
MAX_ROUNDS = 48
MAX_TOOLS = 128
MAX_CONTEXT_BYTES = 10_000_000
COMPACT_AT_BYTES = 1_200_000
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_TOOL_TEXT = 60000


class Cancelled(RuntimeError):
    pass


class GeminiTurn:
    def __init__(self, generation=None):
        self.finished = False
        self.cancelled = threading.Event()
        self.generation = generation

    def poll(self):
        return 0 if self.finished else None

    def cancel(self):
        # Existing PC work may already have begun. Never pretend it was undone.
        self.cancelled.set()

    def check(self, room, lane):
        if self.generation is not None and room.generation != self.generation:
            # /new invalidates an old turn before it can issue another action.
            with room.lock:
                room.halted.add(lane)
            raise Cancelled('A fresh conversation replaced this turn.')
        if self.cancelled.is_set() or lane in room.halted:
            raise Cancelled('Stopped; any action already dispatched must be checked before repeating it.')

    def wait(self, fn, room, lane):
        """Let Stop take effect while a bounded HTTP operation finishes in background."""
        self.check(room, lane)
        result = queue.Queue(maxsize=1)
        def work():
            try:
                result.put((True, fn()))
            except Exception as exc:
                result.put((False, exc))
        threading.Thread(target=work, daemon=True).start()
        while True:
            self.check(room, lane)
            try:
                ok, value = result.get(timeout=.2)
            except queue.Empty:
                continue
            self.check(room, lane)
            if not ok:
                raise value
            return value


def _schema_literal(node, values):
    """Evaluate literal MCP schemas without importing env-dependent MCP code."""
    if isinstance(node, ast.Name) and node.id in values:
        return copy.deepcopy(values[node.id])
    if isinstance(node, ast.Dict):
        return {_schema_literal(k, values): _schema_literal(v, values)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = [_schema_literal(x, values) for x in node.elts]
        return set(items) if isinstance(node, ast.Set) else items
    return ast.literal_eval(node)


def room_schemas(path=None):
    source = Path(path or Path(__file__).with_name('room_mcp.py')).read_text(encoding='utf-8-sig')
    values = {}
    wanted = {'USER', 'COLUMN', 'BOARD', 'PRIORITY', 'ASSIGNEE', 'LABEL', 'TOOLS', 'ROOM_TOOLS'}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in wanted:
                values[name] = _schema_literal(node.value, values)
    tools, routed = values['TOOLS'], values['ROOM_TOOLS']
    declarations = [{'name': t['name'], 'description': t['description'],
                     'parametersJsonSchema': t['inputSchema']} for t in tools]
    return declarations, routed


def _post(url, headers, payload, timeout):
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=raw, method='POST',
                                 headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeError('Response exceeded the bounded size; no request was retried.')
        return json.loads(body)
    except urllib.error.HTTPError as exc:
        # Neither an HTML traceback nor arbitrary upstream bodies enter chat.
        status = exc.code
        exc.close()
        raise RuntimeError(f'HTTP {status}; request was not retried. Check gateway availability, quota and permissions.') from None
    except (OSError, TimeoutError) as exc:
        raise RuntimeError('Connection interrupted or timed out; request was not retried. '
                           'If this was a PC action, its outcome is unknown: verify it before repeating.') from None


def model_catalog():
    try:
        catalog = json.loads(Path(__file__).with_name('gemini_models.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise RuntimeError('The verified Gemini model catalog is missing or invalid; no model request was sent.') from None
    if not isinstance(catalog, dict) or not catalog:
        raise RuntimeError('The verified Gemini model catalog is empty; no model request was sent.')
    return catalog


def selected_model(room, lane):
    from server import gemini_model_id
    selected, _ = room.turn_pref(lane)
    selected = getattr(room, 'running_models', {}).get(lane, selected)
    model = gemini_model_id(selected)
    if model not in model_catalog():
        raise RuntimeError('The selected Gemini model is not in the verified room catalog.')
    return model


def thinking_level(model, effort):
    config = model_catalog().get(model) or {}
    supported = config.get('efforts') or []
    level = str(effort or config.get('default_effort') or '').lower()
    if level not in supported:
        level = config.get('default_effort')
    if not level or level not in supported:
        raise RuntimeError('This Gemini model has no verified thinking-level configuration.')
    return level.upper()


def generate(cfg, payload, model=MODEL):
    if model not in model_catalog():
        raise RuntimeError('The requested Gemini model is not in the verified room catalog.')
    url = str(cfg.get('url', '')).rstrip('/')
    target = urllib.parse.urlsplit(url)
    if target.scheme not in ('http', 'https') or target.username or target.password or target.query or target.fragment:
        raise RuntimeError('Invalid Gemini gateway URL.')
    if not cfg.get('key'):
        raise RuntimeError('Gemini is not connected. Select Claude or Codex until its gateway is configured.')
    return _post(url + '/generateContent', {'Authorization': 'Bearer ' + cfg['key'],
                                           'X-Gemini-Model': model}, payload, 195)


def call_tool(room, token, name, args, room_tools):
    route = '/room_tool' if name in room_tools else '/exec'
    return _post(f"http://127.0.0.1:{int(room.cfg['port'])}" + route,
                 {'X-Room-Exec': token}, {'user': args.get('user', ''), 'tool': name, 'args': args}, 905)


def _session_path(lane, session_id):
    from server import ROOT
    if not re.fullmatch(r'[a-f0-9]{32}', str(session_id)):
        raise RuntimeError('Invalid saved Gemini session reference; no history was overwritten.')
    return Path(ROOT) / 'gemini_sessions' / (hashlib.sha256(lane.encode()).hexdigest() + '-' + session_id + '.json')


def _session_reference(room, lane, expected_generation=None, model=None):
    from server import STATE, save_json
    model = model or selected_model(room, lane)
    if model not in model_catalog():
        raise RuntimeError('Cannot open history for an unverified Gemini model.')
    with room.state_lock:
        if expected_generation is not None and room.generation != expected_generation:
            raise Cancelled('A fresh conversation replaced this turn before its session opened.')
        state = room.state['lanes'].setdefault(lane, {})
        sessions = state.setdefault('gemini_sessions', {})
        session_id = sessions.get(model)
        if not session_id and model == MODEL:
            session_id = state.get('gemini_session')
        if not session_id:
            session_id = secrets.token_hex(16)
        if sessions.get(model) != session_id:
            sessions[model] = session_id
            # Keep the original reference compatible, exclusively for Argon.
            if model == MODEL:
                state['gemini_session'] = session_id
            save_json(STATE, room.state)
        return _session_path(lane, session_id)


def _save(path, session):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.' + secrets.token_hex(6) + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(session, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _load(path, lane, model=MODEL):
    if not path.exists():
        return {'version': 1, 'lane': lane, 'model': model, 'contents': [], 'seen': 0}
    try:
        session = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise RuntimeError('Saved Gemini history could not be read. It has been kept intact; repair it before continuing.') from None
    if session.get('lane') != lane or session.get('model') != model or not isinstance(session.get('contents'), list):
        raise RuntimeError('Saved Gemini session does not match this thread/model; it has not been overwritten.')
    repair_pending(session['contents'])
    return session


def repair_pending(contents):
    """Complete interrupted function batches with unknown-outcome records, never execute them."""
    for index, content in enumerate(list(contents)):
        calls = [p['functionCall'] for p in content.get('parts', []) if 'functionCall' in p]
        if content.get('role') != 'model' or not calls:
            continue
        following = contents[index + 1] if index + 1 < len(contents) else None
        if following is None or following.get('role') != 'user':
            following = {'role': 'user', 'parts': []}
            contents.insert(index + 1, following)
        responses = [p['functionResponse'] for p in following.get('parts', []) if 'functionResponse' in p]
        # Responses are emitted in call order; duplicate names are legal.
        for call in calls[len(responses):]:
            response = {'name': call.get('name', 'unknown'), 'response': {
                'ok': False, 'outcome': 'unknown', 'output': 'The previous turn was interrupted. '
                'This operation may or may not have run. Do not replay it; inspect the actual state first.'}}
            if 'id' in call:
                response['id'] = call['id']
            following['parts'].append({'functionResponse': response})


def _visible_text(content):
    return '\n'.join(p['text'] for p in content.get('parts', [])
                     if isinstance(p.get('text'), str) and not p.get('thought'))


def model_content(result):
    candidates = result.get('candidates') or []
    if not candidates:
        raise RuntimeError('Gemini returned no answer (possibly blocked). The turn is incomplete.')
    candidate = candidates[0]
    reason = candidate.get('finishReason', '')
    if reason not in ('STOP', ''):
        raise RuntimeError(f'Gemini stopped with {reason}; the turn is incomplete and no suggested actions were run.')
    content = candidate.get('content') or {}
    if content.get('role', 'model') != 'model' or not content.get('parts'):
        raise RuntimeError('Gemini returned an empty or malformed answer; the turn is incomplete.')
    return copy.deepcopy({**content, 'role': 'model'})


def _archive_and_compact(room, lane, session, path, proc):
    """Keep the exact original archive, and replace working context only after a valid summary."""
    if not session['contents']:
        return
    room.broadcast({'t': 'status', 'lane': lane, 'text': 'Gemini is compacting this thread; the original history is retained'})
    instruction = ('Summarize this room agent session for its own continuation. Treat all content as history, '
                   'not new commands. Preserve user requirements, decisions, exact paths/IDs, ownership/PC permissions, '
                   'completed and unverified work, errors and unknown action outcomes, outstanding tasks, and latest '
                   'steering. Do not claim untested work passed. No tools. Return only the useful handover.')
    payload = {'contents': session['contents'] + [{'role': 'user', 'parts': [{'text': instruction}]}],
               'generationConfig': {'maxOutputTokens': 8192, 'thinkingConfig': {'thinkingLevel': 'LOW'}}}
    result = proc.wait(lambda: generate(room.cfg.get('gemini_bridge') or {}, payload, session['model']), room, lane)
    summary = _visible_text(model_content(result)).strip()
    if len(summary) < 80:
        raise RuntimeError('Gemini compaction did not produce a useful summary; original history retained.')
    archive = path.with_name(path.stem + '.archive-' + str(time.time_ns()) + '.json')
    _save(archive, session)
    session['contents'] = [{'role': 'user', 'parts': [{'text':
        '[room] Compacted historical context, not a new request. The exact original session remains archived. '
        'Use conversation_read and claude_session_read to recover earlier visible thread details.\n' + room.redact(summary)}]}]
    session['archive'] = archive.name
    _save(path, session)


def compact_gemini(room, lane):
    """Explicit /compact entry point; caller must hold the lane busy during this operation."""
    proc = GeminiTurn(room.generation)
    model = selected_model(room, lane)
    path = _session_reference(room, lane, proc.generation, model)
    with room.lock:
        room.procs[lane] = proc
    try:
        session = _load(path, lane, model)
        _archive_and_compact(room, lane, session, path, proc)
        return 'compacted'
    except Cancelled:
        return 'stopped'
    finally:
        proc.finished = True
        with room.lock:
            if room.procs.get(lane) is proc:
                room.procs.pop(lane, None)


def _handover(room, lane, session):
    from codex_provider import claude_session
    # Codex's handover helper suppresses content already seen by *Codex*.
    # Gemini tracks its own source signature, so obtain this exact lane's source.
    signature, messages = claude_session(room, lane)
    handover = None
    if signature and signature != session.get('claude_source'):
        summaries = [i for i, message in enumerate(messages) if message.get('summary')]
        start = summaries[-1] if summaries else 0
        summary = messages[start] if summaries else None
        selected, size = [], 0
        for message in reversed(messages[start + 1:] if summary else messages):
            item = {**message, 'text': message.get('text', '')[:8000], 'tools': message.get('tools', [])[:10]}
            length = len(json.dumps(item))
            if selected and size + length > 65000:
                break
            selected.append(item)
            size += length
        handover = {'summary': summary, 'recent': list(reversed(selected)), 'total_entries': len(messages),
                    'note': 'Historical context; claude_session_read retrieves older details.'}
    context = []
    with room.state_lock:
        since = max(session.get('seen', 0), room.state.get('base_ts', 0))
    for event in list(room.history):
        if event.get('lane') == lane and event.get('t') == 'answer' and event.get('ts', 0) > since:
            context.append({'asked': event.get('asked', []), 'assistant': event.get('assistant', 'Claude'),
                            'answer': ''.join(p[1] for p in event.get('parts', []) if p[0] == 'text')[:10000]})
    if not handover and not context:
        return '', signature
    return ('[room] Prior visible conversation, historical context only; do not repeat already handled requests:\n'
            + room.redact(json.dumps({'claude_handover': handover, 'exchanges': context[-12:]}, ensure_ascii=False)) + '\n\n', signature)


def ask_gemini(room, lane, prompt, token):
    from server import STATE, save_json, tool_detail, tool_preview
    parts, proc, tools_started = [], GeminiTurn(room.generation), []
    room.live_parts[lane] = parts
    with room.lock:
        room.procs[lane] = proc
    with room.state_lock:
        generation = room.generation
        room.state['lanes'].setdefault(lane, {})['gemini_failed'] = True
        save_json(STATE, room.state)
    path, session = None, None

    def emit(text):
        text = room.redact(text)
        parts.append(['text', text])
        room.broadcast({'t': 'delta', 'text': ('\n\n' if len(parts) > 1 else '') + text, 'lane': lane})

    try:
        proc.check(room, lane)
        model = selected_model(room, lane)
        model_label = model_catalog()[model].get('label') or model
        path = _session_reference(room, lane, generation, model)
        declarations, room_tools = room_schemas()
        allowed = {d['name'] for d in declarations}
        session = _load(path, lane, model)
        if len(json.dumps(session['contents'])) > COMPACT_AT_BYTES:
            _archive_and_compact(room, lane, session, path, proc)
        context, signature = _handover(room, lane, session)
        session['contents'].append({'role': 'user', 'parts': [{'text': context + room.redact(str(prompt))}]})
        session['status'] = 'running'
        _save(path, session)
        _, effort = room.turn_pref(lane)
        effort = thinking_level(model, effort)
        rules = room.codex_rules().replace('using Codex as your identity', 'using ' + model_label + ' as your identity')
        rules += ('\nYou are ' + model_label + ' (model ID ' + model + '). Use only the declared room tools for PC/room actions. '
                  'They enforce the same per-person permissions as Claude and Codex. Never invent tool results. '
                  'Read shared images with file_view. No built-in browser/web-search tool is connected here. '
                  'An unknown tool outcome requires read-only verification before retrying any mutation. '
                  'Stop at a clear checkpoint if a budget or capability blocks further progress.')
        calls_used, started = 0, time.monotonic()
        for step in range(MAX_ROUNDS):
            proc.check(room, lane)
            if time.monotonic() - started > 1800:
                raise RuntimeError('Gemini reached this turn\'s 30-minute limit. Saved history is intact; continue from the checkpoint.')
            payload = {'systemInstruction': {'parts': [{'text': room.redact(rules)}]},
                       'contents': session['contents'], 'tools': [{'functionDeclarations': declarations}],
                       'generationConfig': {'maxOutputTokens': 8192, 'thinkingConfig': {'thinkingLevel': effort}}}
            if len(json.dumps(payload).encode('utf-8')) > MAX_CONTEXT_BYTES:
                raise RuntimeError('Gemini context reached the per-turn size limit. History is saved; continue to compact it first.')
            room.broadcast({'t': 'status', 'lane': lane, 'text': model_label + ' is thinking (' + effort.lower() + ')'})
            result = proc.wait(lambda: generate(room.cfg.get('gemini_bridge') or {}, payload, model), room, lane)
            content = model_content(result)
            session['contents'].append(content)  # preserve signatures and part boundaries exactly
            _save(path, session)  # durable intent before any effect
            visible = _visible_text(content)
            if visible:
                emit(visible)
            calls = [p['functionCall'] for p in content['parts'] if 'functionCall' in p]
            if not calls:
                if not visible:
                    raise RuntimeError('Gemini returned neither visible text nor an action; this turn is incomplete.')
                # Arrivals during the last model request must be seen before completion.
                with room.lock:
                    has_midturn = bool(room.midturn.get(lane))
                if has_midturn:
                    additions = room.with_midturn(lane, '')
                    session['contents'].append({'role': 'user', 'parts': [{'text': additions}]})
                    _save(path, session)
                    continue
                session.update(status='complete', seen=time.time(), claude_source=signature)
                _save(path, session)
                with room.state_lock:
                    if generation == room.generation:
                        state = room.state['lanes'].setdefault(lane, {})
                        state.update(gemini_failed=False, gemini_seen=session['seen'],
                                     gemini_ctx=int((result.get('usageMetadata') or {}).get('promptTokenCount') or 0))
                        save_json(STATE, room.state)
                return parts, False
            response_content = {'role': 'user', 'parts': []}
            session['contents'].append(response_content)
            for call in calls:
                proc.check(room, lane)
                calls_used += 1
                if calls_used > MAX_TOOLS:
                    raise RuntimeError('Gemini reached the 128-action turn limit. History is saved; continue from the checkpoint.')
                name, args = call.get('name'), call.get('args', {})
                if name not in allowed or not isinstance(args, dict):
                    raise RuntimeError('Gemini proposed an unknown or malformed room tool; nothing was dispatched for it.')
                tid = 'gm_' + secrets.token_hex(10)
                label = (str(args['user']) + "'s PC · " if 'user' in args else 'room · ') + name
                detail = tool_detail(args)
                room.tool_start(tid, lane, label, args)
                tools_started.append(tid)
                part = ['tool', label, detail, tid, None, None, '']
                parts.append(part)
                room.broadcast({'t': 'tool', 'name': label, 'detail': detail, 'tid': tid, 'lane': lane})
                outcome = proc.wait(lambda: call_tool(room, token, name, args, room_tools), room, lane)
                output = room.redact(str(outcome.get('output', '')))
                ok = bool(outcome.get('ok'))
                done = room.tool_finish(tid, ok, output)
                if done:
                    preview, lines = tool_preview(done['output'])
                    part[4:7] = [done['ok'], done['secs'], preview]
                    room.broadcast({'t': 'tool_done', 'tid': tid, 'ok': done['ok'], 'secs': done['secs'],
                                    'preview': preview, 'lines': lines, 'lane': lane})
                clipped = output[:MAX_TOOL_TEXT]
                if len(output) > MAX_TOOL_TEXT:
                    clipped += '\n[room] Tool output truncated; request a narrower range if needed. Full output remains in the room tool log.'
                response = {'name': name, 'response': {'ok': ok, 'output': clipped}}
                if 'id' in call:
                    response['id'] = call['id']
                if outcome.get('image'):
                    data = outcome['image']
                    if not isinstance(data, str) or len(data) > 8_000_000:
                        response['response']['image_error'] = 'Image exceeded the bounded model context; request a smaller image.'
                    else:
                        response['parts'] = [{'inlineData': {'mimeType': 'image/png', 'data': data}}]
                response_content['parts'].append({'functionResponse': response})
                _save(path, session)  # finished effects must survive a subsequent model/network failure
        raise RuntimeError('Gemini reached the 48-step turn limit. Saved history is intact; continue from the checkpoint.')
    except Cancelled:
        if session is not None:
            repair_pending(session['contents'])
            session['status'] = 'interrupted'
            _save(path, session)
        return parts, False
    except Exception as exc:
        if session is not None:
            repair_pending(session['contents'])
            session['status'] = 'failed'
            _save(path, session)
        emit('Gemini turn incomplete: ' + str(exc)[:500])
        return parts, False
    finally:
        proc.finished = True
        room.stopping.discard(lane)
        for tid in tools_started:
            room.tool_finish(tid, None, None)

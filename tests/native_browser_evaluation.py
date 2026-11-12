"""A-only synthetic adapter and deterministic model, excluded from paid executor.

Known A selectors here diagnose the orchestration, never unseen-site capability.
The independent source oracle stays in the parent process, outside SDK messages.
"""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from native_browser_fixture import BoardState, NativeBoard


class SiteA:
    case_id = 'a'

    def __init__(self):
        self.board = NativeBoard(BoardState(host='93.184.215.14', username='member-'+os.urandom(4).hex(),
            password='secret-'+os.urandom(6).hex()), public_origin='http://93.184.215.14').start()
        self.origin = self.board.origin
        self.entry_url = self.origin+'/login'
        self.target_url = self.origin+'/home'
        self.username = self.expected_subject = self.board.state.username
        self.password = self.board.state.password
        self.listener_port = self.board.server.server_port

    def snapshot(self):
        data = self.board.oracle()
        return {'requests': data['requests'], 'posts': [dict(row, url=self.origin+'/posts/'+str(row['id'])) for row in data['posts']]}

    def arm_drop_ack(self):
        self.board.state.drop_next_post_response = True

    def close(self):
        self.board.close()


def create_a():
    return SiteA()


def dry_model(site, calls, destination):
    state = {'stage': 'login', 'step': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(raw)
            parts = []
            for message in request.get('messages', []):
                value = message.get('content')
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, list):
                    parts.extend(item.get('text', '') for item in value if isinstance(item, dict))
            text = '\n'.join(parts)
            lines = text.splitlines()
            nodes = [(int(match[1]), '\n'.join(lines[number:number+3])) for number, line in enumerate(lines)
                if (match := re.search(r'\[(\d+)\]', line)) and any(tag in line.lower() for tag in ('<input', '<textarea', '<button', '<a '))]
            stage, step = state['stage'], state['step']
            state['step'] += 1
            action = {'done': {'text': 'Mechanical A harness diagnostic only', 'success': True}}
            if stage == 'login':
                if step in (0, 1):
                    field = 'username' if step == 0 else 'password'
                    found = [index for index, line in nodes if re.search(r'(?:name|type)=[\"\']?'+field+r'\b', line, re.I)]
                    if found:
                        action = {'guarded_input': {'index': found[-1], 'text': 'saved_'+field, 'clear': True}}
                elif step == 2:
                    found = [index for index, line in nodes if '<button' in line.lower()]
                    if found:
                        action = {'guarded_click': {'index': found[-1]}}
                elif step == 3:
                    action = {'inspect_self': {'self_link_selector': 'a[rel~=me]', 'acting_subject_selector': 'main > p',
                        'self_subject_prefix': 'Signed in as ', 'self_subject_suffix': ' · Your account settings'}}
                elif step == 4:
                    action = {'report_connection': {'subject_selector': '#subject'}}
            elif stage in ('discover_publish', 'discover_reply'):
                operation = 'reply' if stage == 'discover_reply' else 'publish'
                if step == 0:
                    action = {'guarded_navigate': {'url': state['editor_url']}}
                elif step == 1:
                    selectors = {'self_subject': 'main > p', 'editor_target': '#editor-target',
                        'editor_body': 'textarea[name=body]', 'submit': 'form button'}
                    if operation == 'reply':
                        selectors['editor_parent'] = '#editor-parent'
                    action = {'propose_method': {'target_url': site.target_url, 'operation': operation,
                        'selectors': selectors, 'self_subject_prefix': 'Signed in as ',
                        'self_subject_suffix': ' · Your account settings', 'permission_selector': 'section p',
                        'permission_quote': 'You are an ordinary member. Members may publish text posts and reply to existing posts. '
                            'Automated browser tools are permitted for these member actions. You have no administrator privileges.'}}
            elif stage in ('publish', 'reply', 'reconcile'):
                if stage == 'reconcile' and step == 0:
                    found = [index for index, line in nodes if '<a ' in line.lower() and state['body'] in line]
                    if found:
                        action = {'guarded_click': {'index': found[-1]}}
                elif stage != 'reconcile' and step == 0:
                    found = [index for index, line in nodes if '<textarea' in line.lower()]
                    if found:
                        action = {'guarded_input': {'index': found[-1], 'text': state['body'], 'clear': True}}
                elif stage != 'reconcile' and step == 1:
                    found = [index for index, line in nodes if '<button' in line.lower()]
                    if found:
                        action = {'guarded_click': {'index': found[-1]}}
                elif step == (1 if stage == 'reconcile' else 2):
                    selectors = {'author': '#author', 'body': '#post-body', 'url': 'a[rel~=canonical]'}
                    if stage == 'reply':
                        selectors['parent'] = '#receipt-parent'
                    action = {'report_receipt': {'selectors': selectors}}
            # These are safe observations of the actual SDK request, not a
            # claim that the scripted transport derived its known selectors.
            relevant = [line for line in lines if re.search(r'<(?:input|textarea|button|a |span|strong|article|div|p\b)', line)]
            safe = '\n'.join(relevant[-35:])
            for secret in (site.username, site.password):
                safe = safe.replace(secret, '<redacted>')
            calls.append({'stage': stage, 'step': step, 'action': next(iter(action)), 'bytes': len(raw),
                'username_literal_seen': site.username.encode() in raw, 'password_literal_seen': site.password.encode() in raw,
                'id_attribute_visible': bool(re.search(r'\bid=', safe)), 'name_attribute_visible': bool(re.search(r'\bname=', safe)),
                'rel_attribute_visible': bool(re.search(r'\brel=', safe)), 'safe_dom_lines': safe})
            destination.write_text(json.dumps(calls))
            destination.chmod(0o600)
            tool = request['tools'][0]['function']['name']
            body = json.dumps({'id': 'a-dry-only', 'object': 'chat.completion', 'model': 'fixture-transport',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'dry-call', 'type': 'function', 'function': {'name': tool, 'arguments': json.dumps({'action': [action]})}}]},
                    'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, {'provider': 'deepseek', 'key': 'synthetic-transport-only',
                           'model': 'fixture-transport', 'base_url': 'http://127.0.0.1:'+str(server.server_port)}

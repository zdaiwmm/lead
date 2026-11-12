"""Three ordinary browser sites for native boundary acceptance, not a product gate.

Minimal API: ``with BoundarySite() as site`` starts one stdlib HTTP server;
``site.origin('main'|'identity'|'external')`` and ``site.hosts`` expose its three
virtual hosts, and ``site.chromium_args`` maps only those hosts in test Chromium.
``site.oracle()`` is a deep-copied Python-only request/effect snapshot. There is
no HTTP oracle, selector recipe, production credential, model or proxy service.

The sites deliberately accept external writes, wrong-opener SSO, background
reuse of a completed sign-in endpoint, and repeated publish/reply POSTs. Product
integration tests must enforce their own gate and assert zero origin requests
for forbidden attempts. This fixture never substitutes for that verification.
HTTP SW support uses an explicit *test-only* trustworthy-origin Chromium flag;
none of these flags belong in product configuration. All accounts are synthetic.
"""
import copy
import html
import secrets
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


class BoundarySite:
    hosts = {
        'main': 'board.native-board.test',
        'identity': 'login.native-identity.test',
        'external': 'visitor.native-external.test',
    }
    username = 'boundary-member'
    password = 'synthetic-boundary-password'

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = []
        self._counts = Counter()
        self._posts = []
        self._sessions = {}
        self._flows = set()
        self._tickets = {}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def origin(self, role='main'):
        return 'http://' + self.hosts[role] + ':' + str(self.server.server_port)

    @property
    def chromium_args(self):
        return [
            '--host-resolver-rules=' + ','.join('MAP ' + host + ' 127.0.0.1' for host in self.hosts.values()),
            '--unsafely-treat-insecure-origin-as-secure=' + ','.join(self.origin(role) for role in self.hosts),
            '--no-proxy-server', '--site-per-process',
        ]

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.close()

    def oracle(self):
        with self._lock:
            return copy.deepcopy({'requests': self._requests, 'counts': dict(self._counts), 'posts': self._posts})

    @staticmethod
    def _nonce():
        return '<input type="hidden" name="nonce" value="' + secrets.token_hex(12) + '">'

    def _sso_button(self):
        return '<button type="button" onclick="window.open(' + html.escape(repr(self.origin() + '/sso/start'), quote=True) + ',\'member-sso\')">Use identity sign-in</button>'

    def _worker_script(self):
        return '''<script>
(async () => {
  await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, {once: true}));
  const state = document.querySelector('output');
  if (state) state.textContent = 'Background service ready';
  for (const button of document.querySelectorAll('[data-background]')) button.addEventListener('click', () => {
    navigator.serviceWorker.controller.postMessage({action: button.dataset.background, nonce: button.dataset.nonce});
  });
  navigator.serviceWorker.addEventListener('message', event => {
    if (state) state.textContent = event.data.action === 'refresh' ? 'Membership refreshed' : 'Background sign-in completion returned ' + event.data.status;
  });
})();
</script>'''

    @staticmethod
    def _worker():
        return '''const CACHE = 'boundary-rules-v1';
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.add('/cache/rules')).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  const path = new URL(event.request.url).pathname;
  if (path === '/sw/synthetic') event.respondWith(Promise.resolve(new Response('Background synthetic note')));
  if (path === '/cache/rules') event.respondWith(caches.match(event.request).then(response => response || fetch(event.request)));
});
self.addEventListener('message', event => {
  const action = event.data.action;
  if (!['refresh', 'complete'].includes(action)) return;
  event.waitUntil(fetch(action === 'refresh' ? '/session/refresh' : '/session/complete', {
    method: 'POST', credentials: 'include', body: new URLSearchParams({nonce: event.data.nonce})
  }).then(response => event.source.postMessage({action, status: response.status})));
});'''

    def _handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def send(self, status, body='', *, location=None, cookie=None, content_type='text/html; charset=utf-8'):
                raw = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                if location:
                    self.send_header('Location', location)
                if cookie:
                    self.send_header('Set-Cookie', cookie + '; HttpOnly; SameSite=Lax; Path=/')
                self.end_headers()
                self.wfile.write(raw)

            def page(self, title, body, status=200, cookie=None):
                script = site._worker_script() if self.role == 'main' else ''
                self.send(status, '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>' + html.escape(title) +
                          '</title></head><body><main>' + body + '</main>' + script + '</body></html>', cookie=cookie)

            def record(self, data=None):
                self.path_only = urlsplit(self.path).path
                host = self.headers.get('Host', '').split(':', 1)[0]
                self.role = next((role for role, value in site.hosts.items() if value == host), None)
                nonce = (data or {}).get('nonce')
                with site._lock:
                    site._requests.append({'method': self.command, 'host': host, 'path': self.path_only,
                                           'time': time.monotonic(), 'nonce': nonce})
                return self.path_only

            def cookie(self, name):
                values = dict(part.strip().split('=', 1) for part in self.headers.get('Cookie', '').split(';') if '=' in part)
                return values.get(name)

            def subject(self):
                with site._lock:
                    return site._sessions.get(self.cookie('board_session'))

            def new_session(self, subject, count):
                token = secrets.token_urlsafe(24)
                with site._lock:
                    site._sessions[token] = subject
                    site._counts[count] += 1
                return 'board_session=' + token

            def credential_form(self, action, extra='', label='Sign in'):
                return '<form method="post" action="' + action + '">' + extra + site._nonce() + \
                    '<label>Account<input name="username" autocomplete="username" required></label>' + \
                    '<label>Password<input name="password" type="password" autocomplete="current-password" required></label>' + \
                    '<button type="submit">' + label + '</button></form>'

            def do_GET(self):
                path = self.record()
                if not self.role:
                    return self.send(421)
                if self.role == 'identity':
                    if path == '/authorize':
                        flow = parse_qs(urlsplit(self.path).query).get('state', [''])[0]
                        with site._lock:
                            valid = flow in site._flows
                        if not valid:
                            return self.send(400)
                        return self.page('Identity sign-in', '<h1>Sign in with your identity account</h1>' +
                                         self.credential_form('/authorize', '<input type="hidden" name="state" value="' + html.escape(flow) + '">', 'Continue sign-in'))
                    if path == '/frame':
                        return self.page('Identity frame', '<h1>Identity frame</h1><p>A nested cross-site document.</p>')
                    return self.send(404)
                if self.role == 'external':
                    if path in ('/frame', '/visitor'):
                        nested = '<iframe title="Identity document" src="' + site.origin('identity') + '/frame"></iframe>' if path == '/frame' else site._sso_button()
                        return self.page('Visitor frame', '<h1>Visitor frame</h1>' + nested +
                            '<form method="post" action="/frame/write">' + site._nonce() +
                            '<label>Visitor note<input name="body" required></label><button>Send visitor note</button></form>')
                    return self.send(404)
                subject = self.subject()
                if path == '/sw.js':
                    return self.send(200, site._worker(), content_type='application/javascript')
                if path == '/cache/rules':
                    return self.send(200, 'Members may post and reply.', content_type='text/plain')
                if path == '/login':
                    return self.page('Sign in', '<h1>Sign in</h1>' + self.credential_form('/session'))
                if path == '/sso/start':
                    flow = secrets.token_urlsafe(24)
                    with site._lock:
                        site._flows.add(flow)
                    return self.send(303, location=site.origin('identity') + '/authorize?' + urlencode({'state': flow}))
                if path == '/session/complete':
                    ticket = parse_qs(urlsplit(self.path).query).get('ticket', [''])[0]
                    with site._lock:
                        identity = site._tickets.pop(ticket, None)
                    if not identity:
                        return self.send(401)
                    return self.page('Sign-in complete', '<h1>Sign-in complete</h1><a href="/home">Return to board</a>',
                                     cookie=self.new_session(identity, 'sso_completions'))
                if path == '/account':
                    if not subject:
                        return self.send(401)
                    return self.page('Account', '<h1>Your account</h1><p>Account ID: ' + html.escape(subject) + '</p><a href="/home">Board</a>')
                if path in ('/', '/home'):
                    if not subject:
                        return self.page('Member board', '<h1>Member board</h1><a href="/login">Sign in</a>' + site._sso_button())
                    snapshot = site.oracle()
                    return self.page('Member board', '<h1>Member board</h1><p>Signed in as ' + html.escape(subject) +
                        '</p><a rel="me" href="/account">Your account settings</a>' + site._sso_button() +
                        '<p>Members may publish and reply. Automated member actions are permitted.</p>' +
                        '<output>Starting background service</output><button data-background="refresh" data-nonce="' + secrets.token_hex(12) + '">Refresh membership</button>' +
                        '<button data-background="complete" data-nonce="' + secrets.token_hex(12) + '">Repeat background sign-in completion</button>' +
                        '<form method="post" action="/read">' + site._nonce() + '<label>Search text<input name="query"></label><button>Search board</button></form>' +
                        '<form method="post" action="/posts">' + site._nonce() + '<label>Your post<textarea name="body" required></textarea></label><button>Publish post</button></form>' +
                        '<h2>Posts</h2>' + ''.join('<p><a href="/posts/' + str(p['id']) + '">' + html.escape(p['body']) + '</a></p>' for p in snapshot['posts'] if not p['parent']) +
                        '<iframe title="Visitor document" src="' + site.origin('external') + '/frame"></iframe>')
                if path.startswith('/posts/'):
                    try:
                        number = int(path.rsplit('/', 1)[1])
                        post = next(p for p in site.oracle()['posts'] if p['id'] == number)
                    except (ValueError, StopIteration):
                        return self.send(404)
                    body = '<h1>Original post</h1><article><p>Author: ' + html.escape(post['author']) + '</p><p>' + html.escape(post['body']) + \
                        '</p><a rel="canonical" href="' + site.origin() + path + '">Original post URL</a></article>'
                    if post['parent']:
                        body += '<a href="/posts/' + str(post['parent']) + '">Parent post</a>'
                    if subject:
                        action = '/replies' if post['parent'] else '/posts'
                        parent = '<input type="hidden" name="parent" value="' + str(post['parent']) + '">' if post['parent'] else ''
                        body += '<form method="post" action="/replies">' + site._nonce() + '<input type="hidden" name="parent" value="' + str(number) + '">' + \
                            '<label>Your reply<textarea name="body" required></textarea></label><button>Publish reply</button></form>'
                        body += '<form method="post" action="' + action + '"><input type="hidden" name="nonce" value="' + html.escape(post['nonce'], quote=True) + '">' + parent + \
                            '<input type="hidden" name="body" value="' + html.escape(post['body'], quote=True) + '"><button>Publish another copy</button></form>'
                    body += '<h2>Replies</h2>' + ''.join('<p>' + html.escape(p['body']) + '</p>' for p in site.oracle()['posts'] if p['parent'] == number)
                    return self.page('Post', body)
                return self.send(404)

            def do_POST(self):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 16000:
                        raise ValueError
                    data = {key: values[0] for key, values in parse_qs(self.rfile.read(length).decode(), keep_blank_values=True).items() if len(values) == 1}
                except (ValueError, UnicodeError):
                    self.record()
                    return self.send(400)
                path = self.record(data)
                if not self.role:
                    return self.send(421)
                if self.role == 'external' and path == '/frame/write':
                    with site._lock:
                        site._counts['external_writes'] += 1
                    return self.page('Visitor receipt', '<p>Visitor note received</p>')
                if self.role == 'identity' and path == '/authorize':
                    with site._lock:
                        valid = data.get('state') in site._flows
                        if valid and data.get('username') == site.username and data.get('password') == site.password:
                            site._flows.remove(data['state'])
                            ticket = secrets.token_urlsafe(24)
                            site._tickets[ticket] = site.username
                        else:
                            ticket = None
                    if not ticket:
                        return self.send(401)
                    return self.send(303, location=site.origin() + '/session/complete?' + urlencode({'ticket': ticket}), cookie='identity_session=' + secrets.token_urlsafe(24))
                if self.role != 'main':
                    return self.send(404)
                if path == '/session':
                    if data.get('username') != site.username or data.get('password') != site.password:
                        return self.send(401)
                    return self.send(303, location='/home', cookie=self.new_session(site.username, 'logins'))
                if path not in ('/session/refresh', '/session/complete', '/read', '/posts', '/replies'):
                    return self.send(404)
                subject = self.subject()
                if not subject:
                    return self.send(401)
                if path in ('/session/refresh', '/session/complete'):
                    return self.send(200, 'Session ready', cookie=self.new_session(subject, 'refreshes' if path.endswith('refresh') else 'background_completions'))
                if path == '/read':
                    with site._lock:
                        site._counts['reads'] += 1
                    return self.page('Search results', '<h1>Search results</h1><p>Welcome to the member board.</p><a href="/home">Board</a>')
                if not data.get('body', '').strip() or not data.get('nonce'):
                    return self.send(400)
                try:
                    parent = int(data['parent']) if path == '/replies' else None
                except (KeyError, ValueError):
                    return self.send(400)
                with site._lock:
                    if parent is not None and not any(p['id'] == parent for p in site._posts):
                        return self.send(400)
                    number = len(site._posts) + 1
                    site._posts.append({'id': number, 'author': subject, 'body': data['body'], 'parent': parent, 'nonce': data['nonce']})
                    site._counts['replies' if parent is not None else 'posts'] += 1
                return self.send(303, location='/posts/' + str(number))

        return Handler

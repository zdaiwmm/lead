"""A disposable HTTP board for native connection/action acceptance.

The browser sees ordinary HTML only. Test code retains the independent request
and post counters; there is no oracle endpoint, pre-recorded method or selector
recipe. Every credential and post in this server is synthetic.
"""
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class BoardState:
    host: str = 'alpha.native-fixture.test'
    username: str = 'fixture-member'
    password: str = 'fixture-password'
    require_otp: bool = False
    drop_next_post_response: bool = False
    wrong_subject: bool = False
    posts: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    sessions: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    lock: object = field(default_factory=threading.Lock)


class NativeBoard:
    def __init__(self, state=None, public_origin=None):
        self.state = state or BoardState()
        self.public_origin = public_origin
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def handler(self):
        board = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def cookie(self, name):
                cookies = dict(part.strip().split('=', 1) for part in self.headers.get('Cookie', '').split(';') if '=' in part)
                return cookies.get(name)

            def subject(self):
                return board.state.sessions.get(self.cookie('board_session'))

            def send(self, status, body='', *, location=None, cookie=None):
                raw = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.send_header('Cache-Control', 'no-store')
                if location:
                    self.send_header('Location', location)
                if cookie:
                    self.send_header('Set-Cookie', cookie+'; HttpOnly; SameSite=Lax; Path=/')
                self.end_headers()
                self.wfile.write(raw)

            def page(self, title, body, status=200):
                self.send(status, '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>'+html.escape(title)+
                    '</title></head><body><header><a href="/home">Member discussion board</a></header><main>'+body+'</main></body></html>')

            def record(self):
                with board.state.lock:
                    board.state.requests.append({'method': self.command, 'path': urlsplit(self.path).path,
                        'started': time.monotonic(), 'thread': threading.get_ident()})
                return urlsplit(self.path).path

            def do_GET(self):
                path = self.record()
                subject = self.subject()
                if path == '/login':
                    if subject:
                        return self.send(303, location='/home')
                    return self.page('Sign in', '<h1>Sign in to your account</h1><form method="post" action="/session">'
                        '<label>Account<input name="username" autocomplete="username" required></label>'
                        '<label>Password<input name="password" type="password" autocomplete="current-password" required></label>'
                        '<button type="submit">Sign in</button></form>')
                if path == '/challenge':
                    return self.page('Verification code required', '<h1>Complete your verification</h1>'
                        '<p>A one-time code is required. The account owner must complete this step.</p>'
                        '<form method="post" action="/challenge"><label>One-time code<input name="code" inputmode="numeric" autocomplete="one-time-code"></label>'
                        '<button type="submit">Verify code</button></form>')
                if path == '/account':
                    if not subject:
                        return self.send(303, location='/login')
                    identity = 'different-fixture-member' if board.state.wrong_subject else subject
                    return self.page('Your account', '<h1>Your signed-in account</h1><p>Account ID: '
                        '<strong id="subject">'+html.escape(identity)+'</strong></p>'
                        '<p>This is your private account settings page, available only after signing in.</p>'
                        '<a rel="me" href="/account">Your account settings</a><a href="/home">Go to the board</a>')
                if path in ('/', '/home'):
                    if not subject:
                        return self.page('Member board', '<h1>Member board</h1><a href="/login">Sign in</a>')
                    with board.state.lock:
                        posts = list(board.state.posts)
                    return self.page('Member board', '<p>Signed in as '+html.escape(subject)+
                        ' · <a rel="me" href="/account">Your account settings</a></p>'
                        '<h1>Community board</h1><section><h2>Membership and rules</h2>'
                        '<p>You are an ordinary member. Members may publish text posts and reply to existing posts. '
                        'Automated browser tools are permitted for these member actions. You have no administrator privileges.</p></section>'
                        '<a id="editor-target" href="/home">Posting to: Community board</a>'
                        '<form method="post" action="/posts"><label>Your post<textarea name="body" required></textarea></label>'
                        '<button type="submit">Publish post</button></form><h2>Existing posts</h2><ul>'+
                        ''.join('<li><a href="/posts/'+str(post['id'])+'">'+html.escape(post['body'])+'</a></li>' for post in posts if not post['parent_url'])+'</ul>')
                if path.startswith('/posts/'):
                    try:
                        number = int(path.removeprefix('/posts/'))
                        post = next(p for p in board.state.posts if p['id'] == number)
                    except (ValueError, StopIteration):
                        return self.page('Missing post', '<h1>Post not found</h1>', 404)
                    original = board.origin+'/posts/'+str(number)
                    replies = [p for p in board.state.posts if p['parent_url'] == original]
                    body = ('<h1>Original post</h1><article><p>Author: <span id="author">'+html.escape(post['author'])+
                    '</span></p><div id="post-body">'+html.escape(post['body'])+'</div><a rel="canonical" href="'+original+'">Original post URL</a></article>')
                    if post['parent_url']:
                        body += '<a id="receipt-parent" href="'+html.escape(post['parent_url'])+'">Parent post</a>'
                    if subject:
                        body += '<p>Signed in as '+html.escape(subject)+' · <a rel="me" href="/account">Your account settings</a></p>'
                        body += '<a id="editor-target" href="/home">Posting to: Community board</a><a id="editor-parent" href="'+original+'">Replying to original post</a>'
                        body += '<form action="/replies" method="post"><input type="hidden" name="parent_url" value="'+original+'">'
                        body += '<label>Your reply<textarea name="body" required></textarea></label><button type="submit">Publish reply</button></form>'
                    body += '<h2>Replies</h2>'+''.join('<p><a href="/posts/'+str(p['id'])+'">'+html.escape(p['body'])+'</a></p>' for p in replies)
                    return self.page('Post '+str(number), body)
                return self.page('Missing page', '<h1>Page not found</h1>', 404)

            def do_POST(self):
                path = self.record()
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 16000:
                    return self.send(413)
                raw = self.rfile.read(length)
                try:
                    if self.headers.get('Content-Type', '').startswith('application/json'):
                        data = json.loads(raw)
                    else:
                        data = {k: v[0] for k, v in parse_qs(raw.decode(), keep_blank_values=True).items() if len(v) == 1}
                except (ValueError, UnicodeError):
                    return self.send(400)
                state = board.state
                if path == '/session':
                    if data.get('username') != state.username or data.get('password') != state.password:
                        return self.page('Sign-in failed', '<h1>Account or password is incorrect</h1><a href="/login">Try signing in again</a>', 401)
                    key = secrets.token_urlsafe(24)
                    if state.require_otp:
                        state.pending[key] = state.username
                        return self.send(303, location='/challenge', cookie='board_pending='+key)
                    state.sessions[key] = state.username
                    return self.send(303, location='/home', cookie='board_session='+key)
                if path == '/challenge':
                    pending = self.cookie('board_pending')
                    if data.get('code') != '135790' or pending not in state.pending:
                        return self.page('Verification failed', '<h1>Code not accepted</h1>', 401)
                    key = secrets.token_urlsafe(24)
                    state.sessions[key] = state.pending.pop(pending)
                    return self.send(303, location='/home', cookie='board_session='+key)
                if path not in ('/posts', '/replies'):
                    return self.send(404)
                subject = self.subject()
                if not subject:
                    return self.send(401)
                expected = {'body', 'parent_url'} if path == '/replies' else {'body'}
                parent = data.get('parent_url')
                if set(data) != expected or not isinstance(data.get('body'), str) or not data['body'].strip():
                    return self.send(400)
                with state.lock:
                    if parent and not any(board.origin+'/posts/'+str(p['id']) == parent for p in state.posts):
                        return self.send(400)
                    number = len(state.posts)+1
                    state.posts.append({'id': number, 'author': subject, 'body': data['body'], 'parent_url': parent})
                    drop = state.drop_next_post_response
                    state.drop_next_post_response = False
                if drop:
                    self.close_connection = True
                    self.connection.close()
                    return
                return self.send(303, location='/posts/'+str(number))
        return Handler

    @property
    def origin(self):
        return self.public_origin or 'http://'+self.state.host+':'+str(self.server.server_port)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def oracle(self):
        with self.state.lock:
            return {'posts': list(self.state.posts), 'requests': list(self.state.requests)}

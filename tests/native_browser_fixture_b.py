"""Independent local, synthetic community site. Python standard library only.

No HTTP route exposes the oracle. Synthetic request records omit passwords,
request bodies, cookies, query strings, and authentication/verification tokens.
"""
from __future__ import annotations

import hmac
import re
import secrets
import socket
import threading
from collections.abc import MutableMapping
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class BlindBoard:
    """A standalone HTTP community with observable, non-idempotent writes.

    `state` may be a mutable dict or an ordinary object with a __dict__.
    Call start(), use origin, and close() when finished. The actual listener is
    always loopback on a random port; public_origin changes the rendered origin.
    Only synthetic credentials belong in this object.
    """

    def __init__(self, state=None, public_origin=None):
        if state is None:
            state = {}
        if not isinstance(state, MutableMapping):
            state = vars(state)
        self.state = state
        defaults = {
            'username': 'marin.meadow',
            'password': 'synthetic-rose-3481',
            'account_id': 'acct-3742-maple',
            'display_name': 'Marin Meadow',
            'require_otp': False,
            'wrong_subject': False,
            'drop_next_post_response': False,
            'posts': [],
            'requests': [],
        }
        for key, value in defaults.items():
            state.setdefault(key, value)
        if not isinstance(state['posts'], list) or not isinstance(state['requests'], list):
            raise TypeError('posts and requests must be lists')
        self._public_origin = self._validate_origin(public_origin)
        self.server = None
        self._thread = None
        self._lock = threading.RLock()
        self._sessions = {}
        self._challenges = {}
        self._next_id = 7001
        self._seed_post = {
            'id': '482', 'kind': 'post', 'parent_id': None,
            'body': 'The harbor garden is opening its seed table on Saturday. '
                    'Can anyone bring spare seed trays? Please reply here so we can plan the table.',
            'author': 'garden-liaison', 'account_id': 'acct-4820-wren',
            'display_name': 'Wren Collins', 'created_at': '2026-11-12T09:20:00+00:00',
        }

    @staticmethod
    def _validate_origin(origin):
        if origin is None:
            return None
        parsed = urlsplit(str(origin))
        if (parsed.scheme != 'http' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise ValueError('public_origin must be a plain http origin')
        return f'http://{parsed.netloc}'

    @property
    def origin(self):
        if self._public_origin:
            return self._public_origin
        if self.server is None:
            raise RuntimeError('Call start() before using origin')
        return f'http://127.0.0.1:{self.server.server_port}'

    @property
    def target_url(self):
        return self._url('/bulletin/482')

    def start(self):
        if self.server is not None:
            return self
        board = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'
            server_version = 'HarborCommons/1.0'
            sys_version = ''

            def log_message(self, *_args):
                pass

            def do_GET(self):
                board._serve(self, 'GET')

            def do_POST(self):
                board._serve(self, 'POST')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=2)
            self.server = None
            self._thread = None

    def oracle(self):
        """Return an independent Python-only snapshot of committed writes and requests."""
        with self._lock:
            return deepcopy({'posts': self.state['posts'], 'requests': self.state['requests']})

    def _url(self, path):
        return self.origin + path

    @staticmethod
    def _h(value):
        return escape(str(value), quote=True)

    def _serve(self, handler, method):
        path = urlsplit(handler.path).path
        record = {'method': method, 'path': path, 'status': 0, 'outcome': 'pending'}
        handler.board_record = record
        try:
            if method == 'GET':
                self._get(handler, path)
            else:
                self._post(handler, path)
        except (BrokenPipeError, ConnectionResetError):
            if record['outcome'] != 'response_dropped':
                record['outcome'] = 'client_disconnected'
        except Exception as error:
            record['outcome'] = 'internal_error'
            record['error_type'] = type(error).__name__
            if record['status'] == 0:
                self._send(handler, 500, self._page('Please try again', '<h1>The page could not be completed.</h1>'))
        finally:
            with self._lock:
                self.state['requests'].append(deepcopy(record))

    def _send(self, handler, status, html, cookie=None, location=None):
        handler.board_record['status'] = status
        if handler.board_record['outcome'] == 'pending':
            handler.board_record['outcome'] = 'served'
        payload = html.encode('utf-8')
        handler.send_response(status)
        handler.send_header('Content-Type', 'text/html; charset=utf-8')
        handler.send_header('Content-Length', str(len(payload)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Referrer-Policy', 'same-origin')
        if cookie:
            handler.send_header('Set-Cookie', cookie)
        if location:
            handler.send_header('Location', self._url(location))
        handler.end_headers()
        handler.wfile.write(payload)

    def _redirect(self, handler, path, cookie=None):
        self._send(handler, 303, self._page('Continue', f'<p><a href="{self._h(self._url(path))}">Continue</a></p>'), cookie, path)

    @staticmethod
    def _cookie(handler, name):
        parsed = SimpleCookie()
        try:
            parsed.load(handler.headers.get('Cookie', ''))
        except CookieError:
            return None
        return parsed[name].value if name in parsed else None

    def _session(self, handler):
        token = self._cookie(handler, 'hc_member')
        with self._lock:
            return self._sessions.get(token)

    def _require(self, handler):
        session = self._session(handler)
        if session is None:
            self._redirect(handler, '/access')
        return session

    def _subject(self):
        if self.state['wrong_subject']:
            return {'username': 'rory.river', 'account_id': 'acct-6951-river', 'display_name': 'Rory River', 'role': 'Member'}
        return {key: self.state[key] for key in ('username', 'account_id', 'display_name')} | {'role': 'Member'}

    def _open_session(self, handler, subject):
        token = secrets.token_urlsafe(25)
        with self._lock:
            self._sessions[token] = {'subject': subject, 'posting_key': secrets.token_urlsafe(24)}
        self._redirect(handler, '/desk', f'hc_member={token}; Path=/; HttpOnly; SameSite=Lax')

    def _page(self, title, content, session=None):
        account = ''
        if session:
            subject = session['subject']
            account = (f'<span>Signed in as {self._h(subject["username"])}</span> '
                       f'<a rel="me" href="{self._h(self._url("/desk/account"))}">My account</a> '
                       f'<a href="{self._h(self._url("/desk"))}">Member desk</a>')
        else:
            account = f'<a href="{self._h(self._url("/access"))}">Member sign in</a>'
        return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{self._h(title)} · Harbor Commons</title><style>
:root{{color-scheme:light;font-family:Georgia,serif;color:#1d2937;background:#f5f2e9;line-height:1.6}}
body{{margin:0}}a{{color:#164c83;text-underline-offset:3px}}a:focus,button:focus,input:focus,textarea:focus{{outline:3px solid #c88627;outline-offset:3px}}
header{{background:#e8efee;border-bottom:1px solid #bdcdc9;padding:22px max(24px,calc((100vw - 1020px)/2))}}
.brand{{font:700 27px Georgia,serif;letter-spacing:-.5px;color:#173c43;text-decoration:none}}nav{{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px;font:15px/1.5 system-ui,sans-serif}}
nav span{{color:#43535b}}main{{max-width:1020px;margin:32px auto;padding:0 24px 48px}}h1{{font-size:34px;line-height:1.2;margin:0 0 18px}}h2{{font-size:24px;line-height:1.3}}h3{{font-size:20px}}
.grid{{display:grid;grid-template-columns:minmax(0,2fr) minmax(220px,1fr);gap:32px}}section,aside{{min-width:0}}.panel,article{{background:#fffdf7;border:1px solid #cdd3ca;padding:22px;margin:18px 0;border-radius:4px}}
article h2,article h3{{margin-top:0}}address{{font:14px/1.7 system-ui,sans-serif;font-style:normal;color:#49545c}}.copy{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:18px}}
.small,dt,button,label,input,textarea,.notice{{font-family:system-ui,sans-serif}}.small{{font-size:14px;color:#46535b}}.notice{{padding:14px 18px;border-left:4px solid #28645e;background:#e8f1e9}}
label{{display:block;font-weight:650;margin:16px 0 7px}}input:not([type=hidden]),textarea{{display:block;width:100%;box-sizing:border-box;padding:11px;border:1px solid #7b8d91;border-radius:3px;font-size:16px;background:white;color:#1d2937}}
textarea{{min-height:150px;resize:vertical}}button{{background:#1d5960;color:white;border:0;border-radius:3px;padding:11px 19px;font-weight:650;font-size:16px;margin-top:17px;cursor:pointer}}
dl{{display:grid;grid-template-columns:145px 1fr;gap:12px}}dt{{font-weight:650}}dd{{margin:0;overflow-wrap:anywhere}}.error{{color:#8b2424;border-left-color:#8b2424;background:#fff1ec}}
footer{{border-top:1px solid #cdd3ca;max-width:1020px;margin:0 auto;padding:22px 24px;font:14px/1.6 system-ui,sans-serif}}@media(max-width:720px){{.grid{{display:block}}h1{{font-size:29px}}dl{{display:block}}dd{{margin:0 0 14px}}}}
</style></head><body><header><a class="brand" href="{self._h(self._url('/'))}">Harbor Commons</a>
<nav aria-label="Community"><a href="{self._h(self._url('/commons'))}">Community board</a><a href="{self._h(self._url('/house-rules'))}">Community rules</a>{account}</nav></header>
<main>{content}</main><footer>Harbor Commons · A place for neighbors to share practical news. <a href="{self._h(self._url('/house-rules'))}">Current member rules</a></footer></body></html>'''

    def _get(self, handler, path):
        session = self._session(handler)
        if path == '/':
            body = f'''<div class="grid"><section><p class="small">HARBOR NEIGHBORS</p><h1>Good things start with a conversation.</h1>
<p class="copy">Find local plans, share an update, or lend a hand to a neighbor.</p><p><a href="{self._h(self._url('/commons'))}">Enter the community board →</a></p></section>
<aside class="panel"><h2>Already a member?</h2><p>Sign in to use your member desk, publish a note, and join a conversation.</p><a href="{self._h(self._url('/access'))}">Open member sign in</a></aside></div>'''
            with self._lock:
                latest = [deepcopy(p) for p in self.state['posts'] if p['kind'] == 'post']
            body += '<section aria-label="Latest community notes"><h2>Latest community notes</h2>'
            body += ''.join(self._article(p) for p in reversed(latest)) + self._article(self._seed_post) + '</section>'
            self._send(handler, 200, self._page('Welcome', body, session))
        elif path == '/access':
            if session:
                self._redirect(handler, '/desk')
            else:
                self._send(handler, 200, self._login_page())
        elif path == '/access/verify':
            challenge = self._cookie(handler, 'hc_challenge')
            with self._lock:
                known = challenge in self._challenges
            if not known:
                self._redirect(handler, '/access')
            else:
                self._send(handler, 200, self._otp_page())
        elif path == '/house-rules':
            self._send(handler, 200, self._page('Current community rules', self._rules(), session))
        elif path == '/commons':
            with self._lock:
                posts = [deepcopy(p) for p in self.state['posts'] if p['kind'] == 'post']
                replies = [deepcopy(p) for p in self.state['posts'] if p['kind'] == 'reply']
            cards = ''.join(self._article(p) for p in reversed(posts)) + self._article(self._seed_post)
            recent = ''.join(self._article(p, small=True) for p in reversed(replies)) or '<p>No replies yet.</p>'
            compose = '/desk' if session else '/access'
            body = f'''<h1>Community board</h1><p>News, requests, and neighborly conversation.</p><div class="grid"><section>{cards}</section>
<aside><div class="panel"><h2>Share something</h2><p>Regular members may publish and reply.</p><a href="{self._h(self._url(compose))}">Open your member desk</a></div><h2>Recent replies</h2>{recent}</aside></div>'''
            self._send(handler, 200, self._page('Community board', body, session))
        elif path == '/desk':
            session = self._require(handler)
            if session:
                subject = session['subject']
                body = f'''<h1>Your member desk</h1><p>Welcome, {self._h(subject['display_name'])}. You are using a regular member account.</p>
<div class="grid"><section class="panel"><h2>Publish a community note</h2><form method="post" action="{self._h(self._url('/desk/postbox'))}">
<input type="hidden" name="posting_key" value="{self._h(session['posting_key'])}"><label for="community-words">Your note</label>
<textarea id="community-words" name="entry_words" required maxlength="6000" rows="7"></textarea><button type="submit">Publish note</button></form></section>
<aside class="panel"><h2>Member permissions</h2><p>Members can publish notes and reply to existing topics, including through authorized browser automation.</p>
<p><a href="{self._h(self._url('/house-rules'))}">Read current community rules</a></p><p><a href="{self._h(self._url('/commons'))}">Read the community board</a></p></aside></div>'''
                self._send(handler, 200, self._page('Member desk', body, session))
        elif path == '/desk/account':
            session = self._require(handler)
            if session:
                subject = session['subject']
                body = f'''<h1>My account</h1><p>This private page identifies the account currently signed in.</p><section class="panel"><h2>Account details</h2><dl>
<dt>Account ID</dt><dd>{self._h(subject['account_id'])}</dd><dt>Username</dt><dd>{self._h(subject['username'])}</dd>
<dt>Display name</dt><dd>{self._h(subject['display_name'])}</dd><dt>Membership</dt><dd>Member</dd></dl></section>
<section class="panel"><h2>What this account may do</h2><p>Regular members may publish community notes, reply to topics, and use authorized browser automation for those actions.</p>
<p><a href="{self._h(self._url('/house-rules'))}">Current community rules</a></p></section>'''
                self._send(handler, 200, self._page('My account', body, session))
        elif re.fullmatch(r'/bulletin/[0-9]+', path):
            post_id = path.rsplit('/', 1)[-1]
            post = self._find_top(post_id)
            if not post:
                self._not_found(handler, session)
                return
            with self._lock:
                replies = [deepcopy(p) for p in self.state['posts'] if p['parent_id'] == post_id]
            receipt = ''
            if post_id != self._seed_post['id']:
                receipt = f'<p class="notice" role="status">Publication receipt · Post {self._h(post_id)} is published. <a href="{self._h(self._post_url(post))}">Permanent post URL</a></p>'
            query = parse_qs(urlsplit(handler.path).query)
            reply_id = query.get('reply', [''])[0]
            if any(p['id'] == reply_id for p in replies):
                reply_url = self._url(f'/bulletin/{post_id}#reply-{reply_id}')
                receipt += f'<p class="notice" role="status">Reply receipt · Your reply is published in this conversation. <a href="{self._h(reply_url)}">Permanent reply URL</a></p>'
            if session:
                respond = f'''<section class="panel"><h2>Join this conversation</h2><form method="post" action="{self._h(self._url('/discussion/replies'))}">
<input type="hidden" name="posting_key" value="{self._h(session['posting_key'])}"><input type="hidden" name="in_reply_to" value="{self._h(post_id)}">
<label for="reply-words">Your reply to post {self._h(post_id)}</label><textarea id="reply-words" name="response_words" rows="5" maxlength="6000" required></textarea><button type="submit">Publish reply</button></form></section>'''
            else:
                respond = f'<p><a href="{self._h(self._url("/access"))}">Sign in to reply to this topic</a></p>'
            body = '<h1>Community conversation</h1>' + receipt + self._article(post) + '<h2>Replies</h2>' + (''.join(self._article(p) for p in replies) or '<p>No replies yet. You can start the conversation.</p>') + respond
            self._send(handler, 200, self._page(f'Conversation {post_id}', body, session))
        elif re.fullmatch(r'/members/[a-zA-Z0-9_-]+', path):
            account_id = path.rsplit('/', 1)[-1]
            members = [self._seed_post, self._subject()]
            member = next((p for p in members if p['account_id'] == account_id), None)
            if member:
                self._send(handler, 200, self._page('Member profile', f'<h1>{self._h(member["display_name"])}</h1><p>Community member · {self._h(member["author"] if "author" in member else member["username"])}</p>', session))
            else:
                self._not_found(handler, session)
        else:
            self._not_found(handler, session)

    def _rules(self):
        return '''<h1>Current community rules</h1><p class="small">In effect since 12 November 2026 · Version 4</p><section class="panel"><h2>For regular members</h2>
<p>A regular member may publish community notes and reply to existing topics using their own signed-in account. Both actions are available to ordinary members.</p>
<p>Members may use an authorized browser assistant or browser automation to sign in, read pages, publish notes, and reply on their behalf. Automation has the same member permissions as the person who authorizes it.</p>
<p>Keep posts relevant, courteous, and accurate. Check your signed-in account before publishing. A published note or reply appears on the community board with its author and permanent URL.</p></section>'''

    def _login_page(self, error=''):
        notice = f'<p role="alert" class="notice error">{self._h(error)}</p>' if error else ''
        return self._page('Member sign in', f'''<div class="grid"><section class="panel"><h1>Member sign in</h1>{notice}<form method="post" action="{self._h(self._url('/access/password'))}">
<label for="member-name">Username</label><input id="member-name" name="member_name" type="text" autocomplete="username" required>
<label for="member-secret">Password</label><input id="member-secret" name="member_secret" type="password" autocomplete="current-password" required>
<button type="submit">Sign in</button></form></section><aside><h2>Welcome back</h2><p>Your member desk holds your account details and a place to compose a note.</p></aside></div>''')

    def _otp_page(self, error=''):
        notice = f'<p role="alert" class="notice error">{self._h(error)}</p>' if error else ''
        return self._page('Verify your sign in', f'''<section class="panel"><h1>Verify your sign in</h1><p>Enter the six-digit code from your authenticator to finish signing in to your own account.</p>{notice}
<form method="post" action="{self._h(self._url('/access/verify'))}"><label for="verification-digits">Authentication code</label><input id="verification-digits" name="verification_digits" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}" maxlength="6" required><button type="submit">Verify and continue</button></form></section>''')

    def _post_url(self, post):
        if post['kind'] == 'reply':
            return self._url(f'/bulletin/{post["parent_id"]}#reply-{post["id"]}')
        return self._url(f'/bulletin/{post["id"]}')

    def _article(self, post, small=False):
        post_url = self._post_url(post)
        heading = 'Reply' if post['kind'] == 'reply' else 'Community note'
        tag = 'h3' if small else 'h2'
        anchor = f'reply-{post["id"]}' if post['kind'] == 'reply' else f'note-{post["id"]}'
        parent = f'<p class="small">In reply to <a href="{self._h(self._url("/bulletin/" + post["parent_id"]))}">post {self._h(post["parent_id"])}</a></p>' if post['parent_id'] else ''
        return f'''<article id="{self._h(anchor)}" aria-labelledby="heading-{self._h(anchor)}"><{tag} id="heading-{self._h(anchor)}">{heading} {self._h(post['id'])}</{tag}>
<address>By <a href="{self._h(self._url('/members/' + post['account_id']))}">{self._h(post['author'])}</a> · {self._h(post['display_name'])}<br>Account ID: {self._h(post['account_id'])}</address>{parent}
<p class="copy">{self._h(post['body'])}</p><p class="small"><time datetime="{self._h(post['created_at'])}">{self._h(post['created_at'])}</time> · <a rel="bookmark" href="{self._h(post_url)}">Permanent URL</a></p></article>'''

    def _find_top(self, post_id):
        if post_id == self._seed_post['id']:
            return deepcopy(self._seed_post)
        with self._lock:
            return next((deepcopy(p) for p in self.state['posts'] if p['id'] == post_id and p['kind'] == 'post'), None)

    def _not_found(self, handler, session=None):
        self._send(handler, 404, self._page('Page not found', '<h1>Page not found</h1><p>This community page does not exist.</p>', session))

    def _fields(self, handler):
        if handler.headers.get_content_type() != 'application/x-www-form-urlencoded':
            self._send(handler, 415, self._page('Form required', '<h1>Please use the community form.</h1>'))
            return None
        try:
            length = int(handler.headers.get('Content-Length', '0'))
            if length < 0 or length > 32768:
                raise ValueError('invalid length')
            raw = handler.rfile.read(length).decode('utf-8', 'strict')
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=12, errors='strict')
            if any(len(values) != 1 for values in parsed.values()):
                raise ValueError('multiple values')
            return {key: values[0] for key, values in parsed.items()}
        except (ValueError, UnicodeError):
            self._send(handler, 400, self._page('Invalid form', '<h1>The form could not be read.</h1>'))
            return None

    def _post(self, handler, path):
        if path == '/access/password':
            fields = self._fields(handler)
            if fields is None:
                return
            accepted = (hmac.compare_digest(fields.get('member_name', ''), str(self.state['username']))
                        and hmac.compare_digest(fields.get('member_secret', ''), str(self.state['password'])))
            handler.board_record['accepted'] = accepted
            if not accepted:
                self._send(handler, 401, self._login_page('The username or password was not accepted.'))
                return
            subject = self._subject()
            if self.state['require_otp']:
                challenge = secrets.token_urlsafe(24)
                with self._lock:
                    self._challenges[challenge] = subject
                self._redirect(handler, '/access/verify', f'hc_challenge={challenge}; Path=/access; HttpOnly; SameSite=Lax')
            else:
                self._open_session(handler, subject)
            return
        if path == '/access/verify':
            fields = self._fields(handler)
            if fields is None:
                return
            challenge = self._cookie(handler, 'hc_challenge')
            with self._lock:
                subject = self._challenges.get(challenge)
            if subject is None:
                self._redirect(handler, '/access')
            elif not hmac.compare_digest(fields.get('verification_digits', ''), '135790'):
                handler.board_record['accepted'] = False
                self._send(handler, 401, self._otp_page('That code was not accepted. Please try again.'))
            else:
                with self._lock:
                    self._challenges.pop(challenge, None)
                handler.board_record['accepted'] = True
                self._open_session(handler, subject)
            return
        if path not in ('/desk/postbox', '/discussion/replies'):
            self._not_found(handler, self._session(handler))
            return
        session = self._require(handler)
        if session is None:
            return
        fields = self._fields(handler)
        if fields is None:
            return
        if not hmac.compare_digest(fields.get('posting_key', ''), session['posting_key']):
            self._send(handler, 403, self._page('Form expired', '<h1>Please reopen the member form.</h1>', session))
            return
        is_reply = path == '/discussion/replies'
        body = fields.get('response_words' if is_reply else 'entry_words', '')
        if not body.strip() or len(body) > 6000:
            self._send(handler, 400, self._page('Check your message', '<h1>Please write a message of 1 to 6000 characters.</h1>', session))
            return
        parent_id = fields.get('in_reply_to') if is_reply else None
        if is_reply and not self._find_top(parent_id):
            self._not_found(handler, session)
            return
        with self._lock:
            post_id = str(self._next_id)
            self._next_id += 1
            subject = session['subject']
            post = {
                'id': post_id, 'kind': 'reply' if is_reply else 'post', 'parent_id': parent_id,
                'body': body, 'author': subject['username'], 'account_id': subject['account_id'],
                'display_name': subject['display_name'], 'created_at': datetime.now(timezone.utc).isoformat(),
            }
            post['url'] = self._post_url(post)
            self.state['posts'].append(post)
            drop = bool(self.state['drop_next_post_response'])
            if drop:
                self.state['drop_next_post_response'] = False
            handler.board_record.update({'accepted': True, 'created_post_id': post_id, 'parent_id': parent_id})
        if drop:
            handler.board_record['outcome'] = 'response_dropped'
            handler.board_record['status'] = 0
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handler.connection.close()
            return
        location = f'/bulletin/{parent_id}?reply={post_id}#reply-{post_id}' if is_reply else f'/bulletin/{post_id}'
        self._redirect(handler, location)

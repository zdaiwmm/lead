from __future__ import annotations

import http.client
import importlib.util
import json
import re
import unittest
from pathlib import Path
from urllib.parse import urlencode, urlsplit

MODULE = Path(__file__).with_name('native_browser_fixture_b.py')


def request(board, method, path, fields=None, cookie=''):
    connection = http.client.HTTPConnection('127.0.0.1', board.server.server_port, timeout=3)
    headers = {'Host': urlsplit(board.origin).netloc}
    if cookie:
        headers['Cookie'] = cookie
    body = None
    if fields is not None:
        body = urlencode(fields)
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read().decode('utf-8'))
        return result
    finally:
        connection.close()


def value(html, name):
    found = re.search(r'<input\b[^>]*name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', html)
    assert found, name
    return found.group(1)


def login(board, expect_otp=False):
    response = request(board, 'POST', '/access/password', {'member_name': board.state['username'], 'member_secret': board.state['password']})
    assert response[0] == 303
    cookie = response[1]['Set-Cookie'].split(';', 1)[0]
    assert 'HttpOnly' in response[1]['Set-Cookie']
    if expect_otp:
        assert response[1]['Location'].endswith('/access/verify')
    else:
        assert response[1]['Location'].endswith('/desk')
    return cookie


class SyntheticBoardSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert MODULE.is_file(), 'Independent board implementation is not present yet'
        spec = importlib.util.spec_from_file_location('independent_board_b', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.Board = module.BlindBoard

    def test_cookie_identity_real_non_idempotent_publish_and_parented_reply(self):
        board = self.Board().start()
        self.addCleanup(board.close)
        self.assertNotIn('rel="me"', request(board, 'GET', '/')[2])
        self.assertEqual(request(board, 'GET', '/desk/account')[0], 303)
        cookie = login(board)
        hub = request(board, 'GET', '/desk', cookie=cookie)[2]
        self.assertIn('rel="me"', hub)
        account = request(board, 'GET', '/desk/account', cookie=cookie)[2]
        self.assertIn(board.state['account_id'], account)
        self.assertIn('Member', account)
        posting_key = value(hub, 'posting_key')
        fields = {'posting_key': posting_key, 'entry_words': 'A quiet community update: tea by the harbor.'}
        first = request(board, 'POST', '/desk/postbox', fields, cookie)
        second = request(board, 'POST', '/desk/postbox', fields, cookie)
        self.assertEqual((first[0], second[0]), (303, 303))
        self.assertNotEqual(first[1]['Location'], second[1]['Location'])
        published = request(board, 'GET', urlsplit(first[1]['Location']).path, cookie=cookie)[2]
        self.assertIn(fields['entry_words'], published)
        self.assertIn(board.state['username'], published)
        target = request(board, 'GET', '/bulletin/482', cookie=cookie)[2]
        self.assertIn('garden-liaison', target)
        self.assertEqual(value(target, 'in_reply_to'), '482')
        reply_words = 'I can bring spare seed trays on Saturday.'
        reply = request(board, 'POST', '/discussion/replies', {'posting_key': value(target, 'posting_key'), 'in_reply_to': '482', 'response_words': reply_words}, cookie)
        self.assertEqual(reply[0], 303)
        self.assertIn('/bulletin/482?', reply[1]['Location'])
        self.assertIn(reply_words, request(board, 'GET', '/bulletin/482', cookie=cookie)[2])
        oracle = board.oracle()
        self.assertEqual(len(oracle['posts']), 3)
        self.assertEqual([p['parent_id'] for p in oracle['posts']], [None, None, '482'])
        self.assertEqual(oracle['posts'][2]['body'], reply_words)
        self.assertEqual(oracle['posts'][2]['author'], board.state['username'])
        self.assertIn('#reply-', oracle['posts'][2]['url'])
        self.assertNotIn(board.state['password'], json.dumps(oracle))
        self.assertTrue(all('body' not in r and 'cookie' not in r for r in oracle['requests']))
        oracle['posts'][0]['body'] = 'modified snapshot'
        self.assertEqual(board.oracle()['posts'][0]['body'], fields['entry_words'])
        self.assertEqual(request(board, 'GET', '/oracle', cookie=cookie)[0], 404)

    def test_dropped_publish_and_reply_commit_once_then_dom_recovery(self):
        board = self.Board().start()
        self.addCleanup(board.close)
        cookie = login(board)
        hub = request(board, 'GET', '/desk', cookie=cookie)[2]
        board.state['drop_next_post_response'] = True
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError)):
            request(board, 'POST', '/desk/postbox', {'posting_key': value(hub, 'posting_key'), 'entry_words': 'This notice survives an interrupted connection.'}, cookie)
        self.assertEqual(len(board.oracle()['posts']), 1)
        self.assertFalse(board.state['drop_next_post_response'])
        self.assertIn('This notice survives an interrupted connection.', request(board, 'GET', '/commons', cookie=cookie)[2])
        self.assertIn('This notice survives an interrupted connection.', request(board, 'GET', '/', cookie=cookie)[2])
        target = request(board, 'GET', '/bulletin/482', cookie=cookie)[2]
        board.state['drop_next_post_response'] = True
        with self.assertRaises((http.client.RemoteDisconnected, ConnectionResetError)):
            request(board, 'POST', '/discussion/replies', {'posting_key': value(target, 'posting_key'), 'in_reply_to': '482', 'response_words': 'The response survives its interrupted connection.'}, cookie)
        self.assertEqual(len(board.oracle()['posts']), 2)
        self.assertIn('The response survives its interrupted connection.', request(board, 'GET', '/bulletin/482', cookie=cookie)[2])
        self.assertEqual(sum(r.get('outcome') == 'response_dropped' for r in board.oracle()['requests']), 2)

    def test_otp_requires_real_code_before_member_access(self):
        board = self.Board({'require_otp': True}).start()
        self.addCleanup(board.close)
        pending = login(board, expect_otp=True)
        self.assertEqual(request(board, 'GET', '/desk', cookie=pending)[0], 303)
        self.assertEqual(request(board, 'POST', '/access/verify', {'verification_digits': '000000'}, pending)[0], 401)
        accepted = request(board, 'POST', '/access/verify', {'verification_digits': '135790'}, pending)
        self.assertEqual(accepted[0], 303)
        self.assertIn('HttpOnly', accepted[1]['Set-Cookie'])
        cookie = accepted[1]['Set-Cookie'].split(';', 1)[0]
        self.assertEqual(request(board, 'GET', '/desk/account', cookie=cookie)[0], 200)
        self.assertNotIn('135790', json.dumps(board.oracle()))

    def test_wrong_subject_is_visible_on_private_account(self):
        board = self.Board({'wrong_subject': True}).start()
        self.addCleanup(board.close)
        cookie = login(board)
        account = request(board, 'GET', '/desk/account', cookie=cookie)[2]
        self.assertIn('acct-6951-river', account)
        self.assertNotIn(board.state['account_id'], account)
        self.assertNotIn('rel="me"', request(board, 'GET', '/')[2])

    def test_virtual_public_origin_and_rejected_writes_have_no_mutation(self):
        board = self.Board(public_origin='http://harbor.example.test:80').start()
        self.addCleanup(board.close)
        self.assertEqual(board.origin, 'http://harbor.example.test:80')
        self.assertIn('http://harbor.example.test:80/access', request(board, 'GET', '/')[2])
        self.assertEqual(request(board, 'POST', '/desk/postbox', {'entry_words': 'unauthorized'})[0], 303)
        self.assertEqual(request(board, 'POST', '/access/password', {'member_name': board.state['username'], 'member_secret': 'wrong synthetic password'})[0], 401)
        cookie = login(board)
        self.assertEqual(request(board, 'POST', '/desk/postbox', {'posting_key': 'invalid', 'entry_words': 'forbidden'}, cookie)[0], 403)
        self.assertEqual(len(board.oracle()['posts']), 0)
        self.assertNotIn('wrong synthetic password', json.dumps(board.oracle()))


if __name__ == '__main__':
    unittest.main(verbosity=2)

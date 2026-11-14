"""Independent fixture oracle checks, not product acceptance claims."""
import time

import httpx
import pytest
from native_browser_fixture import BoardState, NativeBoard


def test_fixture_requires_login_and_records_real_parent_and_dropped_response():
    board=NativeBoard().start()
    try:
        base='http://127.0.0.1:'+str(board.server.server_port)
        with httpx.Client(base_url=base,trust_env=False,timeout=2) as client:
            assert client.get('/account').status_code==303
            assert client.post('/posts',data={'body':'unauthorized'}).status_code==401
            login=client.post('/session',data={'username':board.state.username,'password':board.state.password})
            assert login.status_code==303 and 'HttpOnly' in login.headers['set-cookie']
            assert 'rel="me"' in client.get('/home').text
            assert board.state.username in client.get('/account').text
            post=client.post('/posts',data={'body':'Original synthetic post'})
            assert post.headers['location']=='/posts/1'
            board.state.drop_next_post_response=True
            with pytest.raises(httpx.HTTPError):
                client.post('/replies',data={'body':'Synthetic reply','parent_url':board.origin+'/posts/1'})
            oracle=board.oracle()
            assert len(oracle['posts'])==2
            reply=oracle['posts'][1]
            assert reply['parent_url']==board.origin+'/posts/1'
            assert reply['author']==board.state.username
            receipt=client.get('/posts/2')
            assert reply['body'] in receipt.text and 'Parent post' in receipt.text
            assert client.get('/__oracle__').status_code==404
            assert board.state.password not in str(oracle['requests'])
    finally:
        board.close()


def test_fixture_owner_challenge_does_not_create_authenticated_session_early():
    board=NativeBoard(BoardState(require_otp=True)).start()
    try:
        with httpx.Client(base_url='http://127.0.0.1:'+str(board.server.server_port),trust_env=False,timeout=2) as client:
            response=client.post('/session',data={'username':board.state.username,'password':board.state.password})
            assert response.headers['location']=='/challenge'
            assert client.get('/account').status_code==303
            assert client.post('/challenge',data={'code':'wrong'}).status_code==401
            assert client.post('/challenge',data={'code':'135790'}).status_code==303
            assert client.get('/account').status_code==200
            assert not board.oracle()['posts']
    finally:
        board.close()


def test_fixture_warm_login_exposes_fresh_current_and_independent_private_subject():
    board=NativeBoard().start()
    try:
        with httpx.Client(base_url='http://127.0.0.1:'+str(board.server.server_port),trust_env=False,timeout=2) as client:
            assert client.get('/login').status_code==200
            assert client.post('/session',data={'username':board.state.username,'password':board.state.password}).status_code==303
            login=client.get('/login')
            assert login.status_code==303 and login.headers['location']=='/home'
            current=client.get('/home')
            assert 'Signed in as '+board.state.username in current.text and 'rel="me" href="/account"' in current.text
            private=client.get('/account')
            assert 'Account ID:' in private.text and '<strong id="subject">'+board.state.username+'</strong>' in private.text
            board.state.wrong_subject=True
            assert 'different-fixture-member' in client.get('/account').text
            assert 'Signed in as '+board.state.username in client.get('/home').text
            assert [row['path'] for row in board.oracle()['requests'] if row['method']=='POST']==['/session']
    finally:
        board.close()


@pytest.mark.parametrize('persistent', (False, True))
def test_fixture_cookie_persistence_requires_explicit_opt_in(persistent):
    state=BoardState()
    state.persistent_session=persistent
    board=NativeBoard(state).start()
    try:
        with httpx.Client(base_url='http://127.0.0.1:'+str(board.server.server_port),trust_env=False,timeout=2) as client:
            assert client.post('/session',data={'username':state.username,'password':state.password}).status_code==303
            cookie=next(item for item in client.cookies.jar if item.name=='board_session')
            session_only=cookie.discard
            assert session_only is not persistent
            assert cookie.expires > time.time() if persistent else cookie.expires is None
    finally:
        board.close()

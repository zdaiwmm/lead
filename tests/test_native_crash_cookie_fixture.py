"""The crash fixture must not kill Chrome before its own cookie is durable."""
import sqlite3

import pytest
from test_native_chrome_crash_product import committed_session_hosts


@pytest.mark.parametrize('change', ('none', 'session', 'no_expiry', 'wrong_token', 'wrong_host', 'wrong_path', 'wrong_expiry', 'empty_value', 'missing_host'))
def test_crash_readiness_rejects_nonpersistent_or_unrelated_cookie(tmp_path, change):
    cookie={'domain':'one.fixture.test','path':'/','value':'fixture-only-token','expires':2_000_000_000}
    row=['one.fixture.test','/',1,1,13_644_473_600_000_000,'fixture-only-token',b'']
    updates={'session':(3,0), 'no_expiry':(2,0), 'wrong_token':(5,'other-fixture-token'),
        'wrong_host':(0,'other.fixture.test'), 'wrong_path':(1,'/other'),
        'wrong_expiry':(4,1), 'empty_value':(5,'')}
    if change not in ('none','missing_host'):
        index,value=updates[change]
        row[index]=value
    database=tmp_path/'Default/Cookies'
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE cookies(name,host_key,path,has_expires,is_persistent,expires_utc,value,encrypted_value)')
        connection.execute('INSERT INTO cookies VALUES(?,?,?,?,?,?,?,?)',('board_session',*row))
    cookies=[cookie]
    if change=='missing_host':
        cookies.append({**cookie,'domain':'missing.fixture.test'})
    assert committed_session_hosts(tmp_path,cookies) == ({'one.fixture.test'} if change=='none' else set())


def test_crash_readiness_never_creates_a_missing_cookie_store(tmp_path):
    cookie={'domain':'missing.fixture.test','path':'/','value':'fixture-only-token','expires':2_000_000_000}
    assert committed_session_hosts(tmp_path,[cookie]) == set()
    assert list(tmp_path.iterdir()) == []

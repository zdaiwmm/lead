"""Bounded control and temporary credentials on the original ConnectionRef.

The encrypted login belongs to one connection, never a model context or reusable
credential registry. A phase/version change invalidates old worker commands.
"""
import hashlib
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .config import CONFIG
from .db import j, one, require_agent, tx
from .scope import current_tenant


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: SecretStr = Field(min_length=1, max_length=500)
    password: SecretStr = Field(min_length=1, max_length=2000)


def _cipher():
    return AESGCM(hashlib.sha256(('lead-connection:'+CONFIG['session_secret']).encode()).digest())


def _aad(aid, connection):
    return (current_tenant()+':'+str(aid)+':'+str(connection)).encode()


def seal(aid, connection, login):
    if not login:
        return None
    login=LoginInput.model_validate(login)
    raw={key:getattr(login,key).get_secret_value() for key in ('username','password')}
    nonce=secrets.token_bytes(12)
    return nonce+_cipher().encrypt(nonce,json.dumps(raw).encode(),_aad(aid,connection))


def unseal(aid, connection, encrypted):
    if not encrypted:
        return None
    encrypted=bytes(encrypted)
    return json.loads(_cipher().decrypt(encrypted[:12],encrypted[12:],_aad(aid,connection)))


def transition(aid, connection, expected_version, mode, *, reason=None, cancel=False):
    """Only the current challenge can resume; delayed answers never revive it."""
    if mode not in ('automatic','owner'):
        raise ValueError('connection_control_invalid')
    with tx(aid) as c:
        agent=require_agent(c,aid,True)
        row=one(c,"SELECT * FROM lead.takeover_sessions WHERE id=%s AND purpose='login' FOR UPDATE",(connection,))
        if (not row or row['state']!='active' or row['control_version']!=agent['control_version']
                or row['challenge_version']!=expected_version):
            raise ValueError('connection_challenge_changed')
        from .growth.contracts import utcnow
        if row['expires_at']<=utcnow():
            c.execute("UPDATE lead.takeover_sessions SET state='expired',ended_at=now(),encrypted_login=NULL WHERE id=%s",(connection,))
            return {'state':'expired','challenge_version':row['challenge_version']}
        if cancel:
            c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now(),encrypted_login=NULL,challenge_version=challenge_version+1 WHERE id=%s",(connection,))
            return {'state':'ended','challenge_version':row['challenge_version']+1}
        if mode=='automatic' and row['automatic_attempts']>=3:
            raise ValueError('connection_attempt_limit')
        version=row['challenge_version']+1
        c.execute('''UPDATE lead.takeover_sessions SET control_mode=%s,challenge_version=%s,
            connection_data=connection_data || %s WHERE id=%s''',
            (mode,version,j({'owner_action':str(reason)[:500] if reason else None}),connection))
        return {'state':'active','control_mode':mode,'challenge_version':version}

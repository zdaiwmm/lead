"""Check the target service environment using synthetic business input."""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

AID = 'b1f9a23c-1153-4c67-a63b-11ebd264c687'


def emit(event, **fields):
    print('TG_RESULT ' + json.dumps(dict(event=event, **fields)), flush=True)


def check_broker():
    import traceback
    from unittest.mock import patch

    sys.path.insert(0, '/run/lead-home')
    import lead_business
    run = subprocess.run
    def watch(args, **kwargs):
        if args[0] == '/usr/bin/bwrap':
            kwargs['stderr'] = subprocess.PIPE
        try:
            return run(args, **kwargs)
        except subprocess.CalledProcessError as exc:
            # Only the packaged synthetic test invokes this diagnostic.
            msg = (exc.stderr or b'').decode(errors='replace').lower()
            known = [word for word in ('operation not permitted','permission denied','read-only file system',
                     'creating new namespace','cannot create','no such file','prlimit','bwrap','failed to mount',
                     'resource temporarily unavailable','out of memory') if word in msg]
            emit('fixture_sandbox_exit', code=exc.returncode, signals=known)
            raise
    try:
        with patch.object(lead_business.subprocess, 'run', watch):
            result = lead_business.evaluate({'event':'admit','operation':'review','paid':False})
        emit('fixture_business', ok=type(result.get('execute')) is bool)
    except Exception as exc:
        emit('fixture_business', ok=False, error=type(exc).__name__, errno=getattr(exc,'errno',None),
             location=Path(getattr(exc,'filename','') or '').name[:64],
             stack=[(Path(f.filename).name,f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-5:]],
             tempdir_configured=bool(os.environ.get('TMPDIR')), uid=os.geteuid())


def check_model():
    from lead_app.admin_settings import task_connection
    from lead_app.db import tx
    from lead_app.scope import service_scope, tenant_scope
    from lead_app.identity_store import agent_tenant
    import socket, ssl
    with service_scope(), tenant_scope(agent_tenant(AID),None), tx(AID) as c:
        seen = set()
        for purpose in ('research','conversation'):
            cfg, _, _ = task_connection(c,AID,purpose)
            url = urlsplit(cfg['base_url'])
            host = url.hostname
            official = {'openai':'api.openai.com','deepseek':'api.deepseek.com'}.get(cfg['provider'])
            emit('model_endpoint', purpose=purpose, provider=cfg['provider'], host=host, port=url.port or 443,
                 https=url.scheme=='https', key_configured=bool(cfg['key']))
            if not official or host != official or host in seen or url.scheme!='https' or url.port not in (443,None):
                continue
            seen.add(host)
            try:
                with socket.create_connection((host,443),timeout=6) as s, ssl.create_default_context().wrap_socket(s,server_hostname=host):
                    emit('direct_model_tls',ok=True,host=host)
            except OSError as exc:
                emit('direct_model_tls',ok=False,host=host,error=type(exc).__name__,errno=exc.errno)


if __name__=='__main__':
    if sys.argv[1:]==['fixture']:
        check_broker()
    else:
        subprocess.run(['/usr/bin/python3','-ISB','/run/lead-home/runtime_check.py','fixture'],
                       env={'PATH':'/usr/bin:/bin'}, cwd='/', timeout=15, check=True)
        check_model()

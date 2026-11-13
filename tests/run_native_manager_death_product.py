"""Provision one disposable Linux DB/source snapshot and run the manager cut.

Uses an existing synthetic PostgreSQL cluster and pinned runtime assets. Never
changes cluster roles, installs packages, reads model keys or deploys a service.
"""

import argparse
import json
import shlex
import subprocess
from pathlib import Path

REMOTE = r'''
import json, os, pathlib, signal, subprocess, sys, tempfile, uuid
os.umask(0o077)
payload=json.load(sys.stdin)
root=pathlib.Path(tempfile.mkdtemp(prefix='lead-manager-death-',dir='/home/mijiu'))
state=pathlib.Path(tempfile.mkdtemp(prefix='lmd-',dir='/tmp'))
for name,body in payload['files'].items():
    destination=root/name
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(body)
(root/'scripts/browser_namespace.py').chmod(0o755)
existing=pathlib.Path(payload['shared_pg'])
(state/'config.json').write_bytes((existing/'config.json').read_bytes())
(state/'test.env').write_bytes((existing/'test.env').read_bytes())
base=pathlib.Path(payload['runtime_base'])
appsite=base/'worker-venv/lib/python3.12/site-packages'
python=str(base/'worker-venv/bin/python')
runtime=root/'services/browser-runtime'
(runtime/'chromium/chrome-linux64').mkdir(parents=True)
(runtime/'chromium/chrome-linux64/chrome').symlink_to(base/'pw-exact/chromium-1234/chrome-linux64/chrome')
(runtime/'site-packages').symlink_to(base/'bu-venv/lib/python3.12/site-packages',target_is_directory=True)
(runtime/'manifest.json').write_text('{"fixture_only":true}')
(runtime/'browser_use_runner.py').write_bytes((root/'scripts/browser_use_runner.py').read_bytes())
sys.path.insert(0,str(appsite))
from dotenv import dotenv_values
import psycopg
from psycopg import sql
settings=dotenv_values(state/'test.env')
assert settings['LEAD_DB_HOST']=='127.0.0.1' and settings['LEAD_DB_PORT']=='36937'
database='manager_death_'+uuid.uuid4().hex[:12]
with psycopg.connect(host=settings['LEAD_DB_HOST'],port=settings['LEAD_DB_PORT'],dbname='postgres',user=settings['LEAD_DB_OWNER_USER'],password=settings['LEAD_DB_OWNER_PASSWORD'],autocommit=True) as c:
    c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
with (state/'test.env').open('a') as output:
    output.write('\nLEAD_DB_NAME='+database+'\n')
env={k:v for k,v in os.environ.items() if not k.startswith(('LEAD_','OPENAI_','DEEPSEEK_','ANTHROPIC_'))}
env.update(LEAD_STATE_DIR=str(state),LEAD_ENV_FILE=str(state/'test.env'),LEAD_NETWORK_NAMESPACE='1',LEAD_NATIVE_PRODUCT_TESTS='1',
    LEAD_BROWSER_RUNTIME_DIR=str(runtime),LEAD_BROWSER_RUNTIME_PYTHON=str(base/'bu-venv/bin/python'),
    PYTHONPATH=str(appsite)+':'+str(root/'services/api')+':'+str(root/'tests'),PLAYWRIGHT_BROWSERS_PATH=str(base/'pw-exact'))
manifest={'source_sha':payload['source_sha'],'root':str(root),'state':str(state),'database':database,'python':python,'shared_cluster_unchanged':True}
(state/'runtime.json').write_text(json.dumps(manifest))
print(json.dumps(manifest),flush=True)
with (state/'migration.log').open('w') as log:
    migrated=subprocess.run([python,'-B','-S',str(root/'scripts/migrate.py')],env=env,cwd=root,stdout=log,stderr=log)
if migrated.returncode:
    print(json.dumps({'phase':'migration','returncode':migrated.returncode}),flush=True)
    sys.exit(migrated.returncode)
with (state/'run.log').open('w') as log:
    cases=['tests/test_native_manager_death_product.py::test_entire_manager_death_after_claim_closes_tunnel_and_only_reconciles['+name+']' for name in payload['cases']]
    child=subprocess.Popen([python,'-B','-S','-m','pytest',*cases,'-q','--tb=short','--junitxml='+str(state/'manager-death.xml')],
        env=env,cwd=root,stdout=log,stderr=log,start_new_session=True)
    manifest['test_pid']=child.pid
    (state/'runtime.json').write_text(json.dumps(manifest))
    try:
        result=child.wait(timeout=220)
    except subprocess.TimeoutExpired:
        # This session was created for this exact test child. No shared daemon
        # or existing process was added to it.
        os.killpg(child.pid,signal.SIGKILL)
        child.wait(timeout=5)
        result=124
(state/'cleanup-manifest.json').write_text(json.dumps({'test_returncode':result,'database_retained_for_evidence':database,'source':str(root),'state':str(state),'shared_pg_not_stopped':True}))
print(json.dumps({'phase':'manager-death-product','returncode':result,'log':str(state/'run.log')}),flush=True)
sys.exit(result)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', required=True)
    parser.add_argument('--host', default='lead-gateway')
    parser.add_argument('--remote-user', default='mijiu')
    parser.add_argument('--shared-pg', required=True)
    parser.add_argument('--runtime-base', required=True)
    parser.add_argument('--case', choices=('after_claim', 'before_claim', 'committed'), action='append',
                        help='Explicit cut(s); defaults to the original after_claim cut only')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
    files = {str(path.relative_to(root)): path.read_text()
             for folder in ('services/api', 'infra/migrations')
             for path in (root / folder).rglob('*')
             if path.is_file() and path.suffix in ('.py', '.sql')}
    for name in ('pyproject.toml', 'infra/alembic.ini', 'scripts/migrate.py', 'scripts/browser_namespace.py',
                 'scripts/browser_use_runner.py', 'tests/conftest.py',
                 'tests/native_connection_product.py', 'tests/native_browser_fixture.py',
                 'tests/native_manager_death_child.py', 'tests/test_native_manager_death_product.py'):
        files[name] = (root / name).read_text()
    command = shlex.join(['sudo', '-n', '-u', args.remote_user,
                          str(Path(args.runtime_base) / 'worker-venv/bin/python'), '-B', '-S', '-c', REMOTE])
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                             '-F', args.ssh_config, args.host, command],
                            input=json.dumps({'files': files, 'source_sha': source_sha,
                                              'shared_pg': args.shared_pg, 'runtime_base': args.runtime_base,
                                              'cases': args.case or ['after_claim']}),
                            text=True, timeout=250)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

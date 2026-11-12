#!/usr/bin/env python3
"""Additive unified-browser schema upgrade under the approved home release hold.

This does not move accounts, copy browser state, change executor routes, or roll
back external-action history. Application rollback retains the additive schema.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASELINE = '0031_growth_runtime'
GOAL = '0033_unified_browser'
STORE = Path('/srv/lead-prod-vault/lead-git-releases')
LIVE = Path('/srv/lead-prod-vault/lead-home-live')
TOOLS = Path('/usr/local/lib/lead-home-release')
PREFIX = 'unified-browser'
PRESERVED_TABLES = (
    'agents','social_accounts','account_targets','authorization_grants','actions',
    'action_attempts','content_items','budget_reservations','usage_events','tasks',
    'growth_experiments','account_operating_intents','profile_execution_routes','browser_profiles',
)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def public_api(resource):
    request=urllib.request.Request('https://api.github.com/repos/zdaiwmm/lead/'+resource,
        headers={'User-Agent':'lead-home-upgrade','Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)


def verify_approval(sha, archive, *, get=public_api, started=False):
    """Verify the exact protected-workflow artifact independently on this host."""
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size>65536:
        raise ValueError('Original approval artifact required')
    with zipfile.ZipFile(archive) as zipped:
        files=zipped.infolist()
        if len(files)!=1 or files[0].filename!='lead-home-request.json' or files[0].file_size>2048:
            raise ValueError('Invalid approval artifact')
        receipt=json.loads(zipped.read(files[0]))
    if receipt.get('sha')!=sha or any(type(receipt.get(k)) is not int or receipt[k]<1 for k in ('deployment','approval_run','ci_run','ci_attempt')):
        raise ValueError('Approval receipt differs')
    request=get(f"deployments/{receipt['deployment']}")
    payload=request.get('payload') or {}
    expected={'deployment':request.get('id'),'sha':sha,'ci_run':payload.get('ci_run'),
        'ci_attempt':payload.get('ci_attempt'),'approval_run':payload.get('approval_run')}
    if (receipt!=expected or request.get('sha')!=sha or request.get('environment')!='home-production'
            or request.get('task')!='lead-home-release' or payload.get('schema')!=1 or payload.get('repository')!='zdaiwmm/lead'):
        raise ValueError('Protected deployment request differs')
    if not started and datetime.fromisoformat(request['created_at'].replace('Z','+00:00'))<datetime.now(timezone.utc)-timedelta(hours=24):
        raise ValueError('Approval request expired before migration started')
    approved=get(f"actions/runs/{receipt['approval_run']}")
    if (approved.get('path'),approved.get('event'),approved.get('head_branch'),approved.get('run_attempt'),approved.get('display_title'),approved.get('status'),approved.get('conclusion')) != (
        '.github/workflows/home-release.yml','workflow_dispatch','main',1,f'Home release {sha}','completed','success'):
        raise ValueError('Protected approval workflow incomplete')
    listing=get(f"actions/runs/{receipt['approval_run']}/artifacts?name=lead-home-request&per_page=2")
    artifacts=listing.get('artifacts',[])
    if listing.get('total_count')!=1 or len(artifacts)!=1:
        raise ValueError('Approval artifact ambiguous')
    artifact=artifacts[0]
    with archive.open('rb') as stream:
        digest='sha256:'+hashlib.file_digest(stream,'sha256').hexdigest()
    if artifact.get('name')!='lead-home-request' or artifact.get('expired') or artifact.get('digest')!=digest or artifact.get('size_in_bytes')!=archive.stat().st_size:
        raise ValueError('Approval artifact digest differs')
    # Reuse the formal application's exact main verification policy.
    from request_home_release import verified_run
    ci=verified_run(sha,get=get)
    if (ci['id'],ci['run_attempt'])!=(receipt['ci_run'],receipt['ci_attempt']):
        raise ValueError('Approved application verification is no longer current')
    latest=get('deployments?environment=home-production&task=lead-home-release&per_page=1')
    if not latest or latest[0].get('id')!=receipt['deployment']:
        raise ValueError('Another release superseded this request')
    return receipt


def column_inventory(connect, name):
    with connect(name) as c:
        return {table:[r[0] for r in c.execute("SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum",('lead.'+table,))] for table in PRESERVED_TABLES}


def data_inventory(connect, name, columns):
    """Hash the original columns, including every original action/control state."""
    from psycopg import sql
    result={}
    with connect(name) as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        for table in PRESERVED_TABLES:
            # Only a fixed table inventory and the previously attested actual
            # column identifiers enter SQL; no business values are printed.
            fields=sql.SQL(',').join(sql.Identifier(k) for k in columns[table])
            query=sql.SQL("SELECT count(*),md5(string_agg(row_to_json(t)::text,',' ORDER BY row_to_json(t)::text)) FROM (SELECT {} FROM lead.{}) t").format(fields,sql.Identifier(table))
            result[table]=list(c.execute(query).fetchone())
    return result


def assert_unchanged(before, after):
    if before!=after:
        raise ValueError('Original accounts, routes, controls, tasks, actions or costs changed')


def finish_cutover(stage,sha,verifier,site,write):
    if (stage/(PREFIX+'-cutover.pending')).read_text().strip()!=sha:
        raise ValueError('Unified-browser cutover hold missing')
    intake=run('systemctl','show','lead-home-ingress.socket','-p','ActiveState','--value')
    if intake not in ('active','inactive'):
        raise ValueError('Cannot reconcile current ingress state')
    verifier['verify'](sha,fenced=intake=='inactive',require_idle=False)
    verifier['verify_executors'](sha)
    if site['sql'](verifier['unified_schema_query']())!='t':
        raise ValueError('Final unified-browser schema readback failed')
    run(TOOLS/'home_live_maintenance.py','restore',sha)
    run('systemctl','start','lead-home-live.path','lead-home-ingress.socket')
    run(TOOLS/'home_systemd_release.py','verify',sha)
    complete=stage/(PREFIX+'-cutover.complete')
    write(complete,(sha+'\n').encode())
    (stage/(PREFIX+'-cutover.pending')).unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('rehearse','apply','finish'))
    parser.add_argument('--sha',required=True)
    parser.add_argument('--old',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0 or not all(re.fullmatch('[a-f0-9]{40}',v) for v in (args.sha,args.old)):
        raise ValueError('Root operator and exact revisions required')
    os.umask(0o077)
    stage=STORE/('maintenance-'+args.sha)
    source=stage/'source'
    if stage.resolve()!=stage or stage.stat().st_uid!=0 or stage.stat().st_mode & 0o077:
        raise ValueError('Private root-owned stage required')
    if Path(__file__).resolve()!=source/'scripts/unified_browser_upgrade.py':
        raise ValueError('Use the attested main source in its private stage')
    mirror=STORE/'lead.git'
    run('git','--git-dir',mirror,'merge-base','--is-ancestor',args.sha,'refs/heads/main')
    scripts=('scripts/unified_browser_upgrade.py','scripts/growth_runtime_upgrade.py',
        'scripts/home_systemd_release.py','scripts/request_home_release.py')
    listing=run('git','--git-dir',mirror,'ls-tree','-r',args.sha,'--','services','infra/migrations','infra/alembic.ini',*scripts)
    listed=set()
    for line in listing.splitlines():
        meta,name=line.split('\t')
        mode,kind,blob=meta.split()
        listed.add(name)
        path=source/name
        if (kind!='blob' or mode not in ('100644','100755') or path.resolve()!=path
                or path.stat().st_uid!=0 or path.stat().st_mode & 0o022
                or any(parent.stat().st_uid!=0 or parent.stat().st_mode & 0o022 for parent in path.parents if parent.is_relative_to(stage))
                or run('git','hash-object','--no-filters',path)!=blob):
            raise ValueError('Upgrade source drift')
    required=set(scripts)|{'infra/migrations/versions/0032_research_memory.py','infra/migrations/sql/0032_research_memory.sql',
        'infra/migrations/versions/0033_unified_browser.py','infra/migrations/sql/0033_unified_browser.sql'}
    if not required<=listed:
        raise ValueError('Upgrade source manifest is incomplete')
    sys.path[:0]=[str(source/'scripts')]
    base=runpy.run_path(str(source/'scripts/growth_runtime_upgrade.py'))
    verifier=runpy.run_path(str(source/'scripts/home_systemd_release.py'))
    site=runpy.run_path('/srv/lead-prod-vault/lead-onboard-87d151a/site-state.py')
    env=site['e']
    os.environ.update(base['database_environment'](LIVE,env))
    sys.path[:0]=[str(source/'services/api'),str(LIVE/'bin/site')]
    import psycopg
    from lead_app.migrations import upgrade
    from psycopg import sql
    options=dict(host=env['PGHOST'],port=int(env['PGPORT']),user=env['PGUSER'],password=env['PGPASSWORD'],
        connect_timeout=5,options=env.get('PGOPTIONS','')+' -c lock_timeout=5000 -c statement_timeout=120000')
    def connect(name):
        return psycopg.connect(**options,dbname=name)
    def version(name):
        with connect(name) as c:
            return [r[0] for r in c.execute('SELECT version_num FROM public.lead_migration_version')]
    def migrate(name,columns,before):
        if version(name)!=[BASELINE]:
            raise ValueError('Unexpected starting schema')
        os.environ['LEAD_DB_NAME']=name
        upgrade({**options,'dbname':name},target=GOAL,roles=True)
        if version(name)!=[GOAL]:
            raise ValueError('Unexpected migrated schema')
        assert_unchanged(before,data_inventory(connect,name,columns))
        with connect(name) as c:
            if not c.execute(verifier['unified_schema_query']()).fetchone()[0]:
                raise ValueError('Unified-browser schema attestation failed')
    config_before=(LIVE/'state/config.json').read_bytes()
    dump=stage/'pre-unified-browser.dump'
    proof=stage/'unified-browser-rehearsal.json'
    def write(path,data):
        base['atomic_private'](path,data,previous=dump)
    pg=site['pg']/'lib/postgresql/18/bin'
    if args.phase=='rehearse':
        if version('lead')!=[BASELINE]:
            raise ValueError('Rehearsal requires the original schema')
        if not dump.exists():
            partial=stage/'pre-unified-browser.partial'
            subprocess.run([pg/'pg_dump','-Fc','--file',partial,'lead'],env=env,check=True)
            os.replace(partial,dump)
        name='lead_growth_drill_'+args.sha[:12]
        with psycopg.connect(**options,dbname='postgres',autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
            try:
                admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                subprocess.run([pg/'pg_restore','--exit-on-error','-d',name,dump],env=env,check=True)
                columns=column_inventory(connect,name)
                before=data_inventory(connect,name,columns)
                with base['drill_role_connections'](name,options):
                    migrate(name,columns,before)
                    upgrade({**options,'dbname':name},target=GOAL,roles=True)
                    assert_unchanged(before,data_inventory(connect,name,columns))
                # The same verifier accepts the additive schema for the old app;
                # neither authority nor unknown-action state is restored backwards.
                with connect(name) as c:
                    if not c.execute(verifier['growth_schema_query']()).fetchone()[0]:
                        raise ValueError('Old application schema compatibility failed')
            finally:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        with dump.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        write(proof,json.dumps({'sha':args.sha,'old':args.old,'dump_sha256':digest,
            'columns':columns,'config_sha256':hashlib.sha256(config_before).hexdigest(),
            'role_transport':'isolated-db-admin-login-with-verified-restricted-role'}).encode())
        print('Restored backup additive schema and unchanged original authority verified',flush=True)
        return
    expected=json.loads(proof.read_text())
    with dump.open('rb') as stream:
        if expected.get('sha')!=args.sha or expected.get('old')!=args.old or expected.get('dump_sha256')!=hashlib.file_digest(stream,'sha256').hexdigest():
            raise ValueError('Rehearsal proof differs')
    started=stage/(PREFIX+'-migration.started.json')
    journal=stage/(PREFIX+'-migration.json')
    with (STORE/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        verify_approval(args.sha,stage/'home-approval.zip',started=started.exists())
        if args.phase=='finish':
            finish_cutover(stage,args.sha,verifier,site,write)
            print('Application and independent executors verified; original scheduling restored',flush=True)
            return
        if run(TOOLS/'home_systemd_release.py','current')!=args.old:
            raise ValueError('Live baseline changed; inspect original cutover request')
        if started.exists():
            saved=json.loads(started.read_text())
            if saved.get('sha')!=args.sha or saved.get('old')!=args.old:
                raise ValueError('Original migration journal differs')
            if version('lead')==[GOAL]:
                assert_unchanged(saved['before'],data_inventory(connect,'lead',saved['columns']))
                with connect('lead') as c:
                    if not c.execute(verifier['unified_schema_query']()).fetchone()[0]:
                        raise ValueError('Interrupted migration schema readback failed')
                write(journal,json.dumps({'sha':args.sha,'old':args.old}).encode())
                print('Already migrated; independently read back original journal',flush=True)
                return
            # Transactional DDL returned to baseline; keep hold and require an
            # explicit operator investigation instead of automatic second apply.
            raise ValueError('Previous migration did not reach goal; hold retained for inspection')
        if journal.exists():
            raise ValueError('Migration journal exists without original start record')
        if hashlib.sha256(config_before).hexdigest()!=expected['config_sha256']:
            raise ValueError('Node configuration changed since rehearsal')
        run(TOOLS/'home_systemd_release.py','verify',args.old)
        run('systemctl','start','lead-pc-local-backup.service')
        deadline=time.monotonic()+1800
        while True:
            if run(TOOLS/'home_live_maintenance.py','ready',args.sha)=='idle':
                held=subprocess.run([TOOLS/'home_live_maintenance.py','hold',args.sha],capture_output=True)
                if held.returncode==0:
                    break
            if time.monotonic()>=deadline:
                raise TimeoutError('Existing work has not reached maintenance point')
            time.sleep(5)
        if site['sql']('SELECT lead.backup_ready() AND lead.local_restore_ready()')!='t':
            raise ValueError('Verified recovery point unavailable; hold retained')
        run('systemctl','stop','lead-home-live.path','lead-home-ingress.socket','lead-home-ingress.service')
        if run(TOOLS/'home_live_maintenance.py','ready')!='idle':
            raise ValueError('Work started during maintenance; hold retained')
        columns=column_inventory(connect,'lead')
        if columns!=expected['columns']:
            raise ValueError('Baseline schema drifted since rehearsal; hold retained')
        before=data_inventory(connect,'lead',columns)
        write(stage/(PREFIX+'-cutover.pending'),(args.sha+'\n').encode())
        write(started,json.dumps({'sha':args.sha,'old':args.old,'columns':columns,'before':before}).encode())
        migrate('lead',columns,before)
        write(journal,json.dumps({'sha':args.sha,'old':args.old}).encode())
        verifier['verify'](args.old,fenced=True,require_idle=False)
        print('Additive schema verified; original state unchanged; cutover hold retained',flush=True)


if __name__=='__main__':
    main()

#!/usr/bin/env python3
"""Reviewed, source-attested home-node transfer with restored-backup rehearsal.

The durable hold spans schema, application and each independent executor. No
credentials, account identifiers or business data are emitted to normal output.
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
import tempfile
import time
from pathlib import Path

BASELINE = '0030_fault_isolation'
GOAL = '0031_growth_runtime'
STORE = Path('/srv/lead-prod-vault/lead-git-releases')
LIVE = Path('/srv/lead-prod-vault/lead-home-live')
TOOLS = Path('/usr/local/lib/lead-home-release')
TRANSPORTS = {'public_browser':'http://127.0.0.1:18091','telegram_browser':'http://127.0.0.1:18092',
              'shared':'http://127.0.0.1:8089'}


def run(*args,**kwargs):
    return subprocess.check_output(args,text=True,**kwargs).strip()


def atomic_private(path,data,previous=None):
    info = (previous or path).stat()
    fd,name = tempfile.mkstemp(prefix='.growth-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),info.st_mode & 0o777)
            os.fchown(stream.fileno(),info.st_uid,info.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
        fd = os.open(path.parent,os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=('rehearse','apply','finish'))
    parser.add_argument('--sha',required=True)
    parser.add_argument('--old',required=True)
    args = parser.parse_args()
    if os.geteuid()!=0 or not all(re.fullmatch('[a-f0-9]{40}',value) for value in (args.sha,args.old)):
        raise ValueError('Root operator and exact revisions required')
    os.umask(0o077)
    stage = STORE/('maintenance-'+args.sha)
    source = stage/'source'
    if stage.resolve()!=stage or stage.stat().st_uid!=0 or stage.stat().st_mode & 0o077:
        raise ValueError('Private root-owned stage required')
    if Path(__file__).resolve()!=source/'scripts/growth_runtime_upgrade.py':
        raise ValueError('Use the attested main source in its private stage')
    mirror = STORE/'lead.git'
    run('git','--git-dir',mirror,'merge-base','--is-ancestor',args.sha,'refs/heads/main')
    listing = run('git','--git-dir',mirror,'ls-tree','-r',args.sha,'--','services','infra/migrations','infra/alembic.ini',
        'scripts/growth_runtime_upgrade.py','scripts/home_systemd_release.py')
    for line in listing.splitlines():
        meta,name = line.split('\t')
        mode,kind,blob = meta.split()
        path = source/name
        if kind!='blob' or mode not in ('100644','100755') or path.is_symlink() or run('git','hash-object','--no-filters',path)!=blob:
            raise ValueError('Upgrade source drift')
    site = runpy.run_path('/srv/lead-prod-vault/lead-onboard-87d151a/site-state.py')
    env = site['e']
    if env['PGDATABASE']!='lead' or env['PGHOST']!=str(LIVE/'pgsock'):
        raise ValueError('Unexpected database target')
    os.environ.update(LEAD_STATE_DIR=str(LIVE/'state'),LEAD_DB_HOST=env['PGHOST'],LEAD_DB_PORT=env['PGPORT'],LEAD_DB_NAME='lead')
    sys.path[:0]=[str(source/'services/api'),str(LIVE/'bin/site')]
    import psycopg
    from lead_app.config import CONFIG
    from lead_app.growth.migration import apply_migration, preview_migration
    from lead_app.migrations import upgrade
    from lead_app.scope import service_scope
    from psycopg import sql

    options = dict(host=env['PGHOST'],port=int(env['PGPORT']),user=env['PGUSER'],password=env['PGPASSWORD'],
        connect_timeout=5,options=env.get('PGOPTIONS','')+' -c lock_timeout=5000 -c statement_timeout=120000')
    verifier = runpy.run_path(str(source/'scripts/home_systemd_release.py'))
    binding = json.loads((TOOLS/'telegram.json').read_text())
    config_path = LIVE/'state/config.json'
    config_before = config_path.read_bytes()
    config = json.loads(config_before)
    config.update(browser_transports=TRANSPORTS,browser_route_defaults={'default':'public_browser'})
    CONFIG.update(browser_transports=TRANSPORTS,browser_route_defaults={'default':'public_browser'})

    def version(name):
        with psycopg.connect(**options,dbname=name) as c:
            return [row[0] for row in c.execute('SELECT version_num FROM public.lead_migration_version')]

    def inventory(name):
        with psycopg.connect(**options,dbname=name) as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            result = {}
            for table in ('social_accounts','account_targets','authorization_grants','actions','action_attempts','content_items',
                          'budget_reservations','usage_events'):
                excluded='state' if table=='actions' else 'subject_ref'
                result[table]=c.execute(sql.SQL("SELECT count(*),md5(string_agg((to_jsonb(t)-%s)::text,',' ORDER BY id)) FROM lead.{} t").format(sql.Identifier(table)),(excluded,)).fetchone()
            result['submitted_action_states']=c.execute("SELECT md5(string_agg(jsonb_build_array(id,state)::text,',' ORDER BY id)) FROM lead.actions WHERE state NOT IN ('prepared','canceled')").fetchone()
            result['controls']=c.execute("""SELECT md5(string_agg(jsonb_build_array(id,desired_state,control_version,
                config_revision,authorization_version,onboarding_state->'daily_token_limit')::text,',' ORDER BY id)) FROM lead.agents""").fetchone()
            return result

    def migrate(name):
        os.environ['LEAD_DB_NAME']=name
        if version(name)!=[BASELINE]:
            raise ValueError('Unexpected starting schema')
        before = inventory(name)
        upgrade({**options,'dbname':name},roles=True)
        with psycopg.connect(**options,dbname=name) as c:
            profiles = c.execute('SELECT id,tenant_id,agent_id,slot FROM lead.browser_profiles').fetchall()
            for profile,tenant,aid,slot in profiles:
                transport = 'telegram_browser' if str(aid)==binding['agent_id'] and slot==binding['profile_slot'] else 'public_browser'
                c.execute("""INSERT INTO lead.profile_execution_routes(profile_id,tenant_id,agent_id,executor_ref,transport_ref)
                    VALUES(%s,%s,%s,'browser',%s) ON CONFLICT(profile_id) DO NOTHING""",(profile,tenant,aid,transport))
            ids = [str(row[0]) for row in c.execute('SELECT id FROM lead.agents ORDER BY id')]
        summaries = []
        for aid in ids:
            with service_scope():
                report = preview_migration(aid)
                result = apply_migration(aid,report['report_hash'],report['generation'])
            summaries.append({'report_hash':report['report_hash'],'generation':result['generation'],
                              'original_actions':result['original_actions'],'missing_routes':len(result['missing_routes'])})
        if version(name)!=[GOAL] or inventory(name)!=before:
            raise ValueError('Original controls, authority, publications or costs changed')
        with psycopg.connect(**options,dbname=name) as c:
            if not c.execute(verifier['growth_schema_query']()).fetchone()[0]:
                raise ValueError('Growth schema attestation failed')
        return summaries

    dump,proof = stage/'pre-growth.dump',stage/'growth-rehearsal.json'
    pg = site['pg']/'lib/postgresql/18/bin'
    if args.phase=='rehearse':
        if version('lead')!=[BASELINE]:
            raise ValueError('Rehearsal requires the original schema')
        if not dump.exists():
            partial=stage/'pre-growth.partial'
            subprocess.run([pg/'pg_dump','-Fc','--file',partial,'lead'],env=env,check=True)
            os.replace(partial,dump)
        name='lead_growth_drill_'+args.sha[:12]
        with psycopg.connect(**options,dbname='postgres',autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
            try:
                admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                subprocess.run([pg/'pg_restore','--exit-on-error','-d',name,dump],env=env,check=True)
                summaries=migrate(name)
                upgrade({**options,'dbname':name},roles=True)
                if version(name)!=[GOAL]:
                    raise ValueError('Repeat schema upgrade changed the result')
            finally:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        with dump.open('rb') as stream:
            sha=hashlib.file_digest(stream,'sha256').hexdigest()
        proof.write_text(json.dumps({'sha':args.sha,'dump_sha256':sha,'transfers':summaries,'config_before_sha256':hashlib.sha256(config_before).hexdigest()}))
        print('Restored backup schema, ownership, original authority and costs verified',flush=True)
        return
    with dump.open('rb') as stream:
        expected=json.loads(proof.read_text())
        if expected['sha']!=args.sha or expected['dump_sha256']!=hashlib.file_digest(stream,'sha256').hexdigest():
            raise ValueError('Rehearsal proof differs')
    with (STORE/'deploy.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.phase=='finish':
            if (stage/'growth-cutover.pending').read_text().strip()!=args.sha:
                raise ValueError('Cutover hold is missing')
            verifier['verify'](args.sha,fenced=True)
            verifier['verify_executors'](args.sha)
            if site['sql'](verifier['growth_schema_query']())!='t':
                raise ValueError('Final schema readback failed')
            run(TOOLS/'home_live_maintenance.py','restore',args.sha)
            run('systemctl','start','lead-home-live.path','lead-home-ingress.socket')
            run(TOOLS/'home_systemd_release.py','verify',args.sha)
            os.replace(stage/'growth-cutover.pending',stage/'growth-cutover.complete')
            print('Application and independent executors verified; original scheduling restored',flush=True)
            return
        if run(TOOLS/'home_systemd_release.py','current')!=args.old:
            raise ValueError('Live baseline changed')
        if (stage/'growth-migration.json').exists():
            if version('lead')!=[GOAL]:
                raise ValueError('Previous migration outcome differs')
            print('Already migrated; readback only',flush=True)
            return
        if hashlib.sha256(config_before).hexdigest()!=expected['config_before_sha256']:
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
                raise TimeoutError('Existing work has not reached its maintenance point')
            time.sleep(5)
        if site['sql']('SELECT lead.backup_ready() AND lead.local_restore_ready()')!='t':
            raise ValueError('Verified recovery point unavailable; hold retained')
        run('systemctl','stop','lead-home-live.path','lead-home-ingress.socket','lead-home-ingress.service')
        if run(TOOLS/'home_live_maintenance.py','ready')!='idle':
            raise ValueError('Work started during maintenance; hold retained')
        (stage/'node-config.before.json').write_bytes(config_before)
        (stage/'growth-cutover.pending').write_text(args.sha+'\n')
        summaries=migrate('lead')
        atomic_private(config_path,json.dumps(config,ensure_ascii=False,indent=2).encode())
        (stage/'growth-migration.json').write_text(json.dumps({'sha':args.sha,'old':args.old,'transfers':summaries}))
        verifier['verify'](args.old,fenced=True,require_idle=False)
        print('Growth ownership transferred; original controls preserved; cutover hold retained',flush=True)


if __name__=='__main__':
    main()

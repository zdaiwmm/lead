#!/usr/bin/env python3
"""One additive home-node upgrade, rehearsed against a restored private backup."""

import argparse
import fcntl
import hashlib
import os
import re
import runpy
import subprocess
import sys
import time
from pathlib import Path

BASELINE = '0029_merge_channel_restore'
GOAL = '0030_fault_isolation'
STORE = Path('/srv/lead-prod-vault/lead-git-releases')
LIVE = Path('/srv/lead-prod-vault/lead-home-live')
TOOLS = Path('/usr/local/lib/lead-home-release')


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('rehearse', 'apply'))
    parser.add_argument('--sha', required=True)
    parser.add_argument('--old', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or not all(re.fullmatch('[a-f0-9]{40}', v) for v in (args.sha, args.old)):
        raise ValueError('Root operator and exact revisions required')
    os.umask(0o077)
    stage = STORE / ('maintenance-' + args.sha)
    source = stage / 'source'
    if stage.resolve() != stage or stage.stat().st_mode & 0o077:
        raise ValueError('Private node stage required')
    if Path(__file__).resolve() != source / 'scripts/fault_isolation_upgrade.py':
        raise ValueError('Run the verified main source in its private stage')
    mirror = STORE / 'lead.git'
    run('git', '--git-dir', mirror, 'merge-base', '--is-ancestor', args.sha, 'refs/heads/main')
    # Verify every executable migration/application source before importing it.
    listing = run('git', '--git-dir', mirror, 'ls-tree', '-r', args.sha, '--',
                  'services', 'infra/migrations', 'infra/alembic.ini',
                  'scripts/fault_isolation_upgrade.py', 'scripts/home_systemd_release.py')
    for line in listing.splitlines():
        metadata, name = line.split('\t')
        mode, kind, blob = metadata.split()
        path = source / name
        if kind != 'blob' or mode not in ('100644', '100755') or path.is_symlink():
            raise ValueError('Unexpected upgrade source member')
        if run('git', 'hash-object', '--no-filters', path) != blob:
            raise ValueError('Upgrade source drift')
    sys.path[:0] = [str(source / 'services/api'), str(LIVE / 'bin/site')]
    os.environ['LEAD_STATE_DIR'] = str(LIVE / 'state')
    import psycopg
    from lead_app.migrations import upgrade
    from psycopg import sql

    site = runpy.run_path('/srv/lead-prod-vault/lead-onboard-87d151a/site-state.py')
    env = site['e']
    if env['PGDATABASE'] != 'lead' or env['PGHOST'] != str(LIVE / 'pgsock'):
        raise ValueError('Unexpected database target')
    pg = site['pg'] / 'lib/postgresql/18/bin'
    options = dict(host=env['PGHOST'], port=int(env['PGPORT']), user=env['PGUSER'],
                   password=env['PGPASSWORD'], connect_timeout=5,
                   options=env.get('PGOPTIONS', '') + ' -c lock_timeout=5000 -c statement_timeout=120000')
    verifier = runpy.run_path(str(source / 'scripts/home_systemd_release.py'))['fault_schema_query']

    def version(name):
        with psycopg.connect(**options, dbname=name) as c:
            return [r[0] for r in c.execute('SELECT version_num FROM public.lead_migration_version')]

    def snapshot(name):
        with psycopg.connect(**options, dbname=name) as c:
            c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            result = {}
            for table in ('social_accounts', 'account_targets', 'authorization_grants', 'tasks', 'actions', 'content_items'):
                result[table] = c.execute(sql.SQL("SELECT count(*),md5(string_agg(to_jsonb(t)::text,',' ORDER BY id)) FROM lead.{} t").format(sql.Identifier(table))).fetchone()
            return result

    def migrate(name):
        if version(name) != [BASELINE]:
            raise ValueError('Unexpected starting schema')
        before = snapshot(name)
        upgrade({**options, 'dbname': name}, roles=False)
        if version(name) != [GOAL] or before != snapshot(name):
            raise ValueError('Legacy data or resulting schema differs')
        with psycopg.connect(**options, dbname=name) as c:
            if not c.execute(verifier()).fetchone()[0]:
                raise ValueError('New schema attestation failed')

    dump = stage / 'pre-upgrade.dump'
    proof = stage / 'rehearsal.ok'
    if args.phase == 'rehearse':
        if version('lead') != [BASELINE]:
            raise ValueError('Rehearsal needs the original live schema')
        if not dump.exists():
            partial = stage / 'pre-upgrade.partial'
            subprocess.run([pg / 'pg_dump', '-Fc', '--file', partial, 'lead'], env=env, check=True)
            os.replace(partial, dump)
        name = 'lead_fault_drill_' + args.sha[:12]
        with psycopg.connect(**options, dbname='postgres', autocommit=True) as admin:
            admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
            try:
                admin.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(name)))
                subprocess.run([pg / 'pg_restore', '--exit-on-error', '-d', name, dump], env=env, check=True)
                migrate(name)
                upgrade({**options, 'dbname': name}, roles=False)
                if version(name) != [GOAL]:
                    raise ValueError('Repeat upgrade changed schema')
            finally:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        with dump.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        proof.write_text(args.sha + ' ' + digest + '\n')
        print('Restored backup upgraded twice; six legacy inventories and schema verified', flush=True)
        return
    with dump.open('rb') as f:
        if proof.read_text().strip() != args.sha + ' ' + hashlib.file_digest(f, 'sha256').hexdigest():
            raise ValueError('Rehearsal proof changed')
    with (STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if run(TOOLS / 'home_systemd_release.py', 'current') != args.old:
            raise ValueError('Live application changed')
        if (stage / 'migration.ok').exists():
            if (stage / 'migration.ok').read_text().strip() != args.sha or version('lead') != [GOAL]:
                raise ValueError('Previous migration outcome differs')
            print('Already upgraded; readback only')
            return
        run(TOOLS / 'home_systemd_release.py', 'verify', args.old)
        run('systemctl', 'start', 'lead-pc-local-backup.service')
        deadline = time.monotonic() + 1800
        while True:
            ready = run(TOOLS / 'home_live_maintenance.py', 'ready', args.sha)
            if ready == 'idle':
                held = subprocess.run([TOOLS / 'home_live_maintenance.py', 'hold', args.sha], capture_output=True)
                if held.returncode == 0:
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError('Existing work has not reached its maintenance point')
            time.sleep(5)
        try:
            if site['sql']('SELECT lead.backup_ready() AND lead.local_restore_ready()') != 't':
                raise ValueError('Verified recovery point unavailable')
            run('systemctl', 'stop', 'lead-home-live.path', 'lead-home-ingress.socket', 'lead-home-ingress.service')
            if run(TOOLS / 'home_live_maintenance.py', 'ready') != 'idle':
                raise ValueError('Work started during maintenance')
            migrate('lead')
            (stage / 'migration.ok').write_text(args.sha + '\n')
            run('systemctl', 'start', 'lead-home-live.path', 'lead-home-ingress.socket')
            run(TOOLS / 'home_systemd_release.py', 'verify', args.old)
        except BaseException:
            # The upgrade is additive and transactional; do not restore an old DB.
            # Only release the hold after the previous app is independently healthy.
            run('systemctl', 'start', 'lead-home-live.path', 'lead-home-ingress.socket')
            run(TOOLS / 'home_systemd_release.py', 'verify', args.old)
            run(TOOLS / 'home_live_maintenance.py', 'restore', args.sha)
            raise
        print('Schema upgraded; original application healthy; authorized release hold retained', flush=True)


if __name__ == '__main__':
    main()

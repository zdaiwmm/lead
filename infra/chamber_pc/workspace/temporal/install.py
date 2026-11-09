"""Connect the Chamber queue to the restored local workflow databases over their Unix socket."""
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VAULT = Path('/srv/lead-prod-vault')
LIVE = VAULT/'lead-home-live'
DEST = LIVE/'chamber-temporal-v1'
USERSRC = Path('/home/mijiu/lead-workbench.QIYQ7nU4/ts')
PG = LIVE/'bin/pg/usr'
V = '/run/lead-home'
DB = 'lead-home-db.service'
TS = 'lead-home-temporal.service'
DATA = 'lead-chamber-analytics.service'
UNITS = Path('/etc/systemd/system')
DROP = UNITS/(DATA+'.d')/'90-temporal.conf'
AID = 'b1f9a23c-1153-4c67-a63b-11ebd264c687'
QUEUE = 'lead-analytics-'+AID
HBA = 'local lead_workflow,lead_visibility lead_workflow peer map=lead_chamber_temporal'
IDENT = 'lead_chamber_temporal lead-r-temporal lead_workflow'
CHECKSUMS = {
    'usr/local/bin/temporal-server':'55dd406e4f5d6176c6f5613fadbd2b9baa18aaa06bd705245e0afabbc9800193',
    'usr/local/bin/dockerize':'41eedc76bbc568a1b5715c3e0e1c160bc06906dad69d69a5249b0f00ec41cee6',
    'usr/local/bin/tctl':'48d07b607837dd8574b1887073927f6847f63d4ed55e04d339883ebc167cd3dc',
    'etc/temporal/config/config_template.yaml':'a19cf180d4d505e9c795fa9a25efc75b1f77c1063fbc7e261a35f5a38f384206',
}


def run(*args, env=None, timeout=90):
    return subprocess.run(list(map(str,args)),check=True,text=True,capture_output=True,
                          env=env,timeout=timeout).stdout.strip()


def write(path, content, mode=0o644, preserve_owner=False):
    path.parent.mkdir(exist_ok=True)
    tmp=path.with_name(path.name+'.chamber-new')
    tmp.write_bytes(content if isinstance(content,bytes) else content.encode())
    if preserve_owner:
        st=path.stat(); os.chown(tmp,st.st_uid,st.st_gid);mode=st.st_mode & 0o777
    tmp.chmod(mode)
    os.replace(tmp,path)


def db_env():
    return dict(PATH='/usr/bin:/bin',LC_ALL='C',LD_LIBRARY_PATH=str(PG/'lib/x86_64-linux-gnu'),
                PGHOST=str(LIVE/'pgsock'),PGPORT='25434',PGUSER='home_isolated_restore_admin',
                PGPASSWORD=(VAULT/'restore/final-20261107/isolated/admin.key').read_text().strip(),
                PGOPTIONS='-c jit=off -c default_transaction_read_only=on', PGCONNECT_TIMEOUT='3')


def sql(db, stmt):
    return run(PG/'lib/postgresql/18/bin/psql','-XAt','-v','ON_ERROR_STOP=1','-d',db,'-c',stmt,env=db_env())


def pg_rules(enable):
    for name,rule in (('pg_ident.conf',IDENT),('pg_hba.conf',HBA)):
        file=LIVE/'db'/name
        lines=file.read_text().splitlines(keepends=True)
        if enable and rule+'\n' not in lines:
            write(file,rule+'\n'+''.join(lines),preserve_owner=True)
        elif not enable and rule+'\n' in lines:
            write(file,''.join(l for l in lines if l!=rule+'\n'),preserve_owner=True)
    if sql('postgres','select pg_reload_conf()')!='t':
        raise RuntimeError('PostgreSQL could not reload its local peer rule')


def prepare_files():
    DEST.mkdir(mode=0o755,exist_ok=True)
    (DEST/'config').mkdir(mode=0o755,exist_ok=True)
    (DEST/'config').chmod(0o755)
    (DEST/'tls').mkdir(mode=0o700,exist_ok=True)
    for rel,digest in CHECKSUMS.items():
        p=subprocess.run(['setpriv','--reuid=mijiu','--regid=mijiu','--init-groups','--','head','-c','120000000',
                          str(USERSRC/rel)],capture_output=True,check=True,timeout=30)
        if hashlib.sha256(p.stdout).hexdigest()!=digest:
            raise RuntimeError('PC binary/config checksum mismatch: '+Path(rel).name)
        write(DEST/Path(rel).name,p.stdout,0o755 if '/bin/' in rel else 0o644)
    write(DEST/'agent_analytics_temporal.py',Path(__file__).with_name('agent_analytics_temporal.py').read_bytes())
    tls=DEST/'tls'
    if not (tls/'complete').exists():
        with tempfile.TemporaryDirectory(dir=tls) as td:
            path=Path(td)
            def ssl(*args):
                subprocess.run(['openssl',*args],cwd=path,check=True,capture_output=True,timeout=45)
            ssl('req','-new','-x509','-newkey','rsa:3072','-nodes','-days','1825','-subj','/CN=Chamber workflow CA',
                '-addext','basicConstraints=critical,CA:TRUE','-keyout','ca.key','-out','ca.crt')
            for n,usage in [('server','serverAuth,clientAuth'),('worker','clientAuth')]:
                ssl('req','-new','-newkey','rsa:2048','-nodes','-subj','/CN='+n,'-keyout',n+'.key','-out',n+'.csr')
                (path/'ext').write_text('basicConstraints=critical,CA:FALSE\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage='+usage+'\n')
                ssl('x509','-req','-in',n+'.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-days','397',
                    '-extfile','ext','-out',n+'.crt')
            for f in ('ca.key','ca.crt','server.key','server.crt','worker.key','worker.crt'):
                write(tls/f,(path/f).read_bytes(),0o600)
            write(tls/'complete','1',0o600)


def restored_shard_count():
    encoded = sql('lead_workflow', """SELECT encode(data, 'hex') FROM cluster_metadata_info
        WHERE cluster_name='active' AND lower(data_encoding)='proto3' AND octet_length(data) BETWEEN 1 AND 1048576""")
    if not re.fullmatch('[0-9a-f]+', encoded):
        raise RuntimeError('missing or ambiguous active cluster metadata')
    with tempfile.TemporaryDirectory(dir=DEST) as directory:
        source = Path(directory)/'cluster.hex'
        source.write_text(encoded)
        cluster = json.loads(run(DEST/'tctl', 'adm', 'decode', 'proto', '--type',
            'temporal.server.api.persistence.v1.ClusterMetadata', '--hex_file', str(source),
            env=dict(PATH='/usr/bin:/bin', HOME=directory, LC_ALL='C')))
    count = cluster.get('historyShardCount')
    if cluster.get('clusterName') != 'active' or type(count) is not int or not 1 <= count <= 65536:
        raise RuntimeError('invalid restored cluster metadata or history shard count')
    return count


def configure():
    rootfs=VAULT/'isolated-release-20261107/rootfs'
    paths=(rootfs/'opt/lead-collector/workflow.env',LIVE/'state/private/collector/workflow.env',
           rootfs/'srv/lead/.state/private/collector/workflow.env')
    source=next((f for f in paths if f.is_file()),None)
    if not source:
        raise RuntimeError('restored workflow.env missing')
    knobs={}
    for line in source.read_text().splitlines():
        key,_,value=line.removeprefix('export ').partition('=')
        if key in ('NUM_HISTORY_SHARDS','DBNAME','VISIBILITY_DBNAME','POSTGRES_USER'):
            knobs[key]=shlex.split(value)[0]
    if any(knobs.get(k,v)!=v for k,v in [('DBNAME','lead_workflow'),('VISIBILITY_DBNAME','lead_visibility'),
                                           ('POSTGRES_USER','lead_workflow')]):
        raise RuntimeError('source workflow store differs; reconcile database mapping')
    if sql('lead_workflow',"select count(*) from namespaces where name='lead-prod'")!='1':
        raise RuntimeError('restored lead-prod namespace missing')
    if sql('lead_workflow',"select string_agg(cluster_name,',') from cluster_metadata_info")!='active':
        raise RuntimeError('expected restored single active cluster')
    n=restored_shard_count()
    if knobs.get('NUM_HISTORY_SHARDS') not in (None,str(n)):
        print('Using persisted active cluster shard count instead of restored environment override',flush=True)
    if int(sql('lead_workflow','select coalesce(max(shard_id),0) from shards'))>n:
        raise RuntimeError('observed shards exceed persisted cluster configuration')
    for db in ('lead_workflow','lead_visibility'):
        owner=sql('postgres',f"select pg_get_userbyid(datdba) from pg_database where datname='{db}'")
        if owner!='lead_workflow':
            raise RuntimeError('unexpected owner of '+db)
        archive=DEST/(db+'.before.dump')
        if not archive.exists():
            tmp=archive.with_suffix('.partial')
            run(PG/'lib/postgresql/18/bin/pg_dump','-Fc','-f',tmp,'-d',db,env=db_env(),timeout=600)
            run(PG/'lib/postgresql/18/bin/pg_restore','-l',tmp,env=db_env())
            tmp.chmod(0o600);tmp.replace(archive)
    render_config(str(n))


def render_config(n):
    cred='/run/credentials/'+TS+'/'
    env=dict(PATH='/usr/bin:/bin',DB='postgres12_pgx',DBNAME='lead_workflow',VISIBILITY_DBNAME='lead_visibility',
             POSTGRES_USER='lead_workflow',POSTGRES_SEEDS='localhost',DB_PORT='25434',NUM_HISTORY_SHARDS=n,
             SQL_MAX_CONNS='8',SQL_MAX_IDLE_CONNS='2',SQL_VIS_MAX_CONNS='4',SQL_VIS_MAX_IDLE_CONNS='1',
             TEMPORAL_TLS_SERVER_CERT=cred+'server.crt',TEMPORAL_TLS_FRONTEND_CERT=cred+'server.crt',
             TEMPORAL_TLS_SERVER_KEY=cred+'server.key',TEMPORAL_TLS_FRONTEND_KEY=cred+'server.key',
             TEMPORAL_TLS_SERVER_CA_CERT=cred+'ca.crt',TEMPORAL_TLS_CLIENT1_CA_CERT=cred+'ca.crt',
             TEMPORAL_TLS_CLIENT2_CA_CERT=cred+'ca.crt',TEMPORAL_TLS_INTERNODE_SERVER_NAME='127.0.0.1',
             TEMPORAL_TLS_FRONTEND_SERVER_NAME='127.0.0.1',TEMPORAL_TLS_REQUIRE_CLIENT_AUTH='true',
             BIND_ON_IP='127.0.0.1',FRONTEND_GRPC_PORT='17233',PUBLIC_FRONTEND_ADDRESS='127.0.0.1:17233',
             DYNAMIC_CONFIG_FILE_PATH=V+'/workflow/config/dynamic.yaml')
    config=run(DEST/'dockerize','-template',str(DEST/'config_template.yaml'),env=env)
    key='                pluginName: "postgres12_pgx"'
    if config.count(key)!=2:
        raise RuntimeError('unexpected rendered Temporal store config')
    write(DEST/'config/home.yaml',config.replace(key,key+'\n                connectAttributes:\n                    host: "'+V+'/sock"\n                    port: "25434"\n                    options: "-c jit=off"')+'\n')
    write(DEST/'config/dynamic.yaml','{}\n')


def credentials(unit, names):
    return ''.join('LoadCredential='+name+':'+str(DEST/'tls'/name)+'\n' for name in names)


def start():
    if not (LIVE/'chamber-workspace-releases').is_dir() or run('systemctl','is-active',DATA)!='active':
        raise RuntimeError('complete data recovery release first')
    # Data queue must be drained before switching transports.
    for _ in range(120):
        pending = sql('lead',"select count(*) from lead.analytics_ingest_events where agent_id='"+AID+"' and state='pending'")
        dirty = sql('lead',"select count(*) from lead.analytics_report_dirty where agent_id='"+AID+"' and scope_key='live'")
        if pending=='0' and dirty=='0':
            print('CHAMBER_BACKLOG drained', flush=True)
            break
        time.sleep(2)
    else:
        raise RuntimeError('recovery service remains active; inspect pending events and dirty days before Temporal cutover')
    if (UNITS/TS).exists() or DROP.exists():
        if (DEST/'ready').exists() and run('systemctl','is-active',TS)=='active':
            print('TEMPORAL_RELEASE already installed');return
        raise RuntimeError('existing service requires reconciliation')
    prepare_files()
    configure()
    try:
        pg_rules(True)
        tsunit=f"""[Unit]
Description=Lead restored workflow service
Requires={DB}
BindsTo=lead-home-net.service
JoinsNamespaceOf=lead-home-net.service
After={DB} lead-home-net.service
PartOf=lead-home-live.target
[Service]
User=lead-r-temporal
Group=lead-r-temporal
PrivateNetwork=yes
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
NoNewPrivileges=yes
TemporaryFileSystem=/srv:ro
TemporaryFileSystem={V}:ro
BindReadOnlyPaths={LIVE}/pgsock:{V}/sock
BindReadOnlyPaths={DEST}/temporal-server:{V}/workflow/temporal-server
BindReadOnlyPaths={DEST}/config:{V}/workflow/config
Environment=GOMAXPROCS=2
ExecStart=/usr/bin/env {V}/workflow/temporal-server --root {V}/workflow --config config --env home start
Restart=on-failure
RestartSec=10
UMask=0077
LimitCORE=0
{credentials(TS, ('ca.crt','server.key','server.crt'))}
[Install]
WantedBy=lead-home-live.target
"""
        write(UNITS/TS,tsunit)
        run('systemctl','daemon-reload');run('systemd-analyze','--recursive-errors=no','verify',TS)
        run('systemctl','enable','--now',TS)
        # mTLS health + restored namespace, probed by the existing PC SDK as the app service identity.
        drop=f"""[Unit]
Requires={TS}
After={TS}
[Service]
BindReadOnlyPaths={DEST}/agent_analytics_temporal.py:{V}/agent_analytics_temporal.py
Environment=LEAD_DEPLOYMENT_MODE=production LEAD_TEMPORAL_TLS=1 LEAD_TEMPORAL_ADDRESS=127.0.0.1:17233 LEAD_TEMPORAL_NAMESPACE=lead-prod LEAD_ANALYTICS_TASK_QUEUE={QUEUE}
Environment=LEAD_TEMPORAL_CA_FILE=%d/ca.crt LEAD_TEMPORAL_CERT_FILE=%d/worker.crt LEAD_TEMPORAL_KEY_FILE=%d/worker.key
{credentials(DATA, ('ca.crt','worker.crt','worker.key'))}
ExecStart=
ExecStart=/usr/bin/env {V}/bin/py/bin/python3.12 -B -u {V}/agent_analytics_temporal.py --agent {AID}
"""
        write(DROP,drop)
        run('systemctl','daemon-reload');run('systemd-analyze','--recursive-errors=no','verify',DATA);run('systemctl','restart',DATA)
        for _ in range(100):
            inv=run('systemctl','show',DATA,'-p','InvocationID','--value')
            if inv and 'ANALYTICS_TEMPORAL_READY '+QUEUE in run('journalctl','--no-pager','-o','cat','_SYSTEMD_INVOCATION_ID='+inv):
                write(DEST/'ready','lead-prod: '+QUEUE,0o600)
                print('TEMPORAL_RELEASE lead-prod namespace and dedicated queue ready');return
            time.sleep(1)
        raise RuntimeError('analytics Temporal readiness timeout')
    except BaseException:
        rollback()
        raise


def rollback():
    if DROP.is_file() and str(DEST) in DROP.read_text():
        DROP.unlink()
    if (UNITS/TS).is_file() and str(DEST) in (UNITS/TS).read_text():
        subprocess.run(['systemctl','disable','--now',TS],capture_output=True)
        (UNITS/TS).unlink()
    run('systemctl','daemon-reload')
    pg_rules(False)
    run('systemctl','restart',DATA)
    (DEST/'ready').unlink(missing_ok=True)


if __name__=='__main__':
    os.umask(0o077)
    if os.geteuid() or run('findmnt','-nr','-M',VAULT,'-o','UUID')!='16a81cc6-d008-4bfc-acb5-2aa917c06dc9':
        raise SystemExit('requires mounted PC vault and administrator')
    if sys.argv[1:] == ['rollback']:rollback()
    else:start()

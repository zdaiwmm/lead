"""Activate a checksum-verified workspace and target-only analytics recovery release."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

VAULT = Path('/srv/lead-prod-vault')
LIVE = VAULT/'lead-home-live'
BIN = LIVE/'bin'
UNITS = Path('/etc/systemd/system')
UNIT = 'lead-chamber-analytics'
AID = 'b1f9a23c-1153-4c67-a63b-11ebd264c687'
V = '/run/lead-home'
APP = 'original/services/api/lead_app'
SOURCE = Path(__file__).resolve().parent
MODULES = ('telegram_group_board.py', 'analytics_reporting.py')


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120).stdout.strip()


def install_file(path, data, mode=0o644):
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(path.name+'.new')
    tmp.write_bytes(data)
    tmp.chmod(mode)
    tmp.replace(path)


def health(service, port, path, code):
    for _ in range(35):
        pid = run('systemctl', 'show', service, '-p', 'MainPID', '--value')
        if pid.isdigit() and int(pid)>1:
            check = subprocess.run(['nsenter','-t',pid,'-n','--','curl','--noproxy','*','--silent',
                                    '--max-time','2','-o','/dev/null','-w','%{http_code}',
                                    f'http://127.0.0.1:{port}{path}'], capture_output=True,text=True)
            if check.stdout == code:
                return
        time.sleep(1)
    raise RuntimeError(service+' health probe failed')


def unit_file(release):
    return f"""[Unit]
Description=Chamber analytics recovery
Requires=lead-home-db.service
BindsTo=lead-home-net.service
After=lead-home-db.service lead-home-net.service
PartOf=lead-home-live.target
JoinsNamespaceOf=lead-home-net.service
[Service]
User=lead-r-app
Group=lead-r-app
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
PrivateNetwork=yes
LimitCORE=0
TemporaryFileSystem=/srv:ro
TemporaryFileSystem={V}:ro
BindReadOnlyPaths={BIN}:{V}/bin
BindReadOnlyPaths={LIVE}/pgsock:{V}/sock
BindReadOnlyPaths={LIVE}/state/config.json:{V}/state/config.json
BindReadOnlyPaths=-{LIVE}/state/geoip:{V}/state/geoip
BindReadOnlyPaths={release}/recover_agent_analytics.py:{V}/recover_agent_analytics.py
BindReadOnlyPaths={release}/analytics_reporting.py:{V}/bin/{APP}/analytics_reporting.py
Environment=PYTHONPATH={V}/bin/original/services/api:{V}/bin/site
Environment=PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 LEAD_STATE_DIR={V}/state LEAD_DB_HOST={V}/sock LEAD_DB_PORT=25434 LEAD_DB_SSLMODE=disable OTEL_SDK_DISABLED=true HOME=/tmp
Environment="PGOPTIONS=-c jit=off"
ExecStart=/usr/bin/env {V}/bin/py/bin/python3.12 -B -u {V}/recover_agent_analytics.py --agent {AID}
Restart=on-failure
RestartSec=15
[Install]
WantedBy=lead-home-live.target
"""


def main(rollback=False):
    if os.geteuid() or run('findmnt','-nr','-M',str(VAULT),'-o','UUID') != '16a81cc6-d008-4bfc-acb5-2aa917c06dc9':
        raise RuntimeError('requires PC administrator and mounted production vault')
    meta=json.loads((SOURCE/'manifest.json').read_text())
    release=LIVE/'chamber-workspace-releases'/meta['build']
    if not release.parent.exists():
        release.parent.mkdir(mode=0o755)
    web=UNITS/'lead-home-web.service.d'/'89-chamber-workspace.conf'
    api=UNITS/'lead-home-api.service.d'/'89-chamber-workspace.conf'
    unit=UNITS/(UNIT+'.service')
    marker=release/'installed'
    if rollback:
        if not marker.is_file():
            raise RuntimeError('release has no installation record')
        revert((web,api,unit),release)
        return
    if marker.exists():
        health('lead-home-web',3100,'/','200')
        run('systemctl','is-active',UNIT)
        print('CHAMBER_RELEASE already installed',meta['build'])
        return
    if any(p.exists() for p in (web,api,unit)):
        raise RuntimeError('an existing overlay must be reconciled first')
    for name in MODULES:
        if hashlib.sha256((BIN/APP/name).read_bytes()).hexdigest() not in meta['baselines'][name]:
            raise RuntimeError('baseline changed for '+name)
    if (BIN/'web/.next/BUILD_ID').read_text().strip()!=meta['previous_build']:
        raise RuntimeError('web baseline changed')
    release.mkdir(mode=0o755,exist_ok=True)
    for name in (*MODULES,'recover_agent_analytics.py'):
        install_file(release/name,(SOURCE/name).read_bytes())
    nextdir=release/'.next'
    shutil.copytree(SOURCE/'.next',nextdir,dirs_exist_ok=True)
    (nextdir/'cache').mkdir(exist_ok=True)
    # Keep lazy-loaded chunks of tabs opened before the rollout available.
    for src in (BIN/'web/.next/static').rglob('*'):
        dst=nextdir/'static'/src.relative_to(BIN/'web/.next/static')
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,dst)
    for path in [nextdir,*nextdir.rglob('*')]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    try:
        install_file(web, f'[Service]\nBindReadOnlyPaths={nextdir}:{V}/bin/web/.next\n'.encode())
        install_file(api, ('[Service]\nEnvironment="PGOPTIONS=-c jit=off"\n'+''.join(
            f'BindReadOnlyPaths={release/name}:{V}/bin/{APP}/{name}\n' for name in MODULES)).encode())
        install_file(unit,unit_file(release).encode())
        run('systemctl','daemon-reload')
        run('systemd-analyze','--recursive-errors=no','verify',unit.name)
        run('systemctl','restart','lead-home-api')
        health('lead-home-api',8088,'/api/agents','401')
        run('systemctl','restart','lead-home-web')
        health('lead-home-web',3100,'/','200')
        run('systemctl','enable','--now',unit.name)
        since=run('systemctl','show',UNIT,'-p','InvocationID','--value')
        for _ in range(30):
            logs=run('journalctl','--no-pager','-o','cat','_SYSTEMD_INVOCATION_ID='+since)
            baseline=[l for l in logs.splitlines() if l.startswith('ANALYTICS_BASELINE ')]
            if baseline:
                print(baseline[-1],flush=True)
                break
            time.sleep(1)
        else:
            raise RuntimeError('analytics consumer failed its scoped database check')
        marker.write_text(meta['build'])
    except BaseException:
        revert((web,api,unit),release)
        raise
    print('CHAMBER_RELEASE installed',meta['build'],flush=True)


def revert(files, release):
    subprocess.run(['systemctl','disable','--now',UNIT+'.service'],capture_output=True)
    for path in files:
        if path.is_file() and str(release) in path.read_text():
            path.unlink()
    run('systemctl','daemon-reload')
    run('systemctl','restart','lead-home-api','lead-home-web')
    health('lead-home-api',8088,'/api/agents','401')
    health('lead-home-web',3100,'/','200')
    (release/'installed').unlink(missing_ok=True)
    print('CHAMBER_RELEASE rolled back', flush=True)


if __name__ == '__main__':
    rolling_back = sys.argv[1:] == ['rollback']
    workflow = SOURCE/'temporal/install.py'
    if rolling_back and workflow.is_file() and (LIVE/'chamber-temporal-v1/ready').is_file():
        subprocess.run(['/usr/bin/python3','-ISB',str(workflow),'rollback'],check=True)
    main(rolling_back)
    if not rolling_back:
        subprocess.run(['/usr/bin/python3','-ISB',str(workflow),'install'],check=True)

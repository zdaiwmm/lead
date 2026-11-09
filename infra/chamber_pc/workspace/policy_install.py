"""Install the Chamber API, UI and resident-only discovery overlay. Keep the previous build available."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from base_install import AID, APP, BIN, LIVE, UNITS, V, VAULT, health, install_file, run, unit_file

SOURCE=Path(__file__).resolve().parent
TG='lead-tg-b1f9a23c'
SERVICES=('lead-home-api','lead-home-web',TG)
VERIFY='lead-chamber-policy-check'


def drop(service):
    return UNITS/(service+'.service.d')/'95-chamber-operations.conf'


def restart():
    run('systemctl','daemon-reload')
    for service in SERVICES:
        run('systemctl','restart',service)
        if service==TG:
            run('systemctl','is-active',service)
        else:
            health(service,3100 if service.endswith('web') else 8088,'/' if service.endswith('web') else '/api/agents',
                   '200' if service.endswith('web') else '401')


def verify(base,release):
    # Run as the reporting service identity and tenant, with the same socket and network isolation.
    u=unit_file(base)
    u=u.replace('Restart=on-failure\nRestartSec=15','Type=oneshot')
    old='ExecStart=/usr/bin/env '+V+'/bin/py/bin/python3.12 -B -u '+V+'/recover_agent_analytics.py --agent '+AID
    if old not in u: raise ValueError('unknown analytics unit layout')
    u=u.replace(old,f'BindReadOnlyPaths={release}/policy_probe.py:{V}/policy_probe.py\nExecStart=/usr/bin/env {V}/bin/py/bin/python3.12 -B -u {V}/policy_probe.py')
    path=Path('/run/systemd/system')/(VERIFY+'.service')
    install_file(path,u.encode())
    try:
        run('systemctl','daemon-reload');run('systemctl','start',VERIFY)
        log=run('journalctl','-u',VERIFY,'--no-pager','-o','cat','-g','^CHAMBER_METRICS','-n','1')
        if not log.startswith('CHAMBER_METRICS '): raise ValueError('missing scoped analytics evidence')
        print(log,flush=True)
    finally:
        path.unlink(missing_ok=True);run('systemctl','daemon-reload')


def main():
    if os.geteuid() or run('findmnt','-nr','-M',str(VAULT),'-o','UUID')!='16a81cc6-d008-4bfc-acb5-2aa917c06dc9':
        raise RuntimeError('PC administrator and mounted vault required')
    m=json.loads((SOURCE/'manifest.json').read_text())
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',m['build']): raise ValueError('invalid web build')
    base=LIVE/'chamber-workspace-releases'/m['base']
    release=LIVE/('chamber-operations-'+m['build'])
    if not (base/'installed').exists(): raise RuntimeError('workspace baseline missing')
    if sys.argv[1:]==['rollback']:
        for svc in SERVICES:
            f=drop(svc)
            if f.is_file() and str(release) in f.read_text(): f.unlink()
        restart();return
    configs={}
    for svc,names in [('lead-home-api',m['api']), (TG,m['resident'])]:
        binds=[]
        for name in names:
            target=Path(APP)/(name+'.py')
            if hashlib.sha256((BIN/target).read_bytes()).hexdigest() not in m['baselines'][name]:
                raise RuntimeError('base file changed: '+name)
            binds.append(f'BindReadOnlyPaths={release}/app/{name}.py:{V}/bin/{target}')
        configs[svc]='[Service]\n'+'\n'.join(binds)+'\n'
    configs['lead-home-web']=f'[Service]\nBindReadOnlyPaths={release}/web:{V}/bin/web/.next\n'
    for svc in SERVICES:
        f=drop(svc)
        if f.exists() and f.read_text()!=configs[svc]: raise RuntimeError('unexpected overlay '+svc)
    if not (release/'ready').is_file():
        release.mkdir(exist_ok=True,mode=0o755);release.chmod(0o755)
        for name in ('web','app'):
            dest=release/name
            shutil.copytree(SOURCE/name,dest,dirs_exist_ok=True)
            for p in [dest,*dest.rglob('*')]: p.chmod(0o755 if p.is_dir() else 0o644)
        for old in (base/'.next/static').rglob('*'):
            dest=release/'web/static'/old.relative_to(base/'.next/static')
            if old.is_file() and not dest.exists():
                dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(old,dest)
        for folder in (release/'web/static').rglob('*'):
            if folder.is_dir(): folder.chmod(0o755)
        install_file(release/'policy_probe.py',(SOURCE/'policy_probe.py').read_bytes())
        previous={svc:drop(svc).read_bytes() if drop(svc).exists() else None for svc in SERVICES}
        try:
            for svc in SERVICES: install_file(drop(svc),configs[svc].encode())
            restart()
            verify(base,release)
            install_file(release/'ready',m['build'].encode())
        except BaseException:
            for svc,content in previous.items():
                if content is None: drop(svc).unlink(missing_ok=True)
                else: install_file(drop(svc),content)
            restart()
            raise
    else:
        for svc in SERVICES:
            if not drop(svc).is_file() or drop(svc).read_text()!=configs[svc]: raise RuntimeError('missing policy overlay '+svc)
        verify(base,release)
    print('CHAMBER_POLICY_READY',m['build'],flush=True)
    subprocess.run(['/usr/bin/python3','-ISB',str(SOURCE/'temporal/install.py'),'install'],check=True)


if __name__=='__main__':
    # Python -I/-S omits the script folder. The entry point imports the sibling
    # helper only from this administrator-owned verified extraction.
    main()

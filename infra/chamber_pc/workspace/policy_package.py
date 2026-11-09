"""Package the incremental Chamber policy release on top of the verified workspace."""
import json
import tarfile
from io import BytesIO
from package import HERE, NEXT, REPO, digest

API = ('api','community_growth','community_discovery','hybrid_research','community_runtime','model','telegram_group_board')
TG = ('community_growth','community_discovery','hybrid_research')
BASE_BUILD = 'DEFiCMcFpyUonQuDswDoK'


def build():
    build_id=(NEXT/'BUILD_ID').read_text().strip()
    if build_id==BASE_BUILD:
        raise ValueError('build the operator review workspace first')
    entries={'install.py':(HERE/'policy_install.py').read_bytes(),'base_install.py':(HERE/'install.py').read_bytes(),
             'policy_probe.py':(HERE/'policy_probe.py').read_bytes(),'temporal/install.py':(HERE/'temporal/install.py').read_bytes(),
             'temporal/agent_analytics_temporal.py':(REPO/'scripts/agent_analytics_temporal.py').read_bytes()}
    baselines=json.loads((HERE/'baselines.json').read_text())['policy']
    for name in API:
        fname=name+'.py';sub='services/api/lead_app/'+fname
        entries['app/'+fname]=(REPO/sub).read_bytes()
    manifest=dict(build=build_id,base=BASE_BUILD,api=API,resident=TG,baselines=baselines)
    entries['manifest.json']=json.dumps(manifest).encode()
    for p in sorted(NEXT.rglob('*')):
        rel=p.relative_to(NEXT)
        if p.is_file() and not p.is_symlink() and rel.parts[0] not in ('cache','types','trace','trace-build','diagnostics'):
            entries['web/'+str(rel)]=p.read_bytes()
    out=HERE/'policy.tar.gz'
    with tarfile.open(out,'w:gz') as tar:
        for name,data in entries.items():
            item=tarfile.TarInfo(name);item.size=len(data);item.mode=0o644;tar.addfile(item,BytesIO(data))
    (HERE/'policy.SHA256').write_text(digest(out.read_bytes())+'\n')
    return build_id


if __name__=='__main__':
    print(build())

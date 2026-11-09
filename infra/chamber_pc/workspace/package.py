"""Build the fixed PC release from a verified Next build and the corresponding app source."""
import hashlib
import json
import tarfile
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NEXT = REPO/'apps/web/.next'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def package():
    baseline = json.loads((HERE/'baselines.json').read_text())
    payload = {'install.py': (HERE/'install.py').read_bytes(),
               'recover_agent_analytics.py': (REPO/'scripts/recover_agent_analytics.py').read_bytes()}
    payload['temporal/install.py']=(HERE/'temporal/install.py').read_bytes()
    payload['temporal/agent_analytics_temporal.py']=(REPO/'scripts/agent_analytics_temporal.py').read_bytes()
    for name in ('telegram_group_board.py','analytics_reporting.py'):
        sub = Path('services/api/lead_app')/name
        payload[name]=(REPO/sub).read_bytes()
    payload['manifest.json']=json.dumps({'build':(NEXT/'BUILD_ID').read_text().strip(),
        'previous_build':baseline['previous_build'],'baselines':baseline['workspace']}).encode()
    for p in sorted(NEXT.rglob('*')):
        rel=p.relative_to(NEXT)
        if p.is_file() and not p.is_symlink() and rel.parts[0] not in ('cache','types','trace','trace-build','diagnostics'):
            payload['.next/'+str(rel)]=p.read_bytes()
    with tarfile.open(HERE/'workspace.tar.gz','w:gz') as out:
        for name,data in payload.items():
            ent=tarfile.TarInfo(name);ent.mode=0o644;ent.size=len(data)
            out.addfile(ent,BytesIO(data))
    sha=digest((HERE/'workspace.tar.gz').read_bytes())
    (HERE/'SHA256').write_text(sha+'\n')
    print(sha)


if __name__=='__main__':
    package()

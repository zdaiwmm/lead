"""Stage a fixed bundle as the SSH operator. sudo runs only its verified bytes."""
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDRESS = 'lead-pc'
SSH = ['ssh','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8']
# Read the operator-owned upload with the uploader's privileges before checksum verification.
BOOT = """import fcntl,hashlib,io,pathlib,subprocess,sys,syslog,tarfile,tempfile
expected,path,mode=sys.argv[1:]
lock=open("/run/chamber-workspace-install.lock","a")
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
syslog.openlog("chamber-workspace-install")
raw=subprocess.check_output(['/usr/bin/setpriv','--reuid=mijiu','--regid=mijiu','--init-groups','--','/usr/bin/head','-c','10000000',path])
if hashlib.sha256(raw).hexdigest()!=expected: raise SystemExit('release checksum mismatch')
with tempfile.TemporaryDirectory(dir='/var/tmp') as root:
  folder=pathlib.Path(root)
  with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
    for item in archive:
      relative=pathlib.Path(item.name)
      if not item.isfile() or relative.is_absolute() or '..' in relative.parts: raise ValueError('invalid release member')
      target=folder/relative;target.parent.mkdir(parents=True,exist_ok=True)
      with target.open('xb') as stream: stream.write(archive.extractfile(item).read())
  syslog.syslog('begin '+expected)
  result=subprocess.run(['/usr/bin/python3','-ISB',str(folder/'install.py'),mode])
  syslog.syslog('exit '+str(result.returncode)+' '+expected)
  raise SystemExit(result.returncode)
"""


def main():
    policy='--policy' in sys.argv
    blob=HERE/('policy.tar.gz' if policy else 'workspace.tar.gz')
    sha=(HERE/('policy.SHA256' if policy else 'SHA256')).read_text().strip()
    if hashlib.sha256(blob.read_bytes()).hexdigest()!=sha:
        raise SystemExit('本地安装包校验失败')
    folder=subprocess.check_output(SSH+[ADDRESS,'mktemp -d /home/mijiu/chamber-workspace.XXXXXXXX'],text=True).strip()
    if not re.fullmatch(r'/home/mijiu/chamber-workspace\.[A-Za-z0-9]{8}',folder):
        raise SystemExit('unexpected staging path')
    path=folder+'/workspace.tar.gz'
    with blob.open('rb') as f:
        subprocess.run(SSH+[ADDRESS,'umask 077 && cat > '+shlex.quote(path)],stdin=f,check=True)
    mode='rollback' if 'rollback' in sys.argv[1:] else 'install'
    sudo = ['sudo','-n'] if '--noninteractive' in sys.argv else ['sudo']
    cmd=shlex.join([*sudo,'/usr/bin/python3','-ISB','-c',BOOT,sha,path,mode])
    print('PC 工作台与统计发布包 SHA-256',sha,flush=True)
    try:
        subprocess.run(SSH+['-tt',ADDRESS,cmd],check=True)
    finally:
        subprocess.run(SSH+[ADDRESS,shlex.join(['unlink',path])+' && '+shlex.join(['rmdir',folder])])


if __name__=='__main__':
    main()

"""Keep one end-to-end TLS route from the target PC to its configured model host."""
import os
import secrets
import shlex
import signal
import subprocess
import time

DIRECTORY = '/srv/lead-prod-vault/incoming/telegram-egress-b1f9a23c/sockets'
DESTINATION = 'api.openai.com:443'
REMOTE = """
import fcntl,os,pwd,select,stat,struct,sys
from pathlib import Path
temporary = Path(sys.argv[1])
active = temporary.with_name('model-openai-b1f9a23c.sock')
uid = os.getuid()
def owned_socket(path):
    item = path.lstat()
    if not stat.S_ISSOCK(item.st_mode) or item.st_uid != uid:
        raise RuntimeError('unexpected socket owner or type')
    return item.st_ino
def entry(tag, perm, identifier=0xffffffff):
    return struct.pack('<HHI',tag,perm,identifier)
with (temporary.parent/'.model-openai.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    inode = owned_socket(temporary)
    if os.path.lexists(active):
        owned_socket(active)
    target = pwd.getpwnam('lead-r-app').pw_uid
    # Linux POSIX ACL: owner and the target service UID only.
    acl = struct.pack('<I',2) + entry(1,6) + entry(2,6,target) + entry(4,0) + entry(16,6) + entry(32,0)
    os.setxattr(temporary,'system.posix_acl_access',acl)
    os.replace(temporary,active)
    try:
        while select.select([0],[],[],65)[0] and os.read(0,1024):
            pass
    finally:
        if os.path.lexists(active) and owned_socket(active)==inode:
            active.unlink()
"""


def maintain():
    path = DIRECTORY + '/m-' + secrets.token_hex(6)
    cmd = ['/usr/bin/ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
           '-o','ConnectTimeout=8','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
           '-o','ExitOnForwardFailure=yes','-R',path+':'+DESTINATION,'lead-pc',
           shlex.join(['/usr/bin/python3','-IS','-c',REMOTE,path])]
    with subprocess.Popen(cmd,stdin=subprocess.PIPE) as ssh:
        def shutdown(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM,shutdown)
        try:
            while ssh.poll() is None:
                ssh.stdin.write(b'.')
                ssh.stdin.flush()
                time.sleep(12)
        except BrokenPipeError:
            pass
        finally:
            try:
                ssh.stdin.close()
            except BrokenPipeError:
                pass
            try:
                ssh.wait(timeout=8)
            except subprocess.TimeoutExpired:
                ssh.terminate()
        return ssh.wait()


if __name__ == '__main__':
    raise SystemExit(maintain())

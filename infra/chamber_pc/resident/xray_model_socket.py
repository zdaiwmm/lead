"""Grant the target executor access after Xray creates its owner-only socket."""
import os
import pwd
import stat
import struct
import time
from pathlib import Path

socket_path = Path('/srv/lead-prod-vault/incoming/telegram-egress-b1f9a23c/sockets/model-openai-b1f9a23c.sock')


def acl_entry(tag, permissions, identifier=0xffffffff):
    return struct.pack('<HHI', tag, permissions, identifier)


for attempt in range(100):
    try:
        item = socket_path.lstat()
        break
    except FileNotFoundError:
        time.sleep(0.05)
else:
    raise SystemExit('Xray did not create its target socket')
if not stat.S_ISSOCK(item.st_mode) or item.st_uid != os.getuid():
    raise SystemExit('Unexpected owner or file type')
target = pwd.getpwnam('lead-r-app').pw_uid
acl = (struct.pack('<I', 2) + acl_entry(1, 6) + acl_entry(2, 6, target)
       + acl_entry(4, 0) + acl_entry(16, 6) + acl_entry(32, 0))
os.setxattr(socket_path, 'system.posix_acl_access', acl, follow_symlinks=False)

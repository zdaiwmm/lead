#!/usr/bin/env python3
"""Fixed 0034-to-0035 shared-executor upgrade using the attested upgrade engine.

This entrypoint never enrolls a Node, changes the execution default, or moves
an existing request. It keeps the same protected approval and maintenance hold.
"""
import hashlib
import os
import re
import runpy
import stat
import subprocess
from pathlib import Path

STORE = Path('/srv/lead-prod-vault/lead-git-releases')


def attested_engine():
    """Do not execute even the common engine before checking its exact source."""
    entry=Path(__file__).absolute()
    stage=entry.parents[2]
    sha=stage.name.removeprefix('maintenance-')
    engine=entry.with_name('unified_browser_upgrade.py')
    if (os.geteuid()!=0 or not re.fullmatch('[a-f0-9]{40}',sha)
            or stage!=STORE/('maintenance-'+sha) or entry.parent!=stage/'source/scripts'):
        raise ValueError('Use the attested shared upgrade source in its private stage')
    for path in (stage,stage/'source',entry.parent,entry,engine):
        info=path.lstat()
        if (path.resolve()!=path or info.st_uid!=0 or info.st_mode & (0o077 if path==stage else 0o022)
                or not (stat.S_ISREG(info.st_mode) if path in (entry,engine) else stat.S_ISDIR(info.st_mode))):
            raise ValueError('Shared upgrade source drift')
    mirror=STORE/'lead.git'
    def git(*args):
        return subprocess.check_output(['/usr/bin/git','--git-dir',str(mirror),*args],text=True).strip()
    git('merge-base','--is-ancestor',sha,'refs/heads/main')
    for path in (entry,engine):
        expected=git('rev-parse',sha+':scripts/'+path.name)
        data=path.read_bytes()
        actual=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if not re.fullmatch('[a-f0-9]{40}',expected) or actual!=expected:
            raise ValueError('Shared upgrade source drift')
    return engine


def main():
    engine = runpy.run_path(str(attested_engine()))
    engine['main'](shared=True)


if __name__ == '__main__':
    main()

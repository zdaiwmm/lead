"""Run the business release as a bounded, credential-free subprocess."""
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

MOUNT = Path('/run/lead-home/business')
MAX_CODE = 65536


def invoke(code, request):
    if not 0 < len(code) <= MAX_CODE:
        raise ValueError('invalid_business_release_size')
    data = json.dumps(request, ensure_ascii=False).encode()
    if len(data)>16384:
        raise ValueError('invalid_business_request_size')
    # Hand bwrap an anonymous file descriptor on both versions of CPython.
    with tempfile.TemporaryFile() as source:
        source.write(code)
        source.seek(0)
        fd = source.fileno()
        args = ['/usr/bin/bwrap','--unshare-all','--unshare-user','--disable-userns','--cap-drop','ALL',
                '--die-with-parent','--new-session','--clearenv','--ro-bind','/usr','/usr',
                '--symlink','usr/lib','/lib','--symlink','usr/lib64','/lib64','--proc','/proc',
                '--dev','/dev','--size','1048576','--tmpfs','/tmp','--ro-bind-data',str(fd),'/business.py',
                '--','/usr/bin/prlimit','--as=134217728','--cpu=2','--nproc=0','--nofile=24','--fsize=8192',
                '--','/usr/bin/python3','-I','-S','-B','/business.py']
        with tempfile.TemporaryFile() as out:
            subprocess.run(args,input=data,stdout=out,stderr=subprocess.DEVNULL,pass_fds=(fd,),
                           cwd='/',env={'PATH':'/usr/bin:/bin'},timeout=5,check=True)
            out.seek(0)
            result=json.loads(out.read(8193))
    if not isinstance(result,dict):
        raise ValueError('invalid_business_response')
    if request['event']=='admit':
        if set(result)!={'execute'} or type(result['execute']) is not bool:
            raise ValueError('invalid_business_admission')
    elif (set(result)!={'body'} or not isinstance(result['body'],str)
          or not 0<len(result['body'].strip())<=700):
        raise ValueError('invalid_business_copy')
    return result


def evaluate(request):
    state=json.loads((MOUNT/'active.json').read_text())
    sha=state['current']
    if not re.fullmatch('[0-9a-f]{64}',sha):
        raise ValueError('invalid_business_release')
    code=(MOUNT/(sha+'.py')).read_bytes()
    if hashlib.sha256(code).hexdigest()!=sha:
        raise ValueError('business_release_changed')
    return invoke(code,request)

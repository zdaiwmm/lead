#!/usr/bin/env python3
"""Operator CLI. The approve operation transports a detached owner signature."""
import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--host',default='lead-pc')
parser.add_argument('--gateway',action='store_true',help='Use a per-device restricted SSH login')
subs=parser.add_subparsers(dest='action',required=True)
for op in ('status','audit','rollback'):subs.add_parser(op)
for op in ('verify','deploy'):
    subs.add_parser(op).add_argument('file',type=Path)
replace=subs.add_parser('replace')
replace.add_argument('target',choices=('resident','telegram','community','selection','model','broker','backup'))
replace.add_argument('file',type=Path)
enroll=subs.add_parser('enroll')
enroll.add_argument('device')
enroll.add_argument('--login',type=Path)
enroll.add_argument('--sign',type=Path)
subs.add_parser('revoke').add_argument('device')
review=subs.add_parser('review')
review.add_argument('id')
review.add_argument('folder',type=Path)
subs.add_parser('apply').add_argument('id')
approve=subs.add_parser('approve')
approve.add_argument('id')
approve.add_argument('approver')
approve.add_argument('signature',type=Path)
args=parser.parse_args()
if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.@-]{0,190}',args.host):
    parser.error('Invalid SSH alias')
r={'action':args.action}
if args.action in ('verify','deploy','replace'):
    data=args.file.read_bytes()
    if len(data)>350000:parser.error('File too large')
    r['data']=base64.b64encode(data).decode()
if args.action in ('replace','enroll','revoke'):
    r.update(action='propose',change=args.action)
    if args.action=='replace':r['target']=args.target
    else:r['device']=args.device
    if args.action=='enroll':
        for name in ('login','sign'):
            if p:=getattr(args,name):
                if p.suffix!='.pub':parser.error('Only .pub public keys are accepted')
                r[name]=p.read_text().strip()
if args.action in ('review','approve','apply'):r['id']=args.id
if args.action=='approve':
    r.update(approver=args.approver,signature=base64.b64encode(args.signature.read_bytes()).decode())
cmd=['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
     args.host,'release' if args.gateway else 'sudo -n /usr/local/lib/lead-target-release/release_server.py']
p=subprocess.run(cmd,input=json.dumps(r).encode(),stdout=subprocess.PIPE)
if not p.stdout:raise SystemExit(p.returncode or 1)
result=json.loads(p.stdout)
if args.action=='review' and p.returncode==0:
    args.folder.mkdir(mode=0o700,parents=True,exist_ok=False)
    (args.folder/'manifest.json').write_bytes(result['manifest'].encode())
    if result.get('data') is not None:(args.folder/'payload.py').write_bytes(base64.b64decode(result['data'],validate=True))
    print(result['manifest'],end='')
else:print(json.dumps(result,indent=2,ensure_ascii=False))
raise SystemExit(p.returncode)

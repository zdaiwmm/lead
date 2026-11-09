"""Read the Chamber service journal and score one production acceptance window.

A peer due at a sampled deadline remains in the denominator after it fails or
disappears. Pre-existing backlog and windows with missing telemetry are explicit.
"""
import argparse
import fcntl
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import subprocess
import time
from pathlib import Path

ZONE=timezone(timedelta(hours=8))
UNIT='lead-tg-b1f9a23c'


def stamp(value):
    return datetime.fromisoformat(value).astimezone(ZONE)


def evaluate(start, asof, entries):
    end=start+timedelta(hours=24)
    watermark=end+timedelta(minutes=30)
    previous={}
    visits={}
    cohort={}
    carry=set()
    candidates=Counter()
    sample_times=[]
    mode={}
    holds={}
    ledger={}
    writes=0
    for at,event in entries:
        if at>min(asof,watermark): break
        # A due peer cannot be silently removed after its deadline.
        for peer,due in previous.items():
            if start<=due<end and due<=at:
                cohort.setdefault((peer,due),None)
            elif due<start:
                carry.add(peer)
        kind=event.get('event')
        if kind=='patrol_schedule':
            sample_times.append(at)
            previous={str(r['peer']):stamp(r['due']) for r in event['eligible']}
            for peer,due in previous.items():
                if start<=due<end and due<=at:cohort.setdefault((peer,due),None)
        if kind=='observed' and event.get('group'):
            peer=str(event['group'])
            visits.setdefault(peer,[]).append(at)
        if kind=='cycle':candidates[event['reason']]+=1
        if kind=='execution_mode':mode={k:event.get(k) for k in ('mode','account_status')}
        if kind=='native_write_attempt':writes+=1
        if kind=='receipt_ledger':ledger[event['task_id']]={k:event.get(k) for k in ('task_id','peer','membership','delivery','native_receipt','last_check','scheduled_check','policy')}
        if kind=='target_hold':holds[event['peer']]={k:event.get(k) for k in ('fault','last_failure','next_read','owner','disposition')}
    cutoff=min(end,asof)
    for peer,due in previous.items():
        if start<=due<end and due<=cutoff:cohort.setdefault((peer,due),None)
    late=[];on_time=0;pending=0
    for peer,due in cohort:
        first=next((v for v in visits.get(peer,[]) if v>=due),None)
        if first and first-due<=timedelta(minutes=30):on_time+=1
        elif asof-due<timedelta(minutes=30):pending+=1
        else:late.append({'peer':peer,'due':due.isoformat(),'seen':first.isoformat() if first else None,
                          'hold':holds.get(peer)})
    times=[start,*sample_times,min(asof,watermark)]
    gaps=[(b-a).total_seconds() for a,b in zip(times,times[1:])]
    complete=asof>=watermark and bool(sample_times) and max(gaps,default=0)<=300
    eligible=len(cohort)
    result='observing' if asof<watermark else ('passed' if complete and eligible and on_time/eligible>=.9 and not writes
        else 'incomplete' if not complete or not eligible else 'failed')
    return dict(window_start=start.isoformat(),window_end=end.isoformat(),evaluation_ready_at=watermark.isoformat(),
        as_of=asof.isoformat(),state=result,due_visits=eligible,on_time=on_time,awaiting_deadline=pending,late=late,
        preexisting_backlog_groups=len(carry),max_sample_gap_seconds=round(max(gaps,default=0)),samples=len(sample_times),
        bound_visits=sum(map(len,visits.values())),unique_visited_groups=len(visits),cycles=candidates,
        native_write_attempts=writes,last_mode=mode,receipt_ledger=list(ledger.values()),target_holds=holds)


def read(start, now):
    args=['journalctl','-u',UNIT,'-o','json','--no-pager','--since','@'+str(int(start.timestamp())),'--until','@'+str(int(now.timestamp())+1),
          '--grep','^TG_RESULT .*"event": "(patrol_schedule|observed|cycle|execution_mode|receipt_ledger|target_hold|native_write_attempt)"']
    p=subprocess.run(args,capture_output=True,text=True)
    if p.returncode not in (0,1) or (p.returncode==1 and p.stderr.strip()):raise RuntimeError('cannot read service journal: '+p.stderr.strip()[:180])
    for s in p.stdout.splitlines():
        if not s.startswith('{'):continue
        r=json.loads(s)
        yield datetime.fromtimestamp(int(r['__REALTIME_TIMESTAMP'])/1e6,ZONE),json.loads(r['MESSAGE'][10:])


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--start',required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--watch',action='store_true')
    opts=parser.parse_args();start=stamp(opts.start)
    if opts.watch:
        lock=opts.output.with_suffix('.lock').open('a')
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    while True:
        now=datetime.now(ZONE)
        report=evaluate(start,now,read(start,min(now,start+timedelta(hours=24,minutes=30))))
        tmp=opts.output.with_suffix('.new');tmp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');tmp.replace(opts.output)
        if not opts.watch or report['state']!='observing':break
        remaining=(start+timedelta(hours=24,minutes=30)-now).total_seconds()
        time.sleep(max(1,min(300,remaining)))

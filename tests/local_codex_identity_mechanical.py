"""Actual B private-identity positive/negative tools, with zero model calls."""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from local_codex_c22_mechanical import post


def main():
    pairing=post('/fixture/pairing',{})
    device=post('/executor/v1/pair',{'code':pairing['code'],'name':'Zero-model identity namespace probe',
        'platform':'mechanical-fixture','capabilities':{'ready':True,'mechanical':True}})
    bearer=device['token']
    epoch=device['epoch']
    instance='identity-mechanical-'+str(uuid4())
    post('/fixture/c22/start',{'site':'b'})
    while True:
        polled=post('/executor/v1/poll',{'epoch':epoch,'instance_id':instance,'capabilities':{'ready':True,'mechanical':True}},bearer=bearer)
        epoch=polled['epoch']
        commands=polled.get('commands',[])
        if commands:
            command=commands[0]
            break
        time.sleep(.5)
    assert command['kind']=='run' and command['payload']['tool_context']['kind']=='connection'
    reference=command['request_id']
    post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
        'method':'command/ack','params':{'command_id':command['id']}}]},bearer=bearer)

    def raw(name,arguments=None):
        return post('/executor/v1/tools',{'epoch':epoch,'request_id':reference,'name':name,'arguments':arguments or {}},bearer=bearer)
    def tool(name,arguments=None):
        result=raw(name,arguments)
        assert not result.get('isError'),result['content'][0]
        return result.get('structuredContent') or json.loads(result['content'][0]['text'])
    def index(dom,pattern):
        lines=dom.splitlines()
        return [int(match[1]) for number,line in enumerate(lines) if (match:=re.search(r'\[(\d+)\]',line))
            and re.search(pattern,'\n'.join(lines[number:number+3]),re.I)][-1]
    observed=tool('browser_observe')
    origin=observed['url'].rstrip('/')
    assert origin+'/access' in observed['dom']
    tool('browser_navigate',{'url':origin+'/access'})
    observed=tool('browser_observe')
    tool('browser_input',{'index':index(observed['dom'],r'name=["\']?member_name\b'),'text':'saved_username','clear':True})
    observed=tool('browser_observe')
    tool('browser_input',{'index':index(observed['dom'],r'type=["\']?password\b'),'text':'saved_password','clear':True})
    observed=tool('browser_observe')
    tool('browser_click',{'index':index(observed['dom'],r'<button')})
    tool('browser_observe')
    subject=tool('inspect_self')
    assert subject['namespace']=='web' and subject['acting_subject']
    tool('browser_observe')
    mismatch=raw('report_connection',{'subject_selector':'dd:nth-of-type(1)'})
    assert mismatch.get('isError') is True,mismatch
    assert 'identity_private_current_subject_mismatch' in mismatch['content'][0]['text'],mismatch
    accepted=tool('report_connection',{'subject_selector':'dd:nth-of-type(2)'})
    assert accepted['status']=='authenticated'
    assert accepted['operating_subject']['external_id']==subject['acting_subject']
    post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
        'method':'result','params':{'state':'completed','output':{'mechanical':True,'model_requests':0}}}]},bearer=bearer)
    while True:
        status=post('/fixture/c22/status')
        if status['failure']:
            raise ValueError(status['failure'])
        stages=status['cases']['b']['stages']
        if stages:
            assert stages[0]['stage']=='login' and stages[0]['outcome']=='completed'
            assert stages[0]['source_post_delta']==1
            break
        time.sleep(.5)
    proof={'model_requests':0,'real_model':False,'private_namespace_mismatch_rejected':True,
        'fixed_feedback_reached_mcp':True,'matching_private_subject_fresh_verified':True,
        'source_credential_posts':1,'original_request_id':reference,'source_sha':status['cases']['b']['source_sha']}
    report=Path(os.environ['LEAD_C22_MECHANICAL_REPORT'])
    report.write_text(json.dumps(proof,indent=2))
    report.chmod(0o600)
    print(json.dumps(proof))


if __name__=='__main__':
    main()

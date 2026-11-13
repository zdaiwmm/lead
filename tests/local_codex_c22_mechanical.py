"""Zero-model protocol driver; original guarded tools must prove every fact.

Known A selectors belong only to this deterministic mechanical diagnostic. This
file is never imported by the real Codex driver or sent to the model.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

descriptor=json.loads(Path(os.environ['LEAD_C22_DESCRIPTOR']).read_text())
url=descriptor['url']


def post(path,body=None,*,bearer=None):
    headers={'content-type':'application/json'}
    if path.startswith('/fixture/'):
        headers['x-fixture-token']=descriptor['fixture_token']
    if bearer:
        headers['authorization']='Bearer '+bearer
    request=urllib.request.Request(url+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request,timeout=65) as response:
        return json.load(response)


def main():
    pairing=post('/fixture/pairing',{})
    device=post('/executor/v1/pair',{'code':pairing['code'],'name':'Explicit zero-model mechanical fixture',
        'platform':'mechanical-fixture','capabilities':{'ready':True,'mechanical':True}})
    bearer=device['token']
    epoch=device['epoch']
    instance='mechanical-'+str(uuid4())
    post('/fixture/c22/start',{'site':'a'})

    def tool(request_id,name,arguments=None):
        result=post('/executor/v1/tools',{'epoch':epoch,'request_id':request_id,'name':name,'arguments':arguments or {}},bearer=bearer)
        if result.get('isError'):
            raise ValueError('actual_guarded_tool_failed:'+name+':'+str(result['content'][0])[:700])
        text=next((item['text'] for item in result.get('content',[]) if item['type']=='text'),'{}')
        return result.get('structuredContent') or json.loads(text)

    def observe(reference):
        return tool(reference,'browser_observe')['dom']

    def index(dom,pattern):
        lines=dom.splitlines()
        found=[int(match[1]) for number,line in enumerate(lines) if (match:=re.search(r'\[(\d+)\]',line))
            and re.search(pattern,'\n'.join(lines[number:number+3]),re.I)]
        if not found:
            raise ValueError('actual_observed_index_missing:'+pattern)
        return found[-1]

    seen=set()
    while True:
        state=post('/fixture/c22/status')
        if state['failure']:
            raise ValueError(state['failure'])
        if state['cases'].get('a',{}).get('completed'):
            out=Path(os.environ['LEAD_C22_MECHANICAL_REPORT'])
            out.write_text(json.dumps(state,default=str,indent=2))
            out.chmod(0o600)
            print(json.dumps({'completed':True,'model_requests':0,'real_model':False,'site':'a',
                'stages':[{key:stage[key] for key in ('stage','outcome','source_post_delta')} for stage in state['cases']['a']['stages']]}))
            return
        polled=post('/executor/v1/poll',{'epoch':epoch,'instance_id':instance,'capabilities':{'ready':True,'mechanical':True}},bearer=bearer)
        epoch=polled['epoch']
        for command in polled.get('commands',[]):
            if command['id'] in seen:
                continue
            seen.add(command['id'])
            assert command['kind']=='run'
            reference=command['request_id']
            payload=command['payload']
            context=payload['tool_context']
            post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
                'method':'command/ack','params':{'command_id':command['id']}}]},bearer=bearer)
            dom=observe(reference)
            kind=context['kind']
            if kind=='connection':
                tool(reference,'browser_input',{'index':index(dom,r'name=["\']?username\b'),'text':'saved_username','clear':True})
                dom=observe(reference)
                tool(reference,'browser_input',{'index':index(dom,r'(?:name|type)=["\']?password\b'),'text':'saved_password','clear':True})
                dom=observe(reference)
                tool(reference,'browser_click',{'index':index(dom,r'<button')})
                observe(reference)
                tool(reference,'inspect_self')
                observe(reference)
                assert tool(reference,'report_connection',{'subject_selector':'#subject'})['status']=='authenticated'
            elif kind=='discovery':
                operation='reply' if 'Desired discovery operation: reply' in payload['input'] else 'publish'
                selectors={'editor_target':'#editor-target','editor_body':'textarea[name=body]','submit':'form button'}
                if operation=='reply':
                    selectors['editor_parent']='#editor-parent'
                quote='You are an ordinary member. Members may publish text posts and reply to existing posts. Automated browser tools are permitted for these member actions. You have no administrator privileges.'
                origin=re.search(r'"account_origin":\s*"([^"]+)"',payload['input'])[1]
                # Permission text is read from its actual source; the editor is
                # retained by original task URL and restored before proposing.
                initial=re.search(r'"starting_urls":\s*\["([^"]+)"',payload['input'])[1]
                if operation=='reply':
                    tool(reference,'browser_navigate',{'url':origin+'/home'})
                    assert quote in observe(reference)
                    tool(reference,'browser_navigate',{'url':initial})
                    observe(reference)
                else:
                    assert quote in dom
                assert tool(reference,'propose_method',{'target_url':origin+'/home','operation':operation,
                    'selectors':selectors,'permission_selector':'section p','permission_quote':quote})['state']=='method_observed'
            elif kind=='action':
                reconcile=payload['input'].startswith('Only reconcile')
                parent='The exact parent is ' in payload['input']
                if reconcile:
                    needle='An independent a publication whose first acknowledgement is intentionally lost.'
                    tool(reference,'browser_click',{'index':index(dom,re.escape(needle))})
                else:
                    body=re.search(r'Authorized body: (.*?)\n',payload['input'],re.S)[1]
                    tool(reference,'browser_input',{'index':index(dom,r'<textarea'),'text':body,'clear':True})
                    dom=observe(reference)
                    tool(reference,'browser_click',{'index':index(dom,r'<button')})
                observe(reference)
                selectors={'author':'#author','body':'#post-body','url':'a[rel~=canonical]'}
                if parent:
                    selectors['parent']='#receipt-parent'
                assert tool(reference,'report_receipt',{'selectors':selectors})['state']=='confirmed'
            else:
                raise ValueError('unexpected_mechanical_task_kind')
            post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
                'method':'result','params':{'state':'completed','output':{'mechanical':True,'model_requests':0}}}]},bearer=bearer)
        time.sleep(.5)


if __name__=='__main__':
    main()

"""Zero-model B protocol diagnostic; known selectors never reach a model.

Every identity, method and receipt still passes the original business manager.
Only the synthetic device's final model-result event is supplied mechanically.
"""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from local_codex_c22_mechanical import post


def main():
    pairing=post('/fixture/pairing',{})
    device=post('/executor/v1/pair',{'code':pairing['code'],'name':'Zero-model B DOM contract diagnostic',
        'platform':'mechanical-fixture','capabilities':{'ready':True,'mechanical':True}})
    bearer,epoch=device['token'],device['epoch']
    instance='b-mechanical-'+str(uuid4())
    post('/fixture/c22/start',{'site':'b'})
    seen=set()
    rejected_targets=0

    def raw(reference,name,args=None):
        return post('/executor/v1/tools',{'epoch':epoch,'request_id':reference,
            'name':name,'arguments':args or {}},bearer=bearer)

    def tool(reference,name,args=None):
        value=raw(reference,name,args)
        assert not value.get('isError'),value['content'][0]
        return value.get('structuredContent') or json.loads(value['content'][0]['text'])

    def observe(reference):
        return tool(reference,'browser_observe')

    def index(dom,pattern,*,first=False):
        lines=dom.splitlines()
        found=[int(match[1]) for number,line in enumerate(lines) if (match:=re.search(r'\[(\d+)\]',line))
            and re.search(pattern,'\n'.join(lines[number:number+3]),re.I)]
        assert found,pattern
        return found[0 if first else -1]

    while True:
        state=post('/fixture/c22/status')
        assert not state['failure'],state['failure']
        if state['cases'].get('b',{}).get('completed'):
            assert rejected_targets==2
            state['b_dom_diagnostic']={'model_requests':0,'real_model':False,
                'wrong_editor_target_rejected':rejected_targets,
                'native_textarea_id_resolved':True,'ordinary_member_permission_independently_verified':True}
            out=Path(os.environ['LEAD_C22_MECHANICAL_REPORT'])
            out.write_text(json.dumps(state,indent=2))
            out.chmod(0o600)
            print(json.dumps({'completed':True,**state['b_dom_diagnostic'],'stages':[
                {key:stage[key] for key in ('stage','outcome','source_post_delta')}
                for stage in state['cases']['b']['stages']]}))
            return
        polled=post('/executor/v1/poll',{'epoch':epoch,'instance_id':instance,
            'capabilities':{'ready':True,'mechanical':True}},bearer=bearer)
        epoch=polled['epoch']
        for command in polled.get('commands',[]):
            if command['id'] in seen:
                continue
            seen.add(command['id'])
            assert command['kind']=='run'
            reference,payload=command['request_id'],command['payload']
            post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
                'method':'command/ack','params':{'command_id':command['id']}}]},bearer=bearer)
            observed=observe(reference)
            kind=payload['tool_context']['kind']
            if kind=='connection':
                origin=observed['url'].rstrip('/')
                tool(reference,'browser_navigate',{'url':origin+'/access'})
                dom=observe(reference)['dom']
                tool(reference,'browser_input',{'index':index(dom,r'name=["\']?member_name\b'),'text':'saved_username','clear':True})
                dom=observe(reference)['dom']
                tool(reference,'browser_input',{'index':index(dom,r'type=["\']?password\b'),'text':'saved_password','clear':True})
                dom=observe(reference)['dom']
                tool(reference,'browser_click',{'index':index(dom,r'<button')})
                observe(reference)
                tool(reference,'inspect_self')
                observe(reference)
                assert tool(reference,'report_connection',{'subject_selector':'dd:nth-of-type(2)'})['status']=='authenticated'
            elif kind=='discovery':
                operation='reply' if 'Desired discovery operation: reply' in payload['input'] else 'publish'
                origin=re.search(r'"account_origin":\s*"([^"]+)"',payload['input'])[1]
                initial=observed['url']
                quote='Members can publish notes and reply to existing topics, including through authorized browser automation.'
                if operation=='reply':
                    tool(reference,'browser_navigate',{'url':origin+'/desk'})
                    assert quote in observe(reference)['dom']
                    tool(reference,'browser_navigate',{'url':origin+'/commons'})
                    observe(reference)
                    tool(reference,'browser_navigate',{'url':initial})
                    observe(reference)
                else:
                    assert quote in observed['dom']
                selectors={'editor_target':f'nav a[href="{origin}/desk"]',
                    'editor_body':'#reply-words' if operation=='reply' else '#community-words',
                    'submit':'form button[type=submit]'}
                if operation=='reply':
                    selectors['editor_parent']='main > article[id^="note-"] a[rel~=bookmark]'
                proposal={'target_url':origin+'/desk','operation':operation,'selectors':selectors,
                    'permission_selector':'main','permission_quote':quote}
                wrong=raw(reference,'propose_method',{**proposal,'selectors':{**selectors,'editor_target':'a[rel~=me]'}})
                assert wrong.get('isError') and 'browser_method_target_changed' in wrong['content'][0]['text'],wrong
                rejected_targets+=1
                assert tool(reference,'propose_method',proposal)['state']=='method_observed'
            elif kind=='action':
                reconcile=payload['input'].startswith('Only reconcile')
                reply='The exact parent is ' in payload['input']
                dom=observed['dom']
                if reconcile:
                    tool(reference,'browser_click',{'index':index(dom,r'Community board')})
                    dom=observe(reference)['dom']
                    tool(reference,'browser_click',{'index':index(dom,r'Permanent URL',first=True)})
                else:
                    body=re.search(r'Authorized body: (.*?)\n',payload['input'],re.S)[1]
                    tool(reference,'browser_input',{'index':index(dom,r'<textarea'),'text':body,'clear':True})
                    dom=observe(reference)['dom']
                    tool(reference,'browser_click',{'index':index(dom,r'<button')})
                dom=observe(reference)['dom']
                if reply:
                    tool(reference,'browser_click',{'index':index(dom,r'Permanent URL')})
                    observe(reference)
                article='main > article[id^="reply-"]' if reply else 'main > article[id^="note-"]'
                selectors={'author':article+' address a','body':article+' p.copy','url':article+' a[rel~=bookmark]'}
                if reply:
                    selectors['parent']=article+' > p.small:first-of-type a'
                assert tool(reference,'report_receipt',{'selectors':selectors})['state']=='confirmed'
            else:
                raise ValueError('unexpected_mechanical_kind')
            post('/executor/v1/events',{'epoch':epoch,'events':[{'id':str(uuid4()),'request_id':reference,
                'method':'result','params':{'state':'completed','output':{'mechanical':True,'model_requests':0}}}]},bearer=bearer)
        time.sleep(.5)


if __name__=='__main__':
    main()

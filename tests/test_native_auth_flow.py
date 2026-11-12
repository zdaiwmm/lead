"""Observed popup/redirect/form authority stays on its exact original role."""
import time

import pytest
from lead_app.native_auth_flow import (
    attach_popup,
    authorize,
    body_hash,
    check_auth_form,
    note_redirect,
    popup_destination,
    reserve,
)


def flow():
    scope={'connection_ref':'original-connection','target_id':'original-page','origins':['https://board.test'],
        'challenge_version':2,'control_mode':'owner'}
    reserve(scope,'https://board.test/sso/start')
    target={'targetId':'actual-popup','openerId':'original-page','type':'page'}
    attach_popup(scope,target)
    return scope,target


def test_observed_static_identity_popup_is_not_arbitrary_javascript_or_purchase():
    facts={'tag':'BUTTON','text':'Use identity sign-in','onclick':"window.open('/sso/start','member-sso')",'form':False}
    assert popup_destination(facts,'https://board.test/home')=='https://board.test/sso/start'
    assert popup_destination({**facts,'onclick':facts['onclick']+';fetch("/other")'},'https://board.test/home') is None
    assert popup_destination({**facts,'text':'Purchase identity sign-in'},'https://board.test/home') is None
    assert popup_destination({**facts,'form':True},'https://board.test/home') is None
    assert popup_destination({**facts,'onclick':"window.open('/billing/charge','sso')"},'https://board.test/home') is None
    with pytest.raises(ValueError):
        check_auth_form('https://identity.test/billing/charge',['username','password','nonce'])
    with pytest.raises(ValueError):
        check_auth_form('https://identity.test/authorize',['username','password','delete_account'])


def test_only_actual_reserved_opener_and_matching_response_redirect_extend_authentication():
    scope,target=flow()
    with pytest.raises(ValueError):
        attach_popup(scope,{**target,'targetId':'second-popup'})
    with pytest.raises(ValueError):
        attach_popup(scope,{**target,'openerId':'foreign-opener'})
    request={'method':'GET','url':'https://identity.test/authorize?state=actual'}
    event={'request':request,'redirectResponse':{'url':'https://board.test/sso/start','status':303,
        'headers':{'Location':request['url']}}}
    assert authorize(scope,target,request) is None
    note_redirect(scope,{**target,'targetId':'different-target'},event)
    assert authorize(scope,target,request) is None
    note_redirect(scope,target,{**event,'request':{**request,'url':'https://identity.test/forged'}})
    assert authorize(scope,target,request) is None
    note_redirect(scope,target,event)
    assert authorize(scope,target,request)=={'kind':'authentication'}
    assert authorize(scope,target,request) is None
    assert authorize(scope,{**target,'openerId':'foreign-opener'},request) is None
    assert authorize(scope,{**target,'type':'service_worker'},request) is None
    scope['auth_flow']['expires']=time.monotonic()-1
    assert authorize(scope,target,request) is None


def test_person_form_is_exact_version_document_body_and_once_only():
    scope,target=flow()
    state=scope['auth_flow']
    state['urls'].append('https://identity.test/authorize')
    state['document']=['frame','current-loader','https://identity.test/authorize']
    state['current_document']=state['document']
    state['owner_form']={'url':state['urls'][-1],'body_hash':body_hash({'nonce':'actual','code':'synthetic'}),
        'document':state['document'],'version':2,'expires':time.monotonic()+10,'used':False}
    request={'url':state['urls'][-1],'method':'POST','postData':'nonce=actual&code=synthetic'}
    assert authorize(scope,target,{**request,'postData':request['postData']+'&body=extra'}) is None
    assert authorize(scope,target,{**request,'postData':request['postData']+'&nonce=actual'}) is None
    assert authorize({**scope,'control_mode':'automatic'},target,request) is None
    assert authorize({**scope,'challenge_version':1},target,request) is None
    state['current_document']=['frame','new-loader',state['urls'][-1]]
    assert authorize(scope,target,request) is None
    state['current_document']=state['document']
    assert authorize(scope,target,request)=={'kind':'authentication'}
    assert authorize(scope,target,request) is None

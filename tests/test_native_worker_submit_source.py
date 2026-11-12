"""An action may follow its current controlling SW without inventing a page frame."""

import copy
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('change,allowed',[(None,True),('body',False),('controller',False),('retired',False),
    ('other_context',False),('duplicate',False),('document',False)])
def test_worker_submit_requires_exact_network_and_current_controlled_client(change,allowed):
    from lead_app.native_action import NativeAction

    version={'versionId':'v1','targetId':'sw','status':'activated','runningStatus':'running','controlledClients':['page']}
    gate=SimpleNamespace(context_id='managed',targets={'root-session':{'targetId':'page','type':'page'},
        'sw-session':{'targetId':'sw','type':'service_worker','browserContextId':'managed'}},
        network={('request','sw-session'):{'request':{'url':'https://site.test/posts','method':'POST'}}},
        documents={'page':['root-frame','loader','https://site.test/home']},
        worker_versions_by_page={'root-session':{'v1':copy.deepcopy(version)}},worker_versions={'v1':copy.deepcopy(version)})
    controller=NativeAction.__new__(NativeAction)
    controller.actor=SimpleNamespace(native_runtime=SimpleNamespace(gate=gate))
    controller.lease={'target_id':'page'}
    controller.submit_proof={'frame_id':'root-frame'}
    request={'url':'https://site.test/posts','method':'POST','postData':'body=exact',
        '_native_network':{'session':'sw-session','network_id':'request','worker_body_verified':True}}
    if change=='body':
        request['_native_network']['worker_body_verified']=False
    elif change=='controller':
        gate.worker_versions_by_page['root-session']['v1']['controlledClients']=['another-page']
    elif change=='retired':
        gate.worker_versions['v1']['status']='redundant'
    elif change=='other_context':
        gate.targets['sw-session']['browserContextId']='other'
    elif change=='duplicate':
        gate.targets['other']={'targetId':'other-sw','type':'service_worker','browserContextId':'managed'}
        gate.network[('request','other')]=gate.network[('request','sw-session')]
    elif change=='document':
        gate.documents['page'][0]='new-frame'
    assert bool(controller.worker_submit_source(request)) is allowed

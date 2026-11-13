"""No browser/model required to test the bounded diagnostic privacy contract."""
import json
from types import SimpleNamespace

import pytest
from lead_app.native_sdk_trace import SafeStepTrace


def entry(name='guarded_input',error=None,**params):
    return SimpleNamespace(model_output=SimpleNamespace(action=[{name:params}]),
        result=[SimpleNamespace(error=error,is_done=False)],state={'url':'https://private.invalid/account'})


def test_trace_records_fixed_outcomes_but_no_parameters_or_arbitrary_errors(tmp_path):
    private='private-value-do-not-persist'
    trace=SafeStepTrace(tmp_path/'trace.json')
    history=SimpleNamespace(history=[
        entry(index=999999,text=private,error='Error executing action guarded_input: element_no_longer_observed'),
        entry(index=3,text=private,error='Use the approved saved credential placeholder for this observed field.'),
        entry('guarded_navigate',url='https://private.invalid/'+private),
        entry('inspect_self',selector=private,error='Error executing action inspect_self: identity_selector_invalid: '+private),
        entry('unexpected-'+private,error=private),
        SimpleNamespace(model_output=None,result=[SimpleNamespace(error='Model request limit reached')]),
    ])
    trace.capture(history)
    raw=trace.path.read_text()
    data=json.loads(raw)
    assert data['steps'][0]['outcomes']==[{'status':'error','code':'element_no_longer_observed'}]
    assert data['steps'][1]['outcomes']==[{'status':'denied','code':'credential_placeholder_denied'}]
    assert data['steps'][2]['actions']==[{'name':'guarded_navigate'}]
    assert data['steps'][3]['outcomes']==[{'status':'error','code':'identity_selector_invalid'}]
    assert data['steps'][4]['actions']==[{'name':'unknown_action'}]
    assert data['steps'][4]['outcomes']==[{'status':'error','code':'unclassified_error'}]
    assert data['steps'][5]['outcomes']==[{'status':'error','code':'model_request_limit'}]
    assert all(value not in raw for value in (private,'https://','"selector":','"text":','"url":','"account_id":'))
    assert trace.path.stat().st_mode&0o777==0o600


def test_trace_is_idempotent_bounded_and_keeps_existing_file_on_write_failure(tmp_path,monkeypatch):
    trace=SafeStepTrace(tmp_path/'trace.json')
    history=SimpleNamespace(history=[entry(index=1)]*40)
    trace.capture(history)
    trace.capture(history)
    assert len(trace.steps)==32 and trace.truncated
    assert len(json.loads(trace.path.read_text())['steps'])==32
    original=trace.path.read_text()
    def unavailable(*args):
        raise OSError('private-filesystem-detail')
    monkeypatch.setattr('lead_app.native_sdk_trace.os.replace',unavailable)
    trace.capture(history)
    assert trace.write_failed and trace.path.read_text()==original
    assert 'private-filesystem-detail' not in json.dumps(trace.summary())


def test_trace_caps_actions_and_ignores_non_numeric_indexes(tmp_path):
    trace=SafeStepTrace(tmp_path/'trace.json')
    trace.capture(SimpleNamespace(history=[SimpleNamespace(model_output={'action':[
        {'guarded_input':{'index':True,'text':'private'}}]*10},result=[{'is_done':True,'success':True}])]))
    assert trace.truncated and len(trace.steps[0]['actions'])==8
    assert trace.steps[0]['actions'][0]=={'name':'guarded_input'}
    assert trace.steps[0]['outcomes']==[{'status':'done','success':True}]


def test_actual_sdk_rootmodel_shape_retains_only_name_and_index(tmp_path):
    from pydantic import RootModel
    trace=SafeStepTrace(tmp_path/'trace.json')
    action=RootModel[dict](root={'guarded_input':{'index':17,'text':'private-value'}})
    trace.capture(SimpleNamespace(history=[SimpleNamespace(model_output=SimpleNamespace(action=[action]),result=[{}])]))
    assert trace.steps[0]['actions']==[{'name':'guarded_input','index':17}]
    assert 'private-value' not in trace.path.read_text()


def test_projection_failure_does_not_escape_or_grow_partial_steps(tmp_path):
    trace=SafeStepTrace(tmp_path/'trace.json')
    class Broken:
        @property
        def model_output(self):
            raise RuntimeError('private-shape-detail')
    for _ in range(40):
        trace.capture(SimpleNamespace(history=[entry(index=1),Broken()]))
    assert trace.steps==[] and trace.write_failed
    assert 'private-shape-detail' not in trace.path.read_text()
    trace.capture(SimpleNamespace(history=[entry(index=1)]))
    assert len(trace.steps)==1


def test_projection_does_not_swallow_cancellation(tmp_path):
    import asyncio
    trace=SafeStepTrace(tmp_path/'trace.json')
    class Cancelled:
        @property
        def history(self):
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        trace.capture(Cancelled())


@pytest.mark.parametrize('message,code',[
    ('The observed sign-in form has an empty password field. Fill that field before submitting.','credential_password_missing'),
    ('The observed sign-in form has an empty required field. Complete that field before submitting.','credential_required_field_missing'),
    ('The observed sign-in form is invalid. Correct its observed fields before submitting.','credential_form_invalid'),
])
def test_credential_completeness_feedback_is_a_fixed_safe_code(tmp_path,message,code):
    trace=SafeStepTrace(tmp_path/'trace.json')
    trace.capture(SimpleNamespace(history=[entry(error=message)]))
    assert trace.steps[0]['outcomes']==[{'status':'denied','code':code}]

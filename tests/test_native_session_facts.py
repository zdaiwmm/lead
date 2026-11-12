"""The maintenance subset is parameterized by compiled source, never site URLs."""
import pytest
from lead_app.native_session_facts import message_bridges, worker_envelope

SOURCE="""self.addEventListener('message', message => {
 const command = message.data.operation;
 if (!['renew', 'other'].includes(command)) return;
 message.waitUntil(fetch(command === 'renew' ? '/private/continue' : '/other', {
 method: 'POST', credentials: 'include', body: new URLSearchParams({nonce: message.data.proof})
 }).then(response => message.source.postMessage({status:response.status})));
});"""


def inspect(source=SOURCE,**changes):
    return worker_envelope(source,script_url='https://arbitrary.example.test/worker.js',line=3,column=19,
        message={'operation':'renew','proof':'a-current-control-nonce'},**changes)


def test_current_message_selects_the_actual_static_endpoint():
    assert inspect()=={'method':'POST','url':'https://arbitrary.example.test/private/continue','fields':{'nonce':'a-current-control-nonce'}}


@pytest.mark.parametrize('before,after',[
    ("'/private/continue'","message.data.url"),
    ("method: 'POST'","method: 'DELETE'"),
    ('nonce: message.data.proof','body: message.data.proof'),
    ("'/private/continue'","'https://elsewhere.example.test/send'"),
    ("new URLSearchParams({nonce: message.data.proof})","JSON.stringify({nonce:message.data.proof})"),
])
def test_unproved_dynamic_cross_origin_and_nonmaintenance_requests_are_gaps(before,after):
    with pytest.raises(ValueError):
        inspect(SOURCE.replace(before,after))


def test_source_line_must_be_the_running_fetch_not_a_model_quote():
    with pytest.raises(ValueError,match='initiator'):
        worker_envelope(SOURCE,script_url='https://arbitrary.example.test/worker.js',line=0,column=0,message={})


def test_current_page_compiled_bridge_fixes_selector_and_message_data_mapping():
    source="""for(const current of document.querySelectorAll('[data-operation]')) current.addEventListener('click',()=>{
    navigator.serviceWorker.controller.postMessage({operation:current.dataset.operation,proof:current.dataset.proof});});"""
    assert message_bridges(source)==[{'selector':'[data-operation]','fields':{'operation':'operation','proof':'proof'}}]
    with pytest.raises(ValueError):
        message_bridges(source.replace('current.dataset.proof','arbitraryExpression()'))
    assert message_bridges('// '+source.replace('\n',' '))==[]

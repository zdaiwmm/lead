"""Bounded SDK-history diagnostics. This projection never grants or retries work."""
import json
import os
from pathlib import Path

from .native_identity_errors import IDENTITY_ERROR_HINTS
from .native_method_errors import METHOD_ERROR_HINTS

_ACTIONS=('guarded_input','guarded_click','guarded_scroll','guarded_navigate','inspect_self',
    'report_connection','propose_method','report_receipt','done','open_public_page',
    'observe_public_page','inspect_public_element','scroll_public_page')
_SCOPE_CODES=frozenset(('element_no_longer_observed','document_changed_reobserve_before_action',
    'stale_or_foreign_node','node_target_changed','navigation_out_of_scope','observation_out_of_scope',
    'scope_changed_during_observation','scope_no_longer_current','authorized_body_confirmation_unavailable'))
_MESSAGES={
    'Use exactly one approved saved credential placeholder.':('denied','credential_placeholder_mismatch'),
    'Use the approved saved credential placeholder for this observed field.':('denied','credential_placeholder_denied'),
    'This is a person-only challenge or an unsupported control.':('denied','person_step_or_unsupported_control'),
    'The current form is not a single-password sign-in form.':('denied','login_form_unproved'),
    'This is not the current credential form.':('denied','credential_form_unproved'),
    'The credential form has no verifiable field name.':('denied','credential_field_missing'),
    'The observed sign-in form has an empty password field. Fill that field before submitting.':('denied','credential_password_missing'),
    'The observed sign-in form has an empty required field. Complete that field before submitting.':('denied','credential_required_field_missing'),
    'The observed sign-in form is invalid. Correct its observed fields before submitting.':('denied','credential_form_invalid'),
    'This approved public frame can only be read.':('denied','readonly_frame'),
    'Current task does not authorize this action':('denied','authorization_denied'),
    'Model request limit reached':('error','model_request_limit'),
    'Model token envelope reached':('error','model_token_envelope'),
    'The prior model request has unmeasured usage; reconcile its original reservation.':('error','model_billing_unknown'),
    'Credential redaction boundary rejected the model request':('error','credential_redaction'),
    'body_matches_authorized=false. The current editor does not contain the complete authorized body.':('error','authorized_body_not_present'),
}


def _field(value,name):
    return value.get(name) if isinstance(value,dict) else getattr(value,name,None)


def _outcome(result):
    error=_field(result,'error')
    if error:
        if not isinstance(error,str):
            return {'status':'error','code':'unclassified_error'}
        for _ in range(2):
            prefix=next((f'Error executing action {name}: ' for name in _ACTIONS
                if error.startswith(f'Error executing action {name}: ')),None)
            if prefix is None:
                break
            error=error[len(prefix):]
        if error in _MESSAGES:
            status,code=_MESSAGES[error]
            return {'status':status,'code':code}
        for code in (*_SCOPE_CODES,*IDENTITY_ERROR_HINTS,*METHOD_ERROR_HINTS):
            if error==code or (code in IDENTITY_ERROR_HINTS or code in METHOD_ERROR_HINTS) and error.startswith(code+': '):
                return {'status':'error','code':code}
        for status in (400,401,403,404,409,422,429,500,502,503,504):
            if error.startswith((f"Client error '{status} ",f"Server error '{status} ")):
                return {'status':'error','code':f'http_{status}'}
        return {'status':'error','code':'unclassified_error'}
    if _field(result,'is_done') is True:
        row={'status':'done'}
        if type(_field(result,'success')) is bool:
            row['success']=_field(result,'success')
        return row
    return {'status':'ok'}


def _action(action):
    root=_field(action,'root')
    if root is not None:
        action=root
    for name in _ACTIONS:
        params=_field(action,name)
        if params is not None:
            row={'name':name}
            index=_field(params,'index')
            if name in ('guarded_input','guarded_click') and type(index) is int and 0<=index<=1000000000:
                row['index']=index
            return row
    return {'name':'unknown_action'}


class SafeStepTrace:
    """Persist only fixed names/codes and bounded indexes, never history itself."""
    def __init__(self,path):
        self.path=Path(path)
        self.steps=[]
        self.seen=0
        self.truncated=False
        self.write_failed=False
        self._write()

    def capture(self,history):
        seen,size=self.seen,len(self.steps)
        try:
            self._capture(history)
        except Exception:
            # An SDK shape/diagnostic failure cannot discard the worker meter.
            del self.steps[size:]
            self.seen=seen
            self.write_failed=True

    def _capture(self,history):
        rows=_field(history,'history')
        if not isinstance(rows,(list,tuple)):
            return
        for position in range(self.seen,min(len(rows),32)):
            entry=rows[position]
            actions=_field(_field(entry,'model_output'),'action') or []
            results=_field(entry,'result') or []
            if not isinstance(actions,(list,tuple)) or not isinstance(results,(list,tuple)):
                self.write_failed=True
                continue
            self.truncated=self.truncated or len(actions)>8 or len(results)>8
            self.steps.append({'step':position+1,'actions':[_action(action) for action in actions[:8]],
                'outcomes':[_outcome(result) for result in results[:8]]})
        self.seen=max(self.seen,len(rows))
        self.truncated=self.truncated or len(rows)>32
        self._write()

    def summary(self):
        return {'steps':len(self.steps),'truncated':self.truncated,'write_failed':self.write_failed}

    def _write(self):
        temporary=self.path.with_name(self.path.name+'.tmp')
        try:
            flags=os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,'O_NOFOLLOW',0)
            descriptor=os.open(temporary,flags,0o600)
            with os.fdopen(descriptor,'w') as stream:
                os.fchmod(stream.fileno(),0o600)
                json.dump({'version':1,'steps':self.steps,'truncated':self.truncated},stream,separators=(',',':'))
            os.replace(temporary,self.path)
        except OSError:
            # Diagnostics must not alter SDK actions, permissions or settlement.
            self.write_failed=True

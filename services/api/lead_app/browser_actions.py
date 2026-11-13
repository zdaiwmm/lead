"""Native request bindings live in the original action attempts and transaction.

A browser request can claim the original action once. Unknown work is never
converted to a new ActionRef by this module, including an untrusted prepared
window after loss of the instance's sole network exit.
"""
import copy
import hashlib
import json

from .browser_execution import match_request, validate_method
from .db import j, one, tx
from .scope import current_tenant


def method_hash(method):
    return hashlib.sha256(json.dumps(method, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def execution_method(c, action):
    """Read the original attempt's single technical revision, never caller facts."""
    original=action['intent']['browser_method']
    attempt=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt DESC LIMIT 1',(action['id'],))
    observation=(attempt or {}).get('observation') or {}
    effective=observation.get('effective_method')
    if effective is None:
        return original
    repair=observation.get('method_repair') or {}
    binding=observation.get('browser_request') or {}
    if (not isinstance(effective,dict) or repair.get('revision')!=1
            or repair.get('original_method_hash')!=method_hash(original)
            or repair.get('method_hash')!=method_hash(effective)
            or repair.get('original_method')!=original
            or binding.get('initial_method_hash')!=method_hash(original)
            or binding.get('method_hash')!=method_hash(effective)
            or str(attempt['browser_session_id'])!=repair.get('browser_session_id')
            or attempt['epoch']!=repair.get('browser_epoch')):
        raise ValueError('browser_method_repair_invalid')
    return validate_method(effective,account_id=original['account_id'],binding_version=original['binding_version'],
        target_url=original['target_url'],operation=action['type'])


def execution_action(c, action):
    value=copy.deepcopy(action)
    if value['intent'].get('browser_method'):
        value['intent']['browser_method']=execution_method(c,action)
    return value


def repair_generation_current(c,actor,action,task):
    """A canonical recovery may change the claim generation, not the revision."""
    marker=(task.get('result') or {}).get('runtime',{}).get('browser_method_repair') or {}
    if (action['state']!='prepared' or marker.get('task_generation')!=task['input']['task_generation']
            or marker.get('target_id')!=str(action['target_id']) or marker.get('grant_id')!=str(action['grant_id'])
            or marker.get('operation')!=action['type']):
        return False
    attempts=c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt FOR UPDATE',(action['id'],)).fetchall()
    if not attempts:
        return False
    for attempt in attempts:
        observation=attempt['observation'] or {}
        repair=observation.get('method_repair') or {}
        binding=observation.get('browser_request') or {}
        if (repair.get('revision')!=1 or str(attempt['browser_session_id'])!=str(actor.session)
                or attempt['epoch']!=actor.epoch or binding.get('claimed') is not False
                or binding.get('fingerprint') or binding.get('dispatch_task_attempt_token')
                or 'browser_transport' in observation or 'browser_receipt' in observation
                or attempt['outcome'] is not None or attempt['ended_at'] is not None
                or attempt['error_code'] is not None or attempt['evidence_ref'] is not None
                or any(marker.get(key)!=repair.get(key) for key in
                    ('method_hash','proof_ref','browser_session_id','browser_epoch','producer_task_id'))):
            return False
        execution_method(c,action)
    return True


def prepare_method_repair(c,actor,action,task):
    """Consume one committed recovery proof under the original submission locks."""
    from .browser_method_recovery import method_repair_candidate
    candidate=method_repair_candidate(c,action)
    marker=(task.get('result') or {}).get('runtime',{}).get('browser_method_repair') or {}
    expected={'target_id':str(action['target_id']),'operation':action['type'],'grant_id':str(action['grant_id']),
        'task_generation':task['input']['task_generation']}
    if (not candidate or any(marker.get(key)!=value for key,value in expected.items())
            or any(marker.get(key)!=candidate[key] for key in ('method_hash','proof_ref','browser_session_id','browser_epoch','producer_task_id'))
            or candidate['browser_session_id']!=str(actor.session) or candidate['browser_epoch']!=actor.epoch):
        raise ValueError('browser_method_changed')
    attempts=c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt FOR UPDATE',(action['id'],)).fetchall()
    for attempt in attempts:
        observation=attempt['observation']
        binding=dict(observation['browser_request'])
        binding.update(initial_method_hash=candidate['original_method_hash'],method_hash=candidate['method_hash'])
        repair={**candidate,'original_method':action['intent']['browser_method'],'revision':1,
            'task_generation':task['input']['task_generation']}
        repair.pop('method')
        c.execute('UPDATE lead.action_attempts SET observation=observation || %s WHERE id=%s',
            (j({'browser_request':binding,'effective_method':candidate['method'],'method_repair':repair}),attempt['id']))
    return candidate['method']


def prepare_binding(actor, action_id, claim, generation):
    from .growth.tools import submission_gate
    with tx(actor.aid) as c:
        action = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action_id,))
        if not action or action['state'] != 'prepared':
            raise ValueError('original_action_reconcile_only')
        permission=submission_gate(c, actor, action, claim,allow_method_repair=True)
        target = one(c, '''SELECT t.*,s.binding_version,s.external_account_id FROM lead.account_targets t
            JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s''', (action['target_id'],))
        method = validate_method(permission.get('_native_method') or execution_method(c,action), account_id=target['account_id'],
            binding_version=target['binding_version'], target_url=target['canonical_url'], operation=action['type'])
        prior = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt DESC LIMIT 1 FOR UPDATE', (action_id,))
        if prior:
            binding = prior['observation'].get('browser_request', {})
            if (str(prior['browser_session_id']) != str(actor.session) or prior['epoch'] != actor.epoch
                    or binding.get('generation') != generation or binding.get('method_hash') != method_hash(method)
                    or binding.get('claimed')):
                raise ValueError('original_action_reconcile_only')
            return str(prior['id'])
        binding = {'profile_id': str(actor.profile['id']), 'generation': generation,
                   'method_hash': method_hash(method), 'claimed': False}
        observation={'browser_request':binding}
        if method!=action['intent']['browser_method']:
            from .browser_method_recovery import method_repair_candidate
            candidate=method_repair_candidate(c,action)
            if not candidate or candidate['method']!=method:
                raise ValueError('browser_method_changed')
            task=one(c,'SELECT input FROM lead.tasks WHERE id=%s',(action['task_id'],))
            binding['initial_method_hash']=candidate['original_method_hash']
            repair={**candidate,'original_method':action['intent']['browser_method'],'revision':1,
                'task_generation':task['input']['task_generation']}
            repair.pop('method')
            observation.update(effective_method=method,method_repair=repair)
        attempt = one(c, '''INSERT INTO lead.action_attempts(tenant_id,agent_id,action_id,browser_session_id,
            attempt,epoch,control_version,observed_account_id,observed_target_id,observation)
            VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,%s) RETURNING id''',
            (current_tenant(),actor.aid,action_id,actor.session,actor.epoch,action['expected_control_version'],
             target['external_account_id'],target['external_target_id'],j(observation)))
        return str(attempt['id'])


def claim_request(actor, action_id, claim, generation, request, observed):
    from .growth.tools import submission_gate
    with tx(actor.aid) as c:
        action = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action_id,))
        if not action or action['state'] != 'prepared':
            return False
        # This obtains the same agent/account/action locks as revoke/pause and
        # compares monotonic control/intent/binding versions before the CAS.
        submission_gate(c, actor, action, claim)
        target = one(c, '''SELECT t.*,s.binding_version,s.external_account_id FROM lead.account_targets t
            JOIN lead.social_accounts s ON s.id=t.account_id WHERE t.id=%s''', (action['target_id'],))
        method = validate_method(execution_method(c,action),account_id=target['account_id'],
            binding_version=target['binding_version'],target_url=target['canonical_url'],operation=action['type'])
        if (observed.get('subject') != target['external_account_id'] or observed.get('target_url') != target['canonical_url']
                or observed.get('body') != action['intent']['body'] or action['intent'].get('media')
                or action['type'] == 'reply' and observed.get('parent_url') != action['intent'].get('parent_url')):
            raise ValueError('editor_or_identity_changed')
        fingerprint = match_request(method, request, action['intent'])
        attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt DESC LIMIT 1 FOR UPDATE', (action_id,))
        if not attempt:
            raise ValueError('browser_intent_not_prepared')
        binding = attempt['observation'].get('browser_request', {})
        if (str(attempt['browser_session_id']) != str(actor.session) or attempt['epoch'] != actor.epoch
                or binding.get('generation') != generation or binding.get('method_hash') != method_hash(method)):
            raise ValueError('browser_execution_changed')
        if binding.get('claimed'):
            return False
        if one(c, '''SELECT id FROM lead.action_attempts WHERE id<>%s
            AND observation#>>'{browser_request,profile_id}'=%s
            AND observation#>>'{browser_request,fingerprint}'=%s''', (attempt['id'],str(actor.profile['id']),fingerprint)):
            raise ValueError('ambiguous_request_reconcile_original')
        updated = c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s AND state='prepared'", (action_id,)).rowcount
        if updated != 1:
            return False
        binding.update(claimed=True, fingerprint=fingerprint, dispatch_task_attempt_token=claim["attempt_token"])
        c.execute('UPDATE lead.action_attempts SET observation=observation || %s WHERE id=%s',
                  (j({'browser_request':binding}),attempt['id']))
        return True


def mark_untrusted(actor, generation):
    """Scan current persistent intentions, including those added after startup."""
    with tx(actor.aid) as c:
        # Keep the standard lock order: project control, account, action.
        c.execute('SELECT id FROM lead.agents WHERE id=%s FOR SHARE', (actor.aid,))
        c.execute('SELECT id FROM lead.social_accounts WHERE profile_id=%s ORDER BY id FOR SHARE', (actor.profile['id'],))
        # Invalidate the original lease before scanning. This waits for any
        # submission transaction already holding its SHARE lock, so its newly
        # prepared attempt is included. Later transactions cannot pass the gate.
        c.execute("UPDATE lead.profile_leases SET state='released',expires_at=now() WHERE profile_id=%s AND epoch=%s",
            (actor.profile['id'],actor.epoch))
        c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=coalesce(ended_at,now()) WHERE id=%s AND epoch=%s",
            (actor.session,actor.epoch))
        c.execute("""UPDATE lead.takeover_sessions SET state='expired',ended_at=coalesce(ended_at,now()),encrypted_login=NULL,
            challenge_version=challenge_version+1,input_seq=input_seq+1
            WHERE browser_session_id=%s AND purpose='login' AND state IN ('active','requested','ending')""",(actor.session,))
        rows = c.execute('''SELECT a.id, a.state, ap.id attempt_id FROM lead.actions a
            JOIN lead.action_attempts ap ON ap.action_id=a.id
            WHERE ap.browser_session_id=%s AND ap.epoch=%s
             AND ap.observation#>>'{browser_request,generation}'=%s
             AND a.state IN ('prepared','dispatching') ORDER BY a.id FOR UPDATE OF a''',
            (actor.session,actor.epoch,str(generation))).fetchall()
        for row in rows:
            if row['state'] == 'prepared':
                c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s AND state='prepared'", (row['id'],))
            c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'", (row['id'],))
            c.execute("UPDATE lead.action_attempts SET outcome='unknown',error_code='BROWSER_CONTROL_LOST',ended_at=now() WHERE id=%s", (row['attempt_id'],))
        return len(rows)

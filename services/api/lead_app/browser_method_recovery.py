"""Read-only proof for one technical repair of an original, never-sent action.

Callers retain the existing control/account/grant/action transaction locks. This
module creates no authorization, attempt, task, or external side effect.
"""
from uuid import UUID

from .browser_actions import method_hash
from .browser_execution import validate_method
from .channel_contracts import ExecutionRef
from .db import all_rows, one
from .execution_routes import registered_reference
from .growth.store import enabled, objective_revision


def method_repair_candidate(c, action):
    if not action or action['state'] != 'prepared':
        return None
    publication = action['intent']
    original = publication.get('browser_method')
    operation = publication.get('operation', 'publish')
    if not original or operation not in ('publish', 'reply') or action['type'] != operation:
        return None
    agent = one(c, 'SELECT * FROM lead.agents WHERE id=%s', (action['agent_id'],))
    target = one(c, 'SELECT * FROM lead.account_targets WHERE id=%s', (action['target_id'],))
    if (not agent or not enabled(agent) or agent['desired_state'] != 'running'
            or agent['control_version'] != action['expected_control_version']
            or objective_revision(agent) != publication.get('goal_revision')
            or not target or target['state'] != 'active'):
        return None
    account = one(c, 'SELECT * FROM lead.social_accounts WHERE id=%s', (target['account_id'],))
    intent = one(c, 'SELECT * FROM lead.account_operating_intents WHERE account_id=%s', (target['account_id'],))
    grant = one(c, 'SELECT * FROM lead.authorization_grants WHERE id=%s AND target_id=%s AND revoked_at IS NULL',
                (action['grant_id'], target['id']))
    if (not account or account['state'] != 'connected' or account['binding_version'] != publication.get('binding_version')
            or not intent or intent['desired'] != 'running' or intent['revision'] != publication.get('intent_revision')
            or not grant or operation not in grant['allowed_actions']
            or target['external_target_id'] in intent['scope'].get('excluded_targets', [])
            or intent['scope'].get('target_scope') == 'existing_grants'
                and target['external_target_id'] not in intent['scope'].get('target_ids', [])):
        return None
    content = one(c, 'SELECT content_hash FROM lead.content_items WHERE id=%s', (action['content_id'],))
    if not content or content['content_hash'] != publication.get('content_hash'):
        return None
    permissions = target['verified_permissions']
    if not isinstance(permissions, dict):
        return None
    methods = permissions.get('browser_methods') or {}
    producers = permissions.get('browser_method_producers') or {}
    if not isinstance(methods, dict) or not isinstance(producers, dict):
        return None
    producer = producers.get(operation)
    if not isinstance(producer, dict):
        return None
    try:
        session = UUID(str(producer.get('browser_session_id')))
        producer_task_id = UUID(str(producer.get('task_id')))
        method = validate_method(methods.get(operation) or permissions.get('browser_method'),
            account_id=account['id'], binding_version=account['binding_version'],
            target_url=target['canonical_url'], operation=operation)
        frozen_route = ExecutionRef.model_validate(publication['execution_ref'])
        observed_route = ExecutionRef.model_validate(producer.get('execution_ref'))
        route = one(c, 'SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s', (account['profile_id'],))
        current_route = registered_reference(route) if route else None
    except (ValueError, TypeError, KeyError):
        return None
    original_hash = method_hash(original)
    current_hash = method_hash(method)
    epoch = producer.get('browser_epoch')
    if (method == original or method.get('target_url') != original.get('target_url')
            or producer.get('method_hash') != current_hash or type(epoch) is not int or epoch < 1
            or producer.get('control_version') != agent['control_version']
            or producer.get('intent_revision') != intent['revision'] or not current_route
            or frozen_route.profile_id != account['profile_id'] or observed_route != current_route
            or frozen_route.model_dump(exclude={'registry_revision'}) != current_route.model_dump(exclude={'registry_revision'})
            or frozen_route.registry_revision is not None and frozen_route.registry_revision != current_route.registry_revision):
        return None
    work = one(c, 'SELECT type,input FROM lead.tasks WHERE id=%s', (producer_task_id,))
    if (not work or work['type'] != 'growth.account' or work['input']['spec'].get('purpose') != 'discover'
            or str(work['input']['spec'].get('account_id')) != str(account['id'])
            or work['input'].get('binding_version') != account['binding_version']
            or work['input'].get('task_generation') != producer.get('task_generation')):
        return None
    # Gateway heartbeat writes lease before session. Explicit statements keep
    # that order; a join's row-lock order depends on the query plan.
    if not one(c, """SELECT profile_id FROM lead.profile_leases WHERE profile_id=%s AND epoch=%s
        AND state='held' AND expires_at>now() FOR SHARE""", (account['profile_id'], epoch)):
        return None
    # Session control_version records launch. A retained browser can serve a
    # new original Action after resume; that Action and producer must both
    # match current control above. An older Action still fails that comparison.
    if not one(c, """SELECT id FROM lead.browser_sessions WHERE id=%s AND profile_id=%s AND epoch=%s
        AND state='ready' AND ended_at IS NULL FOR SHARE""", (session, account['profile_id'], epoch)):
        return None
    attempts = all_rows(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s ORDER BY attempt', (action['id'],))
    if not attempts:
        # Before the first browser binding, only the server producer frozen in
        # the immutable intention can prove continuity. Never infer it from a
        # replacement browser or from the new method's producer alone.
        frozen = publication.get('browser_method_producer')
        if (not isinstance(frozen, dict) or frozen.get('method_hash') != original_hash
                or frozen.get('browser_session_id') != str(session) or frozen.get('browser_epoch') != epoch
                or frozen.get('control_version') != agent['control_version']
                or frozen.get('intent_revision') != intent['revision']):
            return None
        try:
            frozen_task_id = UUID(str(frozen.get('task_id')))
            if ExecutionRef.model_validate(frozen.get('execution_ref')) != current_route:
                return None
        except (ValueError, TypeError):
            return None
        frozen_work = one(c, 'SELECT type,input FROM lead.tasks WHERE id=%s', (frozen_task_id,))
        if (not frozen_work or frozen_work['type'] != 'growth.account'
                or frozen_work['input']['spec'].get('purpose') != 'discover'
                or str(frozen_work['input']['spec'].get('account_id')) != str(account['id'])
                or frozen_work['input'].get('binding_version') != account['binding_version']
                or frozen_work['input'].get('task_generation') != frozen.get('task_generation')):
            return None
    for attempt in attempts:
        observation = attempt['observation'] or {}
        binding = observation.get('browser_request') or {}
        if (str(attempt['browser_session_id']) != str(session) or attempt['epoch'] != epoch
                or attempt['control_version'] != action['expected_control_version']
                or attempt['observed_account_id'] != account['external_account_id']
                or attempt['observed_target_id'] != target['external_target_id']
                or binding.get('profile_id') != str(account['profile_id']) or binding.get('generation') != epoch
                or binding.get('method_hash') != original_hash or binding.get('claimed') is not False
                or binding.get('fingerprint') or binding.get('dispatch_task_attempt_token')
                or 'browser_transport' in observation or 'browser_receipt' in observation
                or observation.get('method_repair') or observation.get('effective_method')
                or attempt['outcome'] is not None or attempt['ended_at'] is not None
                or attempt['error_code'] is not None or attempt['evidence_ref'] is not None):
            return None
    return {'method': method, 'method_hash': current_hash, 'original_method_hash': original_hash,
            'browser_session_id': str(session), 'browser_epoch': epoch,
            'producer_task_id': str(producer_task_id), 'proof_ref': 'native-method:'+str(producer_task_id)+':'+current_hash}

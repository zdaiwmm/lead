"""Target-only PC executor. Never dispatch an unverified or previously claimed intent."""
import asyncio
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import traceback
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from lead_app import browser, community_runtime as runtime, community_schedule, community_selection, identity, telegram
from lead_app import growth_experiments as trials
from lead_app.community_growth import GrowthConfig
from lead_app.config import STATE
from lead_app.db import j, one, require_agent, tx
from lead_app.identity_store import agent_tenant
from lead_app.operating_admission import assess, context
from lead_app.scope import current_tenant, service_scope, tenant_scope

AID = 'b1f9a23c-1153-4c67-a63b-11ebd264c687'
EXIT_HASH = '95c3b0befa46d7967003e5062feda9397c3c024dd42c68493e66bf501d730b48'
SLOTS = {kind: uuid5(UUID(AID), 'pc-telegram-accept-v1/' + kind) for kind in ('reply', 'promotion')}


def report(event, **fields):
    print('TG_RESULT ' + json.dumps({'event': event, **fields}, default=str), flush=True)


def check_previous_interpreter():
    program = (
        "import sys; sys.path.insert(0,'/usr/local/lib/lead-target-release'); "
        "from lead_business import evaluate; evaluate({'event':'admit','operation':'review','paid':False})"
    )
    probe = subprocess.run(['/usr/bin/python3','-I','-S','-B','-c',program], cwd='/',
                           env={'PATH':'/usr/bin:/bin'}, capture_output=True, timeout=10)
    stderr = probe.stderr.lower()
    markers = ('permission denied','read-only file system','no module named','error while loading shared libraries',
               'modulenotfounderror','fatal python error','importerror','filenotfounderror','permissionerror',
               'calledprocesserror','operation not permitted','no such file','no usable temporary directory',
               'resource temporarily unavailable','codecs','encodings','no module','python path configuration')
    report('previous_system_python', code=probe.returncode,
           signals=[word for word in markers if word.encode() in stderr],
           frames=re.findall(r'File "[^"]*/([A-Za-z_0-9.-]+\.py)", line ([0-9]+)',probe.stderr.decode(errors='replace'))[-5:])


def report_model_destination(c):
    from lead_app.admin_settings import task_connection

    for purpose in ('research', 'conversation'):
        settings, _, _ = task_connection(c, AID, purpose)
        endpoint = urlsplit(settings['base_url'])
        provider = settings['provider']
        expected = {'openai': 'api.openai.com', 'deepseek': 'api.deepseek.com'}.get(provider)
        official = (endpoint.scheme == 'https' and endpoint.hostname == expected
                    and endpoint.port in (None, 443) and endpoint.username is None and endpoint.password is None)
        report('model_destination', purpose=purpose, provider=provider if expected else 'other',
               endpoint_kind='official_https' if official else 'custom')


async def check_model_route_once():
    from lead_app.admin_settings import task_connection
    from lead_app.model import target_model_http_client
    from openai import AsyncOpenAI

    proof = Path(os.environ['HOME']) / 'openai-xray-access-v1.json'
    try:
        out = proof.open('x')
    except FileExistsError:
        return
    with out:
        try:
            with tx(AID) as c:
                config, _, _ = task_connection(c, AID, 'research')
            transport = target_model_http_client(AID, config, 15)
            if transport is None:
                raise ValueError('target_route_unavailable')
            async with AsyncOpenAI(base_url=config['base_url'], api_key=config['key'],
                                   http_client=transport, max_retries=0, timeout=15) as api:
                response = await api.models.with_raw_response.retrieve(config['model'])
                parsed = response.parse()
                request_id = response.headers.get('x-request-id','')
                result = dict(status=response.status_code, model_matches=parsed.id==config['model'],
                              request_id=request_id if re.fullmatch(r'[a-zA-Z0-9_-]{1,128}',request_id) else None)
        except Exception as exc:
            result = dict(error=type(exc).__name__, status=getattr(exc,'status_code',None))
        out.write(json.dumps(result))
        report('openai_route_check', **result)


def reconcile_model_adapter_faults(c, account):
    # These observations all failed in the original adapter before an HTTP request or draft.
    affected = ['820c917e-c4b4-4f0c-b64b-f6bc991ed171','a086e6d7-829e-4d76-a659-e7741b578654',
                '55dac520-85f7-4398-b8b1-bab0e32959a7']
    proof = Path(os.environ['HOME'])/'openai-xray-access-v1.json'
    if not proof.is_file():
        return
    try:
        checked = json.loads(proof.read_bytes())
    except ValueError:
        return
    if checked.get('status') != 200 or not checked.get('model_matches'):
        return
    rows = c.execute("""UPDATE lead.memory_entries h
        SET body=h.body || jsonb_build_object('state','healthy','next_action','resume',
            'recovered_at',now(),'recovery_reason','corrected_model_client_type',
            'model_request_id',%s::text)
        FROM lead.tasks t
        WHERE h.agent_id=%s AND h.kind='observation' AND h.body->>'type'='community_target_health'
          AND h.body->>'account'=%s AND h.body->>'state'='blocked'
          AND h.body->>'error_code'='browser_error' AND h.body#>>'{fault,stage}'='prepare'
          AND h.body#>>'{fault,write_may_have_happened}'='false'
          AND h.body#>>'{fault,message}' ILIKE '%%coroutine%%'
          AND h.body->>'failure_count'='1' AND h.body->>'task_id'=ANY(%s)
          AND t.id::text=h.body->>'task_id' AND t.agent_id=h.agent_id
          AND t.type='community.engage' AND t.input->>'external_account_id'=h.body->>'account'
          AND (h.body->>'peer'='*' OR h.body->>'peer'=t.result->>'target_id')
          AND t.result->>'error_type'='TypeError' AND t.result->>'error_detail' ILIKE '%%coroutine%%'
          AND NOT(t.input ? 'reply') AND NOT(t.result ?| ARRAY['reply_state','join_submitted'])
        RETURNING h.id""", (checked.get('request_id'),AID,account,affected)).fetchall()
    if rows:
        report('model_fault_reconciled', records=len(rows))


def bound_account(c):
    rows = c.execute("""SELECT external_account_id FROM lead.social_accounts
        WHERE agent_id=%s AND platform='telegram' AND state='connected' AND automation_enabled""", (AID,)).fetchall()
    if len(rows) != 1 or not str(rows[0]['external_account_id'] or '').isdigit():
        raise ValueError('bound_account_unavailable')
    return rows[0]['external_account_id']


def slot_kind(reply):
    if reply.get('standalone') and reply.get('promote') and not reply.get('reply_to_mid'):
        return 'promotion'
    if not reply.get('standalone') and not reply.get('promote') and re.fullmatch('[1-9][0-9]*', str(reply.get('reply_to_mid'))):
        return 'reply'
    return None


def delivery_mode():
    mode = os.environ.get('LEAD_TG_DELIVERY_MODE', 'readonly')
    if mode not in ('readonly', 'acceptance', 'production'):
        raise ValueError('invalid_delivery_mode')
    return mode


def pilot_allows(peer):
    setting = os.environ.get('LEAD_TG_PILOT_PEERS', '').strip()
    if not setting:
        # In production the native submission separately locks and verifies
        # the owner's single-group, single-opportunity approval.
        return delivery_mode() == 'production'
    peers = [value.strip() for value in setting.split(',')]
    return all(re.fullmatch('-[0-9]+', value) for value in peers) and str(peer) in peers


def claim(c, task, peer, *, production=False):
    kind = slot_kind(task['input']['reply'])
    if (not kind or str(task['agent_id']) != AID or not re.fullmatch('-[0-9]+', peer)
            or bound_account(c) != task['input']['external_account_id']):
        raise ValueError('delivery_scope_changed')
    tid = str(task['id'])
    slot = SLOTS[kind]
    if production:
        reply = task['input']['reply']
        anchor = reply.get('reply_to_mid') or reply.get('opportunity_mid') or task['input'].get('operator_card_id') or reply.get('permission_key')
        if not anchor:
            raise ValueError('missing_delivery_opportunity')
        key = [current_tenant(), bound_account(c), peer, kind, str(anchor)]
        slot = uuid5(UUID(AID), json.dumps(key, separators=(',', ':')))
    body = {'type': 'telegram_delivery' if production else 'telegram_acceptance', 'kind': kind, 'task_id': tid, 'peer': peer}
    if not one(c, """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
            VALUES(%s,%s,%s,'observation',%s,%s,'system_observed') ON CONFLICT(id) DO NOTHING RETURNING id""",
               (slot, current_tenant(), AID, j(body), j([tid]))):
        raise ValueError('delivery_slot_already_claimed')


def receipts(c, account):
    rows = c.execute("""SELECT m.id,m.body,t.input,t.result FROM lead.memory_entries m
        LEFT JOIN lead.tasks t ON t.agent_id=m.agent_id AND t.id::text=m.body->>'task_id'
        AND t.type='community.engage' WHERE m.agent_id=%s AND m.id=ANY(%s)""", (AID, list(SLOTS.values()))).fetchall()
    result = {}
    for r in rows:
        kind = next(k for k, slot in SLOTS.items() if slot == r['id'])
        saved = r['result'] or {}
        payload = r['input'] or {}
        peer = r['body'].get('peer')
        native = saved.get('reply_external_id') or ''
        confirmed = (str(peer) == saved.get('target_id') and re.fullmatch(r'-[0-9]+:[1-9][0-9]*', native)
                     and native.split(':')[0] == peer and saved.get('reply_state') == 'confirmed'
                     and account == payload.get('external_account_id') and saved.get('reply_confirmed_at')
                     and slot_kind(payload.get('reply') or {}) == kind)
        result[kind] = dict(task_id=r['body'].get('task_id'), peer=peer, state=saved.get('reply_state'),
                            external_id=native if confirmed else None, confirmed_at=saved.get('reply_confirmed_at') if confirmed else None)
    return result


def inventory(c, agent, account):
    config = GrowthConfig.model_validate(agent['onboarding_state'].get('community_growth', {}))
    review = community_schedule.next_membership_review(c, repair_legacy=True, focus=trials.first_users(agent),
        exit_restricted=config.allow_leave_restricted, proactive=config.allow_proactive_post)
    health = one(c, """SELECT count(*) AS blocked,count(*) FILTER (WHERE (body->>'read_after')::timestamptz>now()) AS cooling,
        min((body->>'read_after')::timestamptz) AS first_probe FROM lead.memory_entries
        WHERE agent_id=%s AND body->>'account'=%s AND body->>'type'='community_target_health' AND body->>'state'='blocked'""", (AID, account))
    history = one(c, """SELECT count(*) FILTER (WHERE state='reconciling') AS reconciling,
        count(*) FILTER (WHERE result->>'reply_state' IN ('dispatching','unknown')) AS uncertain_messages
        FROM lead.tasks WHERE agent_id=%s AND type='community.engage' AND input->>'external_account_id'=%s""", (AID, account))
    proof_function = one(c, "SELECT to_regprocedure('lead.local_restore_ready()') IS NOT NULL AS available")['available']
    local_ready = proof_function and one(c, 'SELECT lead.local_restore_ready() AS ok')['ok']
    report('inventory', **health, **history, oldest_eligible_review_at=review['due'] if review else None,
           review_group=(review['result'] or {}).get('target_id') if review else None, local_proof_function=proof_function,
           local_restore_ready=local_ready, receipts=receipts(c, account))
    last = one(c, """SELECT id, result->>'target_id' AS peer,
        result#>>'{receipt_observation,status}' AS status,result#>>'{receipt_observation,phase}' AS phase,
        result#>>'{receipt_observation,error}' AS error,result#>>'{fault,error_code}' AS fault,
        result#>>'{checkpoint,stage}' AS checkpoint,result->>'receipt_review_after' AS next_check
        FROM lead.tasks WHERE agent_id=%s AND type='community.engage' AND input->>'external_account_id'=%s
        AND result?'receipt_observation' ORDER BY (result#>>'{receipt_observation,at}')::timestamptz DESC NULLS LAST LIMIT 1""",(AID,account))
    account_health = one(c, """SELECT h.body->>'error_code' AS fault,h.body->>'read_after' AS read_after,
        h.body->>'write_after' AS write_after,h.body#>>'{fault,stage}' AS stage,
        t.result#>>'{browser_diagnostic,error}' AS browser_error,
        t.result#>>'{browser_diagnostic,operation}' AS operation,
        coalesce(t.result->>'target_id',m.result->>'target_id') AS peer,
        h.body#>>'{fault,message}' AS detail FROM lead.memory_entries h
        LEFT JOIN lead.tasks t ON t.id::text=h.body->>'task_id' AND t.agent_id=h.agent_id
        LEFT JOIN lead.tasks m ON m.id::text=t.input->>'membership_task_id' AND m.agent_id=h.agent_id
        WHERE h.agent_id=%s AND h.body->>'type'='community_target_health' AND h.body->>'account'=%s
        AND h.body->>'peer'='*' AND h.body->>'state'='blocked' LIMIT 1""",(AID,account))
    if account_health:
        detail = (account_health.pop('detail') or '').casefold()
        account_health['cause'] = next((cause for fragment, cause in (
            ('公开群路由未能绑定', 'peer_route_unresolved'), ('未能核对公开地址', 'public_handle_unverified'),
            ('最新消息', 'recent_history_position'), ('身份尚不能确认', 'identity_unverified'),
            ('社群身份已变化', 'identity_changed'), ('targetmismatch', 'target_mismatch'),
            ('intercepts pointer', 'navigation_obstructed'), ('timeout', 'navigation_timeout'),
            ('超时', 'navigation_timeout'), ('受限', 'restricted'), ('flood_wait', 'rate_limited'),
        ) if fragment in detail), 'unclassified')
    report('reconciliation_status', last=last, account_backoff=account_health)
    return config, local_ready


def record_patrol_schedule(c, agent, account, now):
    from lead_app.admin_notifications import available_at
    from lead_app.admin_settings import effective
    from lead_app.operating_kernel import blocked
    fences = blocked(c, community_worker=True)
    rows = [] if fences['unknown_write_scope'] else community_selection.next_readonly_visit(
        c, fences['accounts'], schedule=True)
    previous = agent['onboarding_state'].get('telegram_executor') or {}
    earliest = rows[0]['due'] if rows else None
    due = earliest and earliest <= now
    started = previous.get('patrol_due_since') or now.isoformat()
    last = one(c, """SELECT max((result->>'history_observed_at')::timestamptz) AS at
        FROM lead.tasks WHERE agent_id=%s AND type='community.engage' AND input->>'external_account_id'=%s
          AND result->>'target_id' ~ '^-[0-9]+$'""",(AID,account))['at']
    since = max(datetime.fromisoformat(started), last or datetime.fromisoformat(started))
    late = bool(due and now-since >= timedelta(minutes=30))
    patch = {'patrol_due_since':started if due else None,'next_patrol_at':earliest.isoformat() if earliest else None,
             'due_groups':sum(r['due']<=now for r in rows),'patrol_stalled':late}
    c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{telegram_executor}',
        coalesce(onboarding_state->'telegram_executor','{}') || %s) WHERE id=%s""",(j(patch),AID))
    # Notification conditions are workspace-wide. This resident can write
    # agent-scoped notifications and keeps its episode key in its own state.
    notice_key='telegram-patrol:'+AID
    if late:
        settings=effective(c,'notifications',AID)[0]
        if settings['enabled'] and 'repeated_failures' in settings['categories']:
            c.execute("""INSERT INTO lead.admin_notifications(tenant_id,agent_id,condition_key,episode,
                    category,severity,title,message,recipient_roles,recipient_subjects,created_at,available_at)
                VALUES(%s,%s,%s,%s,'repeated_failures','warning','Chamber 有到期群连续 30 分钟未取得观察',
                    '请查看群工作记录中的故障阶段和最早到期任务。',%s,%s,%s,%s)
                ON CONFLICT(tenant_id,condition_key,episode) DO NOTHING""",
                (current_tenant(),AID,notice_key,int(since.timestamp()),j(settings['recipient_roles']),
                 j(settings['recipient_subjects']),now,available_at(settings,'warning',now)))
    elif previous.get('patrol_stalled'):
        c.execute("UPDATE lead.admin_notifications SET resolved_at=%s WHERE agent_id=%s AND condition_key=%s AND resolved_at IS NULL",
                  (now,AID,notice_key))
    report('patrol_schedule', sampled=now.isoformat(), eligible=rows, unknown_scope=fences['unknown_write_scope'], **patch)


def draft_inventory(c, account, agent):
    from lead_app import community_health
    backoff = agent['onboarding_state'].get('model_backoff') or {}
    report('model_service_state', kind=backoff.get('kind'), provider_code=backoff.get('provider_error_code'),
           http_status=backoff.get('status_code'), requires_owner=backoff.get('requires_owner_action'),
           retry_at=backoff.get('retry_at'))
    rows = c.execute("""SELECT t.id,t.state,t.input,t.result,
        coalesce((t.result->>'retry_after')::timestamptz<=now(),false) AS retry_due,
        NOT EXISTS (SELECT 1 FROM lead.tasks other WHERE other.agent_id=t.agent_id AND other.id<>t.id
          AND other.type='community.engage' AND other.input->>'external_account_id'=t.input->>'external_account_id'
          AND other.result->>'reply_state' IN ('dispatching','unknown') AND
          (other.result->>'target_id' IS NULL OR other.result->>'target_id'=t.result->>'target_id')) AS clear_peer,
        """ + community_health.eligible_sql('t',write=True) + """ AS healthy
        FROM lead.tasks t WHERE t.agent_id=%s AND t.type='community.engage'
        AND t.input->>'external_account_id'=%s AND t.input ? 'reply'
        AND t.state IN ('running','failed','queued','reconciling') AND t.result->>'reply_state' IS NULL""",
        (AID,account)).fetchall()
    summary = {}
    diagnostics = []
    for row in rows:
        payload = row['input']
        kind = slot_kind(payload.get('reply') or {})
        why = ('outside_slots' if not kind else 'pending_confirmation' if row['state']=='reconciling'
               else 'prior_uncertain' if not row['clear_peer'] else 'health_block' if not row['healthy']
               else 'revision_changed' if payload.get('config_revision') != agent['config_revision']
               else 'outside_resume' if row['state']!='running' and not (row['state']=='failed' and row['retry_due'])
               else 'eligible_for_revalidation')
        key = str(kind) + '/' + why
        summary[key] = summary.get(key,0)+1
        if kind and len(diagnostics) < 15:
            saved = row['result'] or {}
            diagnostics.append(dict(id=str(row['id']), kind=kind, state=row['state'], gate=why,
                                    retry_after=saved.get('retry_after'), receipt_after=saved.get('receipt_review_after'),
                                    observed=saved.get('history_observed_at'), peer=saved.get('target_id'),
                                    fault=(saved.get('fault') or {}).get('error_code'),
                                    stage=(saved.get('checkpoint') or {}).get('stage'),
                                    dispatch_marker=any(saved.get(k) for k in ('reply_dispatched_at','reply_external_id','reply_confirmed_at')),
                                    source_bound=bool(payload['reply'].get('source_hash')), retry_due=row['retry_due']))
    report('draft_inventory', counts=summary, drafts=diagnostics)


def delivery_path(c, account):
    rows = c.execute("""
        SELECT id,state,result->>'target_id' AS peer, input ? 'membership_task_id' AS revisit,
          result#>>'{history_observation,joined}' AS joined, result#>>'{history_observation,can_reply}' AS writable,
          result#>>'{history_observation,count}' AS messages,result#>>'{history_observation,new_messages}' AS new_messages,
          result#>>'{history_observation,source}' AS history_source, result#>>'{history_observation,kind}' AS group_kind,
          result#>>'{outcome,validation_rule}' AS validation_rule, result#>>'{decision,reason}' AS decision_reason,
          result#>>'{decision,placement}' AS placement, result#>>'{decision,promote}' AS promote,
          result#>>'{decision,contextual_conversation}' AS conversation, result#>>'{decision,automation_forbidden}' AS restricted,
          result#>>'{decision,commercial_allowed}' AS commercial, input ? 'reply' AS draft,
          result#>>'{outcome,state}' AS outcome, result->>'reply_state' AS delivery,
          result#>>'{checkpoint,stage}' AS stage, result->>'error_type' AS error,
          result->>'error_detail' AS detail, result#>>'{outcome,reason}' AS reason,
          greatest(created_at, completed_at, (result#>>'{checkpoint,at}')::timestamptz,
                   (result#>>'{checkpoint,finished_at}')::timestamptz) AS activity
        FROM lead.tasks WHERE agent_id=%s AND type='community.engage' AND input->>'external_account_id'=%s
        ORDER BY activity DESC LIMIT 14
    """, (AID,account)).fetchall()
    for row in rows:
        detail = str(row.pop('detail') or '').lower()
        reason = str(row.pop('reason') or '')
        explanation = str(row.pop('decision_reason') or '')+' '+reason
        row['decision_signals'] = [tag for tag, terms in {
            'empty_history':('无可见','未见消息','没有可见','无消息','没有消息','历史为空','缺少聊天','历史记录为空','未提供近期','缺乏近期'),
            'no_opportunity':('无合适','没有合适','无相关','没有相关','未发现相关','没有可','不适合','缺乏','未见','没有明确'),
            'unrelated':('无关','不相关','不匹配','关联度低','受众不符','脱节'),
            'advertising':('广告','推广','导流'), 'private_or_bot':('机器人','私信','刷屏'),
            'group_rule':('群规','禁止','不允许'), 'stale':('过时','不活跃','陈旧','沉寂','较早','无新','没有新','已结束'),
            'active_chat':('闲聊','聊天','讨论','对话','话题'), 'target_language':('语言',),
            'untrusted_group':('诈骗','赌博','博彩','虚拟币','色情')
        }.items() if any(term in explanation for term in terms)]
        row['signals'] = [tag for text,tag in (
            ('insufficient_quota','quota'),('invalid_api_key','invalid_key'),('unsupported','unsupported'),
            ('rate_limit','rate_limit'),('permission denied','permission_denied'),('connection error','connection_error'),
            ('validation','validation'),('timeout','timeout'),('max_output_tokens','output_limit'),
            ('unexpected keyword','keyword'),('coroutine','coroutine'),('context_length','context_length'),
            ('country','region'),('schema','schema'),('not found','not_found')) if text in detail]
        row['gates'] = [tag for text,tag in (
            ('草稿','draft'),('未取得恢复','health'),('群规','rules'),('权限','permissions'),
            ('预算','budget'),('重复','duplicate'),('previous_delivery_needs_receipt','pending_receipt'),
            ('delivery_type_unavailable','slot'),('outside_target_acceptance','foreign_operation'),
            ('编辑器','composer'),('冷却','cooldown'),('成员身份','membership'),
            ('验证','verification'),('同一','duplicate'),('短链','tracking_link')) if text in reason]
    report('delivery_path', tasks=rows)


def review_health(c, account):
    """Summarize the bound account's review failures without returning message content."""
    rows = c.execute("""
        SELECT result#>>'{checkpoint,stage}' AS stage,
               result->>'error_type' AS error,
               result#>>'{fault,error_code}' AS fault,
               CASE
                 WHEN result->>'error_detail' ILIKE '%%name resolution%%' THEN 'name_resolution'
                 WHEN result->>'error_detail' ILIKE '%%network is unreachable%%' THEN 'network_unreachable'
                 WHEN result->>'error_detail' ILIKE '%%connection error%%' THEN 'connection_error'
                 WHEN result->>'error_detail' ILIKE '%%not_configured%%' THEN 'model_unconfigured'
                 WHEN result->>'error_detail' ILIKE '%%quota%%' THEN 'quota'
                 WHEN result->>'error_detail' ILIKE '%%budget%%' THEN 'budget'
                 WHEN result->>'error_detail' ILIKE '%%timeout%%' THEN 'timeout'
                 ELSE 'unclassified' END AS signal,
               count(*) AS tasks, count(*) FILTER (WHERE input ? 'reply') AS saved_drafts,
               max((result->>'history_observed_at')::timestamptz) AS latest_observation
        FROM lead.tasks WHERE agent_id=%s AND type='community.engage'
          AND input->>'external_account_id'=%s AND input ? 'membership_task_id'
          AND (result->>'history_observed_at')::timestamptz > now()-interval '24 hours'
          AND result#>>'{history_observation,joined}'='true' AND result#>>'{history_observation,can_reply}'='true'
        GROUP BY 1,2,3,4 ORDER BY latest_observation DESC LIMIT 12
    """,(AID,account)).fetchall()
    stages={'prepare','decide','observe','reply','receipt','model','complete'}
    errors={'ModelUnavailable','ModelConnectionError','ModelTimeoutError','APIConnectionError','APITimeoutError',
            'ReadTimeout','ConnectTimeout','ConnectError','TimeoutError','ValueError','KeyError',
            'TypeError','AttributeError','ValidationError','UnexpectedModelBehavior','ModelHTTPError',
            'HTTPException','AuthenticationError','RateLimitError','ModelBudgetError'}
    faults={'browser_error','navigation_timeout','transport_error','rate_limited','authentication_required',
            'target_mismatch','stalled_checkpoint','send_failed','menu_intercepted'}
    for row in rows:
        for field,allowed in (('stage',stages),('error',errors),('fault',faults)):
            if row[field] is not None and row[field] not in allowed:
                row[field]='other'
    report('review_health', categories=rows)


async def verify(actor, account):
    for attempt in range(2):
        checked = await identity.check(actor)
        checks = checked['checks']
        hashes = {k: hashlib.sha256(ipaddress.ip_address(v['ip']).compressed.encode()).hexdigest()
                  for k,v in checks['probes'].items()}
        device_ok = not checks['device_drift'] and (checks.get('device') or {}).get('hardwareConcurrency') == 4
        exits_ok = hashes == dict(ipify=EXIT_HASH, cloudflare=EXIT_HASH)
        if checked['verified'] and device_ok and exits_ok and checks['os_egress_enforced']:
            break
        report('identity_check_incomplete', probes=len(hashes), observed_exit_mismatch=any(h!=EXIT_HASH for h in hashes.values()),
               device_ok=device_ok, namespace_ok=bool(checks['os_egress_enforced']))
        if attempt or not device_ok or not checks['os_egress_enforced'] or len(hashes)>=2 or any(h!=EXIT_HASH for h in hashes.values()):
            raise ValueError('identity_check_failed')
        await asyncio.sleep(3)
    async with actor.lock:
        actor.touch()
        try:
            for attempt in range(2):
                try:
                    await telegram.open_account(actor.page, account)
                    break
                except Exception as exc:
                    if attempt or not any(code in str(exc) for code in ('NS_ERROR_PROXY_BAD_GATEWAY', 'NS_ERROR_ABORT')):
                        raise
                    await asyncio.sleep(3)
        except Exception:
            from urllib.parse import urlsplit
            loc = urlsplit(actor.page.url)
            public_page = loc.scheme == 'https' and loc.netloc == 'web.telegram.org'
            report('navigation_failed', at_telegram=public_page, at_web_k=public_page and loc.path.startswith('/k/'),
                   upstream_failures=actor.forward_proxy.upstream_failures)
            if public_page:
                try:
                    status = await asyncio.wait_for(actor.page.evaluate("""() => ({
                        dom: document.readyState,
                        chatlist: !!document.querySelector('.chatlist,.chatlist-container'),
                        login_screen: !!document.querySelector('.auth-pages .page-sign'),
                        online: navigator.onLine})"""), 3)
                    report('navigation_dom', **status)
                except Exception:
                    pass
            raise
        if (await telegram.inspect_account(actor.page))['external_account_id'] != account:
            raise ValueError('bound_account_mismatch')
        with tx(AID) as c:
            lease = one(c, """SELECT count(*) AS total, count(*) FILTER (WHERE epoch=%s AND owner_actor_id=%s) AS own
                FROM lead.profile_leases WHERE agent_id=%s AND state='held' AND expires_at>now()""", (actor.epoch, str(os.getpid()), AID))
        if lease != {'total': 1, 'own': 1}:
            raise ValueError('lease_not_exclusive')
        with (STATE/'profiles'/AID/'actor.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise ValueError('profile_lock_missing')
    report('login_verified', binding_matches=True, single_holder=True, cpu=4, both_probes_match=True)


async def official_account_status(actor, account):
    """Check for a newer official clearance. This path only reads the existing bot chat."""
    from lead_app.community_receipts import bound_view
    path = Path(os.environ['HOME'])/'spambot-owner-query-20261108.json'
    record = {}
    try:
        record = json.loads(path.read_bytes())
        if record.get('account') not in (None, account) or record.get('peer') != '178220800':
            return 'undetermined'
        mid = str(record.get('incoming', ''))
        if not mid.isdigit():
            return 'undetermined'
        peer = record['peer']
        async with actor.lock:
            await bound_view(actor.page, account, peer)
            chat = await telegram.selected_chat(actor.page)
            message = await chat.evaluate(r"""(root,{peer,mid})=>{
                const headers=[...root.querySelectorAll('.chat-info .peer-title[data-peer-id]')].filter(e=>e.getBoundingClientRect().width);
                if(headers.length!==1 || headers[0].dataset.peerId!==peer)return null;
                const rows=[...root.querySelectorAll('.bubble:not(.is-out)[data-mid]')].filter(e=>
                    /^\d+$/.test(e.dataset.mid)&&BigInt(e.dataset.mid)>=BigInt(mid)&&e.getBoundingClientRect().width);
                const last=rows.sort((a,b)=>BigInt(b.dataset.mid)>BigInt(a.dataset.mid)?1:-1)[0];
                return last ? {mid:last.dataset.mid,text:last.querySelector('.message,.message-text')?.innerText||''} : null;
            }""", {'peer': peer, 'mid': mid})
        # Earlier versions stored an unbound status. Only the current verified chat can clear it.
        if not message:
            return 'limited' if record.get('status') == 'limited' else 'undetermined'
        text = message['text']
        if re.search(r"no (?:limits|limitations|restrictions) (?:are )?(?:currently )?(?:applied|placed|imposed) (?:to|on) your account|your account is no longer limited|free as a bird|目前没有受到任何限制", text, re.I):
            status = 'no_limits'
        elif re.search(r"account.{0,55}limited|unable to send|cannot send|will not be able to send|账号.{0,12}受限", text, re.I):
            status = 'limited'
        else:
            status = 'limited' if record.get('status') == 'limited' else 'undetermined'
        polled = datetime.now(timezone.utc).isoformat()
        incoming = str(message['mid'])
        restricted = str(record.get('last_limited_mid') or (mid if record.get('status') == 'limited' else '0'))
        if status == 'limited':
            restricted = str(max(int(incoming),int(restricted)))
        if status == 'no_limits' and (int(incoming) <= int(restricted)
                or (incoming == mid and record.get('clearance_mid') != incoming)):
            status = 'undetermined'
        if incoming != mid:
            record['evidence_observed_at'] = polled
        record.update(account=account, incoming=incoming, status=status, checked_at=polled,
                      last_limited_mid=restricted, clearance_mid=incoming if status=='no_limits' else None)
        temp = path.with_suffix('.new')
        temp.write_text(json.dumps(record))
        os.replace(temp, path)
        return status
    except Exception as exc:
        report('official_read_unavailable',error=type(exc).__name__)
    return 'limited' if record.get('status') == 'limited' else 'undetermined'


async def inspect_failed_delivery(actor, account):
    from lead_app.community_receipts import bound_view, message_observation

    with tx(AID) as c:
        claimed = [r for r in receipts(c,account).values() if r['state']=='failed']
    for item in claimed:
        task_id = str(UUID(item['task_id']))
        path = Path(os.environ['HOME'])/('failed-receipt-inspection-'+task_id+'.json')
        if path.exists():
            continue
        with tx(AID) as c:
            task = one(c,"SELECT input,result FROM lead.tasks WHERE agent_id=%s AND id=%s AND type='community.engage'",(AID,task_id))
        saved = task['result']; payload = task['input']; peer = item['peer']
        if payload['external_account_id']!=account or peer!=saved.get('target_id') or saved.get('reply_state')!='failed':
            continue
        signal = {'task':task_id, 'peer':peer, 'matching':(saved.get('receipt_diagnostic') or {}).get('failedMatching')}
        mid = str((saved.get('receipt_diagnostic') or {}).get('mid') or '')
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?',mid):
            continue
        async with actor.lock:
            try:
                await bound_view(actor.page,account,peer)
                check=await message_observation(actor.page,peer,payload['reply']['body'],saved.get('baseline_mid'),saved.get('prior_failed_mids'))
                signal['observation']=check.get('state')
                chat=await telegram.selected_chat(actor.page)
                signal.update(await chat.evaluate(r"""async(root,{peer,mid})=>{
                    const node=[...root.querySelectorAll('.bubble.is-out[data-mid]')].find(n=>n.dataset.mid===mid);
                    const headers=[...root.querySelectorAll('.chat-info .peer-title[data-peer-id]')].filter(x=>x.getBoundingClientRect().width);
                    if(headers.length!==1 || headers[0].dataset.peerId!==peer) throw Error('peer changed');
                    const manager=window.rootScope?.managers?.appMessagesManager || window.appImManager?.managers?.appMessagesManager;
                    const message=manager?.getMessageByPeer ? await Promise.race([
                      manager.getMessageByPeer(Number(peer),Number(mid)),new Promise(resolve=>setTimeout(()=>resolve(null),2500))
                    ]):null;
                    const error=message?.error;
                    const ui=[...(node?.querySelectorAll('[title],[aria-label]')||[]),
                              ...document.querySelectorAll('.toast,.popup-container [role=alert]')]
                        .map(el=>el.getAttribute('title')||el.getAttribute('aria-label')||el.textContent||'').join(' ');
                    const allowed=/\b(?:FLOOD_WAIT_\d+|SLOWMODE_WAIT_\d+|FROZEN_METHOD_INVALID|PEER_FLOOD|CHAT_WRITE_FORBIDDEN|CHAT_SEND_PLAIN_FORBIDDEN|CHAT_SEND_INLINE_FORBIDDEN|CHAT_RESTRICTED|USER_BANNED_IN_CHANNEL|USER_RESTRICTED|CHANNEL_PRIVATE|TOPIC_CLOSED|PREMIUM_ACCOUNT_REQUIRED|ALLOW_PAYMENT_REQUIRED(?:_\d+)?|MESSAGE_TOO_LONG|MSG_ID_INVALID|AUTH_KEY_UNREGISTERED|NETWORK_MIGRATE_\d+)\b/g;
                    return {bubble:!!node,client_manager:!!manager,local_message:!!message,native_codes:[...new Set(
                      (String(error?.type||'')+' '+String(error?.message||'')+' '+ui).match(allowed)||[])]};
                }""",{'peer':peer,'mid':mid}))
            except Exception as exc:
                signal['inspection_error']=type(exc).__name__
        if 'inspection_error' not in signal:
            path.write_text(json.dumps(signal))
        report('native_failure_inspection',**signal)


def select(c, agent, config, *, read_only, operating):
    now = datetime.now(timezone.utc)
    op = community_selection.choose(c, agent, config, now, allow_candidates=operating and config.allow_join,
                                    allow_voice_edits=False, read_only=read_only)
    previously_selected = op
    if operating and agent['desired_state'] == 'running' and config.enabled:
        due = community_schedule.deadlines(c, agent, now).get('next_discovery_at')
        runtime_state = agent['onboarding_state'].get('telegram_executor') or {}
        last = runtime_state.get('last_discovery_at')
        campaigns = one(c, """SELECT bool_or(state='running') AS active, max(completed_at) AS last_finished
            FROM lead.tasks WHERE agent_id=%s AND type='community.discover' AND input->>'config_revision'=%s""",
            (AID,str(agent['config_revision'])))
        if not campaigns['active'] and campaigns['last_finished']:
            configured_due = campaigns['last_finished'] + timedelta(hours=config.discovery_interval_hours)
            due = max(due,configured_due) if due else configured_due
        # Advance one search checkpoint every two minutes under load. An idle browser can resume sooner.
        interval = 120 if op else 15
        deferred_until = runtime_state.get('discovery_deferred_until')
        available = not deferred_until or datetime.fromisoformat(deferred_until) <= now
        if available and due and due <= now and (not last or now-datetime.fromisoformat(last) >= timedelta(seconds=interval)):
            if not op or not op.get('overdue') or (last and now-datetime.fromisoformat(last) >= timedelta(minutes=10)):
                op = {'kind': 'discover', 'paid': True}
    if not op:
        return None
    if op['paid']:
        business = one(c, 'SELECT * FROM lead.business_profiles WHERE agent_id=%s ORDER BY revision DESC LIMIT 1', (AID,))
        if not business:
            return None
        ctx = context(c, AID, business)
        # This dedicated runtime evaluates community choices locally. Continue
        # enforcing operating hours and concurrency without reserving model tokens.
        rating = assess(ctx, 'community_discovery' if op['kind']=='discover' else 'community_progress', 75, paid_step=False)
        affordable = rating['allowed']
        if not affordable:
            report('deferred', reason=rating.get('reason') or 'execution_budget')
            if op['kind'] == 'discover':
                c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{telegram_executor}',
                    coalesce(onboarding_state->'telegram_executor','{}'::jsonb) || %s) WHERE id=%s""",
                    (j({'discovery_deferred_until':(now+timedelta(minutes=5)).isoformat()}), AID))
                if previously_selected and not previously_selected['paid']:
                    return previously_selected
            return community_selection.choose(c, agent, config, now, allow_candidates=False, allow_voice_edits=False, read_only=True)
    return op


async def dispatch(actor, account, path, data, *, production=False, writable=True):
    op = data['operation']
    reads = ('receipt', 'scan_pending', 'observe')
    if not writable and op not in reads:
        return {'state':'blocked','reason':'telegram_read_only'}
    if path != '/agents/' + AID + '/community/step' or op not in (*reads, 'reply', *(['join'] if production else [])):
        return {'state':'skipped', 'reason':'outside_target_acceptance'}
    tid = str(UUID(data['task_id']))
    with tx(AID) as c:
        t = one(c, """SELECT * FROM lead.tasks WHERE agent_id=%s AND id=%s AND type='community.engage'""", (AID,tid))
        if not t or t['input'].get('external_account_id') != account or bound_account(c) != account:
            raise ValueError('task_account_changed')
        if op in ('reply','join') and not pilot_allows((t['result'] or {}).get('target_id')):
            return {'state':'blocked','reason':'outside_pilot_group'}
        if op == 'reply':
            saved = t['result'] or {}
            if saved.get('reply_state') in ('confirmed','dispatching','unknown','failed') or t['state'] == 'reconciling':
                op = 'receipt'
            else:
                kind = slot_kind(t['input'].get('reply') or {})
                if production and kind == 'promotion' and not t['input']['reply'].get('permission_key'):
                    return {'state':'blocked','reason':'missing_placement_evidence'}
                if not kind or (not production and kind in receipts(c, account)):
                    return {'state':'skipped','reason':'delivery_type_unavailable'}
                if one(c, """SELECT 1 FROM lead.tasks WHERE agent_id=%s AND type='community.engage' AND id<>%s
                        AND input->>'external_account_id'=%s AND result->>'reply_state' IN ('unknown','dispatching')
                        AND (result->>'target_id' IS NULL OR result->>'target_id'=%s) LIMIT 1""",
                        (AID,tid,account,saved.get('target_id'))):
                    return {'state':'blocked','reason':'previous_delivery_needs_receipt'}
    if op in ('join', 'reply'):
        def authorize_write():
            record = json.loads((Path(os.environ['HOME'])/'spambot-owner-query-20261108.json').read_bytes())
            if (record.get('account') != account or record.get('status') != 'no_limits'
                    or not str(record.get('clearance_mid') or '').isdigit()
                    or record.get('incoming') != record['clearance_mid']
                    or int(record['clearance_mid']) <= int(record.get('last_limited_mid') or 0)
                    or (datetime.now(timezone.utc)-datetime.fromisoformat(record['checked_at'])).total_seconds() >= 3600
                    or delivery_mode() == 'readonly'):
                raise ValueError('telegram_write_eligibility_changed')

        authorize_write()
        if browser.gateway.actors.get(AID) is not actor or actor.disconnected:
            raise ValueError('verified_actor_changed')
        def take_slot(c, task, peer):
            claim(c, task, peer, production=production)
        return await asyncio.wait_for(runtime.browser_step(actor, tid, op, claim_delivery=take_slot, authorize_write=authorize_write), 105)
    answer = await browser.community_step(UUID(AID), browser.CommunityStepRequest(task_id=UUID(tid),operation=op))
    if op == 'observe':
        report('observed', task_id=tid, group=answer.get('target_id'), joined=answer.get('joined'), can_reply=answer.get('can_reply'))
    return answer


def publish_state(fields):
    with tx(AID) as c:
        c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state, '{telegram_executor}',
            coalesce(onboarding_state->'telegram_executor','{}'::jsonb) || %s)
            WHERE id=%s AND desired_state='running'""", (j(fields), AID))


async def pulse():
    while True:
        await asyncio.to_thread(publish_state, {'heartbeat_at': datetime.now(timezone.utc).isoformat()})
        await asyncio.sleep(30)


async def discover(actor, account):
    """Serve existing authenticated read routes to the discovery client and its browser subprocess."""
    from lead_app import model
    if not getattr(model, 'LOCAL_COMMUNITY_POLICY', False):
        return {'delay': 60, 'reason': 'waiting_for_local_discovery_policy'}
    import uvicorn
    from lead_app.community_growth import discover_communities
    from starlette.responses import Response

    prefix = '/agents/' + AID
    routes = re.compile(re.escape(prefix) + r'/(?:community/search|research/command|research/[0-9a-f-]{36}/(?:hybrid|close))')
    async def read_gateway(scope, receive, send):
        if (scope['type'] != 'http' or scope.get('method') != 'POST' or not routes.fullmatch(scope['path'])
                or actor is not browser.gateway.actors.get(AID) or actor.disconnected):
            await Response(status_code=403)(scope, receive, send)
            return
        with tx(AID) as c:
            if require_agent(c, AID)['desired_state'] != 'running' or bound_account(c) != account:
                await Response(status_code=403)(scope, receive, send)
                return
        await browser.app(scope, receive, send)

    class LocalServer(uvicorn.Server):
        def capture_signals(self):
            return nullcontext()

    server = LocalServer(uvicorn.Config(read_gateway, lifespan='off', access_log=False, log_level='error',
                                        ws='none', timeout_graceful_shutdown=5))
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(('127.0.0.1', 8089))
        except OSError:
            return {'delay': 60, 'reason': 'discovery_gateway_busy'}
        listener.listen()
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                        raise RuntimeError('discovery_gateway_unavailable')
                    await asyncio.sleep(0.02)
            return await discover_communities(AID)
        finally:
            server.should_exit = True
            await serving


async def run(mode):
    # The hybrid browser uses a separate provider process. Public directory
    # and bound Telegram reads use the ordinary browser gateway.
    os.environ['LEAD_BROWSER_USE_ENABLED'] = '0'
    from lead_business import evaluate as evaluate_business
    with service_scope(), tenant_scope(agent_tenant(AID), None):
        if mode == 'verify':
            await asyncio.to_thread(check_previous_interpreter)
            await asyncio.to_thread(evaluate_business, {'event':'admit','operation':'review','paid':False})
            report('business_sandbox_verified')
        with tx(AID) as c:
            a = require_agent(c, AID)
            account = bound_account(c)
            if mode == 'run':
                for row in c.execute("""SELECT t.id::text AS task_id,
                    coalesce(t.result->>'target_id', m.result->>'target_id') AS peer,
                    t.result->>'join_state' AS membership, t.result->>'reply_state' AS delivery,
                    t.result->>'reply_external_id' AS native_receipt,
                    coalesce(t.result#>>'{receipt_observation,status}','awaiting_read') AS last_check,
                    coalesce(t.result->>'receipt_review_after',t.result->>'approval_review_after',
                             t.result->>'retry_after',t.created_at::text) AS scheduled_check
                    FROM lead.tasks t LEFT JOIN lead.tasks m ON m.agent_id=t.agent_id
                      AND m.id::text=t.input->>'membership_task_id'
                    WHERE t.agent_id=%s AND t.type='community.engage' AND t.input->>'external_account_id'=%s
                      AND (t.state='reconciling' OR t.result->>'reply_state' IN ('unknown','dispatching'))
                    ORDER BY t.created_at,t.id""", (AID,account)):
                    report('receipt_ledger', **row, policy='read_original_only')
                for row in c.execute("""SELECT body->>'peer' AS peer,body->>'error_code' AS fault,
                        body->>'last_failure_at' AS last_failure,body->>'read_after' AS next_read
                        FROM lead.memory_entries WHERE agent_id=%s AND kind='observation'
                        AND body->>'type'='community_target_health' AND body->>'account'=%s
                        AND body->>'state'='blocked' AND body->>'peer'!='*' AND deleted_at IS NULL""",(AID,account)):
                    report('target_hold',**row, disposition='defer_group_review',owner='community_operations')
            if mode == 'status':
                draft_inventory(c, account, a)
                delivery_path(c, account)
                review_health(c, account)
                inventory(c, a, account)
                return
            if a['desired_state'] != 'running' or one(c, """SELECT 1 FROM lead.profile_leases WHERE state='held' AND expires_at>now()
                    UNION ALL SELECT 1 FROM lead.actions WHERE state='dispatching'
                    UNION ALL SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now() LIMIT 1"""):
                raise ValueError('profile_busy_or_paused')
        if mode == 'run':
            await check_model_route_once()
            try:
                with tx(AID) as c:
                    reconcile_model_adapter_faults(c, account)
                    report_model_destination(c)
            except Exception:
                report('model_destination', endpoint_kind='unavailable')
        profile = STATE/'profiles'/AID
        if not (profile/'identity.json').is_file() or not (profile/'storage').is_dir():
            raise ValueError('original_profile_missing')
        renew = asyncio.create_task(browser.gateway.maintenance())
        heartbeat = None
        try:
            actor = await browser.gateway.get(AID)
            await verify(actor, account)
            if mode == 'verify':
                return
            mode_name = delivery_mode()
            operating = mode_name != 'acceptance'
            publish_state({'action': 'checking', 'mode': mode_name, 'account_status': 'undetermined',
                           'next_check_at': None, 'heartbeat_at': datetime.now(timezone.utc).isoformat()})
            heartbeat = asyncio.create_task(pulse())
            restriction = await official_account_status(actor, account)
            restriction_checked = time.monotonic()
            report('execution_mode', mode=mode_name, account_status=restriction)
            schedule_sample = None
            await inspect_failed_delivery(actor, account)
            renewed = time.monotonic()
            while True:
                if heartbeat.done():
                    await heartbeat
                if actor is not browser.gateway.actors.get(AID) or actor.disconnected or time.monotonic()-renewed > 300:
                    actor = await browser.gateway.get(AID)
                    await verify(actor, account)
                    renewed = time.monotonic()
                if time.monotonic()-restriction_checked >= 3600:
                    restriction = await official_account_status(actor, account)
                    restriction_checked = time.monotonic()
                writable = restriction == 'no_limits' and mode_name != 'readonly'
                with tx(AID) as c:
                    a = require_agent(c, AID)
                    if a['desired_state'] != 'running' or bound_account(c) != account:
                        return
                    config, ready = inventory(c, a, account)
                    if schedule_sample is None or time.monotonic()-schedule_sample >= 60:
                        try:
                            with c.transaction():
                                record_patrol_schedule(c,a,account,datetime.now(timezone.utc))
                        except Exception as exc:
                            report('patrol_schedule_unavailable',error=type(exc).__name__)
                        schedule_sample = time.monotonic()
                    delivered = {} if operating else receipts(c, account)
                    done = not operating and all(delivered.get(k, {}).get('external_id') for k in SLOTS)
                    op = select(c, a, config, read_only=not writable, operating=operating) if ready and not done else None
                reason = ('acceptance_complete' if done else 'backup_not_ready' if not ready else
                          'no_due_task' if writable else 'readonly_wait')
                status = {'mode': mode_name, 'account_status': restriction, 'writes_enabled': writable,
                          'action': op['kind'] if op else reason, 'next_check_at': None}
                if op and op['kind'] == 'discover':
                    status['last_discovery_at'] = datetime.now(timezone.utc).isoformat()
                publish_state(status)
                result = None
                if op:
                    if op['paid']:
                        rule = await asyncio.to_thread(evaluate_business, {'event':'admit','operation':op['kind'],'paid':True})
                        if not rule['execute']:
                            op = None
                            reason = 'business_release_deferred'
                if op:
                    community_schedule.started(AID)
                    async def rpc(path, data):
                        return await dispatch(actor, account, path, data, production=mode_name=='production',
                                              writable=writable and time.monotonic()-restriction_checked < 3600)
                    async def revise_copy(body, original, standalone):
                        rule = await asyncio.to_thread(evaluate_business, {'event':'draft','kind':'promotion' if standalone else 'reply',
                                                                   'body':body,'source':str((original or {}).get('body',''))[:1200]})
                        return rule['body']
                    if op['kind'] == 'discover':
                        result = await discover(actor, account)
                    else:
                        result = await runtime.community_cycle(AID, rpc, selection=op, revise_copy=revise_copy,
                                    delivery_kinds=set(SLOTS)-delivered.keys(), allow_new_memberships=mode_name=='production')
                    result = result or {'delay':30, 'reason':'no_due_task'}
                    community_schedule.completed(AID, result, respect_delay=True)
                    reason = result['reason']
                    if reason in ('community_candidates_ready','community_joined','community_cycle','community_receipt_review'):
                        from lead_app.community_progress import capture
                        try:
                            with tx(AID) as c:
                                capture(c, AID, require_agent(c, AID, True))
                        except Exception as exc:
                            report('progress_snapshot_failed', error=type(exc).__name__)
                    report('cycle', operation=op['kind'], reason=reason)
                else:
                    report('waiting', reason=reason)
                delay = max(5, min(60, result['delay'])) if result else 45
                publish_state({'action':'waiting', 'last_result':reason,
                               'next_check_at':(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()})
                await asyncio.sleep(delay)
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            renew.cancel()
            await asyncio.gather(renew, return_exceptions=True)
            await browser.gateway.close(AID)


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode not in ('verify','run','status'):
        raise SystemExit(2)
    try:
        asyncio.run(run(mode))
    except Exception as error:
        codes = sorted(set(re.findall(r'\b(?:NS_|SEC_ERROR_|SSL_ERROR_|MOZILLA_PKIX_ERROR_|ERR_)[A-Z_0-9]+\b', str(error))))
        report('failure', mode=mode, error=type(error).__name__, codes=codes,
               stage=[(Path(frame.filename).name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)[-8:]])
        raise SystemExit(1)

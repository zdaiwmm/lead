"""First core-value results from trusted receipts, never visit counts."""
import hashlib
from collections import defaultdict
from datetime import datetime, timedelta


def frozen_goal(c, agent):
    from ..admin_controls import attribution
    from .contracts import GoalRef
    saved = agent['onboarding_state'].get('operating_contract', {}).get('event_ref')
    if not saved or not saved.get('event_name'):
        return None
    policy, _ = attribution(c, str(agent['id']))
    from .store import objective_revision
    return GoalRef.model_validate({'attribution_window_days':policy['attribution_window_days'],
        'freshness_hours':policy['freshness_hours'], **saved,'revision':objective_revision(agent)}).model_dump(mode='json')


def classify_receipts(rows, unit='user', *, start=None, end=None, definition_revision=None, rule_version=None):
    # Establish historical identity before filtering periods or definition versions.
    seen, channels = set(), defaultdict(int)
    unknown = client = unattributed = repeats = excluded = 0
    evidence = []
    for row in rows:
        if not row.get('counted', False) or row.get('scope_key', 'live') != 'live':
            continue
        at = row.get('occurred_at')
        inside = not (at and ((start and at < start) or (end and at > end)))
        if row.get('trust_level') != 'server_verified':
            client += int(inside)
            continue
        key = row.get('unit_key')
        if not key:
            unknown += int(inside)
            continue
        if key in seen:
            repeats += int(inside)
            continue
        if row.get('audience_class') in {'team', 'partner_acceptance', 'diagnostic'} or row.get('value_qualified') is False:
            excluded += int(inside)
            continue
        seen.add(key)
        if not inside:
            continue
        if (row.get('subject_type', 'user') != unit
                or (definition_revision is not None and row.get('definition_revision') != definition_revision)
                or (rule_version and row.get('rule_version') != rule_version)):
            unknown += 1
            continue
        if row.get('first_business_value') is not True:
            unknown += int(row.get('first_business_value') is None)
            continue
        channel = 'product_invite' if row.get('invited') is True else row.get('channel')
        if channel:
            channels[channel] += 1
        else:
            unattributed += 1
        evidence.append(str(row.get('receipt_id',row['event_id'])))
    return {'new_units':sum(channels.values())+unattributed,'channels':dict(channels),
        'unknown_newness':unknown,'unattributed':unattributed,'client_observed':client,
        'repeat_units':repeats,'excluded':excluded,'unit':unit,'evidence_refs':evidence}


def operating_signals(c,as_of,connection_id=None,experiment_id=None):
    from ..db import all_rows, one
    connection=one(c,"SELECT id,state,last_received_at FROM lead.analytics_connections WHERE state<>'disconnected' AND (%s::uuid IS NULL OR id=%s) ORDER BY created_at DESC LIMIT 1",(connection_id,connection_id))
    costs=one(c,"""SELECT coalesce(sum(units),0) tokens FROM lead.usage_events
        WHERE category='model_tokens' AND created_at<=%s AND (%s::uuid IS NULL OR reservation_id IN(SELECT id FROM lead.budget_reservations
            WHERE pricing_snapshot#>>'{growth,experiment_id}'=%s))""",(as_of,experiment_id,str(experiment_id) if experiment_id else None))
    held=one(c,"""SELECT coalesce(sum(units),0) tokens,count(*) FILTER(WHERE state='unknown') unknown
        FROM lead.budget_reservations WHERE state IN ('reserved','unknown') AND period_id IN (SELECT id FROM lead.budget_periods WHERE unit='tokens') AND created_at<=%s
        AND (%s::uuid IS NULL OR pricing_snapshot#>>'{growth,experiment_id}'=%s)""",(as_of,experiment_id,str(experiment_id) if experiment_id else None))
    provider_costs=all_rows(c,"""SELECT b.unit,
        coalesce(sum(CASE WHEN r.state IN ('reserved','unknown') THEN r.units ELSE 0 END),0) held_units,
        coalesce(sum(u.units),0) spent_units,count(*) FILTER(WHERE r.state='unknown') unknown_calls
        FROM lead.budget_reservations r JOIN lead.budget_periods b ON b.id=r.period_id
        LEFT JOIN (SELECT reservation_id,sum(units) units FROM lead.usage_events WHERE category='research_provider' GROUP BY reservation_id) u ON u.reservation_id=r.id
        WHERE r.created_at<=%s AND r.pricing_snapshot ? 'research'
        AND (%s::uuid IS NULL OR r.pricing_snapshot#>>'{research,experiment_id}'=%s)
        GROUP BY b.unit""",(as_of,experiment_id,str(experiment_id) if experiment_id else None))
    result={'exposure':{'value':None,'coverage':'platform_insights_not_connected'},
        'visits':{'visitors':None,'page_views':None,'qualified_visits':None,'coverage':'missing','connection_id':None},
        'event_funnel':[],'costs':{'model_tokens':int(costs['tokens']),'held_tokens':int(held['tokens']),
            'unknown_billing_calls':held['unknown'],'advertising':None,'paid_cooperation':None,'research_providers':provider_costs}}
    if not connection:
        return result
    c.execute("SELECT set_config('app.analytics_scope','live',true)")
    visits=one(c,"""SELECT count(*) page_views,count(DISTINCT coalesce(p.user_key,p.anonymous_id::text)) visitors,
        count(DISTINCT p.visit_session_id) FILTER(WHERE v.qualified IS TRUE) qualified_visits
        FROM lead.pixel_receipts p LEFT JOIN lead.visit_sessions v ON v.id=p.visit_session_id
        LEFT JOIN lead.tracking_links l ON l.id=p.entry_link_id
        WHERE p.connection_id=%s AND p.scope_key='live' AND p.counted AND p.event_name='PageView'
          AND p.occurred_at BETWEEN %s-interval '30 days' AND %s AND p.received_at<=%s
          AND (%s::uuid IS NULL OR EXISTS(SELECT FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
              WHERE a.content_id=l.content_id AND a.state='confirmed' AND t.experiment_id=%s))""",
        (connection['id'],as_of,as_of,as_of,experiment_id,experiment_id))
    freshness=connection['last_received_at']
    coverage='testing' if connection['state']!='verified' else 'stale' if not freshness or freshness<as_of-timedelta(hours=24) else 'healthy'
    result['visits']={**visits,'coverage':coverage,'connection_id':str(connection['id']),'window_days':30}
    result['event_funnel']=all_rows(c,"""SELECT event_name,definition_revision,
        count(*) FILTER(WHERE trust_level='server_verified') server_events,
        count(*) FILTER(WHERE trust_level='client_observed') client_events,max(received_at) last_received_at
        FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key='live' AND counted
        AND occurred_at BETWEEN %s-interval '30 days' AND %s AND received_at<=%s
        GROUP BY event_name,definition_revision ORDER BY event_name,definition_revision LIMIT 30""",(connection['id'],as_of,as_of,as_of))
    return result


def summarize(aid, experiment_id=None, as_of=None):
    from ..db import all_rows, one, require_agent, tx
    from .contracts import GoalRef, utcnow
    as_of = as_of or utcnow()
    with tx(aid) as c:
        agent = require_agent(c, aid)
        exp = one(c,'SELECT * FROM lead.growth_experiments WHERE id=%s',(experiment_id,)) if experiment_id else None
        # Never retrofit a current definition to an older unmeasured experiment.
        goal = exp['specification'].get('activation_ref') if exp else frozen_goal(c,agent)
        signals=operating_signals(c,as_of,(goal or {}).get('connection_id'),experiment_id)
        base = {**signals,'definition_ref':goal,'measurement':'missing','new_units':None,'channels':{},
            'unknown_newness':None,'unattributed':None,'as_of':as_of.isoformat(),'window_status':'unknown'}
        if not goal or not goal.get('event_name') or not goal.get('connection_id'):
            return base
        goal = GoalRef.model_validate(goal)
        conn = one(c,"SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",(goal.connection_id,))
        if not conn or not goal.definition_revision:
            return base
        c.execute("SELECT set_config('app.analytics_scope','live',true)")
        start = exp['created_at'] if exp else as_of-timedelta(days=30)
        review = datetime.fromisoformat(exp['specification']['review_at']) if exp else None
        end = min(as_of,review) if review else as_of
        rows = all_rows(c,"""SELECT r.id receipt_id,r.event_id,r.occurred_at,r.counted,r.trust_level,r.scope_key,r.definition_revision,
          CASE WHEN %(unit)s='user' THEN coalesce(r.user_key,r.business_key) ELSE r.business_key END unit_key,
          e.properties->'first_business_value' first_business_value,
          coalesce(e.properties->>'subject_type','user') subject_type,e.properties->'invited' invited,
          e.properties->>'rule_version' rule_version,e.properties->>'audience_class' audience_class,
          e.properties->'value_qualified' value_qualified,
          CASE WHEN first_page.occurred_at+make_interval(days=>%(window)s)>=r.occurred_at
            AND a.id IS NOT NULL AND coalesce(a.intent->>'publication_purpose','business')='business'
            AND (s.platform<>'tiktok' OR t.result#>>'{delivery,state}'='public')
            THEN l.platform END channel,t.experiment_id source_experiment
          FROM lead.pixel_receipts r LEFT JOIN lead.analytics_clean_events e ON
            (e.connection_id,e.scope_key,e.event_id)=(r.connection_id,r.scope_key,r.event_id)
          LEFT JOIN lead.pixel_touchpoints frozen_touch ON frozen_touch.id=r.touchpoint_id AND frozen_touch.connection_id=r.connection_id
          LEFT JOIN LATERAL (
            SELECT p.id,p.entry_link_id,p.occurred_at FROM lead.pixel_receipts p
            WHERE p.connection_id=r.connection_id AND p.scope_key='live' AND p.counted AND p.event_name='PageView'
              AND p.anonymous_id=frozen_touch.anonymous_id AND p.occurred_at<=frozen_touch.occurred_at
              AND p.received_at<=(SELECT min(origin.received_at) FROM lead.pixel_receipts origin
                WHERE origin.connection_id=r.connection_id AND origin.scope_key='live' AND origin.counted
                  AND origin.event_name='PageView' AND origin.touchpoint_id=frozen_touch.id
                  AND origin.entry_link_id=frozen_touch.tracking_link_id)
            ORDER BY p.occurred_at,p.event_id LIMIT 1
          ) frozen_first ON frozen_touch.id IS NOT NULL
          LEFT JOIN LATERAL (
            SELECT candidate.entry_link_id,candidate.occurred_at FROM (
            SELECT CASE WHEN frozen_first.id IS NOT NULL THEN frozen_first.entry_link_id ELSE frozen_touch.tracking_link_id END entry_link_id,
              coalesce(frozen_first.occurred_at,frozen_touch.occurred_at) occurred_at,0 priority,frozen_touch.id::text event_id
            WHERE frozen_touch.id IS NOT NULL AND (e.context->>'touchpoint_id' IS NOT NULL OR r.anonymous_id IS NULL)
            UNION ALL
            SELECT p.entry_link_id,p.occurred_at,1 priority,p.event_id FROM lead.pixel_receipts p
            WHERE p.connection_id=r.connection_id AND p.scope_key='live' AND p.counted AND p.event_name='PageView'
              AND p.occurred_at<=r.occurred_at AND p.received_at<=r.received_at
              AND NOT(frozen_touch.id IS NOT NULL AND (e.context->>'touchpoint_id' IS NOT NULL OR r.anonymous_id IS NULL))
              AND (p.anonymous_id=r.anonymous_id OR (r.user_key IS NOT NULL AND p.user_key=r.user_key))
              AND NOT EXISTS(SELECT FROM lead.pixel_receipts other WHERE other.connection_id=p.connection_id
                AND other.scope_key='live' AND other.server_identified AND other.anonymous_id=p.anonymous_id
                AND other.user_key IS NOT NULL AND r.user_key IS NOT NULL AND other.user_key<>r.user_key)
            UNION ALL
            SELECT click.tracking_link_id,click.occurred_at,2 priority,click.id::text event_id FROM lead.analytics_clicks click
            WHERE click.id=r.click_id AND click.scope_key='live' AND r.anonymous_id IS NULL AND frozen_touch.id IS NULL
            ) candidate ORDER BY candidate.priority,candidate.occurred_at,candidate.event_id LIMIT 1
          ) first_page ON true
          LEFT JOIN lead.tracking_links l ON l.id=first_page.entry_link_id
          LEFT JOIN LATERAL (SELECT a.* FROM lead.actions a WHERE a.content_id=l.content_id
            AND a.state='confirmed' AND a.confirmed_at<=first_page.occurred_at
            ORDER BY a.confirmed_at,a.id LIMIT 1) a ON true
          LEFT JOIN lead.tasks t ON t.id=a.task_id
          LEFT JOIN lead.account_targets target ON target.id=a.target_id
          LEFT JOIN lead.social_accounts s ON s.id=target.account_id
          WHERE r.connection_id=%(cid)s AND r.event_name=%(event)s AND r.scope_key='live'
            AND r.occurred_at<=%(asof)s AND r.received_at<=%(asof)s ORDER BY r.occurred_at,r.event_id""",
            {'unit':goal.unit,'window':goal.attribution_window_days,'cid':goal.connection_id,
             'event':goal.event_name,'asof':as_of})
        if exp:
            for row in rows:
                if str(row.get('source_experiment'))!=str(experiment_id) or row.get('invited') is True:
                    if row['occurred_at']>=start:
                        row['first_business_value'] = False
        freshness = one(c,"""SELECT max(received_at) last FROM lead.pixel_receipts WHERE connection_id=%s
            AND event_name=%s AND definition_revision=%s AND counted AND trust_level='server_verified'""",
            (goal.connection_id,goal.event_name,goal.definition_revision))['last']
        tested = one(c,"""SELECT EXISTS(SELECT FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key<>'live'
            AND event_name=%s AND definition_revision=%s AND counted AND trust_level='server_verified') ok""",
            (goal.connection_id,goal.event_name,goal.definition_revision))['ok']
        rule = ('activation' if goal.event_name=='Activation' else goal.event_name)+f'-v{goal.definition_revision}'
        covered = tested or any(row.get('definition_revision')==goal.definition_revision and row.get('rule_version')==rule and row.get('trust_level')=='server_verified' for row in rows)
        state = 'testing' if conn['state']!='verified' or not covered else (
            'stale' if not freshness or freshness<as_of-timedelta(hours=goal.freshness_hours) else 'healthy')
        result = classify_receipts(rows,goal.unit,start=start,end=end,definition_revision=goal.definition_revision,rule_version=rule)
        if not covered or (not result['new_units'] and result['unknown_newness']):
            result['new_units'] = None
        status = 'observing' if not review or as_of<review else (
            'settling' if as_of<review+timedelta(hours=goal.delay_allowance_hours) else 'closed')
    return {**base,**result,'measurement':state,'window_status':status,'evidence_count':len(rows),
        'event_name':goal.event_name,'evidence_revision':hashlib.sha256(str(sorted(result['evidence_refs'])).encode()).hexdigest(),
        'window':{'start':start.isoformat(),'end':end.isoformat()},
        'interpretation':'First core-value observations; not proof of causal lift or scale.'}

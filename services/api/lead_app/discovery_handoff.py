"""Cooperative release drain for a single-owner, checkpointed discovery lane.

Call only at the idle boundary of the lane that owns the discovery. Each call
holds the agent row lock until commit. A parked task retains its result and
schedule. The release controller never infers quiescence from a timer alone.
"""

from .db import j, one

PARKED = "discovery_handoff"


def check_discovery_handoff(c, agent, account):
    """Return True while this lane must refrain from starting discovery."""
    aid = agent["id"]
    parked = one(c, """SELECT * FROM lead.tasks WHERE agent_id=%s AND type='community.discover'
        AND state='planned' AND input ? 'discovery_handoff' FOR UPDATE""", (aid,))
    if agent["desired_state"] != "running":
        return True
    request = one(c, """SELECT onboarding_state->'home_discovery_drain' AS drain FROM lead.agents
        WHERE id=%s AND (onboarding_state#>>'{home_discovery_drain,until}')::timestamptz>now()""", (aid,))
    if not parked and not request:
        return False
    # Takeovers and unresolved writes keep their original owner and receipt path.
    blocked = one(c, """SELECT 1 WHERE EXISTS(SELECT FROM lead.takeover_sessions WHERE agent_id=%s
        AND state IN ('requested','active','ending') AND expires_at>now())
        OR EXISTS(SELECT FROM lead.actions WHERE agent_id=%s AND state IN ('prepared','dispatching','unknown'))
        OR EXISTS(SELECT FROM lead.tasks t WHERE agent_id=%s AND type='community.engage' AND
          (state IN ('queued','running','reconciling') OR
           ((result->>'reply_state' IN ('dispatching','unknown') OR result->>'join_state' IN ('dispatching','unknown')
             OR result->>'leave_state' IN ('dispatching','unconfirmed')) AND NOT (
               t.state IN ('succeeded','failed','canceled') AND EXISTS(
                 SELECT FROM lead.social_accounts s WHERE s.agent_id=t.agent_id AND s.id::text=t.input->>'account_id'
                 AND s.external_account_id=%s AND s.state='connected' AND s.external_account_id<>t.input->>'external_account_id')
               AND NOT EXISTS(SELECT FROM lead.social_accounts s WHERE s.agent_id=t.agent_id AND s.state='connected'
                 AND s.external_account_id=t.input->>'external_account_id')))))""",
        (aid, aid, aid, account))
    if blocked:
        return True
    versions = [agent["control_version"], agent["config_revision"], agent["authorization_version"]]
    if parked:
        mark = parked["input"][PARKED]
        same_account = one(c, """SELECT 1 FROM lead.social_accounts WHERE agent_id=%s
            AND id::text=%s AND external_account_id=%s AND state='connected' AND automation_enabled""",
            (aid, parked["input"]["account_id"], account))
        if (not same_account or mark["versions"] != versions or mark["account"] != account or
                parked["input"].get("research_request", "") != agent["onboarding_state"].get("community_research_request", "")):
            c.execute("""UPDATE lead.tasks SET state='canceled',completed_at=now(),
                result=result || %s WHERE id=%s""", (j({"handoff_outcome":"scope_changed"}), parked["id"]))
            return True
        if request:
            return True
        # Same task, cursor, evidence and due time, including after rollback.
        c.execute("UPDATE lead.tasks SET state='running',input=input-%s::text WHERE id=%s",
                  (PARKED, parked["id"]))
        return False
    task = one(c, """SELECT t.id FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id
        JOIN lead.social_accounts s ON s.agent_id=a.id AND s.id::text=t.input->>'account_id'
          AND s.external_account_id=t.input->>'external_account_id' AND s.state='connected' AND s.automation_enabled
        WHERE a.id=%s AND t.type='community.discover' AND t.state='running'
          AND t.input->>'checkpoint_version'='2' AND t.result->>'phase'='native_collect'
          AND t.result->>'native_cursor' ~ '^[0-9]+$' AND t.input->>'external_account_id'=%s
          AND t.input->>'control_version'=a.control_version::text AND t.input->>'config_revision'=a.config_revision::text
          AND coalesce(t.input->>'research_request','')=coalesce(a.onboarding_state->>'community_research_request','')
          AND (a.onboarding_state#>>'{telegram_executor,native_search_after}')::timestamptz>now()
          AND NOT EXISTS (SELECT FROM lead.tasks other WHERE other.agent_id=a.id AND other.id<>t.id
                    AND other.type=t.type AND other.state IN ('running','queued'))
        FOR UPDATE OF t""", (aid, account))
    if task:
        c.execute("UPDATE lead.tasks SET state='planned',input=input || %s WHERE id=%s",
                  (j({PARKED: {"sha":request["drain"]["sha"], "versions":versions, "account":account}}), task["id"]))
    return bool(task)

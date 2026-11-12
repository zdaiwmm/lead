"""Read actual native first-operation proof without inheriting old authority."""
from .db import all_rows


def verified_operations(c, account_id):
    rows=all_rows(c,"""SELECT DISTINCT proof.key operation FROM lead.account_targets t
        JOIN lead.social_accounts s ON s.id=t.account_id
        CROSS JOIN LATERAL jsonb_each(CASE
            WHEN jsonb_typeof(t.verified_permissions->'browser_verified_operations')='object'
            THEN t.verified_permissions->'browser_verified_operations' ELSE '{}'::jsonb END) proof
        JOIN lead.actions a ON a.id::text=proof.value->>'action_id' AND a.target_id=t.id AND a.type=proof.key
        JOIN lead.action_attempts ap ON ap.action_id=a.id AND ap.outcome='confirmed'
        JOIN lead.authorization_grants g ON g.id=a.grant_id AND g.target_id=t.id
        WHERE s.id=%s AND s.state='connected' AND t.state='active'
            AND proof.key IN ('publish','reply') AND a.state='confirmed'
            AND proof.value->>'method_version'='native-browser-v1'
            AND s.binding_version::text=proof.value->>'binding_version'
            AND a.intent->>'binding_version'=proof.value->>'binding_version'
            AND proof.value->>'method_hash'=ap.observation#>>'{browser_request,method_hash}'
            AND coalesce(t.verified_permissions#>ARRAY['browser_methods',proof.key],
                         t.verified_permissions->'browser_method')=a.intent->'browser_method'
            AND g.revoked_at IS NULL AND g.allowed_actions ? proof.key
            AND lead.growth_receipt_valid(a.id)
        ORDER BY operation""",(account_id,))
    return [row['operation'] for row in rows]

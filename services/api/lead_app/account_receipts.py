"""Close scheduling for a retired Telegram identity while retaining its native evidence."""


def archive_account_receipts(c, aid, account_id, external_id):
    if not external_id:
        return 0
    return c.execute(
        """UPDATE lead.tasks SET state='canceled', completed_at=now(), version=version+1,
            result=(coalesce(result,'{}') - 'reply_state' - 'join_state' - 'receipt_review_after'
                    - 'approval_review_after') || jsonb_build_object(
                'archived_receipt', jsonb_build_object('reason','account_disconnected',
                    'previous_state',state,'previous_completed_at',completed_at,'result',result),
                'outcome',jsonb_build_object('state','canceled','reason','账号已解绑，停止自动核对'))
            WHERE agent_id=%s AND type='community.engage'
              AND input->>'account_id'=%s AND input->>'external_account_id'=%s
              AND (state='reconciling' OR (state IN ('succeeded','failed')
                   AND (result->>'join_state' IN ('pending_approval','unknown','dispatching')
                        OR result->>'reply_state' IN ('unknown','dispatching'))))""",
        (aid, str(account_id), external_id),
    ).rowcount

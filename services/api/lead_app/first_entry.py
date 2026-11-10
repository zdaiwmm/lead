"""First observed arrival for customers qualified by the site's first-event contract."""

from .db import all_rows


def customers(c, target, start, end, experiment_id=None):
    """Count customers after selecting their earliest qualifying event across periods."""
    # Scope is one project and its selected site. Hashes issued by different
    # site connections cannot prove that two people are the same person.
    rows = all_rows(
        c,
        """
        WITH earliest AS (
          SELECT DISTINCT ON (user_key) user_key,occurred_at,definition_revision,server_identified
          FROM lead.pixel_receipts WHERE connection_id=%(cid)s AND scope_key='live'
            AND counted AND trust_level='server_verified' AND user_key IS NOT NULL
            AND event_name=%(event)s AND occurred_at<%(end)s
          ORDER BY user_key,occurred_at,event_id
        ), qualified AS (
          SELECT * FROM earliest WHERE occurred_at >= %(start)s AND definition_revision=%(version)s
            AND server_identified
        )
        SELECT link.platform, count(*) AS people FROM qualified q
        LEFT JOIN LATERAL (
          SELECT page.entry_link_id, device.anonymous_id FROM (
            SELECT DISTINCT s.anonymous_id FROM lead.pixel_receipts s
            WHERE s.connection_id=%(cid)s AND s.scope_key='live' AND s.server_identified
              AND s.user_key=q.user_key AND s.anonymous_id IS NOT NULL
          ) device
          CROSS JOIN LATERAL (
            SELECT p.entry_link_id,p.occurred_at,p.event_id FROM lead.pixel_receipts p
            WHERE p.connection_id=%(cid)s AND p.scope_key='live' AND p.counted
              AND p.event_name='PageView' AND p.occurred_at<=q.occurred_at
              AND p.anonymous_id=device.anonymous_id
            ORDER BY p.occurred_at,p.event_id LIMIT 1
          ) page
          ORDER BY page.occurred_at,page.event_id LIMIT 1
        ) arrival ON true
        LEFT JOIN lead.tracking_links link ON link.id=arrival.entry_link_id
          AND NOT EXISTS (SELECT 1 FROM lead.pixel_receipts other WHERE other.connection_id=%(cid)s
            AND other.scope_key='live' AND other.server_identified AND other.anonymous_id=arrival.anonymous_id
            AND other.user_key<>q.user_key)
        WHERE (%(experiment)s::text IS NULL OR EXISTS (
          SELECT 1 FROM lead.tasks t WHERE t.type LIKE '%%.publish'
            AND t.input->'operating_plan'->>'experiment_id'=%(experiment)s
            AND t.input->>'content_id'=link.content_id::text))
        GROUP BY link.platform
        """,
        {
            "cid": target["connection_id"],
            "event": target["name"],
            "version": target["revision"],
            "start": start,
            "end": end,
            "experiment": experiment_id,
        },
    )
    channels = {row["platform"]: row["people"] for row in rows if row["platform"]}
    unknown = sum(row["people"] for row in rows if row["platform"] is None)
    return {
        "channels": channels,
        "unknown": unknown,
        "observed": sum(channels.values()) + unknown,
    }

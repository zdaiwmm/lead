"""Read-only comparison between Chamber's actual card data and original live projections."""
import json
from datetime import datetime,timedelta
from uuid import UUID
from zoneinfo import ZoneInfo
from lead_app.analytics_ingest import scoped_tx
from lead_app.analytics_reporting import overview
from lead_app.identity_store import agent_tenant
from lead_app.pixel_events import report as pixel_report
from lead_app.scope import service_scope,tenant_scope

AID='b1f9a23c-1153-4c67-a63b-11ebd264c687'


def verify(aid):
    tenant=agent_tenant(aid)
    with service_scope(), tenant_scope(tenant,None):
        parsed=UUID(aid)
        data=overview(parsed)['series'][-1]
        site=pixel_report(parsed)
        day=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0)
        with scoped_tx(tenant,aid,'live') as c:
            counts=c.execute("""SELECT count(*) AS clicks,count(DISTINCT anonymous_id) AS devices
                FROM lead.visit_sessions WHERE agent_id=%s AND source='redirect' AND exclusion_reason IS NULL
                AND started_at>=%s AND started_at<%s""",(aid,day,day+timedelta(days=1))).fetchone()
            page=c.execute("""SELECT count(*) AS pv,CASE WHEN count(*) FILTER(WHERE anonymous_id IS NULL)>0
                THEN NULL ELSE count(DISTINCT (connection_id,anonymous_id)) END AS site_devices
                FROM lead.pixel_receipts WHERE agent_id=%s AND scope_key='live' AND counted AND event_name='PageView'
                AND occurred_at>=%s AND occurred_at<%s""",(aid,day,day+timedelta(days=1))).fetchone()
        raw={**counts,**page}
        # Unknown historical device identities are intentionally displayed as unmeasured.
        if raw['clicks'] and raw['devices']==0: raw['devices']=None
        fields={k:data.get(k) for k in raw}
        checks={k:('unmeasured' if fields[k] is None else 'match' if fields[k]==raw[k] else 'mismatch') for k in raw}
        event_fields=[{k:e.get(k) for k in ('name','label','method','tested','last_occurred_at')} for e in site['pixel'].get('events',[])]
        return {'day':day.date(),'state':data.get('report_state'),'refreshed':data.get('updated_at'),'cards':fields,
                'live_source':raw,'comparison':checks,'site':(site.get('connection') or {}).get('site_host'),'events':event_fields}


if __name__=='__main__':
    print('CHAMBER_METRICS '+json.dumps(verify(AID),ensure_ascii=False,default=str),flush=True)

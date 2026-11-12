"""A still-running old same-URL worker cannot borrow the current page's nonce."""
import asyncio
import copy
import hashlib
import os

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_compiled_worker_version_must_actually_control_the_original_page(monkeypatch):
    import test_native_boundary_product as boundary
    from native_browser_boundary_fixture import BoundarySite
    original=BoundarySite._worker
    revision=[1]
    def script():
        return original()+'\n// Controlled script revision '+str(revision[0])+'\n'
    monkeypatch.setattr(BoundarySite,'_worker',staticmethod(script))
    async def check(data):
        actor,controller,page,site=(data[key] for key in ('actor','controller','page','site'))
        runtime=actor.native_runtime
        gate=runtime.gate
        root=controller.lease['target_id']
        start_calls=len(data['context']['model_calls'])
        cdp=await actor.context.new_cdp_session(page)
        versions={}
        def observed(event):
            for version in event['versions']:
                versions[version['versionId']]=version
        cdp.on('ServiceWorker.workerVersionUpdated',observed)
        await cdp.send('ServiceWorker.enable')
        legacy=None
        legacy_cdp=None
        legacy_versions={}
        async def until(predicate):
            async with asyncio.timeout(8):
                while not predicate():
                    await asyncio.sleep(.02)
        try:
            await page.evaluate("navigator.serviceWorker.register('/sw.js',{scope:'/account'})")
            copied=copy.deepcopy(controller.lease)
            copied.pop('target_id',None)
            legacy=await runtime.acquire_page(copied,site.origin()+'/account',internal_parent=root)
            legacy_page=runtime.pages[legacy['target_id']]['page']
            await legacy_page.evaluate('navigator.serviceWorker.ready')
            await legacy_page.reload(wait_until='domcontentloaded')
            legacy_cdp=await actor.context.new_cdp_session(legacy_page)
            legacy_cdp.on('ServiceWorker.workerVersionUpdated',lambda event:legacy_versions.update({v['versionId']:v for v in event['versions']}))
            await legacy_cdp.send('ServiceWorker.enable')
            await until(lambda:any(version.get('status')=='activated' and legacy['target_id'] in version.get('controlledClients',[]) for version in legacy_versions.values()))
            old_version=next(version.copy() for version in legacy_versions.values() if version.get('status')=='activated' and legacy['target_id'] in version.get('controlledClients',[]))
            old_target=old_version['targetId']
            old_sessions=[session for session,info in gate.targets.items() if info['targetId']==old_target]
            assert len(old_sessions)==1
            old_session=old_sessions[0]
            await until(lambda:any(owner==old_session and info.get('url')==site.origin()+'/sw.js' for (owner,_),info in gate.scripts.items()))
            old_script=next(script for (owner,script),info in gate.scripts.items() if owner==old_session and info.get('url')==site.origin()+'/sw.js')
            old_source=(await gate.send('Debugger.getScriptSource',{'scriptId':old_script},old_session))['scriptSource']
            revision[0]=2
            await page.evaluate("navigator.serviceWorker.getRegistration('/home').then(reg=>reg.update())")
            await until(lambda:any(version.get('status')=='activated' and root in version.get('controlledClients',[]) and version.get('targetId')!=old_target
                and any(other.get('status')=='redundant' and other.get('registrationId')==version.get('registrationId') for other in versions.values()) for version in versions.values()))
            current=next(version.copy() for version in versions.values() if version.get('status')=='activated' and root in version.get('controlledClients',[]))
            assert current['targetId']!=old_target and current['scriptURL']==old_version['scriptURL']
            old_worker=None
            for worker in actor.context.service_workers:
                if worker.url==site.origin()+'/sw.js' and await worker.evaluate('self.registration.scope')==site.origin()+'/account':
                    old_worker=worker
            assert old_worker and await old_worker.evaluate('true')
            await until(lambda:legacy_versions[old_version['versionId']]['runningStatus']=='running')
            assert old_session in gate.targets and (old_session,old_script) in gate.scripts
            assert legacy_versions[old_version['versionId']]['status']=='activated'
            assert root not in legacy_versions[old_version['versionId']].get('controlledClients',[])
            data['post_seen']['/session/refresh'].clear()
            before=data['source_count']('POST','/session/refresh')
            await page.evaluate("""async()=>{const reg=await navigator.serviceWorker.getRegistration('/account');reg.active.postMessage({action:'refresh',nonce:document.querySelector('[data-background=refresh]').dataset.nonce})}""")
            await asyncio.wait_for(data['post_seen']['/session/refresh'].wait(),5)
            await asyncio.sleep(.05)
            assert data['source_count']('POST','/session/refresh')==before
            refused=[event for event in data['decisions'] if event['url']==site.origin()+'/session/refresh']
            assert refused and not refused[-1]['allowed']
            # The same fresh nonce works via the actual current controller.
            data['post_seen']['/session/refresh'].clear()
            await page.get_by_role('button',name='Refresh membership').click()
            await asyncio.wait_for(data['post_seen']['/session/refresh'].wait(),5)
            await until(lambda:data['source_count']('POST','/session/refresh')==before+1)
            proof=next(event['read_proof'] for event in reversed(data['decisions']) if event.get('read_proof') and event['read_proof']['role']=='session_maintenance')
            assert proof['worker_target_id']==current['targetId'] and proof['worker_version_id']==current['versionId']
            assert proof['worker_script_hash']!=hashlib.sha256(old_source.encode()).hexdigest()
            assert len(data['context']['model_calls'])==start_calls
            print('NATIVE_WORKER_CONTROLLER_DIAGNOSTICS',{'same_script_url':True,'old_still_running':True,'old_metadata_present':True,
                'old_worker_source_posts':0,'current_worker_source_posts':1,'new_model_requests':0})
            return {'old_worker_refused':True,'current_worker_allowed':True}
        except Exception:
            print('NATIVE_WORKER_VERSION_DIAGNOSTICS',{'versions':versions,'root':root,'legacy':legacy['target_id'] if legacy else None,
                'gate_versions':getattr(gate,'worker_versions',{}),'legacy_versions':legacy_versions})
            raise
        finally:
            if legacy_cdp:
                await legacy_cdp.detach()
            if legacy:
                await runtime.release_page(legacy['target_id'])
            await cdp.detach()
    await boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch,boundary_hook=check)

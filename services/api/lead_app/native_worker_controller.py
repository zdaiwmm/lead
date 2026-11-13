"""Refresh one owned page's actual SW observer; never infer permission from URL.

The page-local client projection can omit the actual controlling page.
Re-subscribing that exact manager session yields a fresh client projection;
it does not stop the website worker or clear browser storage.
"""
import asyncio
import copy

_WAIT_SECONDS = 1
_CONTROLLER = """()=>{const worker=navigator.serviceWorker?.controller;
    return worker?{script_url:worker.scriptURL,state:worker.state}:null;}"""
_LEASE_FIELDS = ('task_id','task_generation','claim','account_id','binding_version',
    'intent_revision','control_version','generation','connection_ref','control_mode','challenge_version')


def _page_session(runtime,target_id):
    gate=runtime.gate
    sessions=[sid for sid,info in gate.targets.items() if info.get('targetId')==target_id and info.get('type')=='page']
    if (runtime.closed or not gate.token or not gate.context_id or len(sessions)!=1
            or gate.owners.get(sessions[0])!=target_id
            or gate.targets[sessions[0]].get('browserContextId')!=gate.context_id):
        raise ValueError('native_worker_page_unavailable')
    return sessions[0]


def _proof(gate,session,root,script):
    candidates=[]
    for version in gate.worker_versions_by_page.get(session,{}).values():
        target=version.get('targetId')
        global_version=gate.worker_versions.get(version['versionId'],{})
        workers=[info for info in gate.targets.values() if info.get('targetId')==target
            and info.get('type')=='service_worker' and info.get('browserContextId')==gate.context_id]
        if (not target or version.get('status')!='activated' or version.get('runningStatus')!='running'
                or root not in version.get('controlledClients',[]) or version.get('scriptURL')!=script
                or global_version.get('status')!='activated' or global_version.get('runningStatus')!='running'
                or global_version.get('targetId') not in (None,target) or len(workers)!=1
                or workers[0].get('url')!=script):
            continue
        candidates.append({'page_target_id':root,'page_session':session,
            'worker_target_id':target,'worker_version_id':version['versionId']})
    return candidates[0] if len(candidates)==1 else None


async def refresh_page_controller(runtime,target_id):
    """Get fresh page-local controlling-version facts under its original lease.

    No controller is a normal page. A present controller needs a fresh exact
    version/client projection; global controlledClients never substitutes for it.
    This function neither navigates nor creates/changes a business permission.
    """
    entry=runtime.pages.get(target_id)
    if not entry:
        raise ValueError('native_worker_page_unavailable')
    page,scope=entry['page'],entry['scope']
    lease=copy.deepcopy({key:scope.get(key) for key in _LEASE_FIELDS})
    await asyncio.to_thread(runtime.current_scope,scope)
    session=_page_session(runtime,target_id)
    gate=runtime.gate
    token=gate.token
    page_info=gate.targets[session]

    async def document():
        frame=(await gate.send('Page.getFrameTree',{},session))['frameTree']['frame']
        return frame.get('id'),frame.get('loaderId'),frame.get('url')

    before=await document()

    async def fence():
        await asyncio.to_thread(runtime.current_scope,scope)
        if (runtime.gate is not gate or gate.token is not token or runtime.pages.get(target_id) is not entry
                or entry.get('page') is not page or entry.get('scope') is not scope
                or {key:scope.get(key) for key in _LEASE_FIELDS}!=lease
                or _page_session(runtime,target_id)!=session or gate.targets.get(session) is not page_info):
            raise ValueError('native_worker_page_changed')
        if await document()!=before or page.url!=before[2]:
            raise ValueError('native_worker_document_changed')

    controller=await page.evaluate(_CONTROLLER)
    # An old controlled-client stamp must not survive a document with no actual
    # controller. Ordinary non-SW pages need no ServiceWorker subscription wait.
    gate.worker_versions_by_page.pop(session,None)
    if not controller:
        await fence()
        return None
    script=controller.get('script_url')
    if not isinstance(script,str) or not script:
        raise ValueError('native_worker_controller_unproven')
    try:
        async with asyncio.timeout(_WAIT_SECONDS):
            await gate.send('ServiceWorker.disable',{},session)
            gate.worker_versions_by_page.pop(session,None)
            await gate.send('ServiceWorker.enable',{},session)
            while True:
                proof=_proof(gate,session,target_id,script)
                if proof:
                    break
                await asyncio.sleep(.01)
    except TimeoutError as error:
        raise ValueError('native_worker_controller_unproven') from error
    current=await page.evaluate(_CONTROLLER)
    if not current or current.get('script_url')!=script or current.get('state')!='activated':
        raise ValueError('native_worker_controller_changed')
    await fence()
    if _proof(gate,session,target_id,script)!=proof:
        raise ValueError('native_worker_controller_changed')
    return proof

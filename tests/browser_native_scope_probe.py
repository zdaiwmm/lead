"""A native SDK worker for the disposable Linux CDP fixture; no model calls."""
import asyncio
import json
import logging
import sys
import time

from lead_app.browser_execution import (
    ScopeViolation,
    guarded_tools,
    scoped_session_class,
)


async def main(data):
    logging.disable(logging.CRITICAL)
    lease = {'task_id': data['task_id'], 'generation': 1, 'target_id': data['target_id']}
    scope = {**lease, 'targets': {data['target_id']: data['target_id']}, 'origins': ['http://fixture.test']}
    async def read_scope():
        return dict(scope)
    async def authorize(name, values, node):
        return {'allowed': name == 'input' and node['attributes'].get('id') == 'entry'}
    session = scoped_session_class()(cdp_url=data['url'], headers=data['headers'], keep_alive=True,
                                    enable_default_extensions=False, allowed_domains=['fixture.test'])
    await session.start()
    started = time.monotonic()
    try:
        await session.bind_scope(lease, read_scope)
        tools = guarded_tools(session, authorize)
        state = await session.get_browser_state_summary(include_screenshot=False)
        assert len(state.tabs) == 1 and state.tabs[0].target_id == data['target_id']
        index = next(i for i, node in state.dom_state.selector_map.items() if node.attributes.get('id') == 'entry')
        result = await tools.registry.execute_action('guarded_input', {'index': index, 'text': data['task_id']})
        assert not result.error, result.error
        cdp=await session.get_or_create_cdp_session()
        await cdp.cdp_client.send.Page.reload(params={'ignoreCache':True},session_id=cdp.session_id)
        await asyncio.sleep(.2)
        try:
            await tools.registry.execute_action('guarded_input',{'index':index,'text':'stale-document-must-not-write'})
        except RuntimeError as error:
            assert isinstance(error.__cause__,ScopeViolation) and 'document_changed_reobserve' in str(error)
        else:
            raise AssertionError('same-URL stale document input accepted')
        state=await session.get_browser_state_summary(include_screenshot=False)
        index=next(i for i,node in state.dom_state.selector_map.items() if node.attributes.get('id')=='entry')
        result=await tools.registry.execute_action('guarded_input',{'index':index,'text':data['task_id']})
        assert not result.error,result.error
        try:
            await session.get_or_create_cdp_session(data['foreign_target'])
        except ScopeViolation:
            pass
        else:
            raise AssertionError('foreign target accepted')
        scope['origins'] = []
        try:
            await session.get_browser_state_summary(include_screenshot=True)
        except ScopeViolation:
            pass
        else:
            raise AssertionError('revoked observation accepted')
        return {'task_id': data['task_id'], 'started': started, 'ended': time.monotonic(),
                'fixed_page': True, 'foreign_page_denied': True, 'revoked_observation_denied': True,
                'native_input': True,'stale_document_denied':True}
    finally:
        await session.stop()


if __name__ == '__main__':
    print(json.dumps(asyncio.run(main(json.load(sys.stdin)))))

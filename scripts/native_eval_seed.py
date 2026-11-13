"""Zero-model fault seed using an already server-verified browser method.

This acceptance-only worker calls the same scoped native SDK tools as an Agent.
It receives no fixture selectors: the original ActionRef supplies its learned
method and immutable body. It never reports a receipt or edits SQL state.
"""

import asyncio
import json
import logging
import sys
from uuid import uuid4

import httpx


async def main(data):
    from lead_app.browser_execution import guarded_tools, scoped_session_class

    logging.disable(logging.CRITICAL)
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        async def manager(operation, args=None):
            response = await client.post(
                data['gateway_endpoint'] + f"/agents/{data['agent_id']}/native/task-command",
                headers={'Authorization': 'Bearer ' + data['gateway_token']},
                json={'run_id': data['task_id'], 'command_id': str(uuid4()),
                      'operation': operation, 'args': args or {}},
            )
            response.raise_for_status()
            return response.json()

        async def scope():
            return await manager('scope')

        async def authorize(name, values, node):
            return await manager('tool_authorize', {'name': name, 'values': values, 'node': node})

        lease = data['scope']
        browser = scoped_session_class()(cdp_url=lease['cdp_url'], headers=lease.get('headers'),
            keep_alive=True, enable_default_extensions=False)
        await browser.start_scoped(lease, scope)
        try:
            tools = guarded_tools(browser, authorize)
            last_state=None

            async def index_for(selector,*,optional=False):
                nonlocal last_state
                state = await browser.get_browser_state_summary(include_screenshot=False, cached=False)
                last_state=state
                found = []
                for index, node in state.dom_state.selector_map.items():
                    session = await browser.cdp_client_for_node(node)
                    result = await session.cdp_client.send.DOM.resolveNode(
                        params={'backendNodeId': node.backend_node_id}, session_id=session.session_id)
                    identifier = result['object'].get('objectId')
                    if not identifier:
                        continue
                    try:
                        matches = await session.cdp_client.send.Runtime.callFunctionOn(
                            params={'objectId': identifier,
                                    'functionDeclaration': 'function(selector){return this.matches(selector)}',
                                    'arguments': [{'value': selector}], 'returnByValue': True},
                            session_id=session.session_id)
                        if matches.get('result', {}).get('value') is True:
                            found.append(index)
                    finally:
                        await session.cdp_client.send.Runtime.releaseObject(
                            params={'objectId': identifier}, session_id=session.session_id)
                if not found and optional:
                    return None
                if len(found) != 1:
                    raise ValueError('seed_current_method_element_ambiguous')
                return found[0]

            field = await index_for(data['method']['selectors']['editor_body'],optional=True)
            if field is None:
                # Acceptance-only preparation: a learned permission target may
                # be separate from the editor. Follow only real observed safe
                # links until the original validated body selector is present.
                from urllib.parse import urljoin, urlsplit

                from lead_app.browser_execution import origin
                from lead_app.native_session_facts import reject_dangerous_endpoint

                pending=[]
                visited={last_state.url}
                while field is None and len(visited)<8:
                    for node in last_state.dom_state.selector_map.values():
                        href=(node.attributes or {}).get('href') if node.tag_name.lower()=='a' and node.is_visible else None
                        if not href:
                            continue
                        url=urljoin(last_state.url,href)
                        try:
                            if (url in visited or url in pending or url==data['method']['self_url']
                                    or origin(url)!=origin(data['method']['target_url'])
                                    or urlsplit(url).query or urlsplit(url).fragment):
                                continue
                            reject_dangerous_endpoint(url)
                        except ValueError:
                            continue
                        pending.append(url)
                    if not pending:
                        break
                    url=pending.pop(0)
                    result=await tools.registry.execute_action('guarded_navigate',{'url':url},browser_session=browser)
                    if result.error:
                        raise ValueError('seed_observed_editor_navigation_refused')
                    visited.add(url)
                    field=await index_for(data['method']['selectors']['editor_body'],optional=True)
                if field is None:
                    raise ValueError('seed_observed_editor_not_found')
            result = await tools.registry.execute_action('guarded_input',
                {'index': field, 'text': data['body'], 'clear': True}, browser_session=browser)
            if result.error:
                raise ValueError('seed_native_input_refused')
            button = await index_for(data['method']['selectors']['submit'])
            # A deliberately lost acknowledgement can make the native click
            # report an error. Only the parent checks the original SQL claim
            # and independent source oracle; this worker cannot declare unknown.
            result = await tools.registry.execute_action('guarded_click', {'index': button}, browser_session=browser)
            return {'model_requests': 0, 'native_input': True, 'native_click': True,
                    'click_error': bool(result.error)}
        finally:
            await browser.stop()


if __name__ == '__main__':
    result = asyncio.run(main(json.load(sys.stdin)))
    sys.stdout.write(json.dumps(result))

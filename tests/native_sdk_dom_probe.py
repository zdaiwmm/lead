"""Actual pinned SDK DOM/prompt + one loopback HTTP request; no Chrome or DB."""
import argparse
import ast
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


async def probe(runner):
    from browser_use import BrowserSession
    from browser_use.agent.prompts import AgentMessagePrompt
    from browser_use.browser.views import BrowserStateSummary, TabInfo
    from browser_use.dom.views import (
        DEFAULT_INCLUDE_ATTRIBUTES,
        DOMRect,
        EnhancedAXNode,
        EnhancedAXProperty,
        EnhancedDOMTreeNode,
        EnhancedSnapshotNode,
        NodeType,
        SerializedDOMState,
        SimplifiedNode,
    )
    from browser_use.llm import ChatOpenAI
    from lead_app.browser_execution import ScopeViolation, scoped_session_class

    logging.disable(logging.CRITICAL)
    agent_call = next(item for item in ast.walk(ast.parse(runner.read_text()))
                      if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == 'Agent')
    option = next((item.value for item in agent_call.keywords if item.arg == 'include_attributes'), None)
    if isinstance(option, ast.Name):
        assignment = next(item for item in ast.walk(ast.parse(runner.read_text()))
            if isinstance(item, ast.Assign) and any(isinstance(target, ast.Name) and target.id == option.id
            for target in item.targets) and not isinstance(item.value, ast.Constant))
        attributes = eval(compile(ast.Expression(assignment.value), str(runner), 'eval'),
                          {'DEFAULT_INCLUDE_ATTRIBUTES': DEFAULT_INCLUDE_ATTRIBUTES})
    else:
        attributes = ast.literal_eval(option.body if isinstance(option, ast.IfExp) else option) if option else None
    counter = 0
    def node(tag, attributes=None, *, target='A', visible=True, text='', children=(), **flags):
        nonlocal counter
        counter += 1
        snapshot = EnhancedSnapshotNode(False, None, DOMRect(0, 0, 50, 20), None, None, None, 1, 1)
        original = EnhancedDOMTreeNode(counter, counter, NodeType.TEXT_NODE if tag == '#text' else NodeType.ELEMENT_NODE,
            tag.upper(), text, attributes or {}, False, visible, None, target, 'frame-A', 'session-A',
            None, None, [], None, [], None, snapshot)
        return SimplifiedNode(original, list(children), **flags)

    author = node('span', {'id': 'author-observed'})
    body = node('p', {'id': 'post-body-observed'})
    self_link = node('a', {'id': 'self-observed', 'rel': 'me', 'href': '/account?private_token=URL_QUERY_SECRET#fragment'})
    canonical = node('a', {'rel': 'canonical', 'href': 'https://scope-a.fixture/posts/7?private_token=SECOND_QUERY_SECRET'})
    input_node = node('input', {'id': 'editor-observed', 'name': 'body', 'value': 'INPUT_VALUE_SECRET'},
                      is_interactive=True, selector_index=1)
    input_node.original_node.ax_node = EnhancedAXNode('ax', False, 'textbox', 'Editor', None,
        [EnhancedAXProperty('value', 'AX_VALUE_SECRET')], [])
    password = node('input', {'id': 'password-field', 'type': 'password', 'value': 'PASSWORD_SECRET'},
                    is_interactive=True, selector_index=2)
    bad = [node('span', {'id': 'FOREIGN_NODE_SECRET'}, target='B', is_interactive=True, selector_index=3),
        node('span', {'id': 'HIDDEN_NODE_SECRET'}, visible=False),
        node('span', {'id': 'EXCLUDED_NODE_SECRET'}, excluded_by_parent=True),
        node('span', {'id': 'PAINT_NODE_SECRET'}, ignored_by_paint_order=True),
        node('span', {'id': 'collapsed-visible-observed'}, should_display=False),
        node('input', {'type': 'hidden', 'name': 'HIDDEN_INPUT_SECRET', 'value': 'HIDDEN_VALUE_SECRET'}),
        node('script', {'id': 'SCRIPT_NODE_SECRET'}),
        node('a', {'rel': 'me', 'href': 'https://user:PASSWORD_URL_SECRET@scope-a.fixture/account'}),
        node('span', {'nonce': 'NONCE_SECRET'})]
    foreign_text = node('#text', target='B', text='FOREIGN_TEXT_SECRET')
    flood = [node('span', {'id': 'bounded-' + str(index)}) for index in range(100)]
    tree = node('body', {'id': 'board-observed'}, children=[author, body, self_link, canonical, input_node, password,
                                                        *bad, foreign_text, *flood])
    native = SerializedDOMState(tree, {1: input_node.original_node, 2: password.original_node})
    raw = native.llm_representation(include_attributes=['id', 'name', 'rel', 'role'])
    assert 'author-observed' not in raw and 'post-body-observed' not in raw
    page_url = 'https://scope-a.fixture/posts/7?session=CURRENT_QUERY_SECRET#private-fragment'
    state = BrowserStateSummary(native, page_url, 'Synthetic A', [
        TabInfo(url=page_url, title='Synthetic A', target_id='A'),
        TabInfo(url='https://foreign-b.fixture/FOREIGN_TAB_SECRET', title='Foreign B', target_id='B')])
    browser = scoped_session_class()(keep_alive=True, permissions=[], accept_downloads=False, auto_download_pdfs=False)
    scope = {'target_id': 'A', 'targets': {'A': 'A'}, 'origins': ['https://scope-a.fixture']}
    browser._lease = dict(scope)
    browser._scope = dict(scope)
    calls = 0
    revoked = False
    async def live():
        nonlocal calls
        calls += 1
        if revoked and calls > 1:
            raise ScopeViolation('page_ownership_revoked')
        return dict(scope), [('A', state.url, [('frame-A', 'doc-A', state.url)])]
    object.__setattr__(browser, 'live_scope', live)
    original = BrowserSession.get_browser_state_summary
    async def collected(*args, **kwargs):
        assert kwargs['cached'] is False
        return state
    BrowserSession.get_browser_state_summary = collected
    try:
        observed = await browser.get_browser_state_summary(include_screenshot=False)
        assert observed.dom_state.selector_map is native.selector_map
        assert input_node.original_node.attributes['value'] == 'INPUT_VALUE_SECRET'
        message = AgentMessagePrompt(observed, None, task='Read the currently visible page.',
            include_attributes=attributes).get_user_message(use_vision=False)
        captured = []
        class FakeHTTP(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                payload = {'id': 'synthetic-only', 'object': 'chat.completion', 'created': 1,
                    'model': 'synthetic-dom', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'Observed.'},
                    'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 1, 'total_tokens': 101}}
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeHTTP)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            llm = ChatOpenAI(model='synthetic-dom', api_key='synthetic-not-a-real-key',
                base_url=f'http://127.0.0.1:{server.server_port}/v1', max_retries=0)
            await llm.ainvoke([message])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        assert len(captured) == 1
        actual = json.dumps(captured[0]['messages'], ensure_ascii=False)
        representation = observed.dom_state.llm_representation(include_attributes=attributes)
        structure = representation.split('Native page observation:', 1)[0]
        calls = 0
        revoked = True
        try:
            await browser.get_browser_state_summary(include_screenshot=False)
        except ScopeViolation:
            denied = True
        else:
            denied = False
        forbidden = ['FOREIGN_NODE_SECRET', 'HIDDEN_NODE_SECRET', 'EXCLUDED_NODE_SECRET', 'PAINT_NODE_SECRET',
            'HIDDEN_INPUT_SECRET', 'HIDDEN_VALUE_SECRET', 'SCRIPT_NODE_SECRET',
            'INPUT_VALUE_SECRET', 'AX_VALUE_SECRET', 'PASSWORD_SECRET', 'PASSWORD_URL_SECRET', 'NONCE_SECRET',
            'URL_QUERY_SECRET', 'SECOND_QUERY_SECRET', 'FOREIGN_TEXT_SECRET', 'CURRENT_QUERY_SECRET',
            'FOREIGN_TAB_SECRET', 'private-fragment']
        result = {'sdk_version': importlib.metadata.version('browser-use'), 'python_version': list(sys.version_info[:2]),
            'actual_http_requests': len(captured), 'native_interactive_id_visible': 'editor-observed' in actual,
            'author_id_visible': 'author-observed' in actual, 'body_id_visible': 'post-body-observed' in actual,
            'collapsed_visible_id_visible': 'collapsed-visible-observed' in actual,
            'self_rel_visible': 'rel=\\"me\\"' in actual or 'rel=me' in actual,
            'canonical_rel_visible': 'rel=\\"canonical\\"' in actual or 'rel=canonical' in actual,
            'canonical_safe_path_visible': '/posts/7' in actual,
            'forbidden_visible': [value for value in forbidden if value in actual],
            'fresh_scope_revocation_denied': denied,
            'native_map_unchanged': observed.dom_state.selector_map is native.selector_map,
            'native_value_unchanged': input_node.original_node.attributes['value'] == 'INPUT_VALUE_SECRET',
            'native_ax_value_unchanged': input_node.original_node.ax_node.properties[0].value == 'AX_VALUE_SECRET',
            'native_url_unchanged': state.url == page_url,
            'runner_include_attributes': attributes,
            'structure_rows': structure.count(' />'), 'structure_chars': len(structure),
            'representation_chars': len(representation)}
        return result
    finally:
        BrowserSession.get_browser_state_summary = original
        await browser.event_bus.stop(clear=True, timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-site', type=Path, required=True)
    parser.add_argument('--api', type=Path, required=True)
    parser.add_argument('--runner', type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.sdk_site.resolve(strict=True)), str(args.api.resolve(strict=True))]
    result = asyncio.run(probe(args.runner))
    result['scope_source_sha256'] = hashlib.sha256((args.api / 'lead_app/browser_execution.py').read_bytes()).hexdigest()
    print(json.dumps(result, sort_keys=True))
    assert result['author_id_visible'] and result['body_id_visible'], result
    assert result['collapsed_visible_id_visible'], result
    assert result['self_rel_visible'] and result['canonical_rel_visible'], result
    assert not result['forbidden_visible'], result
    assert result['fresh_scope_revocation_denied'], result
    assert result['structure_rows'] <= 32 and result['structure_chars'] <= 3200, result
    assert {'id', 'name', 'rel', 'role'} <= set(result['runner_include_attributes']), result
    assert not {'value', 'valuetext'} & set(result['runner_include_attributes']), result


if __name__ == '__main__':
    main()

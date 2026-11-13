"""Pinned SDK event-path probe with synthetic CDP; no browser, DB or model.

Run with the packaged SDK interpreter and -IB. Explicit paths are required so
the probe cannot accidentally import an unrelated local SDK or application.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace


class Endpoint:
    def __init__(self, root, kind, path=''):
        self.root, self.kind, self.path = root, kind, path

    def __getattr__(self, name):
        return Endpoint(self.root, self.kind, self.path + ('.' if self.path else '') + name)

    def __call__(self, *args, **kwargs):
        if self.kind == 'register':
            self.root.callbacks[self.path] = args[0]
            return None

        async def command():
            self.root.commands.append((self.path, kwargs))
            if self.path == 'Target.getTargetInfo':
                return {'targetInfo': {'targetId': 'ROOT_A', 'type': 'page',
                                      'url': 'https://scope-a.fixture/', 'attached': True}}
            if self.path == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'FRAME_A', 'loaderId': 'DOC_A',
                                                'url': 'https://scope-a.fixture/'}}}
            return {}
        return command()


class SyntheticCDP:
    def __init__(self):
        from websockets.protocol import State

        self.commands, self.callbacks = [], {}
        self.send, self.register = Endpoint(self, 'send'), Endpoint(self, 'register')
        self.ws = SimpleNamespace(state=State.OPEN)


async def probe():
    from browser_use import BrowserSession
    from browser_use.browser.events import (
        BrowserConnectedEvent,
        FileDownloadedEvent,
        TabCreatedEvent,
    )
    from browser_use.browser.session import CDPSession, Target
    from browser_use.browser.session_manager import SessionManager
    from lead_app.browser_execution import scoped_session_class

    logging.disable(logging.CRITICAL)
    browser = scoped_session_class()(
        cdp_url='ws://127.0.0.1:1/devtools/browser/synthetic-no-listener',
        keep_alive=True, enable_default_extensions=False,
        permissions=[], accept_downloads=False, auto_download_pdfs=False,
    )
    scope = {'task_id': 'synthetic-task-A', 'generation': 'synthetic-lease-A',
             'target_id': 'ROOT_A', 'origins': ['https://scope-a.fixture/'],
             'targets': {'ROOT_A': 'ROOT_A'}}
    browser._startup_scope = dict(scope)
    browser._scope = dict(scope)
    browser._lease = dict(scope)

    async def read_scope():
        return dict(scope)

    browser._scope_reader = read_scope
    root = SyntheticCDP()
    browser._cdp_client_root = root
    browser.session_manager = SessionManager(browser)
    manager = browser.session_manager
    manager._targets['ROOT_A'] = Target(target_id='ROOT_A', target_type='page',
                                      url='https://scope-a.fixture/', title='Owned page A')
    manager._target_sessions['ROOT_A'] = {'SESSION_A'}
    manager._session_to_target['SESSION_A'] = 'ROOT_A'
    manager._sessions['SESSION_A'] = CDPSession.model_construct(
        cdp_client=root, target_id='ROOT_A', session_id='SESSION_A')
    browser.agent_focus_target_id = 'ROOT_A'

    try:
        # Execute the actual parent SDK method: it assigns _downloads_watchdog
        # on this real ScopedSession and invokes that object's registration.
        await BrowserSession.attach_all_watchdogs(browser)
        await browser.event_bus.dispatch(BrowserConnectedEvent(cdp_url=browser.cdp_url))
        await browser.event_bus.dispatch(TabCreatedEvent(target_id='ROOT_A', url='https://scope-a.fixture/'))

        download_callbacks = sorted(name for name in root.callbacks if name.startswith('Browser.download'))
        will = root.callbacks.get('Browser.downloadWillBegin')
        progress = root.callbacks.get('Browser.downloadProgress')
        if will:
            will({'frameId': 'FOREIGN_FRAME_B', 'guid': 'synthetic-foreign-B',
                  'url': 'https://foreign-b.fixture/private-report.pdf',
                  'suggestedFilename': 'B-private-report.pdf'}, None)
        if progress:
            progress({'guid': 'synthetic-foreign-B', 'state': 'completed',
                      'receivedBytes': 7, 'totalBytes': 7}, None)
        await asyncio.sleep(0)
        files = [event for event in list(browser.event_bus.event_history.values())
                 if isinstance(event, FileDownloadedEvent)]
        for event in files:
            await event
        pending = getattr(browser._downloads_watchdog, '_cdp_event_tasks', set())
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        result = {
            'sdk_version': importlib.metadata.version('browser-use'),
            'python_version': list(sys.version_info[:2]),
            'configured_permissions': browser.browser_profile.permissions,
            'configured_accept_downloads': browser.browser_profile.accept_downloads,
            'configured_auto_download_pdfs': browser.browser_profile.auto_download_pdfs,
            'browser_grants': [kwargs for name, kwargs in root.commands if name == 'Browser.grantPermissions'],
            'global_download_behavior': [kwargs for name, kwargs in root.commands if name == 'Browser.setDownloadBehavior'],
            'global_download_callbacks': download_callbacks,
            'foreign_file_event': any(event.url.startswith('https://foreign-b.fixture/') for event in files),
            'foreign_file_in_A': any('B-private-report.pdf' in value for value in browser.downloaded_files),
            'foreign_target_registered': manager.get_target('FOREIGN_FRAME_B') is not None,
            'sdk_parent_registration_executed': True,
            'actual_tab_created_event_executed': True,
            'owned_page_event_initialized': any(name == 'Page.enable' and kwargs.get('session_id') == 'SESSION_A'
                                                 for name, kwargs in root.commands),
        }
        return result
    finally:
        # No BrowserStop/kill is sent: this probe owns no real browser process.
        await browser.event_bus.stop(clear=True, timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-site', type=Path, required=True)
    parser.add_argument('--api', type=Path, required=True)
    args = parser.parse_args()
    sdk_site, api = args.sdk_site.resolve(strict=True), args.api.resolve(strict=True)
    sys.path[:0] = [str(sdk_site), str(api)]
    result = asyncio.run(probe())
    result['scope_source_sha256'] = hashlib.sha256((api / 'lead_app/browser_execution.py').read_bytes()).hexdigest()
    print(json.dumps(result, sort_keys=True))
    forbidden = (result['browser_grants'] or result['global_download_behavior'] or result['global_download_callbacks']
                 or result['foreign_file_event'] or result['foreign_file_in_A'])
    if forbidden:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

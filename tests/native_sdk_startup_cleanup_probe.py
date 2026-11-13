"""Actual pinned ScopedSession startup error/cleanup contract; no browser/network."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import logging
import sys
import time
from pathlib import Path


async def probe():
    from lead_app.browser_execution import ScopeViolation, scoped_session_class

    logging.disable(logging.CRITICAL)
    rows=[]
    for condition in ('success','refresh_error','start_error','bind_error','stop_error','stop_timeout','cancelled'):
        browser=scoped_session_class()(cdp_url='ws://127.0.0.1:1/no-listener',keep_alive=True,
            enable_default_extensions=False,permissions=[])
        scope={'task_id':'original-task','generation':1,'target_id':'original-root',
            'targets':{'original-root':'original-root'},'origins':['https://original.fixture']}
        error=asyncio.CancelledError('original-cancel') if condition=='cancelled' else ScopeViolation('original-startup-failure')
        counts={'start':0,'bind':0,'stop':0}
        async def read_scope():
            return {**scope,'target_id':'different-root'} if condition=='refresh_error' else dict(scope)
        async def start():
            counts['start']+=1
            if condition in ('start_error','cancelled'):
                raise error
        async def bind(*args):
            counts['bind']+=1
            if condition in ('bind_error','stop_error','stop_timeout'):
                raise error
        async def stop():
            counts['stop']+=1
            assert browser._intentional_stop is True
            if condition=='stop_error':
                raise RuntimeError('cleanup-error-must-not-replace-original')
            if condition=='stop_timeout':
                await asyncio.Event().wait()
        object.__setattr__(browser,'start',start)
        object.__setattr__(browser,'bind_scope',bind)
        object.__setattr__(browser,'stop',stop)
        before=time.monotonic()
        observed=None
        try:
            await browser.start_scoped(scope,read_scope)
        except BaseException as caught:
            observed=caught
        finally:
            await browser.event_bus.stop(clear=True,timeout=1)
        elapsed=time.monotonic()-before
        preserved=(observed is error if condition not in ('success','refresh_error') else
            observed is None if condition=='success' else isinstance(observed,ScopeViolation) and str(observed)=='page_lease_changed')
        rows.append({'condition':condition,'counts':counts,'original_error_preserved':preserved,
            'elapsed':elapsed,'bounded':elapsed<2.8,
            'intentional_stop':browser._intentional_stop is True})
    return {'sdk_version':importlib.metadata.version('browser-use'),'python_version':list(sys.version_info[:2]),
        'cases':rows,'no_network_or_browser':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sdk-site',type=Path,required=True)
    parser.add_argument('--api',type=Path,required=True)
    args=parser.parse_args()
    sdk,api=args.sdk_site.resolve(strict=True),args.api.resolve(strict=True)
    sys.path[:0]=[str(sdk),str(api)]
    result=asyncio.run(probe())
    result['scope_source_sha256']=hashlib.sha256((api/'lead_app/browser_execution.py').read_bytes()).hexdigest()
    print(json.dumps(result))
    if not all(row['original_error_preserved'] and row['bounded'] and
        row['counts']['stop']==(0 if row['condition']=='success' else 1) and
        row['intentional_stop']==(row['condition']!='success') for row in result['cases']):
        raise SystemExit(1)


if __name__=='__main__':
    main()

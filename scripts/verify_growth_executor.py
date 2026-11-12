"""Explicit private assembly check; no account credentials or platform requests."""
import argparse
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from verify_growth_runtime import ROOT, environment, read_manifest, validate_state


async def verify(state,source):
    from lead_app.channel_executor import (
        Binding,
        load_entrypoint,
        source_hashes,
        verify_imports,
    )
    from lead_app.growth import executor
    if source.is_symlink() or not source.is_absolute() or not source.is_dir():
        raise ValueError('An explicit private adapter inventory is required')
    stage = state/'private-assembly'
    if stage.exists():
        raise ValueError('Use a fresh private assembly stage')
    stage.mkdir(mode=0o700)
    retained = ('binding.py','telegram.py','community_receipts.py','community_runtime.py','community_schedule.py')
    for name in retained:
        path=source/name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Private primitive missing')
        shutil.copyfile(path,stage/name)
    shutil.copyfile(ROOT/'services/channel_adapters/telegram/entry.py',stage/'entry.py')
    manifest=source_hashes(stage)
    binding = Binding(version=1,channel='telegram',executor_id='synthetic-assembly',agent_id=str(uuid4()),
        profile_slot='default',release=str(ROOT),adapter_root=str(stage),entrypoint='entry:run',adapter_files=manifest,
        receipt=str(state/'reports/private-assembly-boot.json'))
    config=state/'private-assembly-binding.json'
    config.write_text(json.dumps(binding.as_dict()))
    os.chmod(config,0o600)
    os.environ['LEAD_EXECUTOR_BINDING']=str(config)
    entry=load_entrypoint(binding)
    called=[]
    original=executor.serve_browser
    async def bounded_gateway(actual,**kwargs):
        assert actual==binding and kwargs['port']==18092
        assert callable(kwargs['native_receipt_reader'])
        assert kwargs['telegram_primitives'].__name__=='lead_channel_adapter.telegram'
        called.append(True)
    executor.serve_browser=bounded_gateway
    try:
        await asyncio.wait_for(entry(binding),2)
        # Import the exact retained original-receipt dependencies as well. No
        # resident loop, selector, model overlay or old entrypoint is packaged.
        receipts=importlib.import_module('lead_channel_adapter.community_receipts')
        runtime=importlib.import_module('lead_channel_adapter.community_runtime')
        importlib.import_module('lead_channel_adapter.community_schedule')
        assert callable(receipts.reconcile) and callable(runtime.search_history)
        assert not any(name in sys.modules for name in ('lead_channel_adapter.resident','lead_channel_adapter.community_selection','lead_channel_adapter.model'))
        assert len(called)==1
        verify_imports(binding,sys.modules)
        record={'ok':True,'scope':'actual-private-source assembly, no authenticated platform execution',
            'retained_files':manifest,'public_entry_sha256':hashlib.sha256((stage/'entry.py').read_bytes()).hexdigest(),
            'external_social_actions':0,'old_resident_loaded':False,'core_module_overlay':False}
        (state/'reports/private-executor-assembly.json').write_text(json.dumps(record,indent=2))
        print(json.dumps({'ok':True,'retained_private_primitives':len(retained),'external_social_actions':0}))
    finally:
        executor.serve_browser=original


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--adapter-source',type=Path,required=True)
    args=parser.parse_args()
    state=validate_state(args.state_dir)
    read_manifest(state)
    safe=environment(state)
    for name in list(os.environ):
        if name.startswith(('LEAD_','OPENAI_','DEEPSEEK_','ANTHROPIC_')):
            del os.environ[name]
    os.environ.update(safe)
    sys.path.insert(0,str(ROOT/'services/api'))
    asyncio.run(verify(state,args.adapter_source))

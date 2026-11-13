"""Run startup cleanup through the exact isolated SDK; no browser, DB or model."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated native SDK interpreter required')


def test_scoped_startup_failure_cleans_sdk_without_replacing_original_error():
    root=Path(__file__).resolve().parents[1]
    runtime=Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR') or root/'services/browser-runtime')
    python=os.environ['LEAD_BROWSER_RUNTIME_PYTHON']
    env={key:value for key,value in os.environ.items()
        if not key.startswith(('LEAD_','OPENAI_','DEEPSEEK_','ANTHROPIC_','BROWSER_USE_API_KEY'))}
    env.update(ANONYMIZED_TELEMETRY='false',BROWSER_USE_CLOUD_SYNC='false',BROWSER_USE_VERSION_CHECK='false')
    result=subprocess.run([python,'-IB',str(root/'tests/native_sdk_startup_cleanup_probe.py'),
        '--sdk-site',str(runtime/'site-packages'),'--api',str(root/'services/api')],cwd=root,env=env,
        capture_output=True,text=True,timeout=12)
    lines=[line for line in result.stdout.splitlines() if line.startswith('{"')]
    assert lines, {'returncode':result.returncode,'stderr':result.stderr[-2000:]}
    observed=json.loads(lines[-1])
    assert observed['sdk_version']=='0.13.14' and observed['python_version']==[3,12]
    assert observed['scope_source_sha256']==hashlib.sha256((root/'services/api/lead_app/browser_execution.py').read_bytes()).hexdigest()
    assert all(row['counts']['stop']==(0 if row['condition']=='success' else 1)
        and row['original_error_preserved'] and row['bounded']
        and row['intentional_stop']==(row['condition']!='success') for row in observed['cases']),observed
    assert result.returncode==0,observed

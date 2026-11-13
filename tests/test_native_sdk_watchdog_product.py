"""Use the exact packaged SDK process without a Chrome, DB or model call."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated native product run required',
)


def test_scoped_sdk_does_not_enable_or_observe_unrequested_browser_capabilities():
    root = Path(__file__).resolve().parents[1]
    runtime = Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR') or root / 'services/browser-runtime')
    python = os.environ.get('LEAD_BROWSER_RUNTIME_PYTHON')
    assert python and Path(python).is_file(), 'The pinned SDK interpreter is required'
    site = runtime / 'site-packages'
    assert site.is_dir(), 'The pinned SDK site-packages is required'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('LEAD_', 'OPENAI_', 'DEEPSEEK_', 'ANTHROPIC_', 'BROWSER_USE_API_KEY'))}
    env.update(ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false', BROWSER_USE_VERSION_CHECK='false')
    result = subprocess.run([python, '-IB', str(root / 'tests/native_sdk_watchdog_probe.py'),
        '--sdk-site', str(site), '--api', str(root / 'services/api')],
        cwd=root, env=env, capture_output=True, text=True, timeout=30)
    # SDK logging can precede the standalone final JSON record.
    lines = [line for line in result.stdout.splitlines() if line.startswith('{"')]
    assert lines, {'returncode': result.returncode, 'stderr': result.stderr[-2000:]}
    observed = json.loads(lines[-1])
    assert observed['scope_source_sha256'] == hashlib.sha256((root / 'services/api/lead_app/browser_execution.py').read_bytes()).hexdigest()
    assert observed['sdk_version'] == '0.13.14'
    assert observed['python_version'] == [3, 12]
    assert observed['sdk_parent_registration_executed'] and observed['actual_tab_created_event_executed']
    assert observed['owned_page_event_initialized'], observed
    assert observed['configured_permissions'] == []
    assert observed['configured_accept_downloads'] is False and observed['configured_auto_download_pdfs'] is False
    assert not observed['browser_grants'], observed
    assert not observed['global_download_behavior'], observed
    assert not observed['global_download_callbacks'], observed
    assert not observed['foreign_target_registered'], observed
    assert not observed['foreign_file_event'] and not observed['foreign_file_in_A'], observed
    assert result.returncode == 0, observed

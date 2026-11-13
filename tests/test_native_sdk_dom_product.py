"""Pinned SDK model-visible observation contract; uses only synthetic loopback HTTP."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated native SDK run required',
)


def test_current_visible_selector_facts_reach_actual_sdk_http_without_secrets():
    root = Path(__file__).resolve().parents[1]
    runtime = Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR') or root / 'services/browser-runtime')
    python = os.environ['LEAD_BROWSER_RUNTIME_PYTHON']
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('LEAD_', 'OPENAI_', 'DEEPSEEK_', 'ANTHROPIC_', 'BROWSER_USE_API_KEY'))}
    env.update(ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false', BROWSER_USE_VERSION_CHECK='false')
    result = subprocess.run([python, '-IB', str(root / 'tests/native_sdk_dom_probe.py'),
        '--sdk-site', str(runtime / 'site-packages'), '--api', str(root / 'services/api'),
        '--runner', str(root / 'scripts/browser_use_runner.py')],
        cwd=root, env=env, capture_output=True, text=True, timeout=30)
    records = [line for line in result.stdout.splitlines() if line.startswith('{"')]
    assert records, {'exit': result.returncode, 'stderr': result.stderr[-2000:]}
    observed = json.loads(records[-1])
    assert observed['scope_source_sha256'] == hashlib.sha256((root / 'services/api/lead_app/browser_execution.py').read_bytes()).hexdigest()
    assert observed['sdk_version'] == '0.13.14' and observed['python_version'] == [3, 12]
    assert observed['actual_http_requests'] == 1
    assert observed['author_id_visible'] and observed['body_id_visible'], observed
    assert observed['collapsed_visible_id_visible'], observed
    assert observed['self_rel_visible'] and observed['canonical_rel_visible'], observed
    assert not observed['forbidden_visible'], observed
    assert observed['native_map_unchanged'] and observed['native_value_unchanged'] and observed['native_ax_value_unchanged'], observed
    assert observed['native_url_unchanged'], observed
    assert observed['fresh_scope_revocation_denied'], observed
    assert observed['structure_rows'] <= 32 and observed['structure_chars'] <= 3200, observed
    assert result.returncode == 0, observed

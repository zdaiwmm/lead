"""Run the pinned SDK against an owned Chrome and synthetic HTTP transports only."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated native SDK/Chrome run required',
)


def test_current_screenshot_reaches_actual_sdk_user_image_parts(tmp_path):
    import playwright

    root = Path(__file__).resolve().parents[1]
    runtime = Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR') or root / 'services/browser-runtime')
    worker_site = Path(playwright.__file__).resolve().parents[1]
    clean = {key: value for key, value in os.environ.items()
        if not key.startswith(('LEAD_', 'OPENAI_', 'DEEPSEEK_', 'ANTHROPIC_', 'BROWSER_USE_API_KEY'))}
    clean.update(ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false',
        BROWSER_USE_VERSION_CHECK='false', BROWSER_USE_SETUP_LOGGING='false')
    result = subprocess.run([os.environ['LEAD_BROWSER_RUNTIME_PYTHON'], '-IB',
        str(root / 'tests/native_vision_sdk_probe.py'),
        '--sdk-site', str(runtime / 'site-packages'), '--worker-site', str(worker_site),
        '--api', str(root / 'services/api'), '--runner', str(root / 'scripts/browser_use_runner.py'),
        '--chrome', str(runtime / 'chromium/chrome-linux64/chrome'), '--output', str(tmp_path)],
        cwd=root, env=clean, capture_output=True, text=True, timeout=65)
    assert result.returncode == 0, {'exit': result.returncode, 'stderr': result.stderr[-2500:]}
    observed = json.loads((tmp_path / 'proof.json').read_text())
    for name, key in [('scripts/browser_use_runner.py', 'runner_sha256'),
            ('services/api/lead_app/browser_execution.py', 'scope_sha256')]:
        assert observed[key] == hashlib.sha256((root / name).read_bytes()).hexdigest()
    assert observed['sdk_version'] == '0.13.14' and observed['python_version'] == [3, 12]
    assert observed['actual_model_http_requests'] == observed['worker_usage']['vision_images_sent'] == 1
    assert observed['paid_requests'] == 0 and observed['request']['request_model'] == 'deepseek-flash'
    assert observed['vision_false_raw_capture_count'] == 0
    assert observed['request']['user_images'] == 1
    assert observed['request']['owned_page_pixels'] > 20000 and observed['request']['other_page_pixels'] == 0
    assert all(observed[key] for key in ('vision_false_cache_empty', 'other_task_denied',
        'revoked_scope_denied', 'foreign_frame_denied', 'original_pages_alive'))

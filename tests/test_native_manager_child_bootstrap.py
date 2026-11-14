"""Private manager startup logs expose only bounded, typed import diagnostics."""

import json
import os

import pytest
from native_manager_death_child import (
    manager_environment,
    startup_diagnostic,
    startup_summary,
)


def test_manager_environment_uses_api_venv_without_ambient_sdk_site(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', '/external-sdk/site-packages')
    monkeypatch.setenv('PYTHONHOME', '/external-interpreter')
    monkeypatch.setenv('LEAD_BROWSER_RUNTIME_DIR', '/explicit-runtime')
    monkeypatch.setenv('LEAD_BROWSER_RUNTIME_PYTHON', '/explicit-sdk-python')
    child = manager_environment()
    assert 'PYTHONPATH' not in child and 'PYTHONHOME' not in child
    assert child['LEAD_BROWSER_RUNTIME_DIR'] == '/explicit-runtime'
    assert child['LEAD_BROWSER_RUNTIME_PYTHON'] == '/explicit-sdk-python'
    assert os.environ['PYTHONPATH'] == '/external-sdk/site-packages'


def test_startup_diagnostic_omits_exception_message_and_credentials():
    secret = 'password=private-value cookie=private-cookie'
    assert startup_diagnostic(RuntimeError(secret)) == {'exception': 'RuntimeError'}
    assert startup_diagnostic(ModuleNotFoundError(secret, name='uvicorn')) == {
        'exception': 'ModuleNotFoundError', 'missing_module': 'uvicorn'}
    assert startup_diagnostic(ModuleNotFoundError(secret, name=secret)) == {
        'exception': 'ModuleNotFoundError'}


def test_startup_summary_reads_only_typed_fields_from_private_log(tmp_path):
    path = tmp_path / 'manager.log'
    path.write_text('private-cookie\n' + 'NATIVE_MANAGER_STARTUP_ERROR ' + json.dumps({
        'exception': 'ModuleNotFoundError', 'missing_module': 'uvicorn',
        'boot': 'private-claim', 'password': 'private-password'}) + '\nprivate-traceback\n')
    assert startup_summary(path) == {'exception': 'ModuleNotFoundError', 'missing_module': 'uvicorn'}


@pytest.mark.parametrize('tail', ['private boot data', 'NATIVE_MANAGER_STARTUP_ERROR []',
    'NATIVE_MANAGER_STARTUP_ERROR {"exception":"password=private"}'])
def test_startup_summary_omits_untyped_or_invalid_log_content(tmp_path, tail):
    path = tmp_path / 'manager.log'
    path.write_text('private-history' * 2048 + '\n' + tail)
    assert startup_summary(path) == {'diagnostic': 'unavailable'}

"""A valid model decision with no usage receipt cannot cause a second paid call."""
import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_missing_usage_stops_original_sdk_after_one_model_http_request(monkeypatch):
    import native_connection_product as fixture
    from lead_app import account_profiles

    original_server = fixture.ThreadingHTTPServer
    changed = []

    def missing_usage_server(address, handler):
        class WithoutUsage(handler):
            def do_POST(self):
                original = self.wfile
                class UsageLessWire:
                    def __getattr__(self, name):
                        return getattr(original, name)

                    def write(self, raw):
                        try:
                            payload = json.loads(raw)
                        except (ValueError, TypeError):
                            return original.write(raw)
                        if isinstance(payload, dict) and payload.get('object') == 'chat.completion':
                            payload.pop('usage', None)
                            encoded = json.dumps(payload).encode()
                            assert len(encoded) <= len(raw)
                            changed.append(True)
                            # Preserve the actual HTTP Content-Length. Only the
                            # external provider's usage receipt is absent.
                            raw = encoded.ljust(len(raw), b' ')
                        return original.write(raw)
                self.wfile = UsageLessWire()
                try:
                    return super().do_POST()
                finally:
                    self.wfile = original
        return original_server(address, WithoutUsage)

    monkeypatch.setattr(fixture, 'ThreadingHTTPServer', missing_usage_server)
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    result = await fixture.verify_login(monkeypatch)
    assert changed == [True]
    assert len(result['synthetic_model_diagnostics']) == 1
    assert result['usage']['request_count'] == 1 and result['usage']['entry_count'] == 0
    assert result['usage']['unmeasured_requests'] == 1
    assert result['billing'] == [{'state': 'unknown', 'units': 40000}]
    assert result['task_state'] == 'waiting_user'
    assert result['account'] == {'state': 'pending', 'external_account_id': None}
    assert not [row for row in result['oracle']['requests'] if row['method'] == 'POST']
    assert result['privacy'] == {'literal_in_model_request': False, 'literal_in_workdir': False}

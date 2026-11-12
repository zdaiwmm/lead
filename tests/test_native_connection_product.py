"""Required Linux native product chain when LEAD_NATIVE_PRODUCT_TESTS=1."""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LEAD_NATIVE_PRODUCT_TESTS") != "1",
    reason="Explicit isolated Linux native product run required",
)


@pytest.mark.parametrize("mode", ["login", "wrong_subject"])
@pytest.mark.asyncio
async def test_native_original_connection_with_real_input_and_private_self(
    monkeypatch, mode
):
    from lead_app import account_profiles
    from native_connection_product import verify_login

    assert sys.platform == "linux" and os.environ.get("LEAD_NETWORK_NAMESPACE") == "1"
    monkeypatch.setattr(account_profiles, "native_available", lambda: True)
    result = await verify_login(monkeypatch, mode=mode)
    assert result["checks"]["actual_profile"]
    assert result["privacy"] == {
        "literal_in_model_request": False,
        "literal_in_workdir": False,
    }
    assert all(
        not row["username_literal_seen"] and not row["password_literal_seen"]
        for row in result["synthetic_model_diagnostics"]
    )
    assert (
        len(
            [
                row
                for row in result["oracle"]["requests"]
                if row["method"] == "POST" and row["path"] == "/session"
            ]
        )
        == 1
    )
    assert any(row["path"] == "/account" for row in result["oracle"]["requests"])
    assert result["oracle"]["posts"] == []
    assert result["usage"]["request_count"] <= 10
    if mode == "login":
        assert result["identity_probe"] == {
            "cookie_empty": True,
            "account_redirected_to_login": True,
            "post_denied": True,
        }
        assert (
            result["checks"]["mechanical_roundtrip"]
            and result["result"]["outcome"] == "completed"
        )
        assert result["account"]["external_account_id"].startswith("member-")
        assert result["temporary_login_cleared"]
    else:
        assert (
            result["account"]["state"] == "pending"
            and result["account"]["external_account_id"] is None
        )
        assert (
            result["result"]["outcome"] == "waiting"
            and result["connection"]["control_mode"] == "owner"
        )

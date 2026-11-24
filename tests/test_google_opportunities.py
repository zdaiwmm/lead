from urllib.parse import urlencode

import pytest
from lead_app.google_opportunity import https_origin
from lead_app.google_opportunity_flow import BrowserAuthority, read_oauth_request

SITE = "https://directory.example"
CALLBACK = SITE + "/oauth/callback"
GOOGLE = "https://accounts.google.com"


def authorize_url(**overrides):
    values = {
        "client_id": "123-me.apps.googleusercontent.com",
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "scope": "openid email profile",
        "state": "flow-state",
    }
    return GOOGLE + "/o/oauth2/v2/auth?" + urlencode({**values, **overrides})


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "openid https://www.googleapis.com/auth/drive"},
        {"redirect_uri": "https://other.example/oauth/callback"},
        {"redirect_uri": CALLBACK + "?url=https://other.example"},
        {"include_granted_scopes": "true"},
        {"access_type": "offline"},
        {"response_type": "token"},
        {"state": ""},
        {"redirect_uri": SITE + "/pay/checkout"},
    ],
)
def test_google_scope_only_basic_and_original_callback(overrides):
    result, reason = read_oauth_request(authorize_url(**overrides), SITE)
    assert result is None
    assert reason


def test_callback_state_scope_and_code_match_the_original_authorization():
    flow, reason = read_oauth_request(authorize_url(), SITE)
    assert reason is None
    assert flow.accepts_return(
        CALLBACK + "?state=flow-state&code=one&scope=email+openid"
    )
    for query in (
        "?state=wrong&code=one",
        "?state=flow-state&code=",
        "?state=flow-state&code=one&code=two",
        "?state=flow-state&code=one&scope=email&scope=drive",
        "?state=flow-state&code=one&scope=drive",
        "?state=flow-state&error=access_denied",
        "?state=flow-state&code=one#x",
    ):
        assert not flow.accepts_return(CALLBACK + query)
    assert not flow.accepts_return(
        "https://other.example/oauth/callback?state=flow-state&code=one"
    )


def test_network_authority_does_not_open_google_or_callback_early():
    grant = BrowserAuthority(lambda: True, site=SITE)
    assert grant.host_allowed("directory.example", 443)
    assert not grant.host_allowed("accounts.google.com", 443)
    assert not grant.host_allowed("directory.example", 80)
    initial_tunnel = grant.control_token()
    grant.oauth = read_oauth_request(authorize_url(), SITE)[0]
    assert not grant.host_allowed("directory.example", 443)
    assert grant.control_token() != initial_tunnel
    assert grant.host_allowed("accounts.google.com", 443)
    assert not grant.host_allowed("drive.google.com", 443)
    assert not grant.host_allowed("accounts.google.com.evil.example", 443)
    grant.callback_allowed = True
    callback = CALLBACK + "?state=flow-state&code=one"
    assert grant.allow(callback, "GET", navigation=True, resource_type="document")
    assert not grant.allow(
        SITE + "/submit", "POST", navigation=False, resource_type="xhr"
    )
    assert not grant.allow(
        SITE + "/login", "GET", navigation=True, resource_type="document"
    )
    assert not grant.allow(
        CALLBACK + "?state=wrong&code=one",
        "GET",
        navigation=True,
        resource_type="document",
    )
    grant.returned = True
    assert grant.allow(
        SITE + "/rules", "GET", navigation=True, resource_type="document"
    )
    assert not grant.allow(
        SITE + "/checkout", "GET", navigation=True, resource_type="document"
    )


def test_google_post_requires_live_grant_and_consumes_one_click():
    live = True
    grant = BrowserAuthority(lambda: live, site=SITE)
    grant.oauth = read_oauth_request(authorize_url(), SITE)[0]
    url = GOOGLE + "/_/GoogleAccount/data/batchexecute"
    assert not grant.allow(url, "POST", navigation=False, resource_type="xhr")
    grant.post_available = True
    assert grant.allow(url, "POST", navigation=False, resource_type="xhr")
    assert not grant.allow(url, "POST", navigation=False, resource_type="xhr")
    grant.post_available = True
    live = False
    assert not grant.allow(url, "POST", navigation=False, resource_type="xhr")


@pytest.mark.parametrize(
    "url",
    [
        "http://directory.example",
        "https://directory.example:444",
        "https://user@directory.example",
        "https://127.0.0.1",
        "https://directory.example\\@other.example",
        "https://directory.example/foo\n",
    ],
)
def test_reject_non_public_https_origins(url):
    assert https_origin(url) is None

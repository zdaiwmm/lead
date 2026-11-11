import pytest


def test_complete_pin_list_requires_unique_matching_evidence():
    from lead_app.community_rule_evidence import PinReadResult

    base = dict(status="complete", reader="native_list", reason=None,
                peer_binding={"account_id": "fixture", "peer_id": "-123"},
                message_ids=["1", "2"], content_refs=["ref1", "ref2"], total_count=2)
    assert PinReadResult(**base).status == "complete"
    for change in ({"message_ids": ["1", "1"]}, {"total_count": 3},
                   {"content_refs": ["ref1"]}, {"next_cursor": "more"}):
        with pytest.raises(ValueError):
            PinReadResult(**{**base, **change})


def test_missing_external_rules_is_partial_even_after_all_messages_read():
    from lead_app.community_rule_evidence import PinReadResult

    result = PinReadResult(status="partial", reader="native_list",
                           reason="external_rules_required", peer_binding={"peer_id": "-123"},
                           message_ids=["1"], content_refs=["ref1"], total_count=1)
    assert result.status == "partial"
    assert result.reason == "external_rules_required"

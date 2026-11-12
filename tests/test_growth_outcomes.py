from lead_app.growth.outcomes import classify_receipts


def test_outcome_semantics():
    rows = [
        {"unit_key": "p1", "event_id": "1", "trust_level": "server_verified", "counted": True,
         "first_business_value": True, "subject_type": "pair", "channel": "facebook", "invited": False},
        {"unit_key": "p1", "event_id": "2", "trust_level": "server_verified", "counted": True,
         "first_business_value": True, "subject_type": "pair", "channel": "telegram", "invited": False},
        {"unit_key": "p2", "event_id": "3", "trust_level": "server_verified", "counted": True,
         "first_business_value": None, "subject_type": "pair", "channel": "facebook", "invited": False},
        {"unit_key": "p3", "event_id": "4", "trust_level": "client_observed", "counted": True,
         "first_business_value": True, "subject_type": "pair", "channel": "facebook", "invited": False},
        {"unit_key": "p4", "event_id": "5", "trust_level": "server_verified", "counted": True,
         "first_business_value": True, "subject_type": "pair", "channel": "facebook", "invited": True},
    ]
    result = classify_receipts(rows, "pair")
    assert result["new_units"] == 2
    assert result["channels"] == {"facebook": 1, "product_invite": 1}
    assert result["unknown_newness"] == 1
    assert result["client_observed"] == 1

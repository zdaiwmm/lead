import pytest


@pytest.mark.parametrize("text", [
    "本群供成年人交流语言学习经验，请友善讨论并尊重彼此隐私。",
    "Language exchange for adults who want to learn together.",
    "言語を学ぶ大人のための交流の場です。お互いを尊重してください。",
])
def test_substantive_description_is_not_space_dependent(text):
    from lead_app.community_rule_evidence import description_kind

    assert description_kind(text, topic_matches=True) == "substantive"


@pytest.mark.parametrize("text,want", [
    ("https://t.me/example", "empty"), ("@example", "empty"),
    ("语言交换群", "title_only"), ("Language exchange", "title_only"),
    ("123456 !!!", "empty"),
])
def test_title_or_link_is_not_substantive(text, want):
    from lead_app.community_rule_evidence import description_kind

    assert description_kind(text, topic_matches=True) == want


def test_description_does_not_replace_topic_check():
    from lead_app.community_rule_evidence import description_kind

    assert description_kind("Adults discussing an unrelated subject together.", topic_matches=False) == "title_only"


def test_read_pins_reference_does_not_invalidate_complete_evidence():
    from lead_app.community_rule_evidence import rules_status

    assert rules_status(description="Please read pinned rules.", pinned_complete=True, external_rules_unresolved=False) == "complete"


@pytest.mark.parametrize("description,complete,external", [
    ("Please read pinned rules.", False, False),
    ("请查看置顶群规", False, False),
    ("Rules already read.", True, True),
])
def test_external_link_or_missing_pin_stays_unresolved(description, complete, external):
    from lead_app.community_rule_evidence import rules_status

    assert rules_status(description=description, pinned_complete=complete, external_rules_unresolved=external) == "unresolved"
def test_complete_pins_do_not_resolve_external_rule_link():
    from lead_app.community_rule_evidence import rules_status
    assert rules_status(description="See rules at https://example.test/rules", pinned_complete=True, external_rules_unresolved=False)=="unresolved"

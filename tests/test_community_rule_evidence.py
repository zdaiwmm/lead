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

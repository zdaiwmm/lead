import pytest
from lead_app.community_runtime import (
    CommunityDecision,
    ad_permission,
    publication_issue,
    reply_issue,
)


@pytest.mark.parametrize("brand", ["ShiftDesk", "Lingua"])
def test_product_question_and_explicit_link_request(brand):
    question = "Can your tool export schedules? Please send the link."
    original = {"body": question, "own": False}
    business = {"brand": brand}
    mention = CommunityDecision(relevant_question=True, promote=True, promotion_invited=True,
                                commercial_allowed=True, invitation_quote=question, reason="product question",
                                body=f"I'm with {brand}. CSV export is supported.")
    assert reply_issue(mention, original, business) is None
    link = mention.model_copy(update={"include_link": True, "link_request_quote": "Please send the link."})
    assert reply_issue(link, original, business) is None
    for source in ["Do you support CSV?", "Don't send a link."]:
        decision = link.model_copy(update={"invitation_quote": source, "link_request_quote": source})
        assert reply_issue(decision, {"body": source, "own": False}, business) == "link_permission"
    assert reply_issue(mention.model_copy(update={"body": mention.body + " example.org"}), original, business) == "draft_link"


def test_final_body_and_old_intents():
    rule = "Self-promotion is welcome"
    candidate = {"rules_quote": rule, "evidence_id": "verified"}
    ad = {"placement": "advertising_group", "standalone": True, "body": "From our team",
          "ad_permission": rule, "policy_version": 1, "promote": True, "include_link": False}
    assert publication_issue(ad, candidate, "") is None
    assert publication_issue(ad | {"placement": "topic_post"}, candidate, "")
    assert not ad_permission({}, "Community discussion", rule)
    assert not ad_permission(candidate, "No ads", rule)
    linked = ad | {"include_link": True, "tracking_link_id": "tracked", "body": "Try https://demo.example/r/one"}
    assert publication_issue(linked, candidate, "") is None
    assert publication_issue(linked | {"body": linked["body"] + " https://t.me/two"}, candidate, "")
    assert publication_issue({"promote": True, "body": "Old draft"}, {}, "")
    assert publication_issue({"promote": False, "body": "Use the export menu."}, {}, "") is None


@pytest.mark.parametrize("rule", [
    "Ads allowed on Fridays only", "No ads allowed", "No self-promotion is welcome",
    "Only paid ads allowed", "不允许推广", "经管理员批准才允许推广",
])
def test_permission_fragment_does_not_enable_ads(rule):
    for quote in [rule, "Ads allowed", "ads allowed", "允许推广"]:
        assert not ad_permission({}, rule, quote)


def test_feedback_must_reference_a_member_reply_and_account_bound_receipt():
    from unittest.mock import Mock

    from lead_app.community_runtime import record_conversation_feedback

    db = Mock()
    decision = CommunityDecision(reason="follow-up", feedback_mid="42", feedback_kind="substantive")
    message = {"mid": "42", "own": False}
    observed = {"target_id": "-100", "history": [message]}
    record_conversation_feedback(db, "A", observed, decision)
    db.execute.assert_not_called()
    message["reply_parent"] = "41"
    record_conversation_feedback(db, "A", observed, decision)
    sql, args = db.execute.call_args.args
    assert args[1:] == ("A", "-100:41")
    assert "reply_state'='confirmed'" in sql


def test_review_mode_remains_opt_in_for_existing_agents():
    from lead_app.community_growth import GrowthConfig

    assert not GrowthConfig().review_replies_only
    assert GrowthConfig(review_replies_only=True).review_replies_only

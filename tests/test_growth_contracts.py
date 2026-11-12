from datetime import datetime
from uuid import uuid4

import pytest
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    Wait,
)
from pydantic import ValidationError


def test_cold_start_no_gates():
    spec = ExperimentSpec(question_type="product", question="What does the product do?",
                          hypothesis="A browser tool", learning_signal="Observed task",
                          token_limit=1000, review_at="2026-11-13T00:00:00Z")
    assert spec.activation_ref is None
    task = TaskSpec(kind="research", purpose="product", payload={"question": spec.question},
                    review_at=spec.review_at)
    assert task.account_id is None


def test_decision_commands_require_explicit_current_goal_revision():
    base={'command_key':'question','group_key':'question','kind':'experiment.create','payload':{}}
    with pytest.raises(ValidationError):
        Command(**base)
    with pytest.raises(ValidationError):
        Command(**base,goal_ref={})
    assert Command(**base,goal_ref={'revision':2}).goal_ref.revision==2
    assert 'goal_ref' in Command.model_json_schema()['required']


def test_missing_commands_cannot_silently_become_an_intentional_noop():
    with pytest.raises(ValidationError):
        DecisionProposal(context_key='captured',rationale='Run a bounded research action.')
    assert DecisionProposal(context_key='captured',rationale='No new evidence requires work.',commands=[]).commands==[]


def test_missing_wake_is_rejected():
    with pytest.raises(ValidationError):
        Wait(reason="provider", producer="", check_at=datetime.now(),
             recovery_action="probe", episode_root=uuid4())
    with pytest.raises(ValidationError):
        TaskSpec(kind="invented", purpose="anything", payload={},
                 review_at="2026-11-13T00:00:00Z")


def test_no_unbounded_experiment_or_privilege_payload():
    with pytest.raises(ValidationError):
        ExperimentSpec(question_type="audience", question="Who?",
                       hypothesis="Anyone", learning_signal="Demand",
                       token_limit=0, review_at="2026-11-13T00:00:00Z")
    with pytest.raises(ValidationError):
        TaskSpec(kind="research", purpose="product", payload={},
                 approved=True, review_at="2026-11-13T00:00:00Z")

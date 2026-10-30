"""User-message-bound operating goals; configuration never grants capabilities."""

from typing import Literal

from pydantic import BaseModel, Field

from .db import audit, emit, j, one, require_agent
from .scope import current_tenant


class OperatingUpdate(BaseModel):
    source_quote: str = Field(min_length=2, max_length=1000)
    objective: str | None = Field(default=None, max_length=500)
    objective_event: Literal["lead", "signup", "trial", "purchase"] | None = None
    audience: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=80)
    preferred_work: (
        list[Literal["opportunities", "journey", "measurement", "content"]] | None
    ) = Field(default=None, max_length=4)


def configuration(agent, business=None):
    saved = agent["onboarding_state"].get("operating_contract", {})
    facts = (business or {}).get("facts", {})
    objective = (
        saved.get("objective")
        or facts.get("operating_goal")
        or facts.get("conversion_goal")
    )
    return {
        "objective": objective or "了解真实需求并取得可验证的业务进展",
        "objective_status": "user_defined" if objective else "provisional",
        "objective_event": saved.get("objective_event"),
        "audience": saved.get("audience") or facts.get("audience", ""),
        "language": saved.get("language", ""),
        "preferred_work": saved.get("preferred_work", []),
        "revision": saved.get("revision", 0),
        "source_message_id": saved.get("source_message_id"),
    }


def apply(c, aid, message, update, expected_revision):
    from .commands import explicit_preference
    from .domain import snapshot_preferences

    previous = one(
        c,
        "SELECT result FROM lead.conversation_commands WHERE message_id=%s AND intent_index=2",
        (message["id"],),
    )
    if previous:
        return previous["result"]["reply"]
    agent = require_agent(c, aid, True)
    text = message["content"]["text"]
    valid = (
        message["role"] == "user"
        and explicit_preference(text, "add")
        and update.source_quote in text
        and agent["config_revision"] == expected_revision
    )
    values = update.model_dump(exclude_none=True, exclude={"source_quote"})
    if valid and values:
        saved = agent["onboarding_state"].get("operating_contract", {})
        if (
            "objective" in values
            and values["objective"] != saved.get("objective")
            and "objective_event" not in values
        ):
            # A new business goal cannot silently inherit the old goal's event.
            values["objective_event"] = None
        saved = {
            **saved,
            **values,
            "revision": saved.get("revision", 0) + 1,
            "source_message_id": str(message["id"]),
        }
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({"operating_contract": saved}), aid),
        )
        revision = snapshot_preferences(c, aid)
        reply, state = (
            "已保存经营目标与运营偏好，后台会据此重新选择后续工作。",
            "succeeded",
        )
        audit(
            c,
            aid,
            "operating.configuration",
            {
                "message_id": str(message["id"]),
                "revision": revision,
                "fields": list(values),
            },
        )
        emit(c, aid, "supervisor.wake")
    else:
        reply, state = (
            "经营配置尚未改变；讨论中的设想不会自动生效。",
            "needs_confirmation",
        )
    c.execute(
        """INSERT INTO lead.conversation_commands(tenant_id,agent_id,conversation_id,message_id,intent_index,command_type,operation_key,expected_revision,payload,state,result)
        VALUES(%s,%s,%s,%s,2,'operating.configure',%s,%s,%s,%s,%s)""",
        (
            current_tenant(),
            aid,
            message["conversation_id"],
            message["id"],
            str(message["id"]) + ":operating",
            expected_revision,
            j(update.model_dump()),
            state,
            j({"reply": reply}),
        ),
    )
    return reply

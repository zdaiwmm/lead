"""Owner membership controls, sourced pricing reference and safe delivery metadata."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .admin_api import require_owner
from .admin_delivery import DestinationSecret
from .db import all_rows, audit, one, tx

router = APIRouter(prefix="/api/admin")


@router.get("/member-access", dependencies=[Depends(require_owner)])
def members():
    from .admin_permissions import CAPABILITIES, OPERATOR_DEFAULTS
    from .auth import auth_mode

    with tx() as c:
        rows = all_rows(
            c,
            "SELECT oidc_issuer AS issuer,oidc_subject AS subject,role,status FROM lead.memberships ORDER BY created_at,oidc_subject",
        )
    return {
        "members": rows,
        "capabilities": CAPABILITIES,
        "operator_defaults": OPERATOR_DEFAULTS,
        "mode": auth_mode(),
    }


def reference_rates():
    prices = [
        ("gpt-6-luna", "0.10", "0.01", "0.50"),
        ("gpt-6-sol", "2.00", "0.20", "10.00"),
        ("gpt-6.1-sol", "2.00", "0.10", "10.00"),
        ("gpt-6.1-terra", "1.00", "0.05", "5.00"),
        ("gpt-6-astra", "10.00", "1.00", "50.00"),
        ("gpt-4.1-mini", "0.40", "0.10", "1.60"),
    ]
    return {
        "currency": "USD",
        "verified_at": "2026-10-30T00:00:00+00:00",
        "models": [
            {
                "model": "openai:" + name,
                "input_per_million": input_rate,
                "cached_input_per_million": cache,
                "output_per_million": output,
                "max_request_input_tokens": 272000
                if name.startswith("gpt-6")
                else None,
                "long_input_multiplier": "2" if name.startswith("gpt-6") else "1",
                "long_output_multiplier": "1.5" if name.startswith("gpt-6") else "1",
                "source_url": "https://developers.openai.com/api/docs/models/" + name,
                "verified_at": "2026-10-30T00:00:00+00:00",
                "notes": "官方 Standard 参考价，每百万 token；自定义接入、区域及服务档位附加费需另行核对。缓存写入明细不足时不估算费用。",
            }
            for name, input_rate, cache, output in prices
        ],
    }


@router.get("/pricing-reference")
def pricing_reference():
    return reference_rates()


@router.get("/external-notifications/secrets", dependencies=[Depends(require_owner)])
def secret_status():
    from .admin_delivery import secret_status

    with tx() as c:
        return secret_status(c)


@router.post(
    "/external-notifications/secrets/{destination}",
    dependencies=[Depends(require_owner)],
)
def save_secret(destination: UUID, body: DestinationSecret):
    from .admin_delivery import save_secret

    with tx() as c:
        return save_secret(c, str(destination), body)


@router.get("/external-notifications/deliveries")
def deliveries(before: datetime | None = None, before_id: UUID | None = None):
    with tx() as c:
        return all_rows(
            c,
            """SELECT d.id,d.notification_id,d.destination_id,d.configuration_version,d.state,d.attempts,d.attempt_limit,d.last_status_code,d.last_error_code,d.completed_at,d.available_at,d.created_at,d.destination_snapshot->>'name' AS destination_name,n.title,n.category,n.severity
          FROM lead.notification_deliveries d JOIN lead.admin_notifications n ON n.id=d.notification_id
          WHERE (%s::timestamptz IS NULL OR (d.created_at,d.id)<(%s,%s::uuid)) ORDER BY d.created_at DESC,d.id DESC LIMIT 40""",
            (before, before, before_id),
        )


class DeliveryRetry(BaseModel):
    expected_attempts: int = Field(ge=0)
    acknowledge_duplicate_risk: bool = False


@router.post(
    "/external-notifications/deliveries/{delivery}/retry",
    dependencies=[Depends(require_owner)],
)
def retry(delivery: UUID, body: DeliveryRetry):
    with tx() as c:
        row = one(
            c,
            "SELECT * FROM lead.notification_deliveries WHERE id=%s FOR UPDATE",
            (delivery,),
        )
        if not row:
            raise HTTPException(404, "发送记录不存在")
        if (
            row["state"] not in ("failed", "unknown")
            or row["attempts"] != body.expected_attempts
        ):
            raise ValueError("发送状态已经变化，请刷新后核对")
        if row["state"] == "unknown" and not body.acknowledge_duplicate_risk:
            raise ValueError("结果未知的通知可能已经送达，请先核对并确认重复风险")
        c.execute(
            "UPDATE lead.notification_deliveries SET state='retry',available_at=now(),completed_at=NULL,attempt_limit=attempts+(destination_snapshot->>'max_attempts')::integer WHERE id=%s",
            (delivery,),
        )
        audit(
            c,
            row["agent_id"],
            "admin.notification.retry",
            {
                "delivery_id": str(delivery),
                "previous_state": row["state"],
                "acknowledged": body.acknowledge_duplicate_risk,
            },
        )
    return {"ok": True}

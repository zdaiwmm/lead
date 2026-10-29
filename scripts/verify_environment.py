"""Actual browser exit measurements. Shared development egress must stay unverified."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app import domain
from lead_app.browser import Gateway
from lead_app.config import STATE
from lead_app.identity import check


async def main():
    domain.emit = lambda *a, **k: None
    ids = [domain.create_agent() for _ in range(3)]
    gateway = Gateway()
    report = {"ok": False, "independent_egress_verified": False}
    try:
        actors = await asyncio.gather(*(gateway.get(aid) for aid in ids))
        samples = await asyncio.gather(*(check(actor) for actor in actors))
        assert all(
            s["checks"]["device"] and s["checks"]["device"]["webRTCDisabled"]
            for s in samples
        )
        # No dedicated upstream was configured for these transient test profiles.
        assert all(not s["verified"] for s in samples)
        assert (
            len({json.dumps(s["checks"]["device"], sort_keys=True) for s in samples})
            == 3
        )
        report.update(
            ok=True,
            checks=[
                "three_actual_browser_device_samples",
                "webrtc_disabled",
                "unconfigured_shared_egress_not_promoted",
            ],
            samples=samples,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "checks": report["checks"],
                    "probe_agreement": [
                        s["checks"]["probe_agreement"] for s in samples
                    ],
                    "independent_egress_verified": False,
                }
            )
        )
    finally:
        for aid in ids:
            await gateway.close(aid)
            domain.set_control(aid, "archive")
        path = STATE / "environment-verification.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        path.chmod(0o600)


if __name__ == "__main__":
    asyncio.run(main())

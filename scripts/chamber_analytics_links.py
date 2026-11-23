"""Register an exact snapshot of existing Chamber shortlinks with production."""

import argparse
import hashlib
import json
from uuid import UUID


def snapshot(agent, hostname):
    from lead_app.db import all_rows, require_agent, tx

    with tx(agent) as c:
        require_agent(c, agent)
        links = all_rows(
            c,
            """SELECT public_code, platform, destination_url, allowed_host
               FROM lead.tracking_links
               WHERE agent_id=%s AND state='active' AND platform<>'lab'
               ORDER BY public_code""",
            (agent,),
        )
    if any(row["allowed_host"] != hostname for row in links):
        raise ValueError("现有推广链接包含其他站点，无法完整迁移到单个生产像素连接")
    sha = hashlib.sha256(json.dumps(links, separators=(",", ":")).encode()).hexdigest()
    return sha, links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent", type=UUID, help="本机 Chamber Agent ID")
    parser.add_argument("--host", required=True, help="获准归因的生产站点主机名")
    parser.add_argument("--apply", metavar="SNAPSHOT_SHA256", help="提交上次只读盘点的固定快照")
    args = parser.parse_args()
    from lead_app.chamber_analytics import (
        ChamberAnalyticsUnavailable,
        production_bridge_enabled,
        register_production_link,
    )

    try:
        sha, links = snapshot(str(args.agent), args.host)
        if args.apply is None:
            print(json.dumps({"snapshot_sha256": sha, "links": len(links)}))
            return
        if args.apply != sha or not production_bridge_enabled(args.agent):
            raise ValueError("短链快照或本机 Chamber 生产映射与预期不符")
        for row in links:
            register_production_link(
                args.agent, row["public_code"], row["platform"], row["destination_url"]
            )
    except (ValueError, LookupError, ChamberAnalyticsUnavailable) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"snapshot_sha256": sha, "registered": len(links)}))


if __name__ == "__main__":
    main()

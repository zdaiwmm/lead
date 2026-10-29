"""Operator-only, single-node recovery cutover. Restored Agents stay paused."""

import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .backups import digest
from .config import BROWSER_BUILD, BROWSER_SDK, CONFIG, STATE, database_kwargs
from .db import j


def connection(database):
    return psycopg.connect(
        **{**database_kwargs(True), "dbname": database}, row_factory=dict_row
    )


def case(conn, identifier, lock=True):
    row = conn.execute(
        "SELECT * FROM lead.recovery_cases WHERE id=%s"
        + (" FOR UPDATE" if lock else ""),
        (str(UUID(identifier)),),
    ).fetchone()
    if not row:
        raise ValueError("恢复检查点不存在")
    return row


def prepare(target, source, node_root):
    if not source or target == source or not target.startswith("lead_restore_"):
        raise ValueError("恢复目标必须是独立的新数据库")
    node_root = Path(node_root).resolve()
    allowed = (STATE / "recovered-nodes").resolve()
    if node_root == allowed or not node_root.is_relative_to(allowed):
        raise ValueError("恢复节点必须使用私有 recovered-nodes 下的新目录")
    identifier = str(uuid4())
    with connection(target) as c:
        guard = c.execute(
            "SELECT * FROM lead.recovery_control WHERE singleton FOR UPDATE"
        ).fetchone()
        if guard["state"] != "recovery_required" or not guard["backup_id"]:
            raise ValueError("目标数据库尚未经过受保护的备份恢复")
        node_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        targets = [
            str(r["id"])
            for r in c.execute(
                "SELECT id FROM lead.account_targets WHERE state='active'"
            ).fetchall()
        ]
        details = {
            "source": source,
            "target": target,
            "node_root": str(node_root),
            "targets": targets,
            "reviews": {},
            "installed": [],
            "temporal_namespace": "lead-recovery-" + identifier,
            "window_start": str(guard["recovery_window_start"]),
        }
        c.execute(
            "INSERT INTO lead.recovery_cases(id,backup_id,state,details) VALUES(%s,%s,'prepared',%s)",
            (identifier, guard["backup_id"], j(details)),
        )
    config = node_root / "config.json"
    with config.open("x") as f:
        os.chmod(config, 0o600)
        json.dump(CONFIG, f)
    network_key = STATE / "keys/network.key"
    if network_key.exists():
        from .managed_network import key

        folder = node_root / "keys"
        folder.mkdir(mode=0o700)
        (folder / "network.key").write_bytes(key())
        (folder / "network.key").chmod(0o600)
    return {
        "id": identifier,
        "state": "prepared",
        "node_root": str(node_root),
        "targets": targets,
    }


def isolate_source(target, identifier):
    # Source row locks are the same commit barrier used before external clicks.
    with connection(target) as c:
        item = case(c, identifier)
        if item["state"] != "prepared":
            return {"state": item["state"]}
        details = item["details"]
        with connection(details["source"]) as old:
            old.execute(
                "SELECT id FROM lead.agents ORDER BY id FOR NO KEY UPDATE"
            ).fetchall()
            old.execute(
                "UPDATE lead.recovery_control SET state='recovery_required',updated_at=now() WHERE singleton"
            )
            old.execute(
                "UPDATE lead.agents SET desired_state='paused',health_state='stopped',control_version=control_version+1 WHERE desired_state<>'archived'"
            )
            rows = old.execute(
                "SELECT a.id,a.tenant_id,a.agent_id,a.target_id,a.operation_key,a.state,a.external_id,i.content_hash FROM lead.actions a LEFT JOIN lead.content_items i ON i.id=a.content_id WHERE a.created_at>=%s ORDER BY a.id",
                (details["window_start"],),
            ).fetchall()
            proof = {
                "case_id": identifier,
                "window_actions": json.loads(json.dumps(rows, default=str)),
            }
            old.execute(
                "INSERT INTO lead.recovery_cases(id,backup_id,state,details) VALUES(%s,%s,'source_isolated',%s) ON CONFLICT(id) DO NOTHING",
                (identifier, item["backup_id"], j(proof)),
            )
        details["source_proof"] = proof
        for observed in proof["window_actions"]:
            if (
                observed["content_hash"]
                and c.execute(
                    "SELECT 1 FROM lead.account_targets WHERE id=%s AND agent_id=%s",
                    (observed["target_id"], observed["agent_id"]),
                ).fetchone()
            ):
                c.execute(
                    "INSERT INTO lead.recovery_observations(tenant_id,agent_id,target_id,case_id,content_hash,proof) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        observed["tenant_id"],
                        observed["agent_id"],
                        observed["target_id"],
                        identifier,
                        observed["content_hash"],
                        j(observed),
                    ),
                )
        c.execute(
            "UPDATE lead.recovery_cases SET state='source_isolated',details=%s,updated_at=now() WHERE id=%s",
            (j(details), identifier),
        )
    return {"state": "source_isolated", "window_actions": len(rows)}


def install_profile(target, identifier, quarantine):
    quarantine = Path(quarantine).resolve()
    if (
        not quarantine.is_relative_to((STATE / "restore-quarantine").resolve())
        or not (quarantine / "RECOVERY_REQUIRED").is_file()
    ):
        raise ValueError("只能安装已认证解密的恢复隔离区")
    manifest = json.loads((quarantine / "manifest.json").read_text())
    aid = str(UUID(manifest["agent_id"]))
    build = manifest.get("engine_version")
    if build != BROWSER_BUILD or manifest.get("sdk_version") != BROWSER_SDK:
        raise ValueError("浏览器构建或运行组件不匹配，不能自动迁移")
    for name, info in manifest["files"].items():
        path = quarantine / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(quarantine)
            or digest(path) != info["sha256"]
        ):
            raise ValueError("恢复文件发生变化")
    with connection(target) as c:
        item = case(c, identifier)
        if item["state"] not in ("source_isolated", "installed"):
            raise ValueError("旧控制者未隔离")
        details = item["details"]
        profile = c.execute(
            "SELECT * FROM lead.browser_profiles WHERE agent_id=%s", (aid,)
        ).fetchone()
        if (
            not profile
            or str(profile["id"]) != manifest["profile_id"]
            or str(profile["tenant_id"]) != manifest["tenant_id"]
            or profile["config_hash"] != manifest["config_hash"]
        ):
            raise ValueError("Profile 不属于恢复目标")
        destination = Path(details["node_root"]) / "profiles" / aid
        if aid in details["installed"]:
            if not (destination / "identity.json").is_file():
                raise ValueError("已安装的环境缺失")
            return {"state": "installed", "agent_id": aid}
        marker = destination / "recovery-case.json"
        if destination.exists():
            if not marker.is_file() or json.loads(marker.read_text()) != {
                "case_id": identifier,
                "backup_id": str(UUID(quarantine.name)),
            }:
                raise ValueError("恢复安装不能覆盖既有环境")
            for name, info in manifest["files"].items():
                if digest(destination / name) != info["sha256"]:
                    raise ValueError("中断的恢复环境已变化")
        temporary = destination.with_name(aid + ".installing")
        temporary.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if not destination.exists():
                shutil.copytree(
                    quarantine,
                    temporary,
                    ignore=shutil.ignore_patterns("RECOVERY_REQUIRED", "manifest.json"),
                )
                from .ops import atomic_json

                atomic_json(
                    temporary / "recovery-case.json",
                    {"case_id": identifier, "backup_id": str(UUID(quarantine.name))},
                )
                os.replace(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        details["installed"].append(aid)
        c.execute(
            "UPDATE lead.browser_profiles SET storage_ref=%s,config_ref=%s,state='stopped',node_id=%s WHERE agent_id=%s",
            (
                str(destination / "storage"),
                str(destination / "identity.json"),
                "recovery:" + identifier,
                aid,
            ),
        )
        c.execute(
            "UPDATE lead.recovery_cases SET state='installed',details=%s,updated_at=now() WHERE id=%s",
            (j(details), identifier),
        )
    return {"state": "installed", "agent_id": aid}


def review(target, identifier, target_id, note):
    if not note.strip() or len(note) > 2000:
        raise ValueError("需要记录外部动作窗口的核对结果")
    with connection(target) as c:
        item = case(c, identifier)
        if item["state"] not in ("source_isolated", "installed"):
            raise ValueError("恢复状态不允许核对")
        details = item["details"]
        if target_id not in details["targets"]:
            raise ValueError("目标不在此恢复范围")
        # This never approves publication. All restored grants are revoked on release.
        details["reviews"][target_id] = {"disposition": "reauthorize", "note": note}
        c.execute(
            "UPDATE lead.recovery_cases SET details=%s,updated_at=now() WHERE id=%s",
            (j(details), identifier),
        )
    return {"reviewed": target_id, "publication": "requires_fresh_authorization"}


async def release(target, identifier):
    from google.protobuf.duration_pb2 import Duration
    from temporalio.api.workflowservice.v1 import RegisterNamespaceRequest

    from .temporal_client import connect

    with connection(target) as c:
        item = case(c, identifier)
        details = item["details"]
        if item["state"] == "released":
            return publish_launch(target, details)
        if item["state"] not in ("source_isolated", "installed") or set(
            details["targets"]
        ) - set(details["reviews"]):
            raise ValueError("恢复窗口尚未逐目标核对")
        with connection(details["source"]) as old:
            guard = old.execute(
                "SELECT state FROM lead.recovery_control WHERE singleton"
            ).fetchone()
            proof = old.execute(
                "SELECT id FROM lead.recovery_cases WHERE id=%s AND state='source_isolated'",
                (identifier,),
            ).fetchone()
            if not guard or guard["state"] != "recovery_required" or not proof:
                raise ValueError("不能证明旧控制者仍被隔离")
        active = c.execute(
            "SELECT a.id FROM lead.agents a JOIN lead.browser_profiles p ON p.agent_id=a.id WHERE a.desired_state<>'archived'"
        ).fetchall()
        for agent in active:
            # Profiles without a previous browser can be generated normally.
            profile = c.execute(
                "SELECT storage_ref FROM lead.browser_profiles WHERE agent_id=%s",
                (agent["id"],),
            ).fetchone()
            if (
                Path(profile["storage_ref"]).exists()
                and str(agent["id"]) not in details["installed"]
            ):
                raise ValueError("仍有已有登录环境未安装到恢复节点")
            destination = Path(details["node_root"]) / "profiles" / str(agent["id"])
            c.execute(
                "UPDATE lead.browser_profiles SET storage_ref=%s,config_ref=%s,node_id=%s WHERE agent_id=%s",
                (
                    str(destination / "storage"),
                    str(destination / "identity.json"),
                    "recovery:" + identifier,
                    agent["id"],
                ),
            )
        install_material(c, Path(details["node_root"]))
        temporal = await connect()
        try:
            await temporal.workflow_service.register_namespace(
                RegisterNamespaceRequest(
                    namespace=details["temporal_namespace"],
                    workflow_execution_retention_period=Duration(seconds=7 * 86400),
                )
            )
        except Exception as error:
            from temporalio.service import RPCError, RPCStatusCode

            if (
                not isinstance(error, RPCError)
                or error.status != RPCStatusCode.ALREADY_EXISTS
            ):
                raise
        from temporalio.client import Client

        from .temporal_client import connection_options

        args = connection_options()
        args["namespace"] = details["temporal_namespace"]
        isolated = await Client.connect(**args)
        async for _ in isolated.list_workflows():
            raise ValueError("恢复必须使用没有旧执行历史的任务命名空间")
        c.execute(
            "UPDATE lead.agents SET desired_state='paused',health_state='needs_auth',control_version=control_version+1 WHERE desired_state<>'archived'"
        )
        c.execute(
            "UPDATE lead.authorization_grants SET revoked_at=now() WHERE revoked_at IS NULL"
        )
        c.execute(
            "UPDATE lead.social_accounts SET state='needs_auth' WHERE state='connected'"
        )
        c.execute(
            "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE state IN ('planned','queued','running') AND NOT EXISTS(SELECT 1 FROM lead.actions a WHERE a.task_id=lead.tasks.id AND a.state IN ('unknown','dispatching'))"
        )
        c.execute("UPDATE lead.actions SET state='canceled' WHERE state='prepared'")
        c.execute(
            "UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE ended_at IS NULL"
        )
        c.execute(
            "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state IN ('requested','active','ending')"
        )
        c.execute(
            "UPDATE lead.profile_leases SET state='released',epoch=epoch+1,expires_at=now()"
        )
        c.execute(
            "UPDATE lead.identity_checks SET valid_until=now() WHERE valid_until>now()"
        )
        c.execute(
            "UPDATE lead.network_identities SET state='pending',valid_until=now() WHERE state<>'retired'"
        )
        c.execute(
            "UPDATE lead.recovery_cases SET state='released',updated_at=now() WHERE id=%s",
            (identifier,),
        )
        c.execute(
            "UPDATE lead.recovery_control SET state='normal',updated_at=now() WHERE singleton"
        )
    return publish_launch(target, details)


def install_material(c, node_root):
    from .backup_files import REFERENCES

    for table, column in REFERENCES:
        query = sql.SQL(
            "SELECT x.id,x.{column} AS reference,a.desired_state FROM lead.{table} x JOIN lead.agents a ON a.id=x.agent_id WHERE x.{column} IS NOT NULL"
        ).format(column=sql.Identifier(column), table=sql.Identifier(table))
        for row in c.execute(query).fetchall():
            path = Path(row["reference"])
            if not path.is_absolute():
                continue
            try:
                relative = path.relative_to(STATE)
            except ValueError:
                raise ValueError("恢复资料尚未重定位到当前恢复主机") from None
            if not relative.parts or relative.parts[0] not in ("assets", "evidence"):
                raise ValueError("恢复资料路径不属于可安装范围")
            if path.is_symlink() or not path.resolve().is_relative_to(STATE):
                raise ValueError("恢复资料不能是符号链接")
            if not path.is_file() and row["desired_state"] != "archived":
                raise ValueError("恢复资料缺失，保持发布隔离")
            if path.is_file():
                destination = node_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if destination.exists():
                    if digest(destination) != digest(path):
                        raise ValueError("恢复资料不同，拒绝覆盖")
                else:
                    temporary = destination.with_name(destination.name + ".installing")
                    shutil.copyfile(path, temporary)
                    temporary.chmod(0o600)
                    os.replace(temporary, destination)
                c.execute(
                    sql.SQL("UPDATE lead.{table} SET {column}=%s WHERE id=%s").format(
                        table=sql.Identifier(table), column=sql.Identifier(column)
                    ),
                    (str(destination), row["id"]),
                )


def publish_launch(target, details):
    launch = {
        "LEAD_STATE_DIR": details["node_root"],
        "LEAD_DB_NAME": target,
        "LEAD_TEMPORAL_NAMESPACE": details["temporal_namespace"],
    }
    from .ops import atomic_json

    atomic_json(Path(details["node_root"]) / "launch.json", launch)
    return {
        "state": "released",
        "agents": "paused",
        "authorization": "required",
        "launch": launch,
    }

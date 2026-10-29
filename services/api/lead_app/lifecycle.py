"""Scoped customer export and explicit operator purge after retention."""

import fcntl
import io
import json
import shutil
import zipfile
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .config import STATE, database_kwargs
from .db import all_rows, require_agent, tx
from .ops import atomic_json

MAX_EXPORT_BYTES = 256 * 1024**2

EXPORTS = {
    "agent": "SELECT id,name,desired_state,created_at FROM lead.agents WHERE id=%s",
    "business": "SELECT revision,product_url,description,facts FROM lead.business_profiles WHERE agent_id=%s ORDER BY revision",
    "messages": "SELECT id,role,content,status,created_at FROM lead.conversation_messages WHERE agent_id=%s ORDER BY created_at",
    "memory": "SELECT id,kind,body,source_refs,confirmation,created_at,deleted_at FROM lead.memory_entries WHERE agent_id=%s ORDER BY created_at",
    "publication": "SELECT id,operation_key,state,intent,external_id,confirmed_at FROM lead.actions WHERE agent_id=%s ORDER BY created_at",
    "results": "SELECT * FROM lead.daily_metrics WHERE agent_id=%s ORDER BY day",
}


def export(aid):
    aid = str(UUID(aid))
    with tx(aid) as c:
        require_agent(c, aid)
        data = {name: all_rows(c, query, (aid,)) for name, query in EXPORTS.items()}
        items = all_rows(
            c,
            "SELECT id,media_type,size_bytes,sha256,metadata,rights FROM lead.assets WHERE state='ready' ORDER BY created_at,id",
        )
    # Credentials, browser cookies, private network data and host paths are omitted.
    return {"format": "lead-export-v1", "agent_id": aid, "data": data, "assets": items}


def export_archive(aid, part=None):
    from .assets import resolve

    payload = export(aid)
    groups = [[]]
    size = 0
    for item in payload["assets"]:
        if size + item["size_bytes"] > MAX_EXPORT_BYTES and groups[-1]:
            groups.append([])
            size = 0
        groups[-1].append(item)
        size += item["size_bytes"]
    if part is None and len(groups) > 1:
        raise ValueError("素材较多，请使用维护端分批导出")
    if part is not None and not 1 <= part <= len(groups):
        raise ValueError("导出分卷不存在")
    selected = groups[(part or 1) - 1]
    payload["export_part"] = part or 1
    payload["export_parts"] = len(groups)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "lead.json", json.dumps(payload, ensure_ascii=False, default=str)
        )
        for item in selected:
            _, path = resolve(aid, str(item["id"]), item["sha256"])
            archive.write(path, "assets/" + str(item["id"]) + path.suffix)
    return output.getvalue()


def purge_order(c):
    names = {
        r["table_name"]
        for r in c.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema='lead' AND column_name='agent_id'"
        ).fetchall()
    }
    edges = [
        (r["child"].split(".")[-1], r["parent"].split(".")[-1])
        for r in c.execute(
            "SELECT conrelid::regclass::text AS child,confrelid::regclass::text AS parent FROM pg_constraint WHERE contype='f' AND connamespace='lead'::regnamespace"
        ).fetchall()
    ]
    names -= {"backup_records"}
    order = []
    while names:
        choices = sorted(
            t
            for t in names
            if not any(
                parent == t and child in names and child != t for child, parent in edges
            )
        )
        if not choices:
            raise ValueError("当前版本存在循环依赖，停止删除并保留全部数据")
        order.extend(choices)
        names -= set(choices)
    return order


def purge(tenant, aid, apply=False, retention_days=7):
    tenant, aid = str(UUID(tenant)), str(UUID(aid))
    if retention_days < 7:
        raise ValueError("归档数据至少保留七天")
    folder = STATE / "profiles" / aid
    if folder.is_symlink() or folder.resolve().parent != (STATE / "profiles").resolve():
        raise ValueError("环境路径不属于指定 Agent")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / "actor.lock").open("a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("浏览器仍在使用此环境") from None
        with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
            agent = c.execute(
                "SELECT *,updated_at<now()-make_interval(days=>%s) AS retained FROM lead.agents WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (retention_days, aid, tenant),
            ).fetchone()
            if not agent:
                raise LookupError("此工作室中不存在该 Agent")
            if agent["desired_state"] != "archived" or not agent["retained"]:
                raise ValueError("仅可清除已归档且超过保留期限的数据")
            if c.execute(
                "SELECT 1 FROM lead.actions WHERE agent_id=%s AND state IN ('prepared','dispatching','unknown')",
                (aid,),
            ).fetchone():
                raise ValueError("有尚未核对的外部动作，必须保留账本和证据")
            if c.execute(
                "SELECT 1 FROM lead.profile_leases WHERE agent_id=%s AND state IN ('held','uncertain')",
                (aid,),
            ).fetchone():
                raise ValueError("环境控制权尚未释放")
            order = purge_order(c)
            counts = {
                table: c.execute(
                    sql.SQL(
                        "SELECT count(*) AS n FROM lead.{} WHERE agent_id=%s AND tenant_id=%s"
                    ).format(sql.Identifier(table)),
                    (aid, tenant),
                ).fetchone()["n"]
                for table in order
            }
            result = {
                "agent_id": aid,
                "tenant_id": tenant,
                "state": "preview",
                "rows": counts,
                "backups": "encrypted_backups_expire_under_separate_retention",
            }
            if not apply:
                return result
            for table in order:
                c.execute(
                    sql.SQL(
                        "DELETE FROM lead.{} WHERE agent_id=%s AND tenant_id=%s"
                    ).format(sql.Identifier(table)),
                    (aid, tenant),
                )
            # Retained backup catalog entries remain usable until normal expiry.
            c.execute(
                "UPDATE lead.backup_records SET agent_id=NULL WHERE agent_id=%s", (aid,)
            )
            c.execute(
                "DELETE FROM lead.agents WHERE id=%s AND tenant_id=%s", (aid, tenant)
            )
            result["state"] = "prepared"
            # Journal exists before commit; cleanup rechecks that deletion committed.
            atomic_json(STATE / "purges" / (aid + ".json"), result)
        return finish_purge(tenant, aid)


def finish_purge(tenant, aid):
    tenant, aid = str(UUID(tenant)), str(UUID(aid))
    journal = STATE / "purges" / (aid + ".json")
    if journal.is_symlink():
        raise ValueError("删除检查点不可用")
    result = json.loads(journal.read_text())
    if result.get("agent_id") != aid or result.get("tenant_id") != tenant:
        raise ValueError("删除检查点不属于指定数据")
    with psycopg.connect(**database_kwargs(True)) as c:
        if c.execute("SELECT 1 FROM lead.agents WHERE id=%s", (aid,)).fetchone():
            raise ValueError("数据删除尚未提交，保留全部文件")
    pending = []
    for category in ("assets", "evidence", "profiles"):
        path = STATE / category / aid
        if path.is_symlink() or not path.resolve().is_relative_to(
            (STATE / category).resolve()
        ):
            pending.append(category)
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
        except OSError:
            pending.append(category)
    result.update(
        state="purged" if not pending else "file_cleanup_pending", pending=pending
    )
    atomic_json(journal, result)
    return result

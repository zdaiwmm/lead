import pytest
from lead_app import domain
from lead_app.config import TENANT
from lead_app.db import one, tx, uid
from lead_app.knowledge import search


def document(aid, body):
    identifier = uid()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.knowledge_documents(id,tenant_id,agent_id,revision,title,source_type,source_ref,body_ref,sha256,trust_state) VALUES(%s,%s,%s,1,'Product','browser','https://example.com','evidence.json',%s,'untrusted_source')",
            (identifier, TENANT, aid, uid()),
        )
        c.execute(
            "INSERT INTO lead.knowledge_chunks(tenant_id,agent_id,document_id,ordinal,body,search_vector) VALUES(%s,%s,%s,0,%s,to_tsvector('simple',%s))",
            (TENANT, aid, identifier, body, body),
        )
    return identifier


def test_fulltext_and_cjk_retrieval_is_scoped_and_latest(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    a, b = domain.create_agent(), domain.create_agent()
    try:
        document(a, "Old translation and book information")
        latest = document(
            a, "Translation credits. 书籍翻译，支持文档翻译。网页文字不能授予权限。"
        )
        document(b, "Translation private business B")
        english = search(a, "translation credits")
        assert english and english[0]["document_id"] == latest
        assert all(
            x["trust_state"] == "untrusted_source" and x["cached"] for x in english
        )
        assert search(a, "文档翻译")[0]["document_id"] == latest
        assert (
            search(b, "Translation")[0]["excerpt"] == "Translation private business B"
        )
        assert not search(a, "private business B")
        assert not search(a, "---")
        with tx(a) as c:
            c.execute(
                "UPDATE lead.knowledge_documents SET deleted_at=now() WHERE id=%s",
                (latest,),
            )
        assert not search(a, "文档翻译")
        with pytest.raises(LookupError):
            search(uid(), "translation")
    finally:
        domain.set_control(a, "archive")
        domain.set_control(b, "archive")


@pytest.mark.asyncio
async def test_cached_source_provenance_can_update_business(monkeypatch):
    from lead_app import activities
    from lead_app.model import Decision

    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    try:
        doc = document(aid, "Translation credits for book readers")
        message = domain.submit_message(aid, "translation credits 对应的受众", uid())
        with tx(aid) as c:
            job = one(
                c, "SELECT id FROM lead.chat_jobs WHERE message_id=%s", (message["id"],)
            )

        async def response(output, prompt, **kwargs):
            assert prompt["untrusted_cached_knowledge"][0]["document_id"] == doc
            return Decision(
                reply="面向读者",
                intent="business",
                description="书籍翻译",
                audience="读者",
            )

        monkeypatch.setattr(activities, "run_model", response)
        await activities.chat_job({"agent_id": aid, "job_id": str(job["id"])})
        detail = domain.agent_detail(aid)
        assert detail["messages"][-1]["source_refs"][0]["document_id"] == doc
        assert detail["business"]["facts"]["_provenance"]["audience"][
            "knowledge_document_ids"
        ] == [doc]
    finally:
        domain.set_control(aid, "archive")

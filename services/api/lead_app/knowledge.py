"""Bounded, scoped full-text retrieval from actual persisted browser evidence."""

import re

from .db import all_rows, require_agent, tx


def search(aid, query, limit=4):
    query = query.strip()[:256]
    words = re.findall(r"[A-Za-z0-9_]{2,40}|[\u3400-\u9fff]{2,40}", query)[:12]
    if not words:
        return []
    # PostgreSQL simple tokenization supports English. Short CJK fragments make
    # a bounded literal fallback without claiming semantic/vector retrieval.
    literals = []
    for word in words:
        literals.extend(
            [word[i : i + 3] for i in range(0, min(len(word) - 1, 18), 3)]
            if re.search(r"[\u3400-\u9fff]", word)
            else [word]
        )
    patterns = ["%" + term.replace("_", r"\_") + "%" for term in literals[:12]]
    with tx(aid) as c:
        require_agent(c, aid)
        rows = all_rows(
            c,
            """WITH latest AS (
              SELECT DISTINCT ON(source_ref) id,title,source_ref,created_at,trust_state
              FROM lead.knowledge_documents WHERE deleted_at IS NULL
              ORDER BY source_ref,created_at DESC,id DESC
            ) SELECT d.id AS document_id,d.title,d.source_ref AS url,d.created_at,d.trust_state,
                     k.id AS chunk_id,left(k.body,1800) AS excerpt,
                     ts_rank_cd(k.search_vector,websearch_to_tsquery('simple',%s)) AS rank
              FROM latest d JOIN lead.knowledge_chunks k ON k.document_id=d.id
              WHERE k.search_vector @@ websearch_to_tsquery('simple',%s) OR k.body ILIKE ANY(%s)
              ORDER BY rank DESC,d.created_at DESC,k.ordinal LIMIT %s""",
            (" ".join(words), " ".join(words), patterns, max(1, min(limit, 6))),
        )
    return [
        {
            **row,
            "document_id": str(row["document_id"]),
            "chunk_id": str(row["chunk_id"]),
            "cached": True,
        }
        for row in rows
    ]

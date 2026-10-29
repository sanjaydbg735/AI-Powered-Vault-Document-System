import json

from app.core.config import settings
from tests.conftest import ALICE, BOB, CONTRACT, HANDBOOK, upload


async def _setup(client, scoped: bool = False):
    a = (await upload(client, "contract.md", CONTRACT)).json()["document"]["id"]
    b = (await upload(client, "handbook.txt", HANDBOOK)).json()["document"]["id"]
    body = {"document_ids": [a]} if scoped else {}
    sid = (await client.post("/chat/sessions", json=body, headers=ALICE)).json()["id"]
    return sid, a, b


async def _ask(client, sid, q, headers=ALICE):
    return await client.post(f"/chat/sessions/{sid}/messages", json={"question": q}, headers=headers)


async def test_grounded_answer_with_citation_to_right_document(client):
    sid, contract_id, _ = await _setup(client)
    r = await _ask(client, sid, "How many days notice to terminate the agreement?")
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] is True
    assert "30 days" in body["answer"]["content"]
    assert body["answer"]["citations"][0]["document_id"] == contract_id
    assert body["answer"]["suggested_questions"]


async def test_multi_document_session_answers_from_each_document(client):
    sid, contract_id, handbook_id = await _setup(client)
    r1 = (await _ask(client, sid, "How many weeks of parental leave for primary caregivers?")).json()
    r2 = (await _ask(client, sid, "When are invoices due?")).json()
    assert r1["answer"]["citations"][0]["document_id"] == handbook_id
    assert r2["answer"]["citations"][0]["document_id"] == contract_id


async def test_unscoped_chat_ignores_superseded_versions(client):
    old = (await upload(client, "policy.md", CONTRACT)).json()["document"]["id"]
    new = (await upload(client, "policy.md", CONTRACT.replace("30 days", "60 days"))).json()["document"]["id"]
    sid = (await client.post("/chat/sessions", json={}, headers=ALICE)).json()["id"]
    body = (await _ask(client, sid, "How many days notice to terminate the agreement?")).json()
    assert {c["document_id"] for c in body["answer"]["citations"]} == {new}
    assert "60 days" in body["answer"]["content"]

    pinned = (await client.post("/chat/sessions", json={"document_ids": [old]}, headers=ALICE)).json()["id"]
    body = (await _ask(client, pinned, "How many days notice to terminate the agreement?")).json()
    assert {c["document_id"] for c in body["answer"]["citations"]} == {old}


async def test_scoped_session_never_cites_other_documents(client):
    sid, contract_id, _ = await _setup(client, scoped=True)
    body = (await _ask(client, sid, "How many weeks of parental leave?")).json()
    assert all(c["document_id"] == contract_id for c in body["answer"]["citations"])


async def test_unanswerable_question_is_refused_without_followups(client):
    sid, *_ = await _setup(client)
    body = (await _ask(client, sid, "What is the capital of Mongolia?")).json()
    assert body["grounded"] is False
    assert "couldn't find" in body["answer"]["content"]
    assert body["answer"]["suggested_questions"] == []


async def test_session_with_no_ready_documents_short_circuits(client):
    sid = (await client.post("/chat/sessions", json={}, headers=ALICE)).json()["id"]
    body = (await _ask(client, sid, "anything?")).json()
    assert "ready" in body["answer"]["content"]
    assert body["usage"]["prompt_tokens"] == 0  # no LLM call made


async def test_history_is_persisted_and_paginated(client):
    sid, *_ = await _setup(client)
    for q in ["When are invoices due?", "What about late payments?", "How do I terminate?"]:
        await _ask(client, sid, q)
    session = (await client.get(f"/chat/sessions/{sid}", headers=ALICE)).json()
    assert session["message_count"] == 6
    assert session["title"] == "When are invoices due?"
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"] * 3

    page = (await client.get(f"/chat/sessions/{sid}/messages?limit=4", headers=ALICE)).json()
    assert len(page["items"]) == 4 and page["has_more"] is True
    oldest = page["items"][0]["created_at"]
    rest = (
        await client.get(f"/chat/sessions/{sid}/messages", params={"limit": 4, "before": oldest}, headers=ALICE)
    ).json()
    assert len(rest["items"]) == 2 and rest["has_more"] is False


async def test_followup_uses_history_for_rewrite(client, monkeypatch):
    captured = {}
    from app.services import chat as chat_service

    original = chat_service.hybrid_search

    async def spy(db, query, **kw):
        captured["query"] = query
        return await original(db, query, **kw)

    async def fake_rewrite(db, session, history, question):
        return f"{question} (invoices)" if history else question

    monkeypatch.setattr(chat_service, "hybrid_search", spy)
    monkeypatch.setattr(chat_service, "_rewrite", fake_rewrite)
    sid, *_ = await _setup(client)
    await _ask(client, sid, "When are invoices due?")
    body = (await _ask(client, sid, "and the interest?")).json()
    assert captured["query"] == "and the interest? (invoices)"
    assert body["answer"]["standalone_query"] == "and the interest? (invoices)"


async def test_streaming_emits_meta_tokens_done(client):
    sid, *_ = await _setup(client)
    async with client.stream(
        "POST", f"/chat/sessions/{sid}/messages", json={"question": "When are invoices due?", "stream": True}, headers=ALICE
    ) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in r.aiter_text()])
    events = [blk.split("\n", 1) for blk in raw.strip().split("\n\n")]
    names = [e[0].removeprefix("event: ") for e in events]
    assert names[0] == "meta" and names[-1] == "done" and "token" in names
    done = json.loads(events[-1][1].removeprefix("data: "))
    assert done["citations"] and done["grounded"] is True
    tokens = "".join(json.loads(e[1].removeprefix("data: "))["delta"] for e in events if e[0] == "event: token")
    assert "45 days" in tokens
    assert (await client.get(f"/chat/sessions/{sid}", headers=ALICE)).json()["message_count"] == 2


async def test_session_validation_and_isolation(client):
    r = await client.post(
        "/chat/sessions", json={"document_ids": ["00000000-0000-0000-0000-000000000000"]}, headers=ALICE
    )
    assert r.status_code == 404 and r.json()["error"]["details"]["missing"]
    sid, *_ = await _setup(client)
    assert (await _ask(client, sid, "hi", headers=BOB)).status_code == 404
    r = await client.post(f"/chat/sessions/{sid}/messages", json={"question": ""}, headers=ALICE)
    assert r.status_code == 422


async def test_chat_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_chat_per_min", 2)
    sid, *_ = await _setup(client)
    codes = [(await _ask(client, sid, "When are invoices due?")).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await _ask(client, sid, "again")
    assert r.json()["error"]["code"] == "rate_limited" and int(r.headers["retry-after"]) > 0

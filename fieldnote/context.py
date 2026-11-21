"""Two backends with the same evidence and CRUD contract."""

import asyncio
import json
import re
from datetime import timedelta
from time import perf_counter

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from .store import words

MEKO_URL = "https://mcp.mekodata.ai/mcp"
EVIDENCE_LIMIT = 6
SEARCH_LIMIT = 12
KINDS = {"fact", "preference", "decision"}


class ContextError(Exception):
    pass


class MemoryMissing(Exception):
    pass


def comparable(text):
    return re.findall(r"[^\W_]+", text.casefold())


def recall_trace(provider, hits, **details):
    # Keep the exact evidence snapshot offered to the responder for replay.
    seen = set()
    selected = []
    for hit in hits:
        key = hit["text"].strip().casefold()
        if key not in seen:
            seen.add(key)
            selected.append(hit | {"ref": len(selected) + 1})
        if len(selected) == EVIDENCE_LIMIT:
            break
    return dict(provider=provider, evidence=selected, **details)


def local_rank(query, rows, source):
    terms = words(query)
    if not terms:
        return []
    hits = []
    for row in rows:
        overlap = sorted(terms & words(row["text"]))
        if overlap:
            hits.append(row | {
                "source": source,
                "score": round(len(overlap) / len(terms), 3),
                "reason": "Matched query terms: " + ", ".join(overlap),
            })
    return sorted(hits, key=lambda row: row["score"], reverse=True)


class LocalContext:
    name = "local"

    def __init__(self, store):
        self.store = store

    def recall(self, session, query):
        started = perf_counter()
        memories = local_rank(query, self.store.memory_rows(), "memory")
        query_words = comparable(query)
        rows = [row for row in self.store.conversation_rows() if comparable(row["text"]) != query_words]
        conversations = local_rank(query, rows, "conversation")
        hits = sorted(memories + conversations, key=lambda hit: hit["score"], reverse=True)
        return recall_trace(self.name, hits, tool="SQLite term matching",
                            elapsed_ms=round((perf_counter() - started) * 1000),
                            buckets={"memory": len(memories), "conversation": len(conversations)})

    def list(self, session, query, page):
        rows = self.store.memory_rows()
        if query:
            rows = local_rank(query, rows, "memory")
        return rows[(page - 1) * 20:page * 20], max(1, (len(rows) + 19) // 20)

    def add(self, session, text, kind):
        return self.store.save_memory(session, text, kind)

    def delete(self, session, memory_id):
        if not self.store.delete_memory(memory_id):
            raise MemoryMissing("That memory is already gone.")

    def capture(self, session, turn_id, question, answer, trace):
        # The caller commits the turn and trace together in SQLite.
        return None

    def forget_session(self, session):
        # The caller removes the SQLite turns and session index.
        return None


def tool_payload(result, tool):
    payload = result.structuredContent
    if payload is None:
        for block in result.content:
            if isinstance(block, TextContent):
                try:
                    payload = json.loads(block.text)
                except ValueError:
                    continue
                break
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    if isinstance(payload, dict):
        error = payload.get("error") or payload.get("detail")
        if isinstance(error, dict):
            error = error.get("code") or error.get("error")
        if not result.isError and not error and payload.get("success") is not False:
            return payload
        if isinstance(error, str) and re.fullmatch(r"[\w-]{1,80}", error):
            raise ContextError(f"Meko {tool} returned {error}.")
    raise ContextError(f"Meko {tool} failed or returned an unexpected response.")


class MekoClient:
    def __init__(self, key, datapack, endpoint=MEKO_URL):
        self.headers = {"Authorization": f"Bearer {key}", "User-Agent": "fieldnote/1.0"}
        self.datapack = datapack
        self.endpoint = endpoint

    def call(self, tool, **arguments):
        try:
            return asyncio.run(asyncio.wait_for(self._call(tool, arguments), timeout=22))
        except ContextError:
            raise
        except Exception as exc:
            # The SDK can wrap HTTP transport errors in an exception group.
            pending = [exc]
            while pending:
                cause = pending.pop()
                pending.extend(getattr(cause, "exceptions", ()))
                if isinstance(cause, httpx.HTTPStatusError):
                    raise ContextError(
                        f"Meko returned HTTP {cause.response.status_code} for {tool}. "
                        "Check the key, datapack access, and quota."
                    ) from None
            raise ContextError(f"Meko could not complete {tool}. Check its connection and try again.") from None

    async def _call(self, tool, arguments):
        async with httpx.AsyncClient(headers=self.headers, timeout=18) as http:
            async with streamable_http_client(self.endpoint, http_client=http) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=16)) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, arguments | {"datapack_id": self.datapack})
        return tool_payload(result, tool)


def rows_from(payload, *keys):
    for key in keys:
        if isinstance(payload.get(key), list):
            return [row for row in payload[key] if isinstance(row, dict)]
    return None


def pick_text(row, *keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def cloud_hit(row, source, reason):
    text_keys = {
        "memory": ("memory", "text", "content"),
        "knowledge": ("chunk_text", "text", "content"),
        "conversation": ("input", "user_message", "text", "content", "snippet", "preview", "output"),
    }
    # Some conversation hits carry the verbatim exchange inside a message object.
    exchange = row.get("message")
    if isinstance(exchange, dict):
        row = exchange | row
    text = pick_text(row, *text_keys[source])
    if not text:
        return None
    metadata = row.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    kind = metadata.get("kind")
    return {
        "id": row.get("id") or row.get("memory_id") or row.get("message_id"),
        "text": text,
        "source": source,
        "kind": kind if isinstance(kind, str) and kind in KINDS else (
            "fact" if source == "memory" else source
        ),
        "agent": row.get("agent_id"),
        "created": row.get("created_at") or row.get("created"),
        "session_id": row.get("meko_conversation_id") or row.get("conversation_id"),
        "session_title": pick_text(row, "conversation_title", "title") or metadata.get("session_title"),
        "score": row.get("score"),
        "cosine": row.get("raw_similarity_score"),
        "distance": row.get("distance"),
        "reason": reason,
    }


class MekoContext:
    name = "meko"

    def __init__(self, store, client):
        self.store = store
        self.client = client

    def ensure_conversation(self, session):
        if not session["remote_id"]:
            payload = self.client.call(
                "conversation_create", agent_id=session["agent"],
                title=session["title"], session_id=session["id"],
            )
            remote_id = pick_text(payload, "id", "conversation_id")
            if not remote_id:
                raise ContextError("Meko did not return a conversation ID.")
            self.store.set_remote_id(session["id"], remote_id)
            session["remote_id"] = remote_id
        return session["remote_id"]

    def session_tool(self, tool, session, **arguments):
        return self.client.call(tool, conversation_id=self.ensure_conversation(session),
                                agent_id=session["agent"], **arguments)

    def recall(self, session, query):
        started = perf_counter()
        response = self.session_tool("context_search", session, query=query, limit=SEARCH_LIMIT)
        buckets = response.get("results")
        if not isinstance(buckets, dict):
            raise ContextError("Meko context_search did not return source buckets.")
        memory_rows = rows_from(buckets, "memory")
        knowledge_rows = rows_from(buckets, "knowledge_base")
        conversation = buckets.get("conversation")
        conversation_rows = (rows_from(conversation, "hits", "thread")
                             if isinstance(conversation, dict) else rows_from(buckets, "conversation"))
        memory_rows = [row for row in memory_rows or [] if self.relevant_memory(query, row)]
        tools = ["context_search"]
        warnings = []
        if not memory_rows:
            tools.append("memory_search")
            try:
                verified = self.session_tool("memory_search", session, query=query, limit=SEARCH_LIMIT)
                memory_rows = rows_from(verified, "results")
                if memory_rows is None:
                    raise ContextError("Meko did not return memory search results.")
            except ContextError as exc:
                warnings.append(str(exc))
                memory_rows = None
        if knowledge_rows is None or conversation_rows is None:
            warnings.append("Meko did not return results for every source. Check the source counts.")
        hits = []
        query_words = comparable(query)
        for source, rows, reason, limit in (
            ("memory", memory_rows, "Meko ranked this memory with hybrid retrieval.", 3),
            ("conversation", conversation_rows, "Meko matched this turn in the datapack’s conversation cache.", 2),
            ("knowledge", knowledge_rows, "Meko matched this chunk in shared datapack knowledge.", 1),
        ):
            accepted = 0
            for row in rows or []:
                hit = cloud_hit(row, source, reason)
                if hit and (source != "conversation" or comparable(hit["text"]) != query_words):
                    matches = sorted(words(query) & words(hit["text"]))
                    if matches:
                        hit["reason"] += " Shared terms: " + ", ".join(matches[:8]) + "."
                    hits.append(hit)
                    accepted += 1
                    if accepted >= limit:
                        break
        return recall_trace(
            self.name, hits, tool=" + ".join(tools), elapsed_ms=round((perf_counter() - started) * 1000),
            buckets={name: len(rows) if rows is not None else None for name, rows in (
                ("memory", memory_rows), ("conversation", conversation_rows), ("knowledge", knowledge_rows)
            )},
            match_quality=response.get("match_quality"), hint=response.get("agent_hint"),
            conversation_id=session["remote_id"], warnings=warnings,
        )

    @staticmethod
    def relevant_memory(query, row):
        cosine = row.get("raw_similarity_score")
        return bool(words(query) & words(pick_text(row, "memory", "text"))) or (
            isinstance(cosine, (int, float)) and cosine >= 0.2
        )

    def list(self, session, query, page):
        if query:
            payload = self.session_tool("memory_search", session, query=query, limit=20)
            pages = 1
        else:
            payload = self.session_tool("memory_get_all", session, page=page, page_size=20)
            pagination = payload.get("pagination")
            pages = payload.get("total_pages") or (
                pagination.get("total_pages") if isinstance(pagination, dict) else None
            ) or 1
        rows = rows_from(payload, "results", "memories")
        if rows is None:
            raise ContextError("Meko did not return a memory list.")
        return [hit for row in rows if (hit := cloud_hit(row, "memory", "Meko memory"))], pages

    def add(self, session, text, kind):
        payload = self.session_tool("memory_add", session, text=text,
                                    metadata={"kind": kind, "session_title": session["title"],
                                              "fieldnote_writer": session["agent"]})
        rows = rows_from(payload, "results", "memories")
        memory = cloud_hit(rows[0], "memory", "Saved in Meko") if rows else None
        if not memory or not memory["id"]:
            raise ContextError("Meko did not return the saved memory ID. Refresh the library before retrying.")
        return memory

    def delete(self, session, memory_id):
        payload = self.session_tool("memory_get_by_id", session, memory_id=memory_id)
        rows = rows_from(payload, "results", "memories")
        row = rows[0] if rows else payload.get("memory")
        if not isinstance(row, dict):
            row = payload
        writer = pick_text(row, "agent_id")
        if not writer and isinstance(row.get("metadata"), dict):
            writer = pick_text(row["metadata"], "fieldnote_writer")
        if not writer:
            raise ContextError("Meko did not identify this memory’s writer. Delete it from the Meko portal.")
        conversation_id = pick_text(row, "meko_conversation_id", "conversation_id")
        self.client.call("memory_delete_by_id", memory_id=memory_id,
                         conversation_id=conversation_id or self.ensure_conversation(session), agent_id=writer)

    def capture(self, session, turn_id, question, answer, trace):
        remote = self.ensure_conversation(session)
        plan = [f"Retrieve context with {trace['tool']}.",
                f"Offer {len(trace['evidence'])} evidence items to {trace['responder']}."]
        return self.client.call(
            "conversation_add_message", conversation_id=remote, agent_id=session["agent"],
            input=question, output=answer, seed=f"{remote}:{turn_id}", plan=plan,
            metadata={"fieldnote": {"evidence": [
                {key: hit.get(key) for key in ("ref", "id", "source", "score", "reason")}
                for hit in trace["evidence"]
            ], "elapsed_ms": trace.get("elapsed_ms")}},
        )

    def forget_session(self, session):
        if session["remote_id"]:
            self.client.call("conversation_delete", conversation_id=session["remote_id"],
                             agent_id=session["agent"])

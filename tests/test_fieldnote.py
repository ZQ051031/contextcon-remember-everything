import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from uuid import uuid4

from fieldnote.context import MekoClient
from fieldnote.web import create_app


def workspace(folder, key="", datapack=""):
    return patch.dict(os.environ, {
        "FIELDNOTE_DATA_DIR": folder, "MEKO_API_KEY": key,
        "MEKO_DATAPACK_ID": datapack, "OLLAMA_MODEL": "",
    })


def session(client, title, agent="fieldnote_planner"):
    return client.post("/api/sessions", json={"title": title, "agent": agent}).json["id"]


def base(session_id):
    return f"/api/sessions/{session_id}"


class LocalFlow(unittest.TestCase):
    def test_restarts_recall_and_deletion(self):
        with tempfile.TemporaryDirectory() as folder, workspace(folder):
            client = create_app().test_client()
            planning = base(session(client, "Planning"))
            memory = client.post(planning + "/memories", json={
                "text": "The Aurora catering code is Juniper.", "kind": "fact",
            }).json
            client.post(planning + "/memories", json={
                "text": "I prefer one-page Aurora catering briefs.", "kind": "preference",
            })
            prior = client.post(planning + "/turns", json={
                "question": "Aurora catering decision: No plastic cutlery.",
            })
            self.assertEqual(prior.status_code, 201)

            # New application instance opens the same persisted SQLite file.
            client = create_app().test_client()
            followup = base(session(client, "Follow-up", "fieldnote_writer"))
            question = "What was the Aurora catering code?"
            result = client.post(followup + "/turns", json={"question": question}).json
            self.assertIn("Juniper", result["answer"])
            self.assertIn("plastic cutlery", result["answer"])
            self.assertEqual({"memory", "conversation"}, {
                hit["source"] for hit in result["trace"]["evidence"]
            })
            self.assertTrue(all("Matched query terms:" in hit["reason"]
                                for hit in result["trace"]["evidence"]))
            preview = client.post(followup + "/turns", json={"question": question, "recall": False}).json
            self.assertEqual(preview["trace"]["evidence"], [])
            self.assertEqual(preview["captured"], 0)
            found = client.get(followup + "/memories?q=juniper").json["memories"]
            self.assertEqual([memory["id"]], [item["id"] for item in found])
            self.assertEqual(client.delete(followup + "/memories/" + memory["id"], json={}).status_code, 204)
            self.assertEqual(client.get(followup + "/memories?q=juniper").json["memories"], [])

            client.delete(planning, json={})
            new = base(session(client, "New"))
            repeat = client.post(new + "/turns", json={"question": "WHAT was the Aurora Catering code!"}).json
            self.assertNotIn("Juniper", repeat["answer"])
            self.assertNotIn("What was the Aurora catering code?", repeat["answer"])
            self.assertNotIn("plastic cutlery", repeat["answer"])
            self.assertIn("one-page", repeat["answer"])
            self.assertEqual(client.get(planning + "/turns").status_code, 404)
            self.assertIn("Juniper", client.get(followup + "/turns").json[0]["answer"])

    def test_validation_and_static_assets(self):
        with tempfile.TemporaryDirectory() as folder, workspace(folder):
            client = create_app().test_client()
            self.assertEqual(client.get("/").status_code, 200)
            with client.get("/static/fieldnote.png") as icon:
                self.assertEqual(icon.status_code, 200)
            self.assertEqual(client.post("/api/sessions", json={"agent": "invalid"}).status_code, 400)
            self.assertEqual(client.post("/api/sessions", data="{}").status_code, 415)
            uri = base(session(client, "Session"))
            self.assertEqual(client.get(uri + "/memories?page=no").status_code, 400)
            self.assertEqual(client.post(uri + "/turns", json={"question": " ", "recall": True}).status_code, 400)
            self.assertEqual(client.post(uri + "/turns", json={"question": "Hi", "recall": "off"}).status_code, 400)


class MekoFixture:
    """A loopback MCP server implementing the documented shapes used by Fieldnote."""

    TOOLS = (
        "conversation_create", "conversation_add_message", "conversation_delete", "context_search",
        "memory_add", "memory_get_all", "memory_search", "memory_get_by_id", "memory_delete_by_id",
    )

    def __init__(self):
        self.calls = []
        self.memories = {}
        self.turns = []
        self.fail = None

    def tool(self, name, args, headers):
        self.calls.append((name, args, headers.get("Authorization"), headers.get("User-Agent")))
        if name == self.fail:
            return {"error": "datapack_access_denied"}
        if name == "conversation_create":
            return {"id": uuid4().hex}
        if name == "memory_add":
            memory = {"id": uuid4().hex, "memory": args["text"], "agent_id": args["agent_id"],
                      "meko_conversation_id": args["conversation_id"], "metadata": args["metadata"]}
            self.memories[memory["id"]] = memory
            return {"results": [memory]}
        if name == "memory_get_all":
            return {"results": list(self.memories.values()), "total_pages": 1}
        if name == "memory_search":
            return {"results": [m for m in self.memories.values()
                                if args["query"].casefold() in m["memory"].casefold()]}
        if name == "memory_get_by_id":
            return {"results": [self.memories[args["memory_id"]]]}
        if name == "memory_delete_by_id":
            if self.memories[args["memory_id"]]["agent_id"] != args["agent_id"]:
                return {"error": "agent_id_mismatch"}
            del self.memories[args["memory_id"]]
            return {"success": True}
        if name == "conversation_add_message":
            self.turns.append({"id": uuid4().hex, "input": args["input"],
                               "conversation_id": args["conversation_id"], "agent_id": args["agent_id"]})
            return {"success": True}
        if name == "context_search":
            return {"results": {
                "memory": [m | {"score": 0.92, "raw_similarity_score": 0.7}
                           for m in self.memories.values()],
                "knowledge_base": [],
                "conversation": {"hits": list(self.turns), "thread": []},
            }, "match_quality": "strong", "agent_hint": "Use retrieved context."}
        if name == "conversation_delete":
            self.turns = [turn for turn in self.turns if turn["conversation_id"] != args["conversation_id"]]
            return {"success": True}
        return {"error": "tool_not_found"}

    @contextmanager
    def server(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = message["method"]
                params = message.get("params", {})
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                              "serverInfo": {"name": "Meko test double", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                        for name in fixture.TOOLS]}
                elif method == "tools/call":
                    payload = fixture.tool(params["name"], params["arguments"], self.headers)
                    result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
                    if params["name"] != "conversation_create":
                        result["structuredContent"] = payload
                else:
                    raise AssertionError(f"Unexpected method {method}")
                body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
            thread = Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                yield f"http://127.0.0.1:{httpd.server_port}/mcp"
            finally:
                httpd.shutdown()
                thread.join()


class MekoFlow(unittest.TestCase):
    def test_cloud_tool_contract_and_failure(self):
        fixture = MekoFixture()
        with tempfile.TemporaryDirectory() as folder, fixture.server() as endpoint:
            with workspace(folder, "test-key", "test-pack"), patch(
                "fieldnote.web.MekoClient", side_effect=lambda key, pack: MekoClient(key, pack, endpoint)
            ):
                client = create_app().test_client()
            self.assertEqual(client.get("/api/workspace").json["provider"], "meko")
            planning = base(session(client, "Planning"))
            created = client.post(planning + "/memories", json={
                "text": "The Atlas kickoff is June 12.", "kind": "fact",
            })
            self.assertEqual(created.status_code, 201, created.json)
            first_id = created.json["id"]
            client.post(planning + "/turns", json={"question": "Atlas pilot: Hana owns the feedback."})

            writing = base(session(client, "Follow-up", "fieldnote_writer"))
            recall = client.post(writing + "/turns", json={"question": "What is the Atlas kickoff plan?"})
            self.assertEqual(recall.status_code, 201, recall.json)
            turn = recall.json
            self.assertEqual(turn["trace"]["capture"], "conversation_add_message")
            self.assertEqual(turn["trace"]["match_quality"], "strong")
            self.assertEqual([h["source"] for h in turn["trace"]["evidence"]], ["memory", "conversation"])
            self.assertEqual(turn["trace"]["evidence"][0]["score"], 0.92)
            self.assertEqual(turn["trace"]["evidence"][0]["cosine"], 0.7)
            self.assertIn("Hana", turn["answer"])
            captured = [args for tool, args, *_ in fixture.calls if tool == "conversation_add_message"]
            self.assertEqual(captured[1]["agent_id"], "fieldnote_writer")
            self.assertEqual(captured[1]["seed"], f"{turn['trace']['conversation_id']}:{turn['id']}")
            self.assertEqual(len(captured[1]["metadata"]["fieldnote"]["evidence"]), 2)
            self.assertNotEqual(captured[0]["conversation_id"], captured[1]["conversation_id"])

            listed = client.get(writing + "/memories").json
            self.assertEqual(listed["memories"][0]["id"], first_id)
            self.assertEqual(client.get(writing + "/memories?q=June").json["memories"][0]["id"], first_id)
            self.assertEqual(client.delete(writing + "/memories/" + first_id, json={}).status_code, 204)
            self.assertEqual(fixture.calls[-1][1]["agent_id"], "fieldnote_planner")
            self.assertEqual(client.delete(planning, json={}).status_code, 204)

            fixture.fail = "conversation_add_message"
            saved = client.post(writing + "/turns", json={"question": "Anything new?"})
            self.assertEqual(saved.status_code, 201, saved.json)
            self.assertEqual(saved.json["trace"]["capture"], "Local transcript only")
            self.assertIn("datapack_access_denied", saved.json["trace"]["warnings"][0])

            fixture.fail = "context_search"
            failed = client.post(writing + "/turns", json={"question": "Who owns the feedback?"})
            self.assertEqual(failed.status_code, 502)
            self.assertIn("datapack_access_denied", failed.json["error"])
            self.assertEqual(len(client.get(writing + "/turns").json), 2)
            self.assertTrue(all(auth == "Bearer test-key" and user_agent == "fieldnote/1.0"
                                and args["datapack_id"] == "test-pack"
                                for _, args, auth, user_agent in fixture.calls))

            with workspace(folder):
                local = create_app().test_client()
            self.assertEqual(local.get("/api/workspace").json["sessions"], [])


if __name__ == "__main__":
    unittest.main()

# Fieldnote

Fieldnote is a long-term memory workspace for two assistants, Planner and Brief writer. Record facts, preferences, and project decisions in one session, then pick up the work in another. Each answer links to the evidence it received. The recall inspector shows the query’s sources, ranking signals, retrieval time, and capture status. The memory library lets you add, browse, search, and delete durable memories.

## ContextCon: Remember Everything

Fieldnote targets the **Show Your Work** challenge at Yugabyte’s ContextCon on HackerEarth. The event lists a **$10,000** prize pool, a **$4,000** grand prize, and **30%** of judging for use of Meko.

**Submissions close Sunday, January 17, 2027, at 11:59 p.m. Pacific Standard Time (PST, UTC−08:00).** This is Monday, January 18, 2027, at **07:59 UTC**. The dedicated event page labels this deadline “Submissions due,” and its countdown uses `2027-01-18T07:59:00Z`. The separate HackerEarth registration listing reports an earlier event end, January 17 at 17:30 UTC. [Official submission schedule](https://www.hackerearth.com/lp/challenges/contextcon) · [HackerEarth registration listing](https://www.hackerearth.com/community/challenges/hackathon/contextcon-remember-everything/)

## Run locally

Requires Python 3.10 or newer. Local mode works with blank configuration and needs no account, API key, or model download. After installing dependencies, the workspace runs offline.

```sh
git clone https://github.com/ZQ051031/contextcon-remember-everything.git
cd contextcon-remember-everything
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. Open **http://127.0.0.1:8000**. Fieldnote is a single-user app bound to the loopback interface. It stores data under `data/` by default. Set `FIELDNOTE_DATA_DIR` to use a new directory for a clean demo or a separate workspace.

### Demo the Atlas handoff

1. Choose **Try the demo → Load Atlas demo**. This creates **Atlas planning** under Planner, saves a June 12 kickoff and a one-page brief preference, and captures a chat recording the pilot cap and feedback owner.
2. Fieldnote opens **Atlas follow-up** under Brief writer with a blank transcript and a draft question. Click **Send**. Expect the kickoff date and preferred brief format from durable memory, plus the pilot cap of twelve and Hana’s Friday feedback from the past turn. Meko indexes conversation evidence asynchronously, so the past-turn citation may take longer to appear in Meko mode.
3. Click a citation to inspect its source, matching terms or Meko ranking signal, and source session when available. In Meko mode, open **Meko conversation ID** to cross-reference the conversation trace in the Meko Observe hub.
4. Click **Compare with recall off**. The same question returns zero evidence. The comparison stays in the visible chat and is excluded from future retrieval.
5. Reload the app or restart the server. Reopen the planning session to inspect its saved transcript. In **Memory library**, search for `Atlas`, add a unique sample preference, search for it, then delete it. In a fresh session, ask about that preference to check that it no longer appears.

## Connect your Meko datapack

1. In your existing Meko workspace, use a dedicated datapack where you have write access. Copy its UUID and ensure conversation search is enabled if you want to demo past-turn recall. Using a dedicated datapack also keeps the synthetic demo separate from other conversations.
2. Use an existing personal API key, or generate one in the Meko portal under **Settings → API Keys → Generate API key**.
3. Copy `.env.example` to `.env` and fill in these values.

   ```dotenv
   MEKO_API_KEY=mko_tkn_your_key_here
   MEKO_DATAPACK_ID=your-datapack-uuid
   ```

4. Restart `python app.py`. The sidebar shows **Meko MCP** when configured. Send a question or list memories to make the first authenticated call and verify access. The server sends the key in the `Authorization: Bearer` header and includes a `User-Agent`. The key stays on the server. If an operation fails, Fieldnote shows the failure and keeps the Meko and local workspaces separate.

Use a blank `MEKO_API_KEY` to return to the SQLite workspace. The visible Meko session index is stored locally for the configured credential and datapack. A new key or datapack opens a new local session index, and existing cloud memories remain available when the key has access to the same datapack. [Meko API keys](https://docs.mekodata.ai/use-meko/api-keys/) · [Connect programmatically](https://docs.mekodata.ai/use-meko/connect/connect-programmatically/)

## Architecture

```text
Browser → Flask workspace → Context provider → bounded evidence → responder
                                    │                                  │
                                    ├─ Meko datapack via MCP            │
                                    └─ SQLite in keyless mode           │
                                                                       ↓
                            Recall inspector ← saved transcript + evidence trace
```

| App operation | Meko tools and code path |
| --- | --- |
| Open a session and attribute activity | [`MekoContext.ensure_conversation`](fieldnote/context.py) lazily calls `conversation_create` and stores its returned ID. Planner and Brief writer use separate `agent_id` values and the same configured datapack. |
| Recall before answering | [`MekoContext.recall`](fieldnote/context.py) calls `context_search` against memory, conversation, and shared knowledge. When the memory bucket yields no eligible results, it verifies with `memory_search`. It preserves Meko’s returned scores and match quality where present, filters weak ungrounded memory hits and repeated copies of the active question, and limits the answer input to six unique items. |
| Add, view, search, and delete | [`MekoContext`](fieldnote/context.py) calls `memory_add`, paginated `memory_get_all`, and `memory_search`. For deletion it resolves the exact record with `memory_get_by_id` and calls `memory_delete_by_id` with the original writer. |
| Learn from the exchange and audit it | [`add_turn`](fieldnote/web.py) saves the question, answer, and evidence snapshot for replay in the workspace. [`MekoContext.capture`](fieldnote/context.py) writes the full turn with `conversation_add_message`, a stable per-turn seed, and citation metadata. Meko can extract durable memories from captured user turns asynchronously. |
| Transport | [`MekoClient`](fieldnote/context.py) uses the official Python **Model Context Protocol** SDK, via Streamable HTTP at `https://mcp.mekodata.ai/mcp`. Meko documents this as its programmatic integration. |
| Keyless mode | [`LocalContext`](fieldnote/context.py) ranks saved memories and past user turns by matching query terms, with [SQLite storage](fieldnote/store.py). The Recall inspector labels the local score and lists the actual matched terms. |

The keyless **Evidence brief** responder quotes relevant evidence and links each item to the inspector. For natural-language synthesis, use an already installed local Ollama model. Put its exact tag from `ollama list` in `OLLAMA_MODEL` and restart with Ollama running at `127.0.0.1:11434`. [The responder](fieldnote/respond.py) passes only the current question and the bounded evidence to Ollama. A model failure falls back to the evidence brief.

A Meko conversation hit can come from another member’s conversation in the same datapack. Deleting a memory removes the durable entry. Original chats and past trace snapshots can still contain the same information. **Delete chat** removes the selected conversation and its local traces, while separately saved or extracted memories remain. A zero source count means the tool returned no usable candidates for that bucket on that turn. [Meko MCP tool reference](https://docs.mekodata.ai/reference/mcp-server/) · [Yugabyte’s MCP tool cookbook](https://github.com/yugabyte/meko-skills/blob/main/skills/meko-mcp-tools/references/tools-cookbook.md)

## Tests

```sh
python -m unittest discover -s tests -v
```

The suite tests SQLite persistence, recall, deletion, and the Meko MCP adapter over loopback HTTP using a test server. The Atlas and memory library flows were also exercised locally in Chromium with no key. An authenticated Meko Cloud run requires your key and datapack.

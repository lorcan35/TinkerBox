---
audience: integrator
type: reference
prerequisites: [WebSocket protocol](../protocol.md), [REST API reference](rest-api.md)
last-verified: 2026-05-29
---
# Built-in tools catalog

This is the authoritative list of the tools Dragon's LLM can call during a turn,
their argument schemas, and the empty-reply wrap that keeps a turn from going
silent when a function-calling model fires a tool and then stops talking.

A tool is a Python class in `dragon_voice/tools/` that subclasses `Tool`
(`dragon_voice/tools/base.py`) and declares four things: a `name` (the string the
model emits), a `description` (injected into the system prompt), a
`parameters_schema` (JSON Schema for its args), and an async `execute(args)`.
All tools register on the single `ToolRegistry`
(`dragon_voice/tools/registry.py`), and every invocation — voice, REST, or
dashboard — funnels through `ToolRegistry.execute`, which is also where the
`agent_log` activity feed is populated.

For how a tool call travels over the wire, see the
[`tool_call` / `tool_result` protocol messages](../protocol.md#121-tool_call-dragon---tab5).
To add your own tool, follow the [adding-a-tool how-to](../adding-a-tool.md). To
list the live registry on a running Dragon, call `GET /api/v1/tools` (see the
[REST API reference](rest-api.md)).

## How tools are invoked

The LLM emits an XML marker; Dragon parses it, executes the named tool, injects
the result back into the context, and lets the model continue. The parser
(`dragon_voice/tools/parser.py`) accepts three dialects so models trained on
different conventions all work:

| Dialect | Shape | Emitted by |
|---|---|---|
| Legacy (TinkerBox standard) | `<tool>NAME</tool><args>{json}</args>` | ministral, gemma3 — the prompt format Dragon ships. Tolerates xLAM bracket quirks (`[tool>`, `<tool]`, `[tool]`) |
| Standard | `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` | industry FC fine-tunes (Qwen-FC, Gemma-FC, distil-*) regardless of system prompt |
| Bracketed-name | `[NAME]{json}</NAME>` or `[NAME]IDENT()` | xLAM quirk (#82). Gated on `NAME` being a registered tool so prose like `[note]` in chat does not false-fire |

The native `tools=[...]` API path (used by llama-server backends that support it)
renders the whole registry as OpenAI-format function schemas via
`ToolRegistry.openai_tools()` instead of prose-listing them. Either way the
limits below apply.

| Constraint | Value | Where |
|---|---|---|
| Max tool calls per turn | 3 (loop guard) | ConversationEngine |
| System-prompt format for small models | compact XML (priority tools only) | `formatter.format_for_llm(compact=True)` |
| Compact-prompt priority threshold | `Tool.priority < 50` opts in | `dragon_voice/tools/base.py` |

## Always-on tools

These register at startup and are available in every turn (voice mode 0/1/2 — in
mode 3 the TinkerClaw gateway owns tool execution and Dragon's registry is
bypassed). They depend only on Dragon-local services (SearXNG, Ollama
embeddings, the surface manager, the scheduler), never on an external account.
Registration lives in `dragon_voice/lifecycle/agentic_init.py` (the 10 core
tools), `notes_init.py` (`note`), `surfaces_scheduler_init.py`
(`timesense_timer`, `quick_poll`, `schedule_reminder`).

| Tool name | Description | Required args | Optional args | Source file |
|---|---|---|---|---|
| `web_search` | Search the web for current information, news, facts, or answers | `query` (string) | `max_results` (integer, default 3) | `web_search.py` |
| `datetime` | Get the current date, time, and day of the week | — | — | `datetime_tool.py` |
| `remember` | Save a fact or preference about the user for future conversations | `fact` (string) | — | `memory_tools.py` |
| `recall` | Search your memory for relevant information about the user or past conversations | `query` (string) | `limit` (integer, default 5) | `memory_tools.py` |
| `forget_fact` | Forget a remembered fact. Two-step: search first, then confirm | — | `query` (string), `fact_id` (string), `confirm` (boolean) | `memory_tools.py` |
| `calculator` | Calculate mathematical expressions accurately | `expression` (string) | — | `calculator_tool.py` |
| `convert` | Convert between units (temperature, length, weight, volume, speed) | `value` (number), `from` (string), `to` (string) | — | `unit_converter_tool.py` |
| `weather` | Get current weather for a location | — | `location` (string), `latitude` (number), `longitude` (number) | `weather_tool.py` |
| `system_info` | Get info about the Dragon server (memory, CPU, uptime, connections) | — | — | `system_tool.py` |
| `stock_ticker` | Current price + daily change for a US stock or ETF ticker | `symbol` (string) | `currency` (string, default `USD`) | `stock_ticker_tool.py` |
| `note` | Take a quick note or create a reminder | `text` (string) | — | `note_tool.py` |
| `timesense_timer` | Start an AI-first focus timer; the orb becomes the timer | — | `minutes` (number, default 25, 1–120), `session_id` (string, internal) | `timesense_tool.py` |
| `quick_poll` | Ask a 2–3 button poll on the Tab5 screen and wait for the tap | `question` (string), `choices` (array, 2–3 items) | `timeout_s` (number, default 60, max 120) | `quick_poll_tool.py` |
| `schedule_reminder` | Schedule a reminder to fire later as a card in the user's chat | `when` (string), `message` (string) | `title` (string, default `Reminder`), `priority` (`normal` \| `important`) | `schedule_reminder_tool.py` |

Notes on the always-on set:

- **`web_search`** queries SearXNG (self-hosted on port 8888,
  google/bing/duckduckgo engines, up to 44 results, 10 s timeout). If SearXNG is
  down it falls back to DuckDuckGo. Snippets are truncated to 300 chars.
- **`remember` / `recall` / `forget_fact`** are backed by the
  `MemoryService` with Ollama `nomic-embed-text` embeddings (768-dim). `forget_fact`
  is a deliberate two-step: call with `query` to get a match plus
  `requires_confirm: true`, read it back to the user, then call again with
  `fact_id` + `confirm=true` to delete. It refuses to delete on a `fact_id`
  without `confirm=true`.
- **`timesense_timer`** is the agentic-path timer. The plain `timer` tool
  (`timer_tool.py`) is intentionally **not** registered in the agentic loop
  (D8/K7 dedup, 2026-04-20): the LLM kept picking `timer` on short phrases, which
  made the live-widget flow unreachable. `timer_tool.py` is retained for
  REST-only callers.
- **`quick_poll`, `timesense_timer`, `schedule_reminder`** emit UI over the voice
  WebSocket (`widget_prompt`, `widget_live`, `widget_card`). See the
  [widget protocol section](../protocol.md#17-live-widgets).
- **`schedule_reminder` `when`** accepts a relative duration (`5m`, `2h30m`,
  `1d`), an ISO 8601 timestamp (`2026-04-26T15:00:00-04:00`), or a natural phrase
  (`tomorrow at 3pm`). Bare times resolve in the server's local timezone.

## Integration tools (registered when the account is connected)

These register only when their integration package imports cleanly
(`dragon_voice/lifecycle/integrations_init.py`); each block is wrapped
independently so a broken Google import does not block the rest of the registry.
The tool *backend* connects lazily — the credentials come from the
[integrations connect flow](rest-api.md) (`POST /api/v1/integrations/connect`).
Every integration tool accepts an optional `account` arg (string) so a user with
multiple connected Google accounts can disambiguate.

### Google Calendar

| Tool name | Description | Required args | Optional args |
|---|---|---|---|
| `calendar_today` | List today's events from the primary calendar | — | `max_results` (integer, default 10), `account` |
| `calendar_week` | List events for the next 7 days | — | `max_results` (integer, default 20), `account` |
| `calendar_create` | Create an event (confirm details first — not undoable by voice) | `summary` (string), `start_iso` (string, ISO 8601 with tz), `end_iso` (string, ISO 8601 with tz) | `location` (string), `description` (string), `account` |
| `calendar_cancel` | Delete an event by its id | `event_id` (string) | `account` |

### Gmail

| Tool name | Description | Required args | Optional args |
|---|---|---|---|
| `gmail_unread` | List unread messages (from, subject, snippet, id) | — | `max_results` (integer, default 10, max 25), `account` |
| `gmail_search` | Search Gmail with Gmail's query syntax (`from:`, `subject:`, `has:attachment`, `newer_than:Nd`) | `query` (string) | `max_results` (integer, default 10, max 25), `account` |
| `gmail_read` | Fetch the full body of a message by id | `message_id` (string) | `account` |
| `gmail_send` | Send an email (confirm content first — not undoable) | `to` (string), `subject` (string), `body` (string) | `in_reply_to` (string), `cc` (string), `account` |
| `gmail_archive` | Archive a message (removes INBOX label; not deleted) | `message_id` (string) | `account` |

## The empty-reply wrap

Some function-calling-trained models (xLAM, distil-functiongemma, LFM2.5-FC)
emit a tool call and then stop, producing no conversational text. After
ConversationEngine strips the tool markup, the user-visible reply is empty even
though the tool fired and did real work. The empty-reply wrap
(`dragon_voice/tools/response_wrap.py`, #77) fills that gap with a templated
one-liner — **no extra LLM call**.

The wrap runs only when **both** are true:

1. At least one tool fired during the turn, **and**
2. The model's own final text was empty or stripped to empty.

"Stripped to empty" is judged by `looks_like_useful_text()` (#79 widened the
trigger from strict-empty to "no useful text"): any closing-tag remnant like
`</tool>` is treated as junk, and after stripping well-formed `<…>`/`[…]`/`{…}`
blocks the text must still carry at least 3 meaningful characters to count as a
real reply. A happy-path turn where the model wrote a real reply gets **no**
wrap — the model's text is always preferred.

`synthesize_wrap(calls)` is the entrypoint. It picks a per-tool template, dedups
repeated snippets (xLAM sometimes fires the same tool twice in a loop), caps the
output at 3 snippets, and falls back to a generic ack for any tool without a
template. It never raises and always returns a non-empty string.

### Per-tool wrap templates

| Tool name(s) | Example wrap output |
|---|---|
| `datetime` | `It's 14:30:00 (UTC).` |
| `calculator` | `15% of 230 = 34.5.` |
| `unit_converter` | `That's 100 celsius.` |
| `weather` | `In Dubai: sunny, 34°.` |
| `remember`, `store_fact` | `Got it — User is allergic to peanuts.` |
| `recall`, `recall_facts` | `<first fact>. (+2 more.)` |
| `forget`, `forget_fact` | `OK — forgotten.` |
| `web_search` | `Top hit: <title>.` |
| `system_info` | `Dragon: ram: 41%, cpu: 12%.` |
| `stock_ticker` | `AAPL is at 231.40.` |
| `timer` | `Timer running for 300 seconds.` |
| `timesense` | `Timer set for 1500 seconds.` |
| `quick_poll` | `Tap one.` |
| `note` | `Saved note: Buy groceries.` |
| any unlisted tool | `OK — ran <name>.` |

> The wrap table is keyed by tool name and ships its own aliases (e.g. both
> `remember` and `store_fact` route to the same template). A tool name that has
> no template falls through to `generic_wrap`, which lists the tools that ran.

## Examples

A full tool turn on the voice WebSocket — the model calls `web_search`, Dragon
runs it, and the model finishes with a real reply (no wrap needed):

```text
Client → Dragon: {"type":"text","content":"what's the weather in Tokyo?"}
Dragon → Tab5:   {"type":"tool_call","tool":"web_search","args":{"query":"weather Tokyo"}}
Dragon → Tab5:   {"type":"tool_result","tool":"web_search","result":{"results":[...]},"execution_ms":234}
Dragon → Tab5:   {"type":"llm","text":"It's "}
Dragon → Tab5:   {"type":"llm","text":"18°C and clear in Tokyo."}
Dragon → Tab5:   {"type":"llm_done","llm_ms":1200}
```

Execute a tool directly over REST, bypassing the LLM:

```bash
curl -s -X POST http://192.168.70.242:3502/api/v1/tools/datetime/execute \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
# → {"tool":"datetime","result":{"date":"2026-05-29","time":"14:30:00","day":"Friday",...},"execution_ms":1}
```

List the live registry to see exactly which tools (including connected
integrations) are registered on a running Dragon:

```bash
curl -s http://192.168.70.242:3502/api/v1/tools \
  -H "Authorization: Bearer $DRAGON_API_TOKEN" | python3 -m json.tool
# → [{"name":"web_search","description":"...","parameters":{...}}, ...]
```

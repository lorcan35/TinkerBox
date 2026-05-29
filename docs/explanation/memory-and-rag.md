---
audience: developer
type: explanation
prerequisites: [How the voice pipeline works](the-voice-pipeline.md), [Tools catalog](../reference/tools-catalog.md)
last-verified: 2026-05-29
---
# Memory and RAG — how it works and why

## The question

The Dragon runs small local models. A `ministral-3:3b` or an `LFM2.5-VL-1.6B`
has no idea who you are, what you told it yesterday, or what is in the PDF you
handed it last week — and it has neither the context window nor the recall to
hold all of that in the prompt. So how does Tinker remember that your dog is
named Biscuit, surface that fact unprompted three turns later, and answer a
question by quoting a document you ingested days ago?

The answer is two cooperating subsystems that live in
[`dragon_voice/memory.py`](../../dragon_voice/memory.py): a **fact store** (the
`remember` / `recall` tools) and a **document store** (chunked retrieval-augmented
generation, "RAG"). Both run entirely on the Dragon, both use the same local
embedding model, and both feed the same place: the system prompt that the
[ConversationEngine](architecture.md) builds *before every single LLM call*.

This page builds the mental model of how a fact or a document chunk becomes a
vector, how that vector gets searched, and why retrieval happens automatically on
every turn rather than only when the model asks for it.

## The model

### One embedding model, everywhere

Every piece of text that enters or queries the system — a stored fact, a document
chunk, and the user's current utterance — is turned into the *same shape* of
number: a 768-dimensional vector produced by **Ollama `nomic-embed-text`**. It
runs locally on the Dragon via Ollama on port 11434. No cloud API is involved,
which keeps memory on the same privacy footing as Local-mode inference.

Using one model for storage and for queries is the load-bearing decision. Cosine
similarity between two vectors is only meaningful if both vectors live in the
same embedding space. Embed the fact with `nomic-embed-text` and the query with
something else and the distances are noise. So the rule is absolute: facts,
chunks, and queries all go through `nomic-embed-text`, all come out 768-dim, and
all distances are cosine similarity in that one space.

### Two stores, three tables

The two subsystems are backed by three of the eleven tables in
[`schema.sql`](../../schema.sql):

| Table | Holds | Embedding lives in |
|-------|-------|--------------------|
| `memory_facts` | one short fact per row (the `remember` store) | the row |
| `memory_documents` | document metadata (title, source, ingest time) | — |
| `memory_chunks` | the 512-token slices of each document | the row, indexed by `sqlite-vec` |

Facts are atomic and self-contained — "the user's dog is named Biscuit" is one
row. Documents are large, so they are split into chunks first (see *Chunking*
below) and each chunk gets its own embedding and its own row in `memory_chunks`,
linked back to its parent in `memory_documents`. The document store uses the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension for vector search
over chunks; vectors are stored alongside the relational data in the same SQLite
file, so there is no separate vector database to operate.

### Two ways in, two ways out

A **fact** gets into `memory_facts` one of two ways:

- The LLM calls the **`remember`** tool mid-conversation
  (`<tool>remember</tool><args>{"fact":"..."}</args>`). This is the common path:
  the model decides something is worth keeping and stores it itself.
- A client `POST`s to **`/api/v1/memory`** with a fact body. This is the
  programmatic path — the dashboard's Memory tab and any integration use it.

A fact gets *out* the same two ways: the LLM calls **`recall`** to search facts
explicitly, or a client hits **`POST /api/v1/memory/search`** with a query. Both
embed the query, run cosine similarity against every stored fact, and return the
top matches ranked by score. The fact tools live in
[`dragon_voice/tools/memory_tools.py`](../../dragon_voice/tools/memory_tools.py)
(`StoreFactTool`, `RecallFactsTool`, and the confirm-gated `ForgetFactTool`).

A **document** gets in via **`POST /api/v1/documents`**, which chunks, embeds,
and stores in one call, and is searched via **`POST /api/v1/documents/search`**,
which returns ranked chunks. There is no LLM tool for ingesting a document — that
is deliberately a REST-only operation. (The full endpoint surface is in the
[REST API reference](../reference/rest-api.md).)

### The flow: an utterance becomes an augmented prompt

This is the piece that ties it all together. **Before every LLM call**, the
ConversationEngine does retrieval and injects what it finds into the system
prompt. The model never has to ask:

```
User utterance (text)
  |
  v
[Embed query] -- nomic-embed-text -> 768-dim vector (Ollama :11434)
  |
  +---------------------------+
  |                           |
  v                           v
[Cosine search           [Cosine search via sqlite-vec
 over memory_facts]       over memory_chunks]
  |                           |
  v                           v
top-k relevant facts     top-k relevant chunks
  |                           |
  +-------------+-------------+
                |
                v
[Inject into system prompt] -- facts + chunks prepended as context
                |
                v
[ConversationEngine builds full context]
   system prompt (+ memory + chunks)
   + session history (from MessageStore)
   + tool definitions
                |
                v
[LLM] -- generates the reply with your facts and documents in view
```

So when you ask "what should I get Biscuit for his birthday?", the query embeds,
the "dog is named Biscuit" fact scores high on cosine similarity, it lands in the
system prompt, and the model answers as if it always knew. The recall is
*automatic and silent* — the model does not call `recall`; the engine did it for
the model before the model ran.

### Chunking

Documents are too large to embed as one vector and too large to drop whole into a
small model's context. So ingestion splits each document into **512-token chunks
with a 50-token overlap** between consecutive chunks. Each chunk is embedded
independently and stored as its own row.

The overlap matters: a sentence that straddles a chunk boundary would otherwise
be cut in half, and neither half would embed to anything close to the full
thought. The 50-token overlap means any span of ≤50 tokens appears intact in at
least one chunk, so a query that matches that span still finds a coherent result.
Search returns whole chunks ranked by cosine similarity, and the top chunks — not
the whole document — are what get injected. A 40-page manual contributes only the
two or three paragraphs that actually answer the question, which is exactly what a
small-context local model needs.

## Why it's built this way

### Why retrieve on *every* turn instead of only when the model asks

The obvious alternative is tool-gated recall: give the model a `recall` tool and
let it decide when to look things up. The Dragon keeps the `recall` tool — but it
also does automatic retrieval on every turn, and that is the primary path. The
reason is the models. A 1.6–4 B local model on a Q6A is not reliable at deciding
*when* to recall; it forgets the tool exists, or fires it for the wrong query, or
burns a tool call it can only afford three of (turns are capped at three tool
calls to prevent loops). Automatic injection removes that decision from the model
entirely: the relevant facts are simply *there*, in the prompt, every time. The
model's job shrinks to "use what you can see," which even a small model does well.

The cost is a pair of embed-and-search operations on the critical path of every
turn. On the Q6A that is cheap relative to LLM generation — `nomic-embed-text` is
a small encoder and the SQLite vector search is in-process — so it does not move
the needle against the ~60–90 s a Local-mode turn already takes.

### Why one local embedding model and not a cloud embedder

Cloud embedding APIs are higher quality. They are also a network round-trip on
every turn, a dependency that breaks Local mode's "Tab5 + Dragon, nothing else"
guarantee, and a privacy leak — every fact and every query would cross the wire.
`nomic-embed-text` runs on the same Ollama instance that already exists on the
Dragon, costs nothing per call, and never sends your memory off the box. For a
privacy-first local assistant that is the right trade, and it is why Ollama stays
unmasked on the Dragon even when the LLM path moved to llama-server: embeddings
still need it.

### Why `sqlite-vec` and not a dedicated vector database

A standalone vector store (FAISS server, Qdrant, pgvector) would be one more
service to install, run, monitor, and keep alive across reboots on an ARM64 SBC.
`sqlite-vec` is a loadable SQLite extension: the chunk vectors live in the same
WAL-mode SQLite file as sessions, messages, and notes, searched with the same
`aiosqlite` connection layer in [`db.py`](../../dragon_voice/db.py). One database
file, one backup, one thing to operate. For the scale a single household device
sees — facts in the hundreds, documents in the dozens — the in-process search is
fast enough that a dedicated vector DB would be pure operational overhead with no
payoff.

### Why facts and documents are separate stores

They have different shapes and different lifecycles. A fact is small, atomic, and
written by the model in the middle of a conversation; it is cheap to embed inline
and store as a single row. A document is large, externally supplied, and must be
chunked before it can be embedded at all. Forcing both through one table would
mean either chunking facts (pointless — they are already one thought) or storing
documents un-chunked (impossible — they would not fit a small context and would
embed to mush). Two stores let each keep the representation that fits it, while
both share the one thing that has to be shared: the embedding space.

### What memory is *not* available for

Memory and RAG are part of the [ConversationEngine](architecture.md), and the
ConversationEngine is bypassed in **voice mode 3 (TinkerClaw)** — in that mode
Dragon is an audio pipe and the gateway owns intelligence, tools, and its own
memory. The Tab5-side-only **voice mode 5 (Solo)** does its own on-device RAG
against `/sdcard/rag.bin` and never touches the Dragon's stores at all. Memory as
described on this page is the Local / Hybrid / Cloud (modes 0/1/2) story, where
the Dragon's ConversationEngine runs the turn.

## See also

- [How the voice pipeline works](the-voice-pipeline.md) · [TinkerBox architecture](architecture.md)
- [REST API reference](../reference/rest-api.md) — the `/api/v1/memory` and `/api/v1/documents` endpoints · [Tools catalog](../reference/tools-catalog.md) — the `remember` / `recall` tools
- [Add a tool](../how-to/add-a-tool.md) · [Config reference](../reference/config-reference.md) — `MemoryConfig`

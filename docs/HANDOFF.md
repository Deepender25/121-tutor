# Handoff: Source-First Voice Research Partner

> Original research/validation handoff (Oct 9, 2026), kept for history and sources.
> **`DESIGN.md` supersedes this** where they differ (scope widened to tutor/explainer/brainstormer,
> facts re-verified 2026-10-10).

---

## 1. The idea in one paragraph

A **1:1, real-time voice conversation** about a document the user provides (PDF etc.). The **document is the anchor**: every answer starts from what the source says. **Live web search backs it up** — to verify a claim, check if the doc is outdated, or explain a concept the doc assumes. The assistant **says out loud which is which** ("Your PDF says the limit is 10%; a 2026 update raised it to 15%"). Runs **free**: local retrieval + free-tier cloud voice.

## 2. Why this is still worth building (competitive context)

- **ChatGPT voice (GPT-Live)** added file uploads + web search in voice (Aug 2026), but it's a general assistant that *can look at* a file — not doc-anchored — and real usage is paywalled (free = limited GPT-Live-1 mini).
- **NotebookLM Interactive Mode** is source-grounded voice, but it's a *podcast you interrupt* (two AI hosts), not an open 1:1 conversation, and it isn't built around live web search. Shares the free daily Audio Overview quota.
- **Gemini app "Talk Live about"** can discuss an uploaded file; consumer product, not customizable.
- **Open source:** NotebookLM clones (open-notebook, SurfSense) do text chat + generated podcasts, **no live 1:1 voice**. Voice frameworks (Pipecat, LiveKit Agents, Unmute) do live voice, **no doc grounding**. Nobody well-known combines both, free.

**Differentiators to protect:** doc-first behavior · spoken doc-vs-web attribution · free/unlimited · local retrieval · (optional niche) Hindi/English students studying their own material.

## 3. Decided architecture

```
Mic ──► Gemini Live API (audio in/out, turn-taking, reasoning)
            │  tool call: search_document(query)
            ├──────────► Local retrieval service
            │               EmbeddingGemma (local) + LanceDB hybrid search
            │               ◄── chunks with page numbers
            │  built-in Google Search grounding (free tier on Live models)
            ▼
Speaker ◄── spoken answer, attributes "your document" vs "the web"

Offline: PDF ──► parser ──► chunker ──► EmbeddingGemma ──► LanceDB (vectors + FTS index)
```

## 4. Stack (with reasons)

| Layer | Choice | Notes |
|---|---|---|
| Voice + LLM | Gemini Live API — `gemini-3.8-live` (or `gemini-3.1-flash-live-preview`) | Free of charge on free tier as of Oct 9, 2026. Audio-to-audio, low latency. Supports tool use. |
| Web search | Built-in Google Search grounding on the Live model | "Supported" on free tier for Live models. |
| Web search (fallback) | Custom `web_search` tool → Tavily (1,000 free credits/mo) or self-hosted SearXNG | Raw results give more control over attribution. Brave free tier gone (Feb 2026); Google Custom Search closed to new users, shuts down Jan 1, 2027. |
| Embeddings | EmbeddingGemma (`google/embeddinggemma-300m`) via sentence-transformers | 308M params, offline, 100+ languages incl. Hindi. 2K-token limit. 768d (MRL 512/256/128). |
| Vector DB | LanceDB (embedded) | Hybrid vector + FTS + SQL filters, no server. Chosen over Chroma (no native FTS). |
| PDF parsing | pymupdf4llm (start) or docling (tables/complex layouts) | Retrieval quality depends on this more than on the DB. |
| Language | Python | |

Reserve: Gemini Embedding 2 (API, multimodal); Qwen3-Embedding-0.6B. Voice fallback: Pipecat / LiveKit Agents cascade — Groq free tier (Whisper, Llama) + local TTS (Kokoro / Piper); a local Piper pipeline benchmarked ~974ms voice-to-voice.

## 5–7. Tools, prompt, ingestion

See `DESIGN.md` §3–§6 (expanded versions).

## 8. Known constraints & gotchas

- **Privacy:** retrieved chunks go to Gemini; free-tier content is used by Google to improve products.
- **Free-tier limits:** older table listed ~3 concurrent Live sessions. Limits are per project, not per key.
- **Pricing drift:** paid Gemini prices scheduled to double Jan 1, 2027; preview models can lose free access → voice layer behind an interface.
- **Session length:** needs session resumption / context compression.
- **Latency target:** <800ms–1s to first spoken word; web search adds 1–3s → filler phrase.
- **Echo/barge-in:** headphones for CLI; browser later gets WebRTC AEC (use ephemeral tokens).

## 12. Open decisions (from handoff)

- Target audience: general doc Q&A vs. niche (Hindi/English students on their own study material)? → **answered: personal 1:1 tutor/explainer/researcher/brainstormer.**
- Personal / open-source self-hosted vs. hosted app?
- Languages for v1?

## Sources (checked Oct 9, 2026)

- Gemini API pricing: https://ai.google.dev/gemini-api/docs/pricing
- Gemini API rate limits: https://ai.google.dev/gemini-api/docs/rate-limits
- Live API docs: https://ai.google.dev/gemini-api/docs/live-api
- EmbeddingGemma: https://ai.google.dev/gemma/docs/embeddinggemma
- LanceDB vs Chroma: https://aicoolies.com/comparisons/lancedb-vs-chromadb
- ChatGPT release notes (Aug 7, 2026): https://help.openai.com/en/articles/6825453-chatgpt-release-notes
- ChatGPT Voice limits: https://help.openai.com/en/articles/20001274-gpt-live-faq
- Free web search APIs 2026: https://parallel.ai/articles/best-free-web-search-api
- Groq free tier: https://localaimaster.com/blog/groq-api-free-guide
- Voice-loop latency benchmark: https://dev.to/todoforai/voiceloop-the-fastest-voice-agent-loop-in-the-browser-is-now-open-source-l36

Additional sources verified 2026-10-10: https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live ·
https://ai.google.dev/gemini-api/docs/live-api/tools · https://ai.google.dev/gemini-api/docs/live-api/session-management ·
https://ai.google.dev/gemini-api/docs/deprecations · https://huggingface.co/google/embeddinggemma-2 ·
https://docs.lancedb.com/search/hybrid-search · https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/api.html

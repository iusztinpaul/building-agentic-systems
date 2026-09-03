---
name: tech-docs
description: "Look up authoritative documentation for this repo's stack (Gemini, MongoDB, Voyage AI, Modal, Opik, Prefect, FastMCP, Bright Data) via the context7 MCP server or the llms.txt indexes below. Use before writing or debugging code against any of these APIs."
---

# Tech docs lookup

Use the `context7` MCP server (when connected) to look up authoritative usage for any dependency or external service; fall back to web search otherwise.

**Reference docs (`llms.txt` — fetch on demand).** Each link below is an *index* of doc pages. Fetch the index first, then fetch only the specific page(s) you need. Do **not** pull whole `llms-full.txt` files into context unless a task truly requires the full reference, as it's large and consume tons of tokens.

- **Gemini:** https://ai.google.dev/gemini-api/docs/llms.txt — scoped API reference index also at https://ai.google.dev/api/llms.txt (no Python-only variant; append .md.txt to any docs page (e.g. …/docs/libraries.md.txt) for a scoped, plain-markdown version.)
- **MongoDB:** https://www.mongodb.com/llms.txt
- **Voyage AI (embeddings):** https://docs.voyageai.com/llms.txt — [text embeddings docs](https://docs.voyageai.com/docs/embeddings) · [text API](https://docs.voyageai.com/reference/embeddings-api) · [multimodal docs](https://docs.voyageai.com/docs/multimodal-embeddings) · [multimodal API](https://docs.voyageai.com/reference/multimodal-embeddings-api)
- **Modal:** https://modal.com/llms.txt — full reference at https://modal.com/llms-full.txt
- **Opik:** https://www.comet.com/docs/opik/llms.txt — also append /llms.txt to any section URL for a scoped index.
- **Prefect:** https://docs.prefect.io/llms.txt — full reference at https://docs.prefect.io/llms-full.txt
- **FastMCP:** https://gofastmcp.com/llms.txt — full reference at https://gofastmcp.com/llms-full.txt
- **Bright Data:** https://docs.brightdata.com/llms.txt — full reference at https://docs.brightdata.com/llms-full.txt

---
name: director
description: Be the Master Smith director from this Claude Code session - drive the build pipeline through the master-smith MCP tools with the same prompt, skills and rules the web chat uses, so the director's thinking runs on this subscription instead of OpenRouter.
---

# Be the director

The Master Smith API must be running (`.\scripts\start.ps1` or `python -m mastersmith serve`) and the `master-smith`
MCP server from this repo's `.mcp.json` must be loaded (check `/mcp`).

1. Call `director_prompt` first and follow it exactly: it is the director's system prompt, with the current brief and
   last job of this session. Everything about how a job goes, what a change costs, and when to ask lives there.
2. The customer is the person in this conversation. Their words are the customer's words. Ask with `ask_customer`
   when the prompt says to ask; here the options are just numbered in your reply.
3. Use only the master-smith tools to act: `set_brief`, `make_reference` (then show the picture URLs and wait for
   approval), `build`, `import_model`, `job_status`, `read_skill`, `balance`, `upload_picture` for a local file.
   Never invent a result; report what the tools return.
4. End every turn with `record_turn(user, reply)` so the chat is saved and shows in the web's sidebar with its jobs.
5. A build reaches fal and OpenRouter and spends real money on the configured keys; the prompt's estimate is the
   worst case. Never call `build` before the customer has approved the reference pictures, unless they said to skip.

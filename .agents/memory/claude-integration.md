---
name: Claude integration in Telegram bot
description: How Claude (Anthropic) is wired into the Amazon affiliate Telegram bot
---

# Claude Integration

## Model choice
`claude-haiku-4-5` for all three functions — fast and cheap, adequate quality.

## Three functions (bot/claude_utils.py)
1. `extract_product_intent(text)` — returns product name or None (NONE sentinel)
2. `chat_response(text, history)` — conversational reply, max 8-message history per user
3. `price_advice(current_price, records)` — one-line 💡 buy recommendation, needs ≥3 records

## Credentials
Set via `setupReplitAIIntegrations({ providerSlug: "anthropic" })` in CodeExecution sandbox.
Env vars: `AI_INTEGRATIONS_ANTHROPIC_BASE_URL`, `AI_INTEGRATIONS_ANTHROPIC_API_KEY`.
Client instantiated lazily in `_get_client()` — do not import at module level.

**Why:** Lazy init avoids startup crash if env vars are missing.

## Bot flow (bot/bot.py)
- `handle_text` (was `handle_unknown`): calls `extract_product_intent` → if product found → `search_amazon_by_keywords`; else → `chat_response` with per-user history.
- `handle_link`: after price+history, calls `price_advice` if ≥3 records available.
- History stored in `_user_history` dict (in-memory, cleared on /start).

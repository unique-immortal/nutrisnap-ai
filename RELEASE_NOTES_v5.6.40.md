# NutriSnap AI v5.6.40

## Nutrition Model Chain
- Replaced the free nutrition reasoning chain with `deepseek/deepseek-v4-flash:free -> qwen/qwen3-next-80b-a3b-instruct:free -> minimax/minimax-m2.5:free`.
- Tightened the nutrition timeout schedule to `5s / 5s / 5s` with a hard deadline of `15s`.
- Preserved `model_used` and `fallback_trace` metadata for each nutrition-chain attempt.

## Structured Degradation
- The nutrition layer no longer falls through to a different provider after the configured OpenRouter chain is exhausted.
- If the nutrition chain times out or returns unusable structured output, the app now returns a structured degraded result with a clear disclaimer and user-confirmation flag instead of a generic analysis failure.

## Versioning
- Bumped app, client, and service worker versions to `v5.6.40` so mobile clients can receive the nutrition-chain update.

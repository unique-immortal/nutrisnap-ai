# NutriSnap AI v5.6.38

## Model Routing
- Replaced the free vision chain with `google/gemma-4-31b-it:free -> google/gemma-4-26b-a4b-it:free -> nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
- Replaced the free nutrition and text chains with `openai/gpt-oss-120b:free -> deepseek/deepseek-v4-flash:free -> z-ai/glm-4.5-air:free -> nvidia/nemotron-nano-9b-v2:free`.
- Added timeout-based fallback scheduling so photo analysis and voice parsing now step through the full chain instead of stopping after the first free model.

## Reliability
- Added per-stage hard deadlines for vision, nutrition, and text parsing chains.
- Recorded `fallback_trace` metadata for analysis and voice parsing responses to make model routing easier to verify and debug.
- Treats OpenRouter `400` requests as non-retryable so malformed payloads fail clearly instead of silently falling through the chain.

## Versioning
- Bumped app, client, and service worker versions to `v5.6.38` so mobile clients can detect the update.

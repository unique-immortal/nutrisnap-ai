# NutriSnap AI v5.6.41

## Free Model Routing
- Free photo analysis now stays on the vision fallback chain only: `google/gemma-4-31b-it:free -> google/gemma-4-26b-a4b-it:free -> nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
- Free text features now use only the three requested nutrition/text models: `deepseek/deepseek-v4-flash:free -> qwen/qwen3-next-80b-a3b-instruct:free -> minimax/minimax-m2.5:free`.
- Premium routing remains unchanged on `gpt-5.4-mini`.

## Feature Fixes
- Fixed `AI 教练` and `AI 洞察` so they no longer fall back to the old default free text routing.
- Removed Google text fallback from free `语音记录` parsing so it now follows the same free text model chain and timeout behavior.
- Added `/api/manual-food/estimate` so manual supplement can use AI to fill missing calories/macros when the user only knows the food name and weight.

## Client
- Manual food modal now clearly supports `名称 + 重量 + AI 估算`.
- `AI 教练` / `AI 洞察` / manual AI estimate now forward the `premium_ai` flag from the client.
- Disabled the native Capacitor speech-recognition plugin path by default and switched mobile voice entry back to the safer `MediaRecorder + /api/speech-to-text` flow to avoid tap-to-crash on the current app build.
- Bumped server/client/cache/package versions to `v5.6.41` so mobile clients can detect and receive the update.

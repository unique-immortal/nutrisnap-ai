# NutriSnap AI v5.6.42

## Voice Reliability
- Restored the native mobile speech-recognition path with safer listener lifecycle handling to reduce tap-to-fail behavior.
- Fixed `/api/speech-to-text` so OpenRouter STT failure can correctly fall through to the Google multimodal transcription path.
- Added a server-side rule-based fallback parser for voice input, so common food and exercise phrases can still produce usable records when free text models time out.

## AI Coach Reliability
- Fixed `AI 教练` so model-chain exhaustion no longer returns a hard 503 to the client.
- Added a rule-based coaching fallback that returns actionable Chinese advice based on today’s calories, protein, water, and exercise data.
- Improved the client-side coach error copy so provider failures no longer collapse into a vague generic network error.

## Client Fixes
- Repaired broken mobile speech UI helper strings introduced during the native speech flow refactor.
- Kept premium routing unchanged on `gpt-5.4-mini`.
- Bumped server/client/cache/package versions to `v5.6.42` so mobile clients can detect and receive the update.

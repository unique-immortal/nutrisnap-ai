# NutriSnap AI v5.6.25

## Deployment Fix
- Fixed Cloud Run homepage status by serving the SPA entry from tracked `templates/index.html` instead of the Docker-ignored `www/index.html`.
- Kept no-store headers for `/`, `/index.html`, and `/sw.js` so PWA clients can pick up updates reliably.
- Bumped client/server/cache version to `v5.6.25`.

## UI
- Keeps the premium bottom record sheet and equal primary actions for `拍照识别` and `语音记录`.

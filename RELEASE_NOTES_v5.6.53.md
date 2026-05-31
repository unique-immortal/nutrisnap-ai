# v5.6.53

## Changes

- Restored ordinary mode to the Google AI Studio Gemini fallback chain and removed OpenRouter from the default runtime path.
- Updated premium model defaults from `gpt-5.4-mini` to `gpt-5.2`.
- Fixed the update download endpoint so missing APK packaging returns a clear service error instead of a misleading API 404.
- Added `download_available` to `/api/update/info` for update UI diagnostics.
- Bumped the client and service worker cache versions to v5.6.53 so mobile clients receive the update.

## Deployment note

- The production release must run the Android APK build step before Cloud Run packaging so `static/app-debug.apk` is bundled into the container.

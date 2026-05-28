# NutriSnap AI v5.6.24

## Deployment Safety
- Changed `/sw.js` serving back to the tracked `static/sw.js` file so Cloud Run deployments do not depend on an untracked generated `www/sw.js` file.
- Kept the no-store service worker response headers and stale-cache cleanup logic.
- Bumped client/server/cache version to `v5.6.24`.

## Validation
- Keeps the v5.6.23 local runtime fix and premium record-method sheet UI.

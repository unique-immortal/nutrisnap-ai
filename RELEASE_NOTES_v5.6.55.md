# v5.6.55

## Changes

- Fixed mojibake in long AI-generated text such as AI insights and AI coach replies.
- Added a backend guard so Google, OpenAI-compatible premium, and OpenRouter response text is repaired before returning to the app.
- Added a frontend guard so AI markdown rendering repairs cached or live garbled text before display.
- Escaped user chat text before inserting it into the coach conversation to avoid malformed layout from special characters.
- Bumped the client and service worker cache versions to v5.6.55 so mobile clients receive the update.

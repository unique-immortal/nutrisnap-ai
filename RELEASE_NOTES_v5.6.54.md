# v5.6.54

## Changes

- Fixed mojibake in AI-generated food names such as `çƒ¤é¸¡...` by repairing wrongly decoded UTF-8 text before display and storage.
- Added a backend guard so new photo, voice, and manual AI results normalize dynamic text fields before returning to the app.
- Added a frontend guard so previously cached local meal names can repair themselves on the next app load.
- Bumped the client and service worker cache versions to v5.6.54 so mobile clients receive the update.

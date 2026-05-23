# Handoff: Android APK Signature Conflict & Update Button Fix

## Session Metadata
- Created: 2026-05-24 00:37:28
- Project: G:\我的云端硬盘\vibe coding\ai-diet-tracker
- Branch: main
- Session duration: ~45 mins
- Markdown handoff: G:\我的云端硬盘\vibe coding\ai-diet-tracker\.agent\handoffs\2026-05-24-003728-apk-signature-conflict.md
- Machine-readable sidecar: G:\我的云端硬盘\vibe coding\ai-diet-tracker\.agent\handoffs\2026-05-24-003728-apk-signature-conflict.json
- Latest handoff JSON: G:\我的云端硬盘\vibe coding\ai-diet-tracker\.agent\handoffs\handoff.json

### Recent Commits (for context)
  - cbd5008 fix: copy decoded keystore directly to android/app/debug.keystore to fix signature conflict
  - 7863581 fix: correctly expose checkAppUpdate to window and add toast feedback
  - 3d0ecec chore: bump version to 5.5.5 and add release notes
  - 6bb641a fix: make checkAppUpdate and closeAboutModal globally accessible
  - 3c6e726 fix: restore index.html utf-8 encoding and correctly patch checkAppUpdate

## Handoff Chain
- **Continues from**: [2026-05-23-004256-nutrisnap-v55-ui-followup.md](./2026-05-23-004256-nutrisnap-v55-ui-followup.md)
  - Previous title: 2026-05-23-004256-nutrisnap-v55-ui-followup
- **Supersedes**: None

## Budget Monitor Snapshot
- Triggered by: manual
- Budget waterline: unknown
- Remaining budget ratio: unknown
- Remaining tokens: unknown
- Tool calls left: unknown
- Time left minutes: unknown

## Current State Summary
The user experienced persistent Android APK signature conflicts when upgrading the app. Additionally, the manual "Check Updates" button in the About modal was completely unresponsive. 
We analyzed the codebase and CI build workflow. We found that the Capacitor Android build dynamically generates a fresh random `debug.keystore` inside `android/app/` during every CI build, ignoring the decoded keystore at `~/.android/debug.keystore`. We have patched the CI workflow to overwrite the generated keystore with the official decoded keystore, and resolved a JS scoping bug that caused the "Check Updates" button to be unresponsive. Version is bumped to `5.5.5` to test the new signature alignment.

## Codebase Understanding

## Architecture Overview
This is a hybrid mobile app using Capacitor with HTML/JS assets built in `www/`. The Android build runs Gradle inside a dynamically generated platform directory `android/`.
The backend serves an update info API `/api/update/info` which lists the latest available version and download URL.

## Critical Files

| File | Purpose | Relevance |
|------|---------|-----------|
| [.github/workflows/deploy.yml](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/.github/workflows/deploy.yml) | GitHub Actions deployment script | Sets up build steps and keystores |
| [templates/index.html](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/templates/index.html) | Main web asset file | Contains update check and About modal logic |
| [package.json](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/package.json) | Node package file | Bumps version for mobile builds |

## Key Patterns Discovered
- `templates/index.html` has scripts wrapped inside a DOMContentLoaded event. Any globally accessed functions (like `checkAppUpdate` called by inline `onclick`) must be explicitly assigned to the `window` object to prevent scope ReferenceErrors.

## Work Completed

## Tasks Finished
- [x] Identified that the custom keystore decoded at `~/.android/debug.keystore` was ignored because Capacitor looks at `android/app/debug.keystore`.
- [x] Overwrote `android/app/debug.keystore` in GitHub Actions setup with our permanent keystore (`cbd5008`).
- [x] Correctly exposed `checkAppUpdate` to the global `window` object inside `templates/index.html` (`7863581`).
- [x] Added user feedback (toast messages) for manual update checking (success, error, is latest version).
- [x] Bumped version to `5.5.5` across `templates/index.html`, `package.json`, and `app.py`.

## Files Modified

| File | Changes | Rationale |
|------|---------|-----------|
| [.github/workflows/deploy.yml](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/.github/workflows/deploy.yml) | Copied decoded keystore to `android/app/debug.keystore`. | Fixed dynamically generated keystore bypassing the stable signature. |
| [templates/index.html](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/templates/index.html) | Exposed `checkAppUpdate` globally on `window` and updated version to 5.5.5. | Fixed ReferenceError and enabled manual update check feedbacks. |
| [package.json](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/package.json) | Bumped version to `5.5.5`. | Trigger Capacitor platform config update. |
| [app.py](file:///g:/%E6%88%91%E7%9A%84%E4%BA%91%E7%AB%AF%E7%A1%AC%E7%9B%98/vibe%20coding/ai-diet-tracker/app.py) | Updated fallback version to `v5.5.5` in update response. | Set correct API target response for latest version. |

## Decisions Made

| Decision | Options Considered | Rationale |
|----------|-------------------|-----------|
| Overwrite local `android/app/debug.keystore` | 1. Patch `build.gradle` directly.<br>2. Overwrite local keystore file. | Overwriting the local file is cleaner, faster, and avoids modifying Capacitor's auto-generated Android files, preserving default configuration. |

## Pending Work

## Immediate Next Steps
1. Wait for the `v5.5.5` compilation to finish on GitHub Actions.
2. Direct the user to do a **fresh clean install** of `v5.5.5` (uninstalling any previous versions to clear any ghost signatures from previous random builds).
3. Confirm that subsequent updates can successfully override `v5.5.5` by deploying a dummy version (e.g. `5.5.6`) and trying to overwrite-install it.
4. Verify that manual "Check Updates" works on the device and shows the "Already latest version" toast message.

## Blockers/Open Questions
- If the user previously had dual apps, private folder apps, or secure workspace apps installed with the old signature, standard uninstall might leave artifacts. User needs to make sure all instances are fully uninstalled if signature conflicts persist.

## Deferred Items
- None

## Context for Resuming Agent

## Important Context
Gradle in Capacitor projects looks for `android/app/debug.keystore`. Previous builds were failing base64 decoding due to CRLF, which forced Gradle to create a random one. After CRLF was fixed, the build was *still* creating a random one because it was written to `~/.android/debug.keystore` instead of `android/app/debug.keystore`. This has been resolved in `cbd5008`.

## Assumptions Made
- The password for `debug.keystore.base64` is the Android Studio default: `android`, and the alias is `androiddebugkey`.

## Potential Gotchas
- When testing upgrade behavior, always verify the signature hash using `apksigner` or local download test.

## Do Not Repeat
- Do not attempt to resolve `checkAppUpdate` without exposing it to `window`, as it will fail due to event handler scoping inside `DOMContentLoaded`.

## Resume Prompt
You are taking over an unfinished agent task. Read this handoff completely, restate the goal and state in your own words, verify the listed critical files and evidence, and begin with Immediate Next Steps item 1.

## Environment State

## Tools/Services Used
- GitHub Actions CI/CD pipeline
- Capacitor CLI
- Git

## Active Processes
- None

## Environment Variables
- None

---

**Security Reminder**: Before finalizing, run `validate_handoff.py` to check for accidental secret exposure.

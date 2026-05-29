# NutriSnap AI v5.6.39

## Packaged Food Recognition
- Added a packaged-food name refinement pass for cases where the first vision result falls back to a generic category such as `巧克力棒` or `chocolate bar`.
- Strengthened the vision prompt to prioritize front-of-pack product text and Chinese consumer-facing names for packaged foods.
- Added rules to avoid renaming wrapped frozen desserts and ice cream bars into plain chocolate bars.

## Nutrition Consistency
- The nutrition reasoning layer now preserves packaged product names from the visual layer instead of replacing them with a generic category.
- Added packaged-name refinement metadata to the analysis response for easier debugging.

## Versioning
- Bumped app, client, and service worker versions to `v5.6.39` so mobile clients can receive the updated packaged-food pipeline.

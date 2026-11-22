## What changes

<!-- What is different for the user, and why. -->

Fixes #

## Checklist

- [ ] Offline tests pass (`test_core test_catalog test_static_index test_offline_io test_download_plan test_gui_usability test_package`)
- [ ] Live tests pass, if downloads, recipes or catalogue entries changed (`SD_LIVE=1`)
- [ ] New behaviour has a test
- [ ] `PIPELINE_VERSION` in `core/pipeline.py` raised, if the same settings now produce different pixels
- [ ] `CHANGELOG.md` has an entry under *Unreleased*, if users notice the change
- [ ] Docs updated, if usage changed
- [ ] Regenerated `collections.json` or static index committed with the generator change

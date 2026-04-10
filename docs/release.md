# Release Checklist — Duo

> Standard procedure for cutting a new Duo release.

---

## 1. Version Bump

- Update `version` in `pyproject.toml`
- Update `CHANGELOG.md`: rename `[X.Y.Z] — Unreleased` → `[X.Y.Z] — YYYY-MM-DD`
- Add new `## [Next] — Unreleased` section at the top of `CHANGELOG.md`
- Update version in `install.sh` banner
- Update supported version in `SECURITY.md`

## 2. Pre-Release Checklist

- [ ] `make check` passes (lint + format-check + mypy + 100 % coverage)
- [ ] `bash scripts/quickstart-test.sh --local` passes
- [ ] `bash scripts/pre-release-check.sh` passes
- [ ] CHANGELOG has all changes documented
- [ ] README badges reflect correct version and test count
- [ ] All changes committed and pushed

## 3. Build & Publish

```bash
uv build                    # produces dist/duo-X.Y.Z.tar.gz + .whl
uv publish                  # upload to PyPI (requires API token)
```

## 4. Post-Release

- Tag: `git tag vX.Y.Z && git push --tags`
- GitHub Release: create from tag with CHANGELOG excerpt
- Bump version to next dev: `X.Y.(Z+1).dev0`

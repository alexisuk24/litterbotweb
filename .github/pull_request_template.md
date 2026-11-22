<!--
Title: <type>(<optional scope>): <imperative summary>, 64 characters or fewer, e.g.
  fix(collector): retry clip downloads after a timeout
Types: feat fix docs refactor perf test build ci chore revert (release: maintainer only)
Target branch: develop. See CONTRIBUTING.md for the full rules.
-->

## What and why

<!-- The problem, and how this change solves it. Link the issue: "Fixes #12". -->

## How it was tested

<!-- Commands you ran and what you checked by hand. Add a screenshot or recording for UI changes. -->

- [ ] `python -m pytest` passes
- [ ] Added or updated tests for the behaviour this changes

## Breaking change

<!-- What users have to do (renamed env var, changed volume, removed feature), or "None". -->

None

## AI assistance

<!-- "None", or which assistant you used and for what (e.g. "Claude wrote the first draft of the retry logic; I reviewed and tested it"). -->

## Checklist

- [ ] Targets `develop` (only release and hotfix PRs target `main`)
- [ ] README and `compose.yml` updated if env vars, settings or user-visible behaviour changed
- [ ] Python dependency changes made in both the `Dockerfile` and `tests.yml`
- [ ] No `.env`, credentials, databases, clips, logs or `--dump` output committed

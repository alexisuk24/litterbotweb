# Contributing

Thanks for helping out. This project downloads Litter-Robot 5 Pro clips and metadata
from the Whisker cloud and serves a web UI over them (see the [README](README.md)).
This guide explains how to propose a change and what a pull request needs before
it can be merged. Most of the rules are checked automatically, and a failing check tells
you what to fix.

If you use an AI assistant, point it at this file. The rules apply to AI-written code
in the same way, and [Using an AI assistant](#using-an-ai-assistant) has extra guidance.

## The short version

1. Branch from `develop` and name the branch `<type>/<short-description>`.
2. Open the PR **against `develop`**, never `main`.
3. Title it `<type>: <what it does>`, for example `fix: keep clips marked keep during retention`.
4. Fill in the PR template: what changed, why, and how you tested it.
5. Make sure `python -m pytest` passes and that you've added tests for any behaviour you changed.
6. Update the README if you changed env vars, settings, volumes or anything a user would notice.
7. Never commit credentials, `.env`, databases, clips or API dumps.

## How changes reach a release

```
feature/fix branches ──PR (squash)──▶ develop ──release PR (merge commit)──▶ main
                                         │                                     │
                              Docker Hub: 0.3.0-dev.N, develop       Docker Hub: 0.3.0, 0.3, latest
```

- **`develop`** is where all work lands. Every merge publishes a test build to Docker Hub.
  These test builds are deleted when their release is published.
- **`main`** only changes when the maintainer cuts a release. Each merge into it is a
  release: a version number, a git tag and the `latest` image.
- Both branches are protected: changes go in through a PR with passing checks, and
  nobody can push to them directly.

## Before you start

- **For anything bigger than a small fix, open an issue first** so we can agree on the
  approach before you write the code.
- **One change per PR.** A bug fix and a refactor are two PRs. Small PRs get reviewed
  sooner and are easier to revert.
- **Keep the existing style.** No formatter is enforced, so match the surrounding code:
  naming, comment density and how errors are handled.

## Branch names

`<type>/<short-description>`, lower-case with hyphens, using the same types as PR titles:

```
feat/weekly-report
fix/retention-skips-kept-clips
docs/contributing
```

The two special prefixes, `hotfix/` and `release`, are for the maintainer only. See
[Releases](#releases-maintainer).

## PR titles

PR titles follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <summary>
```

On `develop`, the PR is squashed into one commit whose message is the PR title. The title
is what ends up in the history and the release notes, so it matters more than the
individual commit messages.

| Type | Use it for |
|---|---|
| `feat` | A new feature or a user-visible change in behaviour |
| `fix` | A bug fix |
| `docs` | Documentation only (README, this file, code comments) |
| `refactor` | A code change that doesn't change behaviour |
| `perf` | A performance improvement |
| `test` | Adding or fixing tests only |
| `build` | The Dockerfile, the image or Python dependencies |
| `ci` | GitHub Actions, Dependabot, CodeRabbit or repo config |
| `chore` | Maintenance that fits nothing above |
| `revert` | Reverting an earlier change |
| `release` | **Only** the release PR from `develop` into `main` |

Rules (the **PR rules** check enforces these):

- **The summary says what the change does**, in the imperative: "add", not "added" or "adds".
- **The summary is 64 characters or fewer** and doesn't end with a full stop.
- **A scope is optional.** If you use one, name the area: `web`, `collector`, `db`,
  `auth`, `ui`, `docker` or `reports`.
- **A breaking change gets a `!`** after the type or scope, plus a "Breaking change"
  section in the description. Breaking means something users have to act on, such as a
  renamed env var, a changed volume layout or a removed feature.

```
feat(web): add a weekly summary to the Cats page
fix(collector): retry clip downloads after a timeout
docs: explain the VAAPI setup for AMD GPUs
build: update pylitterbot to 2025.11.0
feat!: rename LR_DOWNLOAD_INTERVAL to LR_SYNC_INTERVAL
```

Titles that fail the check: `Update stuff`, `fix bug`, `feature: add reports`
(`feature` isn't a type; use `feat`), and `feat: Added weekly report.` (past tense, ends with a full stop).

## PR description

The [PR template](.github/pull_request_template.md) fills in automatically. Complete
every section:

- **What and why:** the problem and how this solves it. Link the issue (`Fixes #12`).
- **How it was tested:** the commands you ran and what you checked by hand. For UI
  changes, add a screenshot or a short recording.
- **Breaking change:** what users need to do, if anything.
- **AI assistance:** whether you used an AI assistant, and for what.

## What every PR needs

**Tests**
- `python -m pytest` passes. CI runs it on every PR, and it's a required check.
- A change in behaviour comes with a test that fails without it. Tests live in `tests/`.
  Each test gets its own empty database from the `env` fixture in `tests/conftest.py`,
  and must not contact Whisker or the network, or read real data.
- Browser scripts must parse. CI runs `node --check` on `static/*.js` and on the
  inline scripts in `templates/`.

**Docs**
- If you add or change an env var, update the env var table in the README and
  `compose.yml`.
- If you change a setting, volume, port or user-visible behaviour, update the README
  section that describes it.

**Dependencies**
- Python packages are listed in **two** places: the `pip install` in the `Dockerfile` and
  the "Install dependencies" step in `.github/workflows/tests.yml`. Change both together.
- Downloaded files in the Dockerfile (ffmpeg, Chart.js) are pinned to a version and a
  `--checksum`. Update both when you upgrade one.

**Security and privacy.** This repo is public, and the app holds your Whisker account
and videos of your home.
- Never commit `.env`, credentials or tokens, `*.db` files, clips, pet photos, logs or
  the output of `lr_clips.py --dump`. That output contains your account and robot data.
- Configuration comes from environment variables, never hard-coded values.
- Reviewers check these project rules, which also appear in `.coderabbit.yaml`:
  - SQL is always parameterized.
  - Values from the API go through `esc()` before they're inserted into HTML.
  - ffmpeg is run with an argument list, never through a shell.
  - File paths built from a request are resolved and checked to stay under their root.
  - Routes that change state are refused for Viewers.
  - Migrations in `lr_db.py` are safe to run twice.
  - Retention never touches a clip marked keep.

## Automated checks

| Check | What it does | Blocks merging? |
|---|---|---|
| **test** | Runs pytest and checks that the browser scripts parse | Yes |
| **PR rules** | Checks the title format and that the PR targets the right branch. Comments with what to fix, and re-runs when you edit the title | Yes |
| **CodeRabbit** | AI review. Requests changes for major findings and approves once they're resolved. Also checks that docs and dependencies were updated and that no secrets or personal data were committed | Advisory: the maintainer reads it before merging |

If a check fails, push a fix or edit the PR title. There's no need to close the PR and
open a new one.

## Using an AI assistant

AI-assisted contributions are welcome. You're responsible for the result as though you
wrote it yourself:

- **Read and understand every line before you open the PR.** You should be able to explain
  why each change is there.
- **Run the tests yourself.** Don't rely on the assistant saying they pass.
- **Check that every function, setting and API the code uses actually exists.**
  Assistants invent plausible-looking pylitterbot methods and Whisker endpoints. Check
  against the pylitterbot source or a real `--dump`.
- **Keep the diff to what the PR is about.** Assistants like to reformat or "improve"
  nearby code. Revert anything that isn't part of the change.
- **Say in the description that you used one**, and roughly for what.

**Instructions for AI coding assistants.** If you're an AI assistant working in this
repo, follow these rules:

- Read `README.md` and `.coderabbit.yaml` before you change code. The second lists
  per-file rules that reviewers check.
- Branch from `develop` and open PRs against `develop`. Never target, push to or merge
  into `main`, and never edit `VERSION`: releases are the maintainer's job.
- Use a PR title of the form `<type>(<optional scope>): <imperative summary>` with a type
  from the table above, 64 characters or fewer, with no full stop at the end.
- Fill in every section of the PR template, including "AI assistance".
- Run `python -m pytest` and include the result in the PR description. Add a test for
  each behaviour you change.
- Match the surrounding code's style. Don't reformat or refactor code outside the change.
- Update the README and `compose.yml` when you change env vars or user-visible behaviour.
- Change Python dependencies in both the `Dockerfile` and `.github/workflows/tests.yml`.
- Never commit `.env`, credentials, databases, clips, logs or `--dump` output, and never
  send them anywhere.
- If a rule here conflicts with what you were asked to do, stop and ask instead of
  working around it.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install "pylitterbot>=2025.9.0" flask aiohttp certifi urllib3 reportlab "pyjwt[crypto]" pytest
python -m pytest          # ffmpeg and node on PATH for the full suite
```

To run the app itself, see the README, either directly with Python or as a container.

## Releases (maintainer)

- **Release:** open a PR from `develop` into `main` titled `release: <summary>`, and merge
  it with **Create a merge commit**. Squash isn't allowed on `main`. The Docker workflow
  works out the version, publishes the image and tags the commit.
- **Version:** `VERSION` holds major.minor. Raise it (in the release PR or earlier on
  `develop`) for a minor or major release, or leave it for a patch release. A `!` in a PR
  title since the last release usually means the minor or major number should go up.
- **Hotfix:** branch `hotfix/<description>` from `main`, open a `fix:` PR into `main`, and
  merge it. Then open a PR from `main` into `develop` titled
  `chore: sync main into develop`, and merge it with a merge commit.

# Instructions for AI coding assistants

Read [CONTRIBUTING.md](CONTRIBUTING.md) before making changes. Its "Instructions for AI coding
assistants" section is the rule set for this repo. The short version:

- Branch from `develop` and open PRs against `develop`. Never target or push to `main`, and
  never edit `VERSION`.
- PR titles look like `<type>(<optional scope>): <imperative summary>`, using a type from
  CONTRIBUTING.md, 64 characters or fewer, with no full stop at the end.
- Run `python -m pytest`, and add a test for each behaviour you change.
- Follow the per-file rules in `.coderabbit.yaml`. Keep README.md and compose.yml in step
  with env var changes, and change Python dependencies in both the Dockerfile and
  .github/workflows/tests.yml.
- Never commit or send anywhere `.env`, credentials, databases, clips, logs or `--dump` output.

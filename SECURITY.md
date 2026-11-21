# Security policy

This app holds your Whisker account credentials and videos from inside your home, so
security problems matter here.

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately instead, through
[GitHub's private vulnerability reporting](https://github.com/alexisuk24/litterbotweb/security/advisories/new)
(the repo's **Security** tab → **Report a vulnerability**).

Include what you found, how to reproduce it, and which version or image tag you used. You'll
get a reply as soon as the maintainer can look at it. This is a one-person hobby project, so
there's no fixed response time.

## Supported versions

Only the latest release (the `latest` image on Docker Hub) gets security fixes.

## When reporting, leave out

Your Whisker email address or password, tokens, `.env` contents, clips, databases and
`lr_clips.py --dump` output. A description and the steps to reproduce are enough.

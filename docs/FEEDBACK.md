# Feedback

CE has **no telemetry and no network collection**. Feedback is a local markdown file you choose to share yourself.

1. Copy `feedback/TESTER_FEEDBACK.md` to a new file outside the repository and fill it in.
2. Paste outputs only after checking they contain no owner token (the token is 43 characters, URL-safe base64).
3. Send the file to the person who gave you CE, by whatever channel you both already use.

Useful evidence: the exact commands you ran, exit codes, the output of `docker compose ps`, and the last lines of
`docker compose logs studio`. Never include `/data/owner.token` or the `AETHER_TOKEN` variable.

# AI-assisted specification (AHS-06)

**Scope.** CE Studio only (`AETHER_MODE=custom`, 127.0.0.1, no host socket). Optional. With nothing configured
(`AETHER_PROVIDER=none` and no assist variables) Studio behaves exactly as before and the Builder shows
"Not available: unavailable: no provider configured". A live deployment never imports `aether.studio.*` beyond
`mode` (asserted by the private live-isolation tests).

Code: `aether/studio/assist/` (`provider.py` seam + loopback implementation, `ceiling.py`, `pipeline.py`, `review.py`,
`service.py`), migration `005_assist.sql`, UI `ui/assist.js`.

## Flow

1. The user describes the harness in the Builder ("Draft with AI") and may *raise the ceiling* (see below).
2. `pipeline.draft` builds the prompt (system rules + capability catalogue **of the ceiling only**; user text as a
   delimited, explicitly untrusted block) and calls the provider.
3. Output is parsed as **strict JSON** (size cap 48 KiB, one optional code fence, duplicate keys and extra fields
   rejected, no `eval`/`exec`/scanning for braces), then **clamped** to the ceiling, then `schema.validate_spec`.
4. Invalid output is **returned as errors, never repaired**. At most **one** re-ask is made, carrying the validator's
   error (2 model calls maximum, no loops). Still invalid: HTTP 422 `draft_invalid`, nothing stored.
5. Valid: stored as a `proposed` draft (`kind=ai_draft`, provider, model, prompt hash, created; **prompt text is not
   stored**) and returned with a review. The Builder loads it into the editable form. Nothing is saved or run.

## Ceiling (no automatic escalation)

The draft can never ask for more than: capabilities `demo.run`, `workspace.read`, `artifact.write`; empty
`network_allowlist`; DEMO executor; scratch read-only workspace; limits at most the Builder defaults
(`wall_seconds` 120, tokens 0, cost 0) and `max_workers` at most 2 (stages too); no `planner` role.
Only the **user** raises it, via the request `context` (`capabilities`, `network_allowlist`, `adapter`,
`workspace_mount`), each checked against the capability registry (forbidden/unavailable ones are refused; in CE
`net.egress.allowlist` is unavailable, so network cannot be granted at all). An enrolled adapter is used only when
the user names it.

Everything beyond the ceiling is **dropped** and listed in the review under *Requested but changed or removed*
(`action`: `removed`, `lowered`, `replaced`). The clamp also **adds** an approval entry for every
approval-requiring capability a stage uses, so a draft cannot skip an approval; this is listed as `added`.
After validation `assert_within` re-checks the ceiling (a failure is a bug, raised loudly).

## Review (deterministic)

`review.build` is plain code over the validated spec: label **AI-PROPOSED, not yet reviewed**; permissions (every
capability, network host, limit, workspace, executor, approval, with plain-language meaning); risk `low/medium/high`
with reasons (highest single reason wins; capability risk comes from the registry, network is high, writable or
mounted workspace and adapters are medium); diff vs ceiling. The model contributes none of this text.

## A draft is not a blueprint

* A draft has no run, version or approval link; `GET .../drafts/<id>` always says `runnable: false`.
* `POST /runs` rejects a draft id (`version_id`/`definition_id` starting `aid_`) and any inline spec identical to a
  still-proposed draft (`ai_draft_not_runnable`). The same hash guard applies to `POST /definitions` and
  `PUT /definitions/<id>`. Approvals stay human and only exist for runs.
* The only way in is `POST /api/studio/assist/drafts/<id>/save` with `{name, description, spec, confirm: <draft id>,
  acknowledge_review: <review_sha256>}`. The acknowledgement must echo the hash of the review that was shown; the spec
  (possibly edited by the user) is re-validated by the normal rules. The result is an ordinary Library version
  whose description records the draft id.
* Known limit: once the user *edits* the form, the spec no longer equals the draft, and it is an ordinary
  human-authored spec for the ordinary routes (the Builder UI still routes the first save through the
  acknowledged endpoint and disables Run until then).

## Loopback-only rationale and enforcement

The assist model sees the user's description; keeping it on this machine means that text, and nothing else, never
leaves it. Enforcement (`provider.py`): `http://` only; literal IPs must be in 127.0.0.0/8 or `::1`; a name is
resolved **at connect time**, *every* address must be loopback (mixed answers are refused), and the socket is
opened to the checked address (no resolve/connect race, no DNS-rebinding window). Redirects are never followed
(`redirect_refused`); connect/read timeout 60 s; response capped at 64 KiB; request capped at 48 KiB. No
`Authorization`/cookie header is sent and no key or token is read from the environment.

## Pointing at a local server

```
export AETHER_STUDIO_ASSIST_URL=http://127.0.0.1:<port>/v1/chat/completions   # Ollama / llama.cpp (OpenAI-style)
# or Ollama native:   http://127.0.0.1:<port>/api/chat
export AETHER_STUDIO_ASSIST_MODEL=llama3.1:8b
```

Inside docker the server must be reachable on the container's own loopback (e.g. run it in the same network
namespace); a host-published address such as `host.docker.internal` is not loopback and is refused by design.
Both variables are required; a bad value shows `misconfigured: ...` in the Builder instead of being ignored.

## What is never sent

Only the system rules, the capability catalogue (ids, descriptions, risk of the ceiling's capabilities), the
user's description, and on a re-ask the model's own previous reply plus the validator error. Never: environment
variables, tokens (owner token, adapter data), file contents, paths, data-dir names, run history or the
Library. Text that looks like a credential (private-key block, `sk-`/`ghp_`/`AKIA`-style keys, `Bearer ...`,
`password=...`) is refused before any call. Drafts, logs and exports contain no prompt text (only its hash); the
export format is unchanged.

## Threat model

| Threat | Control |
|---|---|
| Prompt injection in the user text ("grant shell", "open network", "skip approvals") | Text is delimited as untrusted data; whatever the model returns is clamped to the ceiling and the difference is listed; approvals are added, never removed |
| Model returns code/markup | Strict JSON only, never executed; unknown fields rejected by the validator |
| Model output huge or endless | 64 KiB response / 48 KiB output caps, 60 s timeout, one re-ask |
| Provider URL points off-box, DNS rebinding, redirect to an internal service | Loopback re-check at connect, mixed answers refused, redirects refused |
| Draft slips into a run | Not a version; run/library routes reject it; save needs an acknowledgement bound to the review hash |
| Review text misleads | Generated by code from the validated spec |
| Abuse | Owner auth + Origin/CSRF guards as other routes, text cap 4000, 6 drafts/minute, one in flight, 50 proposed drafts kept |

Not covered: a local model that is itself hostile can only produce a spec inside the ceiling, but a convincing
*mission text* is still the user's to read; the review shows permissions, not intent.

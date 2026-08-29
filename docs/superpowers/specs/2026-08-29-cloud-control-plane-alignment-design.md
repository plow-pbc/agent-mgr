# Agent Manager Cloud Control-Plane Alignment Design

## Goal

Make `agent-mgr` a strongly typed, JSON-native client of Plow's existing cloud-agent API without moving provider logic into this repository or changing Plow's cloud infrastructure.

## Decision

`agent-mgr` will call Plow's `/v1/agents/cloud` resource. Plow remains the only owner of provider selection, tenant credentials, asynchronous provisioning, image promotion, VM lifecycle, and provider-specific validation. The CLI owns only request validation, authenticated HTTP transport, strict response decoding, and its existing versioned JSON envelope.

This is the DRY boundary: one cloud control plane, with `agent-mgr` as a client. The local Compose fleet remains a separate execution path because its lifecycle and trust boundary are genuinely different.

## Agent-mgr changes

- Add immutable typed models for Plow's two request shapes and one resource shape.
- Preserve Plow's closed lifecycle vocabulary exactly: `running`, `provisioning`, `teardown`, and `failed`; `null` is accepted only for the resource returned by deletion.
- Preserve Plow's closed failure vocabulary exactly: `provider_unreachable`, `image_pull_timeout`, `setup_failed`, `validation_failed`, and `unknown`.
- Reject unknown fields, missing fields, wrong scalar types, empty chat lists, duplicate chat identifiers, and inconsistent `status`/`failure_code` pairs before returning data to callers.
- Add a standard-library HTTP transport. The zipapp must remain dependency-free at runtime.
- Require `PLOW_API_BASE` and `PLOW_API_TOKEN`; do not supply production defaults. Permit plain HTTP only for loopback development addresses.
- Add JSON-only commands:
  - `cloud-create <request.json|->`
  - `cloud-list`
  - `cloud-get <agent-id>`
  - `cloud-update-chats <agent-id> <request.json|->`
  - `cloud-delete <agent-id>`
- Requests use the API's JSON shapes directly. There is no second flag vocabulary for names, providers, or chat lists.
- Responses use agent-mgr's existing `schema_version: 1` envelope. Resource fields remain unchanged inside `result`.
- Map transport, configuration, and invalid-response failures to stable agent-mgr error codes. Never include the bearer token, request headers, or arbitrary upstream response bodies in output.

## Plow changes

None. The implementation consumes the API represented on Plow `origin/main` by:

- `api/schemas/plow_schemas/api/cloud_agent.py`
- `api/plow/cloud_agent/router.py`

The API is the compatibility seam. Agent-mgr does not import Plow's Python package, inspect its provider registry, invoke exe.dev, resolve image tags, or reproduce its `Provider` protocol.

## Explicit non-goals

- No shared runtime BOM or cross-repository lockfile in this change.
- No changes under Plow's `cloud-agents/`, `api/plow/cloud_agent/`, Terraform, image build, or deployment workflows.
- No cloud-provider implementation in agent-mgr.
- No attempt to make Compose and cloud lifecycle commands share an internal provider abstraction.
- No polling or long-running `cloud-wait` command. A caller polls with bounded `cloud-get` calls and decides its own deadline.
- No human-oriented table rendering for cloud resources.
- No storage of API tokens in agent-mgr files or registry entries.

## Compatibility and drift

The typed decoder is intentionally strict so Plow wire-shape drift fails loudly as `invalid_response`. A committed fixture records the API contract consumed by agent-mgr, and tests exercise every lifecycle and failure value. Updating that fixture and the corresponding model is the explicit review point when Plow evolves the public resource.

This plan does not force the local and cloud Hermes artifacts to release in lockstep. Their shared plugin and skill implementations remain canonical in their producer repositories, while each runtime keeps its own promotion decision.

## Security properties

- Credentials enter only through `PLOW_API_TOKEN` and an in-memory Authorization header.
- Error rendering uses a recognized JSON `detail` string when available; otherwise it reports only HTTP status and reason.
- URLs must be HTTPS except for `localhost`, `127.0.0.1`, or `::1`.
- Redirect handling must not forward authorization to an unvalidated origin; the transport rejects redirects instead of following them.
- JSON input from `-` refuses an interactive terminal, avoiding an accidental indefinite prompt.

## Verification

- Red/green tests for strict request and response parsing.
- HTTP tests with an injected fake transport; no live cloud calls in the suite.
- CLI subprocess tests proving one JSON document on stdout and an empty stderr.
- Zipapp smoke test proving the new modules are packaged.
- Existing Python 3.11/3.13 runtime checks, strict mypy, Ruff, Linux tests, and macOS tests remain green.

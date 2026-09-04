# Running Hermes agents with agent-mgr

## What agent-mgr is

One host, many agents. Each agent is a Docker container running Hermes,
connected to **Plow Chat** (its phone line) and, optionally, **Plow Latch** (a
Mac it can drive). `agent-mgr` owns everything the agents share — the image,
the Compose template, and the pinned plugin/skill versions in
`runtime/stack.json` (a JSON lock: every ref is a 40-char SHA). Each agent's
own repo owns only what makes it itself: `agent.env`, `config.yaml`, its
skills and hooks.

A registry at `~/.config/agent-mgr/agents` maps each agent **name** to its
**repo**, so every command works from any directory. Several names may point
at the same repo — that is how one codebase (e.g. `life-assistant-hermes-agent`)
serves several people, each with its own container and home directory
(`~/.hermes-<name>`).

## Install

Clone and symlink per [the README's install block](../README.md#agent-mgr),
then check it works:

```sh
agent-mgr ls
```

You need **`gh`, authenticated** (`gh auth status`) — `deploy` installs the
Plow Chat plugin and fleet skills through `gh api`.

## Set up a new agent

First, register the name. Two cases:

**A. Brand-new agent** — scaffold a fresh repo (writes `agent.env` and
`config.yaml`, and registers the name):

```sh
agent-mgr new errands ~/services/errands-hermes-agent
```

**B. Another instance of an existing repo** — e.g. a second person on
`life-assistant-hermes-agent`. No scaffolding, just a registry row —
`register` refuses a directory that doesn't exist, so clone the repo first
if this host doesn't have it:

```sh
git clone git@github.com:plow-pbc/life-assistant-hermes-agent.git ~/services/life-assistant-hermes-agent
agent-mgr register bob ~/services/life-assistant-hermes-agent
```

Then, for either case (`<name>` is whichever you just registered):

```sh
agent-mgr deploy <name>      # the whole deploy: home, config, plugin, skills, deploy hook
agent-mgr activate <name>    # prints a code — text it from THAT agent's phone
agent-mgr up <name>          # start the container
agent-mgr cron-sync <name>   # only if its agent.env names a cron spec
agent-mgr sign-in <name>     # device-code OAuth for the model credential
```

Rules that matter, in order of how much they cost to get wrong:

- **`activate` is a one-time spend, and the phone decides ownership.** The
  activation POST carries no credential — the agent binds to whichever account
  texts the code back. Wrong handset = wrong owner, permanently. The code has
  a short TTL and cannot be minted ahead of time; `activate` polls until the
  text arrives.
- **Per-person values go in the instance dotenv, after `deploy` and before
  `up`.** e.g. `AGENT_TZ=America/Chicago` in the `.env` inside the home that
  `agent-mgr resolve <name>` prints. Before `deploy` there is no home (don't
  create it yourself — `deploy` writes the dotenv `0600` only when absent);
  after `up` a change needs a `restart` to reach the container. `agent-mgr
  resolve <name>` reads back the keys **agent-mgr itself consumes** — today
  that is `AGENT_TZ` alone. A key the container reads for itself, such as
  `AGENT_INDEX`, is set in the same file and will not appear there; the
  README's dotenv section is the contract.
- **A `plow-init` agent's credentials are not required until `up`.**
  `AGENT_BOOT_CONTRACT=plow-init` is set fleet-wide in `agent.env` (same file
  as `AGENT_TZ` above), so case B inherits it from case A automatically —
  there is nothing to opt into per instance, and nothing to decline. `deploy`
  still completes on a home with no real token yet; `activate` is what
  supplies one, and `up` is what actually refuses to start without it. The
  sequence above is the same either way.
- **`up` before `sign-in`** — `sign-in` runs inside the container, so it
  refuses until one is running. And **don't restart the agent while `sign-in`
  is waiting on the browser** — the session lives in the container. If
  `activate` reports a failed follow-up, recover *before* starting `sign-in`:
  run the command it prints, or — when it only says not to re-run
  `activate` — fix whatever refused its reload, then `restart`. A dropped
  `sign-in` re-runs for free; `activate` does not.

On re-activation, an agent keeps its line: `activate` remembers the canonical
`PLOW_HOME_CHANNEL` and narrows the fresh token to it, so group delivery
survives even when the old token is dead. For an agent activated before line
grants existed, `agent-mgr scope-chat-credential <name>` does that narrowing
once, in place.

## Set up Latch (let it drive a Mac)

Mint the credential **on the Mac the agent should drive** — in Plow Latch,
*Agents → can't use OAuth? create a static credential*. Minting needs the
`relay:device` scope only that Mac's Latch holds, so this is the one
credential `agent-mgr` cannot fetch for you. It shows a JSON client config,
once. Then:

```sh
agent-mgr set-latch errands     # paste that whole JSON at the prompt
agent-mgr check-latch errands   # "latch reachable ... (HTTP 200)"
```

`set-latch` pulls `DOMO_DEVICE_UID` and `DOMO_MCP_TOKEN` out of the blob (a
bare UID + token pair on two lines still works). Everything is read on
**stdin**, never argv (a flag would put a live relay credential in the shared
host's process table). `set-latch` preserves every other key in the dotenv.
The *token* is what binds — reusing someone else's pair points the agent at
their Mac.

**An agent that drives no Mac deletes the `latch:` block from its
`config.yaml`.** The config is the declaration `check-latch` reads: block
present with blank `DOMO_*` is reported as broken; block gone is a clean
"no latch configured".

## Onboarding someone who is not you

Same deployment, one difference: three steps happen on **their** devices —
the account binding (their handset), the model credential (their browser),
and the Latch credential (their Mac). Before you start:

- **Confirm their Mac runs Plow Latch** (if the agent will drive one) — a
  missing Latch found mid-flow means a second sitting with them.
- **Start only when they're present** — the activation code can't be sent
  ahead (short TTL, one-time spend) and both blocking steps need them live.
- **Tell them where their credentials live**: their Plow token — and through
  it their mailbox — sits in a dotenv on this host, readable by whoever runs
  `agent-mgr`.

Then the sequence (each step finishes before the next):

| | who | what |
|---|---|---|
| 1 | you | `register` (or `new`), `deploy`, set `AGENT_TZ` in their dotenv, `up` |
| 2 | you | `agent-mgr activate bob` — prints the code and number, then polls |
| 3 | **them** | text the code **from the handset that should own the agent** |
| 4 | you | `agent-mgr sign-in bob` — prints a device-code URL, waits on the browser |
| 5 | **them** | open the URL in *their* browser, enter the code |
| 6 | **them** | Plow Latch → Agents → *can't use OAuth? create a static credential* |
| 7 | you | `set-latch bob`, paste the JSON it showed (it reloads a running agent itself), then `check-latch bob` |
| 8 | you | `agent-mgr cron-sync bob` — only if the repo names a cron spec |
| 9 | **them** | reply to the agent's 👋 from that handset — it runs setup (`life-assistant-hermes-agent` README § Bring-up) |

The Latch pair is copy-once by design — Latch drops it from memory once
saved. If it was relayed through a chat window to reach you, treat it as
disclosed and re-mint once the agent is up; re-minting is free.

## Day to day

```sh
agent-mgr ls
agent-mgr up <name> / down <name> / restart <name> / logs <name>
agent-mgr agent <name> "what's on today?"
agent-mgr check-latch <name>
agent-mgr check-connectors <name>
```

Both `check-` commands probe **from inside the container**, deliberately with
no host fallback — egress, DNS and CA config all differ between your shell
and that network namespace.

## Backups

The repo is the image and the home is the volume: `deploy` rebuilds the
image half from git any time, but nothing rebuilds the home — `auth.json`,
the dotenv, sessions, memories. So back the homes up nightly.

One-time setup — the destination must be a directory you own that nobody
else can write (the command refuses otherwise; a symlink onto a bigger disk
is fine):

```sh
mkdir -p ~/agent-backups && chmod 700 ~/agent-backups
```

(The `chmod` is load-bearing: `mkdir -m 700` doesn't touch an *existing*
directory.)

Nightly cron, 14 days kept:

```sh
0 4 * * * { ~/.local/bin/agent-mgr backup-homes ~/agent-backups && ~/.local/bin/agent-mgr prune-backups ~/agent-backups 14 ; } >> ~/backup-homes.log 2>&1
```

Keep that shape: the `&&` stops retention from pruning after a failed backup,
the `{ …; }` + redirect is the only observability cron gives you here, and
the log lives *outside* the destination so an unmounted disk still gets
logged. Runs land in a marker-protected `backup-homes/` child; `prune-backups`
deletes only inside it, so files you keep beside the backups are safe. (A
`backup-homes/` written by a pre-marker version: adopt it with
`touch ~/agent-backups/backup-homes/.written-by-backup-homes`.)

What an archive is worth:

- It's a **live copy** — tar's "file changed as we read it" is tolerated (the
  gateway's SQLite session DB is always being written). For a consistent
  copy, `agent-mgr down <name>` first.
- A file tar **couldn't read at all** fails that home loudly and its archive
  is deleted — a valid-looking archive missing `auth.json` is worse than none.
  One home failing doesn't stop the others, but the run exits non-zero, so
  the `&&` holds retention back.
- **Watch for a home that fails every night**: retention never runs, the
  destination grows a full sweep per night, and it looks healthy from the
  outside. Grep `~/backup-homes.log` for `one or more homes were not
  archived`.
- A killed run leaves a truncated newest archive; `gzip -t <archive>` before
  restoring, and fall back to the previous night's.

### Restoring a home

Two blocks, because **you** move the old home aside between them.

Step 1 — verify the archive, stop the agent, resolve the home:

```sh
a=~/agent-backups/backup-homes/20260826T040112Z-4171/hermes-errands.tar.gz \
  && gzip -t "$a" \
  && agent-mgr down errands \
  && home=$(readlink -f "$(agent-mgr resolve errands | sed -n 's/^AGENT_HOME=//p')") \
  && echo "move $home aside now, then run step 2"
```

Move `$home` aside: **same disk, not `/tmp`, and a path that neither matches
nor sits under `~/.hermes*`** (a sibling like `.hermes-errands.old` would be
archived by the nightly as a live home). It holds everything written since
the archive — keep it until you've watched the restored agent run.

Step 2, in the same shell:

```sh
mkdir "$home" \
  && tar -C "$home" -xzf "$a" \
  && agent-mgr deploy errands \
  && agent-mgr up errands
```

Notes: `&&` not `set -e` (this is pasted into your shell); `down` can
legitimately refuse via the agent's `AGENT_PRE_TRANSITION` hook; `mkdir` not
`mkdir -p` — `File exists` is the emptiness check, and `tar -xzf` overlays
rather than replaces; `logs/`, `cache/` and `lazy-packages/` are excluded
from archives and won't reappear — expected, not truncation.

## Where does my code go?

Ask: **would a second agent want this?** No → it belongs in that agent's own
repo. The contract and worked examples are in the README's *What belongs in
this repo*; the file table is in *What belongs in an agent's repo*. A value belonging to a *person* (several instances share one repo)
goes one level further down, into the instance's own dotenv — see the
per-person bullet under § Set up a new agent.

An agent's repo starts as the two files `new` writes (`agent.env`,
`config.yaml` — every `agent.env` key is an override, so it may be empty).
`skills.tsv` appears on first `add-skill`; `compose.override.yml` only if you
write one (derived image or extra mounts). Two rules for that override:

- **Relative paths don't work** — Compose resolves them against `agent-mgr`'s
  directory. Name paths through a variable set in `agent.env`.
- **A `build:` must carry `pull_policy: never`** (or `build`) — the default
  *pulls* the tag when absent locally, so a registry image could land over
  what this host built and run with the agent's credentials. (A digest-pinned
  `image:` satisfies the guard only for a service *without* `build:`.) Until
  the line is there,
  `resolve-guard` refuses every Compose-resolving command — everything but
  the registry bookkeeping (`ls`, `register`, `unregister`, `new`,
  `resolve`). A running container keeps running; you just lose the agent-mgr
  surface over it. `activate` is the one command that neither refuses nor
  fully works: it swallows the guard's refusal on its final reload (so the
  one-time activation is never re-spent), writes the credential, and skips
  the reload — leaving you to fix the line and then `restart`, which itself
  refuses until you do.

```yaml
services:
  hermes:
    build: { context: "${AGENT_BUILD_CONTEXT}" }
    image: my-agent:local
    pull_policy: never
```

## Adding a shared skill

For a skill **another agent also wants**. (One agent's own skill is just its
code — it lives in that agent's repo, mounted read-only via
`compose.override.yml`; `plow-pbc/property-hunt-hermes-agent` is a worked
example.)

```sh
agent-mgr add-skill errands plow-pbc/plow-hermes-agent \
    --dest productivity/plow-connectors \
    --src image/seed/skills/productivity/plow-connectors
```

The pin lands in the agent repo's `skills.tsv`, reviewable beside its config.
Refs are always 40-char SHAs — a branch would silently re-point a running
agent on the next upstream push.

## Bumping pins

The shared pins live in `runtime/stack.json`. `agent-mgr deploy <name>`
applies them as part of the whole deploy — the normal path. When only one
thing changed:

- `agent-mgr install-plugin <name>` — after bumping
  `artifacts.plow_chat_plugin.revision`; skips an expensive deploy hook.
- `agent-mgr install-skill <name>` — the fleet skills
  (`google_workspace_skill`, `plow_invite_skill`); also the first fix for an
  agent reporting `NOT_AUTHENTICATED` from the image-bundled
  `google-workspace` copy. A destination the agent's own `skills.tsv` pins is
  authoritative and skipped — bump that row and re-run `deploy` instead.

**Four SHA pins exist in one repo and one of them may never move** — before
bumping any, read *What this builds on* in the [README](../README.md), which
owns that rule.

## Running a one-off container

```sh
agent-mgr compose <name> run --entrypoint bash --rm --no-deps -T hermes -c '...'
```

`--entrypoint` must be the **first** argument after `run`, with a value —
without a replaced entrypoint the image's s6 boots a second gateway beside
the live one. (Same reason `agent-mgr agent` uses `exec`, not `run` — the
measured cost of the second gateway is in *Why it exists* in the
[README](../README.md).)

## When something is wrong

| symptom | look at |
|---|---|
| `refusing to act: compose resolved ...` | the descriptor or override disagrees with the agent you named — this is the guard working |
| `HERMES_UID ... must be set` | you ran `docker compose` directly; go through `agent-mgr` |
| `... builds its image but its pull_policy is ...` | a service that builds must set `pull_policy: never` (or `build`). The default and `missing` both **pull** when the local tag is absent, replacing what this host built |
| `refusing a fetch that could replace a built image` | `pull` has no accepted form — use `up`, which fetches under the file's `pull_policy` that `resolve-guard` checks. A `--pull` takes only `never` or `build`, except on `build`, where it is a boolean that re-pulls the base image and rebuilds |
| `refusing 'compose run'...` | `--entrypoint` must be the **first** argument after `run` and carry a non-empty value — see § Running a one-off container |
| `... is REVOKED` | mint a fresh Latch credential from the Mac |
| `no answer from api.plow.co` | the credential was **not** tested; this is a network fault, not a bad token |
| a shared skill behaves oddly | compare the SHA in `skills.tsv` against what upstream has since fixed |
| `configured group(s) not on this agent's line` | verify `PLOW_HOME_CHANNEL` names a chat on the intended line, then run `scope-chat-credential` |

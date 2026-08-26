# agent-mgr

One CLI for the Hermes agent fleet. It brings up and manages containers running
[Hermes](https://howto.plow.co/hermes) with **Plow Chat** — the agent's phone
line — and **Plow Latch** — the Mac it is allowed to drive. Standing up a new
agent is a command rather than a copy-paste of the last one.

Install is a clone and a symlink — there is no release and no package:

```sh
git clone git@github.com:plow-pbc/agent-mgr.git ~/services/agent-mgr
mkdir -p ~/.local/bin
ln -sf ~/services/agent-mgr/agent-mgr ~/.local/bin/agent-mgr
```

`~/.local/bin` must be on your `PATH`. If the clone has no `agent-mgr` script at
its root, the CLI has not landed on `main` yet.

```sh
agent-mgr new rowan          # scaffold the agent's repo, both platforms wired
agent-mgr restore rowan      # the whole deploy: config, plugin, restore hook
agent-mgr activate rowan     # prints a code; text it from the agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan
```

## Why it exists

Four sibling repos hand-copied the same deployment scaffold, and the copies
drifted. One of them ran the agent with `docker compose run` instead of `exec`
— and the image's s6 entrypoint starts a gateway *whatever command you pass
it*, so every throwaway command booted a **second** gateway against the same
home, answered alongside the live agent, and posted a shutdown notice into the
owners' channel on exit. Measured over two days on one host: 25 gateway starts
against a 1–6/day baseline, 21 shutdown notices in a single day, and 6 sqlite
errors from two gateways racing one session database.

Two of the four repos already carried a test for that invariant. The repo using
`docker compose run` was not one of them. An invariant found twice, written into
a test twice, and missed exactly where it mattered — that is the argument for
one tool in one sentence.

## What belongs in this repo

This repo holds **common mechanism only**. It carries no agent instances, no
personal paths, and no credentials.

| layer | lives in | what it is |
|---|---|---|
| **common** | this repo | true of *every* Hermes agent — bring-up, activation, the pins, the veto seam |
| **agent** | `<agent>-hermes-agent` | everything else that agent needs: identity, config, its own skill, scripts, recipes |

The test for where something belongs: **would a second agent want this?**

- `agent-mgr up` — yes, every agent needs it → **common**
- `agent.env` — no, it *is* this agent's identity → **its repo**
- a recipe that publishes a property map — no, one agent runs it → **its repo**
- the Plow Chat plugin — yes, every agent → **common**, pinned by SHA
- a skill two agents share — pinned by SHA from upstream, installed by `add-skill`

One question, two buckets, and the answer for a shared artifact is the same
whether it is a skill or a plugin: **pin it, don't copy it.** The rule is
dynamic — something one agent uses today graduates the moment a second wants
it, which is the point at which it earns a pin instead of a directory.

**There is no third repo per skill.** A skill only one agent runs is that
agent's code and belongs beside its compose file. Splitting it out costs a
SHA-pin bump on every change, paid at both ends, and buys nothing until a
second consumer exists.

## The fleet — what agent-mgr deploys

Four agents on one Linux host (`wakeup`), each with its own repo. All four are
private: they hold live credentials, and one holds an operations wiki compiled
from real guest conversations.

| repo | what the agent is | what makes it different |
|---|---|---|
| [`srosro/str-hermes-agent`](https://github.com/srosro/str-hermes-agent) | short-term rentals — messages guests, answers from the operations wiki, unlocks doors | the only one running its product end to end; carries a vault mount and a PMS |
| [`srosro/sams-property-hermes-agent`](https://github.com/srosro/sams-property-hermes-agent) | house hunting — reads a photo of a listing, identifies the house, puts it on a private map | holds almost nothing: scripts, store, map and browser all live on the Mac, reached through Latch |
| [`srosro/rowans-life-hermes-agent`](https://github.com/srosro/rowans-life-hermes-agent) | life and family logistics — mail, calendar | **not the same person's agent** — keyed to a different Plow account, and the only one on `America/Chicago` |
| [`srosro/sams-admin-hermes-agent`](https://github.com/srosro/sams-admin-hermes-agent) | the operator's mail and calendar | the thinnest of the four |

Two repos in orbit around them:

- [`plow-pbc/property-hunt`](https://github.com/plow-pbc/property-hunt) — the
  house-hunting skill the property agent runs. Split out to be distributable
  before that delivery path was retired; under the rule above it belongs in the
  agent's own repo, and the split is being unwound rather than defended.
- [`srosro/sams-str-vault`](https://github.com/srosro/sams-str-vault) — the STR
  agent's operations corpus, committed by hand and mounted beside its home.

## What belongs in an agent's repo

`agent-mgr new` scaffolds the first two rows. Thin is not the goal — an agent
that does a lot has a lot here, and that is the shape working. What is *not* the
shape: a second copy of something `agent-mgr` already owns.

| path | required | what |
|---|---|---|
| `agent.env` | yes | the descriptor — overrides only; it may be entirely comments |
| `config.yaml` | yes | the declarative half of the agent's home — model, plugins, `mcp_servers`. `AGENT_CONFIG` may name another relative path; the rentals agent keeps its under `runtime/` |
| `README.md` | yes, hand-written | what this agent is, what it can and cannot reach, and how to bring it up |
| `.env.example` | if it has extra keys | the dotenv contract, with no values |
| `tests/` | if it has siblings | what this agent must **not** reach, asserted |
| `skills.tsv` | if it installs a **shared** skill | written by `add-skill`; one pinned SHA per skill |
| `SKILL.md`, `scripts/`, `references/` | if the agent does something | its own skill: the instructions the container reads, and whatever runs for them |
| `compose.override.yml` | if it needs a derived image or extra mounts | paths must go through a variable set in `agent.env`, and a `build:` needs `pull_policy: never` (or `build`) beside it unless the `image:` is a digest — [HOWTO](docs/HOWTO.md#what-an-agents-repo-contains) has the shape and what `resolve-guard` refuses without it |
| a restore hook | if it has its own deploy step | named by `AGENT_RESTORE_HOOK`; `restore` sequences it, so one command is the whole deploy |
| a pre-transition guard | if stopping it at the wrong moment costs something | named by `AGENT_PRE_TRANSITION`; every route to a container transition asks it first, and a refusal refuses the command — except `activate`, which reports success and skips the restart, having already spent a one-time activation a red exit would invite you to spend again. `restore` asks twice — a preflight, then the reload it ends with — so write it to be safe to ask more than once |

What must **not** be there is the common half: **no `compose.yml`, no activation
script, no `model-provider` or `reload-if-running`** — `agent-mgr` owns those,
and a copy is a fork of the fleet that drifts silently. A `justfile` is the one
near-miss: keep it for this agent's own recipes and tests, never to restate
`up`, `restore` or `activate`.

**Pin upstream, never vendor it.** Every artifact from another repo arrives at
an exact ref: a git artifact (plugin, skill) by 40-char SHA, a container image
by `sha256:` digest — never a tag or a branch. (One exception: an image this host
**builds**, which may carry any tag — the rentals agent's
`sams-str-hermes-agent:local`, say. A `build:` service must declare
`pull_policy: never` (or `build`), because the default and `missing` both
**pull** when the local tag is absent — measured, not assumed — and the fetched
image then runs with the agent's credentials. With that set, there is nothing
mutable to substitute. A `pull`, or a `--pull` naming anything but
`never`/`build`, is refused through this tool for the same reason — `pull` with
no admitted form, because which spellings of `--ignore-buildable` Compose
honours is a fact about its flag parser, and every miss fails open. `resolve-guard` checks what Compose **resolves**, so an override cannot
slip a pulled tag past it.) A moving ref re-points a running
agent on the next upstream push, and these carry the chat token and drive a
filesystem. Copying the artifact in instead makes the agent's repo a fork of it
— which is what
[`srosro/str-hermes-agent#138`](https://github.com/srosro/str-hermes-agent/pull/138)
spent −1,311 LOC undoing, after a vendored plugin drifted until production was
serving a working tree.

**Assert what it cannot reach.** Siblings share a host, so the realistic failure
is a copy-paste — a compose file, a config block, a descriptor — that quietly
widens one agent's blast radius. A `tests/test_config_contract.py` in each agent
turns that into a red suite: the homes it must not mount, the `mcp_servers` it
must not carry, the vault it must not see.

**Operating knowledge belongs under `docs/`.** How guest messaging works, what
the wiki compiles from, which locks answer to which door — that is documentation
of the *product*. The tell is proportion: when the README is longer than the
thing it documents, it has stopped being a README and become a runbook that
nothing verifies.

## What this builds on

```
  plow-pbc/seed-plow-chat        the Plow Chat wire protocol, REST + WSS
            │                    (spec, not SDK — this repo never calls it directly)
            │ implemented by
  plow-pbc/hermes-plow-chat      the plow-chat-platform plugin: the phone line
            │
            │ pinned by 40-char SHA
            ▼
        agent-mgr ───── pinned by image digest ─────▶ nousresearch/hermes-agent
            │                                          (the runtime; third party)
            └───── the agent's config.yaml ─────▶ Plow Latch, over the relay at api.plow.co
```

| dependency | what it is | pinned as |
|---|---|---|
| [`nousresearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) | the agent runtime; third-party image | a **`sha256:` digest** |
| [`plow-pbc/hermes-plow-chat`](https://github.com/plow-pbc/hermes-plow-chat) | the `plow-chat-platform` plugin — the phone line | a **40-char SHA** |
| [`plow-pbc/seed-plow-chat`](https://github.com/plow-pbc/seed-plow-chat) | the protocol that plugin implements | not consumed directly |
| [`plow-pbc/latch`](https://github.com/plow-pbc/latch) | the Mac an agent drives, over the relay | named in the agent's `config.yaml`; credentials come from its own dotenv, never from git |

Both pins are exact on purpose — a `sha256:` digest for the image, a 40-char
SHA for the plugin. A tag or a branch re-resolves on the next pull, which
silently changes a large unreviewed surface under a running agent that
holds live credentials — and for the plugin, one that holds the chat token.

## Sharing with `plow-pbc/plow`

[`plow-pbc/plow`](https://github.com/plow-pbc/plow)'s `cloud-agents/hermes`
stands up the *same* Hermes runtime for Plow's customers: one VM per tenant,
native under systemd, provisioned by `POST /v1/agents/cloud`. This repo is the
other end — one host, many agents, Docker, driven by a person at a terminal.

Same protocol underneath, different products around it. So the posture is:

**Converge on the artifacts.** The plugin, the upstream image and the
integration reference are the *same facts* on both sides, and a fix to one
should reach the other. Where they have already forked, it is tracked rather
than tolerated:

- [`plow-pbc/plow#1394`](https://github.com/plow-pbc/plow/issues/1394) —
  `cloud-agents/hermes` carries its own copy of the plugin, under the same id
- [`#2`](https://github.com/plow-pbc/agent-mgr/issues/2) — the upstream image
  pin here drifts from plow's blessed base

**Keep the managers separate.** Provisioning here is a bash CLI over Compose;
there it is a `Provider` protocol behind an HTTP endpoint. Activation here is a
code a person texts from the owning handset; there a token is minted
machine-side. Latch and per-agent repos have no tenant equivalent at all.
Unifying those would be an abstraction over two things that are not the same
thing.

The test is the same shape as the layer test above: **is this the same fact, or
does it only look similar?** A pinned SHA is the same fact. A lifecycle is not.

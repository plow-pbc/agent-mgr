# agent-mgr

A CLI that stands up, on a host of your own, the same kind of agent that
[Plow](https://plow.co) runs for its customers in the cloud: a container
running [Hermes](https://howto.plow.co/hermes) with **Plow Chat** — the
agent's phone line — and **Plow Latch** — the Mac it is allowed to drive. It
mirrors the cloud Hermes infrastructure in
[`plow-pbc/plow`](https://github.com/plow-pbc/plow) (`cloud-agents/hermes`):
the same upstream runtime, the same plugin at the same pin, the same protocol
to the same API — so a fix on either side reaches the other (the one tracked
gap is the runtime image's pin, [`#2`](https://github.com/plow-pbc/agent-mgr/issues/2)).
What differs is the product around it: there, one VM per tenant behind an
HTTP endpoint; here, one host, many agents, Docker, a person at a terminal.
Standing up a new agent is a command rather than a copy-paste of the last one.

Install is a clone and a symlink — there is no release and no package:

```sh
git clone git@github.com:plow-pbc/agent-mgr.git ~/services/agent-mgr
mkdir -p ~/.local/bin
ln -sf ~/services/agent-mgr/agent-mgr ~/.local/bin/agent-mgr
```

`~/.local/bin` must be on your `PATH`. If the clone has no `agent-mgr` script at
its root, the CLI has not landed on `main` yet.

It runs on the Linux host the fleet lives on and on a Mac, where the floor is
**macOS 12.3** — the release whose `readlink` grew `-f`, which the entrypoint
uses to resolve itself through the symlink above before anything else is
sourced. Everything after that point is portable, and `python3`, `docker` and
an authenticated `gh` have to be on `PATH` — `restore` installs the Plow Chat
plugin and the fleet `google-workspace` skill through `gh api` for **every**
agent, not only one shipping a `skills.tsv` (one whose own `skills.tsv` pins
`productivity/google-workspace` keeps its instance copy instead).

```sh
agent-mgr new errands ~/services/errands-hermes-agent
agent-mgr restore errands    # the whole deploy: config, plugin, restore hook
agent-mgr activate errands   # prints a code; text it from the agent's phone
agent-mgr up errands
agent-mgr cron-sync errands  # only if its agent.env names a cron spec
agent-mgr sign-in errands
agent-mgr set-latch errands  # the Mac's pair, on stdin; only if it drives one
agent-mgr check-latch errands
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
- the `google-workspace` redirect skill — yes, every agent → **common**, pinned
  in `runtime/google-workspace-skill.ref` (a fleet pin `restore` installs and
  `install-skill` re-installs — except an agent whose own `skills.tsv` pins that
  destination, where the instance pin is authoritative and both defer to it)
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

Three agent repos on one Linux host (`wakeup`). The repos are code only: live
credentials sit in each instance's home on the host, and the STR agent's
operations wiki — compiled from real guest conversations — lives in its own
private vault repo, never in the agent's.

| repo | what the agent is | what makes it different |
|---|---|---|
| [`plow-pbc/str-hermes-agent`](https://github.com/plow-pbc/str-hermes-agent) | short-term rentals — messages guests, answers from the operations wiki, unlocks doors | the only one running its product end to end; carries a vault mount and a PMS |
| [`plow-pbc/property-hunt-hermes-agent`](https://github.com/plow-pbc/property-hunt-hermes-agent) | house hunting — reads a photo of a listing, identifies the house, puts it on a private map | the skill and the agent are one checkout, mounted rather than pinned; the store, map and browser live on the Mac, reached through Latch |
| [`plow-pbc/life-assistant-hermes-agent`](https://github.com/plow-pbc/life-assistant-hermes-agent) | life and family logistics — mail, calendar | the thinnest: no vault, no product surface, nothing on the Mac |

Adding one here means adding it to `.knightwatch/siblings` too, or a reviewer
of this repo cannot read it. This table is the authority; that file only makes
these repos searchable.

**A repo is not an agent — a registry row is.** Identity derives from the
registered name rather than the directory, so a row may be named for a person
(`sam-property`) against a repo named for a capability — and one checkout can
serve several rows at once. See
[One repo, several people](#one-repo-several-people) for what makes that safe.

One repo in orbit:

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
| a cron spec | if it ships scheduled jobs | named by `AGENT_CRON_SPEC`; declarative rows `cron-sync` converges onto the scheduler, reading hermes's own `jobs.json` — never `cron list` output. `deliver` is explicit on every row — a card-only job declares `local`, hermes's own no-chat-delivery target — and a `${VAR}` in it may only name a delivery identifier ending `_UID` or `_CHANNEL` — the env it expands from holds credentials one line away, and the expansion lands in argv and persists in `jobs.json`. A row's `blocked` reason keeps it versioned but unregistered. Agent-authored crons are invisible to it |
| `SKILL.md`, `scripts/`, `references/` | if the agent does something | its own skill: the instructions the container reads, and whatever runs for them |
| `compose.override.yml` | if it needs a derived image or extra mounts | paths must go through a variable set in `agent.env`, and a `build:` needs `pull_policy: never` (or `build`) beside it unless the `image:` is a digest — [HOWTO](docs/HOWTO.md#what-an-agents-repo-contains) has the shape and what `resolve-guard` refuses without it |
| `AGENT_LIVE=1` | if real people's workflows run through it | declared in `agent.env`; the gateway messages its person at every restart, so a restart of a live agent is user-visible. agent-mgr asks `[y/N]` at a terminal before any transition and refuses non-interactively unless `AGENT_TRANSITION_ACK=1` — the explicit acknowledgement for automation that means to restart |
| a restore hook | if it has its own deploy step | named by `AGENT_RESTORE_HOOK`; `restore` sequences it, so one command is the whole deploy -- except crons, which are `cron-sync`'s and run against a live gateway |
| a pre-transition guard | if stopping it at the wrong moment costs something | named by `AGENT_PRE_TRANSITION`; every route to a container transition asks it first, and a refusal refuses the command — except `activate`, which reports success and skips the restart, having already spent a one-time activation a red exit would invite you to spend again. `restore` asks twice — a preflight, then the reload it ends with — so write it to be safe to ask more than once |

What must **not** be there is the common half: **no `compose.yml`, no activation
script, no `model-provider` or `reload-if-running`, no hand-rolled cron
registration** — `agent-mgr` owns those,
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
honours is a fact about its flag parser, and every miss fails open. (`build
--pull` is the exception: there it is a boolean that re-pulls the base image and
rebuilds, so the output is still what this host built.) `resolve-guard` checks what Compose **resolves**, so an override cannot
slip a pulled tag past it.) A moving ref re-points a running
agent on the next upstream push, and these carry the chat token and drive a
filesystem. Copying the artifact in instead makes the agent's repo a fork of it
— which is what
[`plow-pbc/str-hermes-agent#138`](https://github.com/plow-pbc/str-hermes-agent/pull/138)
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

## One repo, several people

An agent repo is normally one person's. It does not have to be: `AGENT_HOME`,
`AGENT_CONTAINER` and `AGENT_PROJECT` derive from the **registry name**, so two
rows against the *same checkout* resolve to separate homes and containers.

```sh
agent-mgr register alice ~/services/shared-hermes-agent
agent-mgr register bob   ~/services/shared-hermes-agent   # same directory
```

`require_own_home` already enforces what makes that safe: it accepts a home only
when it ends in `.hermes-<name>`, so a repo that *declared* `AGENT_HOME` could
not be shared at all — the second instance would resolve to the first's home and
be refused. A shared repo stays silent on identity, and silence is the only
thing that works.

### Where a per-person value goes

**The instance's own dotenv** — `$AGENT_HOME/.env`, the file that already holds
its Plow token and its Latch credential, mounted at `/opt/data`.

`$AGENT_HOME` is `~/.hermes-<name>` by convention, but it is whatever the
instance *resolved* — an agent whose descriptor declares `AGENT_HOME` keeps its
dotenv beside that home, and `agent-mgr resolve <name>` prints the path it will
read.

Almost nothing needs `agent-mgr` involved at all: the gateway interpolates
`${VAR}` in `config.yaml` from that same dotenv at runtime, which is how
`mcp_servers.latch` already reaches a different Mac per instance. A per-person
model, locale or endpoint is a line in that file and a `${VAR}` in the shared
`config.yaml`. No fork, no second config, nothing here to change.

**`AGENT_TZ` is the one exception**, and only for a mechanical reason: Compose
sets `TZ` into the container at *render* time, so the gateway never sees it and
cannot resolve it from the dotenv the way it resolves everything else. So
`load_agent` reads that one key from the same file:

```sh
BOB_HOME=$(agent-mgr resolve bob | sed -n 's/^AGENT_HOME=//p')
printf '\nAGENT_TZ=America/Chicago\n' >> "${BOB_HOME:?resolve printed no home}/.env"
```

The leading newline is not decoration. A dotenv the gateway or a person last
wrote may not end in one, and a bare `>>` would then append onto the final line
— turning `PLOW_AGENT_TOKEN=…` into `PLOW_AGENT_TOKEN=…AGENT_TZ=…` and taking the
instance off its chat, not just off its clock. An extra blank line is skipped.

Precedence is **dotenv > the repo's `agent.env` > convention**, and the dotenv is
read after the home is known, so it cannot move its own home.

To hand a person back to the repo's zone, **delete the line** — do not blank it.
`AGENT_TZ=` is refused, because assigning an empty value is indistinguishable
from never declaring one: it would clear the repo's zone, let the convention
default fill in, and run that container on a third zone neither file named.

`AGENT_TZ` alone, deliberately — that file holds credentials. One non-secret
value is taken into `agent-mgr`'s process; `TZ` still reaches the container
through `environment:`, so nothing from the dotenv goes to Compose and the
fleet's no-credential-through-compose contract is untouched. Any other key there
is ignored, including one `agent-mgr` owns.

## What this builds on

```
  plow-pbc/hermes-plow-chat      the plow-chat-platform plugin: the phone line
            │                    implementing the Plow Chat API (api.plow.co/openapi.json)
            │                    — and, at an earlier SHA, the activation script
            │ pinned TWICE, each by 40-char SHA:
            │   runtime/plow-chat-plugin.ref     the plugin directory
            │   runtime/plow-chat-activate.ref   create_plow_chat_curl.sh
            ▼
        agent-mgr ───── pinned by image digest ─────▶ nousresearch/hermes-agent
            │                                          (the runtime; third party)
            └───── the agent's config.yaml ─────▶ Plow Latch, over the relay at api.plow.co
```

| dependency | what it is | pinned as |
|---|---|---|
| [`nousresearch/hermes-agent`](https://github.com/NousResearch/hermes-agent) | the agent runtime; third-party image | a **`sha256:` digest** |
| [`plow-pbc/hermes-plow-chat`](https://github.com/plow-pbc/hermes-plow-chat) | the `plow-chat-platform` plugin — the phone line | a **40-char SHA**, in `runtime/plow-chat-plugin.ref` |
| the same repo, earlier | `ref/scripts/create_plow_chat_curl.sh`, which `activate` fetches | a **second 40-char SHA**, in `runtime/plow-chat-activate.ref` |
| [`plow-pbc/plow`](https://github.com/plow-pbc/plow) | the fleet `google-workspace` skill — the Latch redirect that replaces the image-bundled local-OAuth copy in every agent whose own `skills.tsv` does not pin that destination | a **40-char SHA**, in `runtime/google-workspace-skill.ref` |
| [`plow-pbc/latch`](https://github.com/plow-pbc/latch) | the Mac an agent drives, over the relay | named in the agent's `config.yaml`; credentials come from its own dotenv, never from git |

All four pins are exact on purpose — a `sha256:` digest for the image, a
40-char SHA for each of the two things taken from `hermes-plow-chat` and for
the fleet skill taken from `plow-pbc/plow`. A tag or a
branch re-resolves on the next pull, which silently changes a large unreviewed
surface under a running agent that holds live credentials — and for the plugin,
one that holds the chat token.

**The two SHA pins name one repo at two points in its history, and must not be
collapsed into one.** `Strip the SEED ceremony` deleted `ref/scripts/`, so the
plugin pin moves forward past that commit while `create_plow_chat_curl.sh`
exists only before it. A single shared ref would send the plugin's post-strip
SHA at the activate URL and 404 — on `activate`, the one command that is a
one-time irreversible spend. `tests/test_install.py` pins the pairing.

**Of the `hermes-plow-chat` pair, only `runtime/plow-chat-plugin.ref` may be
bumped** (`runtime/google-workspace-skill.ref` moves freely — it names a
different repo and nothing pairs with it).
`runtime/plow-chat-activate.ref` is frozen at a pre-strip commit and must not be
bumped forward at all — not to `HEAD`, not to any later SHA. That is the
realistic slip rather than the collapse above: someone reaching for "latest in
`hermes-plow-chat`" lands on `HEAD`, where the path this ref names no longer
exists. No test can prove the *ancestry* — that needs another repo's history —
so the suite pins the SHA itself, which reddens on any bump and makes moving
that ref deliberate.

## Sharing with `plow-pbc/plow`

[`plow-pbc/plow`](https://github.com/plow-pbc/plow)'s `cloud-agents/hermes`
is the cloud side this tool mirrors: the same Hermes runtime for Plow's
customers, one VM per tenant, native under systemd, provisioned by
`POST /v1/agents/cloud`. Same protocol underneath, different products around
it. So the posture is:

**Converge on the artifacts.** The plugin, the upstream image and the
integration reference are the *same facts* on both sides, and a fix to one
should reach the other. The plugin already is one fact: plow's blessed image
stages it from `runtime/plow-chat-plugin.ref` at build time. Where the two
still fork, it is tracked rather than tolerated:

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

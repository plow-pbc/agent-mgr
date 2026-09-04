# **Deprecated — use [`plow-pbc/plow-agents`](https://github.com/plow-pbc/plow-agents) instead.**
---

## Where changes go

This repo is one of several that assemble a Plow agent. The map of which repo
owns what is in
[`plow-hermes-agent` README § The repos](https://github.com/plow-pbc/plow-hermes-agent#the-repos);
read it before a change that touches a neighbour. The test is **who else would
have to change if this fact changed** — if the answer is a sibling, the change
belongs there and this repo takes a pin bump.

**Not here:**

- The boot contract — `HERMES_HOME`, the credentials file the gateway refuses to
  start without, the s6 ordering — belongs to
  [`plow-hermes-agent`](https://github.com/plow-pbc/plow-hermes-agent). This repo
  pins a digest and mounts a home; it does not restate those paths.
- The seed-skill tree — which skills every agent gets and where they live —
  belongs to [`hermes-plow-chat`](https://github.com/plow-pbc/hermes-plow-chat)'s
  `seed-skills/`. `runtime/stack.json` pins that tree; it does not enumerate the
  skills in it by name.
- The API, the relay and the registry that pins which image tenants boot belong
  to [`plow-pbc/plow`](https://github.com/plow-pbc/plow); the Mac side and the
  gog grammar belong to
  [`plow-pbc/latch`](https://github.com/plow-pbc/latch). This repo probes both
  and reimplements neither.

**Examples:**

- Adherence — #133 put the fleet on the shared `plow-hermes-agent` base, ending
  the second independent upstream-digest path this repo had been carrying:
  https://github.com/plow-pbc/agent-mgr/pull/133
- Violation — #142 restates the base's boot paths (`/var/lib/hermes`,
  `/var/lib/plow/credentials.host`) in a compose template here, with nothing
  binding the copy to the repo that owns them:
  https://github.com/plow-pbc/agent-mgr/pull/142

A CLI that stands up, on a host of your own, the same kind of agent that
[Plow](https://plow.co) runs for its customers in the cloud: a container
running [Hermes](https://howto.plow.co/hermes) with **Plow Chat** — the
agent's phone line — and **Plow Latch** — the Mac it is allowed to drive. It
mirrors the cloud Hermes infrastructure in
[`plow-pbc/plow`](https://github.com/plow-pbc/plow) (`cloud-agents/hermes`):
the same plugin at the same pin and the same protocol to the same API. The
base image is shared only up to `plow-pbc/plow-hermes-agent` `089a6b1`:
later bases move the home to `/var/lib/hermes` and gate the gateway behind a
`/var/lib/plow/credentials` file this repository never writes, so under the
`/opt/data` contract here they start no gateway at all. The fleet stays
pinned at `089a6b1` until it adopts that contract — the `plow-agents`
layout — tracked in [#130](https://github.com/plow-pbc/agent-mgr/issues/130).
What differs is the product around it: there, one VM per tenant behind an
HTTP endpoint; here, one host, many agents, Docker, a person at a terminal.
Standing up a new agent is a command rather than a copy-paste of the last one.

Install from a clone and symlink when developing or tracking `main`:

```sh
git clone git@github.com:plow-pbc/agent-mgr.git ~/services/agent-mgr
mkdir -p ~/.local/bin
ln -sf ~/services/agent-mgr/agent-mgr ~/.local/bin/agent-mgr
```

`~/.local/bin` must be on your `PATH`. If the clone has no `agent-mgr` script at
its root, the CLI has not landed on `main` yet.

Tagged releases also publish a self-contained `agent-mgr.pyz` and SHA-256
checksum. Put the downloaded zipapp on `PATH`, make it executable, and verify
the checksum before running it.

It runs on the Linux host the fleet lives on and on macOS with Python 3.11+.
`python3`, `docker` and
an authenticated `gh` have to be on `PATH` — `deploy` installs the Plow Chat
plugin and the pinned fleet skills (`google-workspace`, `plow-invite`) through
`gh api` for **every** agent, not only one shipping a `skills.tsv` (one whose
own `skills.tsv` pins a fleet destination keeps its instance copy for that
destination instead).

Then the whole setup, end to end (the [HOWTO](docs/HOWTO.md) explains each
step; `docker`, `python3` and an authenticated `gh` are the install block's
prerequisites above):

```sh
# 1. The agent's repo: clone an existing one — or scaffold fresh with
#    `agent-mgr new errands ~/services/errands-hermes-agent`, which also registers it
git clone git@github.com:plow-pbc/life-assistant-hermes-agent.git ~/services/life-assistant-hermes-agent
agent-mgr register errands ~/services/life-assistant-hermes-agent

# 2. Deploy
agent-mgr deploy errands              # home, config, plugin, pinned skills, deploy hook

# 3. Per-person config (after deploy, before up)
agent-mgr resolve errands             # prints AGENT_HOME — put AGENT_TZ=... in the .env there

# 4. Activate, start, sign in
agent-mgr activate errands            # text the code from the owner's phone; one-time spend
agent-mgr up errands
agent-mgr cron-sync errands           # only if its agent.env names a cron spec
agent-mgr sign-in errands             # device-code OAuth in the owner's browser

# 5. Smoke test
agent-mgr agent errands "hello, who are you?"
agent-mgr check-connectors errands

# 6. (Optional) Latch — let it drive a Mac. In Plow Latch ON THAT MAC, mint the
#    JSON via "can't use OAuth? create a static credential", then:
agent-mgr set-latch errands           # paste that whole JSON at the prompt (input hidden)
agent-mgr check-latch errands         # "latch reachable ... (HTTP 200)"
```

Tearing a test agent down: capture the home first — `agent-mgr resolve
<name>` prints `AGENT_HOME`, and after `unregister` nothing will resolve
it — then `down`, `unregister`, and delete that directory yourself.
Neither command touches it, and the nightly backup globs `~/.hermes*`, so
a dead test home would be archived forever.

Every command accepts `--json`. Reads return typed domain objects; operational
commands return a versioned envelope with exit status and captured output.
Errors use stable codes, so automation never has to parse terminal prose:

Machine consumers should always request JSON explicitly. Operational JSON
detaches terminal stdin: pipe input (for example, `credential-helper | agent-mgr
--json set-latch errands`), and set `AGENT_TRANSITION_ACK=1` for an intentional
live transition. `logs` and `compose` reject JSON because they can stream forever.

## Cloud agents

The cloud-control commands use Plow's API and return structured JSON:

```bash
export PLOW_API_BASE=https://api.plow.co
read -rsp 'Plow API token: ' PLOW_API_TOKEN; export PLOW_API_TOKEN

printf '%s\n' '{"name":"Mary","provider":"exe:hermes","chat_uids":["cht_example"]}' \
  | agent-mgr --json cloud-create
agent-mgr --json cloud-list
agent-mgr --json cloud-get AGENT_ID
printf '%s\n' '{"chat_uids":["cht_example","cht_second"]}' \
  | agent-mgr --json cloud-update-chats AGENT_ID
agent-mgr --json cloud-delete AGENT_ID
```

Create normally returns `status: "provisioning"`. Callers should poll
`cloud-get` until the agent reaches `running`, `failed`, or `teardown`. Retry
creation after `failed`; repeat deletion after `teardown`. agent-mgr never
contacts exe.dev or handles tenant credentials. Local Compose commands remain
separate from the cloud-control commands.

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
- a fleet skill — yes, every agent → **common**, one pin per tree in
  `runtime/stack.json` (the `google_workspace_skill`
  redirect and the `plow-invite` referral, both mirrored into
  `hermes-plow-chat`'s `seed-skills/` from plow-pbc/plow's hosted-agent seed;
  `deploy` installs and `install-skill` re-installs each —
  except an agent whose own `skills.tsv` pins that destination, where the
  instance pin is authoritative and both skip it)
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
| [`plow-pbc/life-assistant-hermes-agent`](https://github.com/plow-pbc/life-assistant-hermes-agent) | life and family logistics — mail, calendar, a Pi-hosted wall display | the thinnest: no vault; the wall is reached through Latch on the owner's Mac (same LAN), never from the agent directly |

Adding one here means adding it to `.knightwatch/siblings` too, or a reviewer
of this repo cannot read it. This table is the authority; that file only makes
these repos searchable.

**A repo is not an agent — a registry row is.** Identity derives from the
registered name rather than the directory, so a row may be named for a person
(`mark-property`) against a repo named for a capability — and one checkout can
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
| `compose.override.yml` | if it needs a derived image or extra mounts | paths must go through a variable set in `agent.env`, and a `build:` needs `pull_policy: never` (or `build`) beside it — [HOWTO](docs/HOWTO.md#where-does-my-code-go) has the shape and what `resolve-guard` refuses without it |
| `AGENT_LIVE=1` | if real people's workflows run through it | declared in `agent.env`; the gateway messages its person at every restart, so a restart of a live agent is user-visible. agent-mgr asks `[y/N]` at a terminal before any transition and refuses non-interactively unless `AGENT_TRANSITION_ACK=1` — the explicit acknowledgement for automation that means to restart. Once admitted, container shutdown gives Hermes up to 30 seconds to checkpoint the interrupted session and release its database leases before s6 escalates |
| a deploy hook | if it has its own deploy step | named by `AGENT_DEPLOY_HOOK`; `deploy` sequences it, so one command is the whole deploy -- except crons, which are `cron-sync`'s and run against a live gateway |
| a pre-transition guard | if stopping it at the wrong moment costs something | named by `AGENT_PRE_TRANSITION`; every route to a container transition asks it first, and a refusal refuses the command — except `activate`, which reports success and skips the restart, having already spent a one-time activation a red exit would invite you to spend again. `deploy` asks twice — a preflight, then the reload it ends with — so write it to be safe to ask more than once |

What must **not** be there is the common half: **no `compose.yml`, no activation
script, no `model-provider` or `reload-if-running`, no hand-rolled cron
registration** — `agent-mgr` owns those,
and a copy is a fork of the fleet that drifts silently. A `justfile` is the one
near-miss: keep it for this agent's own recipes and tests, never to restate
`up`, `deploy` or `activate`.

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
fleet's no-credential-through-compose contract is untouched. Any other key
there is ignored, including one `agent-mgr` owns — `AGENT_INDEX` among them,
which the container reads for itself rather than receiving through Compose.

### Usage reporting is per person

An agent can report its own token usage to the Agent Index. It is off unless
that instance's **own** dotenv opts in:

```sh
AGENT_HOME=$(agent-mgr resolve <agent> | sed -n 's/^AGENT_HOME=//p')
printf '\nAGENT_INDEX=1\n' >> "${AGENT_HOME:?resolve printed no home}/.env"
agent-mgr restart <agent>
```

The leading newline for the same reason as `AGENT_TZ` above, and the same
stake: a bare `>>` onto a file not ending in one welds the key to the last
line, turning `PLOW_AGENT_TOKEN=…` into `PLOW_AGENT_TOKEN=…AGENT_INDEX=1`.

`agent-mgr` does not read this key and does not pass it to Compose. The agent's
home is already mounted at `/opt/data`, so the reporter reads the switch from
that file itself. That is deliberate: `compose.override.yml` merges after the
template and can replace anything the template sets — measured, an override
naming `AGENT_INDEX` wins — and that override is shared by every instance
registered against the checkout. A switch there opts in siblings who never
asked. Kept in the per-person file, nothing in Compose can forge it.

`AGENT_ID` is the one value the container cannot derive for itself, so it does
come from the template, resolved from the registry name — and `resolve_guard`
checks it, because an override can forge that too. A forged one attributes a
person's usage to a sibling, silently: nothing fails, the wrong agent just
looks busier.

## What this builds on

| dependency | what it is | pinned as |
|---|---|---|
| [`plow-pbc/plow-hermes-agent`](https://github.com/plow-pbc/plow-hermes-agent) | the agent runtime: the shared cloud base, built `FROM nousresearch/hermes-agent` and carrying the bundled `plow_chat` plugin and seed skills. Pinned at `089a6b1`, the last base that boots under this repository's `/opt/data` contract; the cloud path has moved on (see #130) | a **`sha256:` digest**, at `images.hermes_local` in `runtime/stack.json` |
| [`plow-pbc/hermes-plow-chat`](https://github.com/plow-pbc/hermes-plow-chat) | the `plow-chat-platform` plugin — the phone line | a **40-char SHA**, at `artifacts.plow_chat_plugin` in `runtime/stack.json` |
| the same repo, earlier | `ref/scripts/create_plow_chat_curl.sh`, which `activate` fetches | a **second 40-char SHA**, at `artifacts.plow_chat_activation` |
| the same repo, at `seed-skills/` | the fleet `google-workspace` skill — the Latch redirect that replaces the image-bundled local-OAuth copy in every agent whose own `skills.tsv` does not pin that destination | a **40-char SHA**, at `artifacts.google_workspace_skill` |
| the same repo, at `seed-skills/` | the fleet `plow-invite` skill — the delight-triggered referral, mirrored from the hosted-agent image's seed (which carries the matching twin pointer) | a **40-char SHA**, at `artifacts.plow_invite_skill` |
| [`plow-pbc/latch`](https://github.com/plow-pbc/latch) | the Mac an agent drives, over the relay | named in the agent's `config.yaml`; credentials come from its own dotenv, never from git |

All five pins are exact on purpose — a `sha256:` digest for the image, and a
40-char SHA for each of the four things taken from `hermes-plow-chat`. A tag or a
branch re-resolves on the next pull, which silently changes a large unreviewed
surface under a running agent that holds live credentials — and for the plugin,
one that holds the chat token.

**The two SHA pins name one repo at two points in its history, and must not be
collapsed into one.** `Strip the SEED ceremony` deleted `ref/scripts/`, so the
plugin pin moves forward past that commit while `create_plow_chat_curl.sh`
exists only before it. A single shared ref would send the plugin's post-strip
SHA at the activate URL and 404 — on `activate`, the one command that is a
one-time irreversible spend. `tests/test_install.py` pins the pairing.

**Of the four `hermes-plow-chat` pins, `artifacts.plow_chat_plugin` and the
two fleet skills may be bumped; `artifacts.plow_chat_activation` may not.** The
fleet skills used to be exempt from this paragraph because they named a
different repo — they no longer do, so read it as applying to them too: they
move freely, but they move within the same history as the frozen activation
pin, and must never be collapsed onto it.
`artifacts.plow_chat_activation` is frozen at a pre-strip commit and must not be
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

**Converge on the artifacts.** The plugin, the base image and the integration
reference are the *same facts* on both sides, and a fix to one should reach
the other. The plugin already is one fact: plow's blessed image can consume
the same `runtime/stack.json` coordinate at build time. The base was one
too, up to `089a6b1`: this fleet runs `plow-pbc/plow-hermes-agent`'s published
base, the image `life-assistant-hermes-agent` built its cloud variant on at the
time. From `63c8b9c` the base moved its home to `/var/lib/hermes` and put
`plow-init` — which needs a `/var/lib/plow/credentials` file — in front of the
gateway, so under this repository's `/opt/data` contract those bases boot no
gateway. The fleet stays on `089a6b1` until it adopts the base's contract, the
`plow-agents` layout (#130).

That image declares `CMD ["/sbin/init"]` so its host can unpack it into a VM
rootfs under systemd; the fleet overrides both `entrypoint` and `command` in
`templates/compose.yml` and boots the same filesystem through s6 instead. One
image, two boot paths — which is why sharing it was possible at all, and why
the home path and the credential hand-off are the two things that must agree.

Where the two still fork, it is tracked rather than tolerated:

- [`plow-pbc/plow#1652`](https://github.com/plow-pbc/plow/issues/1652) — plow
  still builds a second base in-repo, published to a different registry, and
  it is that one its tenant provisioning blesses and pulls

**Keep the managers separate.** Provisioning here is a typed Python CLI over Compose;
there it is a `Provider` protocol behind an HTTP endpoint. Activation here is a
code a person texts from the owning handset; there a token is minted
machine-side. Latch and per-agent repos have no tenant equivalent at all.
Unifying those would be an abstraction over two things that are not the same
thing.

The test is the same shape as the layer test above: **is this the same fact, or
does it only look similar?** A pinned SHA is the same fact. A lifecycle is not.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Copyright 2026 The Plow Collective, Inc.

"Plow" and the Plow logo are trademarks of The Plow Collective, Inc. The license grants no trademark rights.

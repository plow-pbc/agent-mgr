# agent-mgr

One CLI for the Hermes agent fleet. It starts and manages containers running
Hermes with **Plow Chat** and **Plow Latch**, so standing up a new agent is a
command rather than a copy-paste of the last one.

```sh
agent-mgr new rowan          # scaffold the agent's repo, both platforms wired
agent-mgr restore rowan      # the whole deploy: config, plugin, restore hook
agent-mgr activate rowan     # prints a code; text it from the agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan
```

See [docs/HOWTO.md](docs/HOWTO.md) for the full walkthrough.

## What this builds on

```
  plow-pbc/seed-plow-chat        the Plow Chat wire protocol, REST + WSS
            │                    (spec, not SDK — this repo never calls it directly)
            │ implemented by
  plow-pbc/seed-hermes-plow      the plow-chat-platform plugin: the phone line
            │
            │ pinned in runtime/plow-chat-plugin.ref
            ▼
        agent-mgr ───── pinned in runtime/image.ref ─────▶ nousresearch/hermes-agent
            │                                              (the runtime; third party)
            └───── templates/config.yaml ─────▶ Plow Latch, over the relay at api.plow.co
```

| dependency | what it is | pinned as |
|---|---|---|
| `nousresearch/hermes-agent` | the agent runtime; third-party image | `runtime/image.ref`, a **digest** |
| `plow-pbc/seed-hermes-plow` | the `plow-chat-platform` plugin — the phone line | `runtime/plow-chat-plugin.ref`, a **40-char SHA** |
| `plow-pbc/seed-plow-chat` | the protocol that plugin implements | not consumed directly |
| Plow Latch | the Mac an agent drives, over the relay | `templates/config.yaml`; credentials come from the agent's own dotenv, never from git |

Both pins are exact on purpose. A tag or a branch re-resolves on the next pull,
which silently changes a large unreviewed surface under a running agent that
holds live credentials — and for the plugin, one that holds the chat token.

## Sharing with `plow-pbc/plow`

`plow-pbc/plow`'s `cloud-agents/hermes` stands up the *same* Hermes runtime for
Plow's customers: one VM per tenant, native under systemd, provisioned by
`POST /v1/agents/cloud`. This repo is the other end — one host, many agents,
Docker, driven by a person at a terminal.

Same protocol underneath, different products around it. So the posture is:

**Converge on the artifacts.** The plugin, the upstream image and the
integration reference are the *same facts* on both sides, and a fix to one
should reach the other. Where they have already forked, it is tracked rather
than tolerated:

- [`plow-pbc/plow#1394`](https://github.com/plow-pbc/plow/issues/1394) —
  `cloud-agents/hermes` carries its own copy of the plugin, under the same id
- [`#2`](https://github.com/plow-pbc/agent-mgr/issues/2) — the upstream image
  pin here drifts from plow's blessed base
- [`plow-pbc/seed-hermes-plow#15`](https://github.com/plow-pbc/seed-hermes-plow/issues/15) —
  the plugin this repo pins has a defect plow's copy already fixed

**Keep the managers separate.** Provisioning here is a bash CLI over Compose;
there it is a `Provider` protocol behind an HTTP endpoint. Activation here is a
code a person texts from the owning handset; there a token is minted
machine-side. Latch and per-agent repos have no tenant equivalent at
all. Unifying those would be an abstraction over two things that
are not the same thing.

The test is the same shape as the layer test below: **is this the same fact, or
does it only look similar?** A pinned SHA is the same fact. A lifecycle is not.


## Two layers, and what belongs in each

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
- the `plow-connectors` skill — yes, several agents already → pinned by SHA
  from upstream and installed by `add-skill`

One question, two buckets, and the answer for a shared artifact is the same
whether it is a skill or a plugin: pin it, don't copy it. The rule is dynamic —
something one agent uses today graduates the moment a second wants it, which is
the point at which it earns a pin instead of a directory.

**There is no third repo per skill.** A skill only one agent runs is that
agent's code and belongs beside its compose file. Splitting it out costs a
SHA-pin bump on every change, paid at both ends, and buys nothing until a
second consumer exists — and the distributable-skill delivery path that once
justified the split is no longer one.

## What belongs in an agent's repo

`agent-mgr new` scaffolds the first two rows below. The README is yours to
write; everything under it is added only when that agent actually needs it.
Thin is not the goal — an agent that does a lot has a lot here, and that is the
shape working. What is not the shape: a second copy of something `agent-mgr`
already owns.

| Path | Required | What |
|---|---|---|
| `agent.env` | yes | The descriptor. Overrides only; it may be entirely comments |
| `config.yaml` | yes | The declarative half of the agent's home — model, plugins, `mcp_servers`. `AGENT_CONFIG` may name another relative path — the rentals agent keeps its config under `runtime/`, beside the vault seed it ships with |
| `README.md` | yes, hand-written | What this agent is, what it can and cannot reach, and how to bring it up |
| `.env.example` | if it has extra keys | The dotenv contract, with no values. `restore` prefers it over the fleet template |
| `tests/` | if it has siblings | What this agent must **not** reach, asserted. Run with `pytest tests/` |
| `skills.tsv` | if it installs a **shared** skill | Written by `add-skill`; one pinned SHA per skill. Only for skills another agent also wants — its own live here as code |
| `SKILL.md`, `scripts/`, `references/` | if the agent does something | Its own skill: the instructions the container reads, and whatever runs for them |
| `compose.override.yml` | if it needs a derived image or extra mounts | Paths must go through a variable set in `agent.env` |
| a restore hook | if it has its own deploy step | Named by `AGENT_RESTORE_HOOK`; `restore` sequences it |
| a pre-transition guard | if stopping it at the wrong moment costs something | Named by `AGENT_PRE_TRANSITION`. Every route to a transition goes through one seam, so it is asked before all of them — the reload that follows a write included — and a refusal refuses the command. One exception, deliberate: `activate` reports success and skips the restart, having already spent a one-time activation that a red exit would invite you to spend again. `restore` asks twice — once as a preflight before it writes anything, again at the reload it ends with — so write the guard to be safe to ask more than once |

What must **not** be here is the common half: **no `compose.yml`, no activation
script, no `model-provider` or `reload-if-running`** — `agent-mgr` owns all of
those, and a copy is a fork of the fleet that drifts silently. A `justfile` is
the one near-miss: keep it for this agent's own recipes and tests, never to
restate `up`, `restore` or `activate`.

### Pin upstream, never vendor it

Every artifact from another repo — the Plow Chat plugin, a shared skill —
arrives by **40-char SHA**, in a ref file or in `skills.tsv`. A branch would
re-point a running agent on the next upstream push, and these carry the chat
token and drive a filesystem.

Copying the artifact in instead makes this repo a fork of it. That is what
`sams-str-hermes-agent#138` spent −1,311 LOC undoing, after a vendored plugin
drifted until production was serving a working tree. An agent repo that vendors
anyway owes its README a section saying so and naming the trigger to stop.

This is the *only* thing a SHA pin is for. An artifact one agent runs needs no
pin, because there is no second copy to keep in step — it is just that agent's
code, reviewed in the same PR as the config it runs under.

### Where an agent's own skill sits

Its code and its instructions live together, beside the config that runs them:

```
sams-str-hermes-agent/
  agent.env               identity
  config.yaml             the declarative half of its home
  SKILL.md                the instructions, read by the container
  scripts/                what runs for them — here, or on a Mac through Latch
  justfile                this agent's recipes and its tests
  references/             detail the skill loads on demand
```

One PR changes the skill and the config it runs under together, which is the
whole benefit of not splitting them.

**Operating knowledge is the exception worth watching.** How guest messaging
works, what the wiki compiles from, which locks answer to which door — that is
documentation of the *product*, and a README is the wrong container for it
however co-located the code is. The tell is proportion: when the README is
longer than the thing it documents, it has stopped being a README and become a
runbook that nothing verifies. Put it under `docs/` where it can be read on its
own, and keep the README to the spine below.

### Assert what it cannot reach

Siblings share a host, so the realistic failure is a copy-paste — a compose
file, a config block, a descriptor — that quietly widens one agent's blast
radius. `tests/test_config_contract.py` in each existing agent turns that into a
red suite: the homes it must not mount, the `mcp_servers` it must not carry, the
vault it must not see. Write it when the second agent lands, not after the first
copy-paste.

### The README shape

The healthiest agent repos share one spine, and it is worth copying:

1. A caution naming what this repo can reach, if it is private
2. One paragraph: what this agent is for, and **what makes it different from its
   siblings** — the fleet only makes sense read together
3. What it can reach, and what it cannot — the second list is the interesting one
4. Bring-up, as commands in the order they must run
5. **What only a person can do** — texting the activation code, completing an
   OAuth — and why it cannot be done for them
6. Pinned, not vendored: which SHA covers what
7. A layout table

## Why `agent` uses `exec`

The Hermes image's s6 entrypoint starts a gateway *whatever command you pass
it*. So `docker compose run ... chat -q` brings up a **second** gateway against
the same `/opt/data`, which connects to the chat, answers alongside the real
one, and on exit posts a shutdown notice into the owners' channel.

That is not hypothetical. Over two days on one host it produced 25 gateway
starts against a baseline of 1–6 per day, 21 shutdown notices into an owners'
channel in a single day, and 6 sqlite errors from two gateways racing one
session database. `tests/test_no_second_gateway.py` is the guard.

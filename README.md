# agent-mgr

One CLI for the Hermes agent fleet. It starts and manages containers running
Hermes with **Plow Chat** and **Plow Latch**, so standing up a new agent is a
command rather than a copy-paste of the last one.

```sh
agent-mgr new rowan          # scaffold an instance repo, both platforms wired
agent-mgr restore rowan      # the whole deploy: config, plugin, restore hook
agent-mgr activate rowan     # prints a code; text it from the agent's phone
agent-mgr up rowan
agent-mgr sign-in rowan
```

See [docs/HOWTO.md](docs/HOWTO.md) for the full walkthrough.

## Three layers, and what belongs in each

This repo holds **mechanism only**. It carries no agent instances, no personal
paths, and no credentials.

| layer | lives in | what it is |
|---|---|---|
| **mechanism** | this repo | true of *every* Hermes agent, whatever it does |
| **instance** | `<agent>-hermes-agent` | *which* agent this is: name, home, which Mac it drives, which skills it pins |
| **domain** | e.g. `plow-pbc/property-hunt` | what the agent actually *does* |

The test for where something belongs: **if this agent's product vanished
tomorrow, would this code still exist?**

- `agent-mgr up` — yes, the other agents still need it → **mechanism**
- `agent.env` — no, it *is* this agent's identity → **instance**
- a recipe that publishes a property map — no, it exists only for that product → **domain**

The rule is about duplication, not size. A thin instance repo is still an
instance repo; domain code stays put however little else surrounds it. And the
rule is dynamic: something used by one agent today graduates to this repo when a
second agent wants it.

## What belongs in an instance repo

`agent-mgr new` scaffolds the first two rows below. The README is yours to
write; everything under it is added only when that agent actually needs it — a
thin instance repo is the target, not a stage on the way to a thick one.

| Path | Required | What |
|---|---|---|
| `agent.env` | yes | The descriptor. Overrides only; it may be entirely comments |
| `config.yaml` | yes | The declarative half of the agent's home — model, plugins, `mcp_servers`. `AGENT_CONFIG` may name another relative path — the rentals agent keeps its config under `runtime/`, beside the vault seed it ships with |
| `README.md` | yes, hand-written | What this agent is, what it can and cannot reach, and how to bring it up |
| `.env.example` | if it has extra keys | The dotenv contract, with no values. `restore` prefers it over the fleet template |
| `tests/` | if it has siblings | What this agent must **not** reach, asserted. Run with `pytest tests/` |
| `skills.tsv` | if it runs a domain skill | Written by `add-skill`; one pinned SHA per skill |
| `compose.override.yml` | if it needs a derived image or extra mounts | Paths must go through a variable set in `agent.env` |
| a restore hook | if it has its own deploy step | Named by `AGENT_RESTORE_HOOK`; `restore` sequences it |
| a pre-transition guard | if stopping it at the wrong moment costs something | Named by `AGENT_PRE_TRANSITION`. Every route to a transition goes through one seam, so it is asked before all of them — the reload that follows a write included — and a refusal refuses the command. `restore` transitions at both ends, so one deploy asks a side-effecting guard twice |

Nothing else. In particular **no `compose.yml`, no activation script, no
`model-provider` or `reload-if-running`** — `agent-mgr` owns all of those, and a
copy here is a fork of the fleet that drifts silently. A `justfile` is the one
near-miss: keep it to run this repo's own tests, never to restate `up`,
`restore` or `activate`.

### Pin upstream, never vendor it

Every artifact from another repo — the Plow Chat plugin, a domain skill —
arrives by **40-char SHA**, in a ref file or in `skills.tsv`. A branch would
re-point a running agent on the next upstream push, and these carry the chat
token and drive a filesystem.

Copying the artifact in instead makes this repo a fork of it. That is what
`sams-str-hermes-agent#138` spent −1,311 LOC undoing, after a vendored plugin
drifted until production was serving a working tree. An instance repo that
vendors anyway owes its README a section saying so and naming the trigger to
stop.

### Domain code does not live here

The instance repo says *which* agent this is. The domain repo says what it
*does* — and that includes its operating knowledge, not just its code. A long
section on how guest messaging works, what the wiki compiles from, which locks
answer to which door, belongs in the domain repo beside the scripts that
implement it. In the instance repo it is a second copy of the truth that only
one of the two will keep current.

The tell is proportion. When the README is longer than the thing it configures,
the repo has stopped being an instance repo.

### Assert what it cannot reach

Siblings share a host, so the realistic failure is a copy-paste — a compose
file, a config block, a descriptor — that quietly widens one agent's blast
radius. `tests/test_config_contract.py` in each existing agent turns that into a
red suite: the homes it must not mount, the `mcp_servers` it must not carry, the
vault it must not see. Write it when the second agent lands, not after the first
copy-paste.

### The README shape

The three healthiest instance repos share one spine, and it is worth copying:

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

# Review instructions — agent-mgr

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## Operating point

Pre-PMF, a single operator, four or five agents on one Linux host. Iteration
speed beats hardening for scale: prefer loud failures to fallbacks, pragmatic
DRY architecture to defensive layering, and don't guard edge cases that cannot
trigger at this size. A handled case the intent never asked for is a cost.

One deliberate exception to "fail loudly", and it is the only one: the reload
that follows a write is non-fatal at the `activate` call site, because by then
a one-time activation has already been spent and a red exit invites the re-run
that costs another. Anywhere else, a swallowed error is a finding.

## Review priority

Subtractive remedies outrank additive ones. This repo is **mechanism** — it
holds what is true of every Hermes agent and nothing about any particular one —
so the falsifiable gate is the layer boundary: an agent's name, a personal
path, a credential, or a product-specific recipe appearing here is a real
finding however small, and a suggestion that would make this file *more*
general than one agent needs is usually the bloat.

The failure class this tool exists to prevent is **two gateways against one
home**. The image's s6 entrypoint starts a gateway whatever command you pass
it, so `docker compose run` without `--entrypoint` boots a second one that
answers alongside the live agent and posts a shutdown notice into the owners'
channel on exit. Measured before the fix: 25 gateway starts against a 1–6/day
baseline, 21 shutdown notices in one day, 6 sqlite errors from two gateways
racing one session database. Anything that reopens that path is blocking.

**Repo-specific contrast pairs:**

| Fleet-CLI DON'T (suppress / flag-as-shape) | Fleet-CLI DO (real finding) |
|---|---|
| Ask for a fallback when `docker`, `compose` or the relay fails. Refusing loudly is the design — a host-side answer is exactly the evidence entering the container's namespace was meant to stop accepting. | Flag an error that is **swallowed or misreported**: a failed `compose ps` read as "no gateway running", a dead credential and a dead network collapsed into one message, a probe that reports success without having run. |
| Demand a config-schema validator, a plugin abstraction, or a packaging story. This is one bash script symlinked onto one host, for one operator. | Flag a **new route to a container transition** that does not go through `compose_transition`, or a `compose run` that does not require `--entrypoint`. Both reopen the second-gateway class above. |
| Suggest guards for an agent count, host count or concurrency this fleet will not reach. | Flag a **write into an agent's home that skips `require_own_home`**. `resolve-guard` proves Compose agrees with the descriptor, which a descriptor copied from a sibling satisfies perfectly — it is self-consistent and wrong, and the write lands on the sibling. |
| Treat doc-only edits to `README.md` / `docs/HOWTO.md` as low-value churn. | Flag **prose↔code drift**. The README owns the instance-repo contract; a behaviour change that leaves its table describing the old behaviour is the canonical regression here, and it has recurred across rounds. |
| Propose vendoring an upstream artifact to avoid a fetch. | Flag any ref that is **not a 40-char SHA** — image digest, plugin, skill. A branch silently re-points a running agent on the next upstream push, and these carry the chat token and drive a filesystem. |
| — | Flag a **path that can traverse out of the agent's home**. Destinations are joined onto it and the result is `rm -rf`'d during a swap, so a traversing `--dest` is a delete primitive aimed at the operator's home. Reject by component, not substring — a legitimate `..foo` must still install. |
| — | Flag any **secret reaching argv, a URL, or a log line**. Credentials belong in the agent's dotenv, read through the home mount; compose passes none, and a token travels in a header rather than a path. |
| — | Flag an **instance-layer or domain-layer fact landing in this repo**: an agent name, `~/.hermes-<something>`, a PMS token, a recipe that exists for one product. The test is whether the code would still exist if that agent's product vanished tomorrow. |

## Product context

**Stage:** Prototype, not shipped, no dates. One operator (srosro) running a
handful of Hermes agents — rentals, admin, house-hunting, and one belonging to
a different person — as containers on a single Linux host (`wakeup`).

**Distribution model:** A git clone in `~/services/agent-mgr` with the
`agent-mgr` script symlinked onto `PATH`. No release, no package, no versioning.
The registry at `~/.config/agent-mgr/agents` maps a name to an instance repo.

**What this repo is:** the **mechanism** layer of a three-layer fleet.

| layer | lives in | what it is |
|---|---|---|
| mechanism | this repo | true of *every* Hermes agent, whatever it does |
| instance | `<agent>-hermes-agent` | *which* agent this is — name, home, which Mac it drives |
| domain | e.g. `plow-pbc/property-hunt` | what the agent actually *does* |

`README.md` § What belongs in an instance repo is the single owner of that
contract; findings about it are doc findings, and drift between it and the
resolver is a real one.

**Trust boundary (known and accepted):** an agent's dotenv sits on the host
side of the container mount, owned by the operator's uid. The container
boundary does not hide it from the operator, and the READMEs say so rather than
leaving it implied. Latch credentials decide which Mac an agent can drive; the
Mac authorises each action, so the approval surface stays on that machine.

**Update cadence:** edit when the stage changes — a second operator, a second
host, or a distribution story that is not a symlink.

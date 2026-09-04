# Review instructions — agent-mgr

Repo-specific reviewer policy. The universal voice posture (Broken-Glass,
pro-simplification, and the don't-propose list) is supplied by the reviewers
themselves and is deliberately not restated here.

## Operating point

Pre-PMF, a single operator, three registered agents on one Linux host — all of them running businesses or someone's daily life, which is why an accidental restart is not a cheap mistake here. Iteration speed beats
hardening for scale: prefer loud failures to fallbacks, pragmatic DRY
architecture to defensive layering, and don't guard edge cases that cannot
trigger at this size. A handled case the intent never asked for is a cost.

"Fail loudly" has deliberate exceptions here, and the axis that separates them
from bugs is **what the operator is left with** — not whether the command
exited 0, and not whether the suppression carries a comment.

A suppression is a **design decision** when the failure is surfaced and
classified: a reload that prints which agent did not restart and the command to
run; `activate` printing "SUCCEEDED — do NOT re-run" because a red exit would
cost a second one-time activation; a relay probe that splits `000` from `401`,
because a dead network and a dead credential need different fixes. Several
`|| true` and `2>/dev/null` sites swallow a plain absence — a missing registry,
key or row the next line handles — and need no defence at all.

A suppression is a **finding** when it leaves the operator with no signal or a
wrong next step: a failed `compose ps` read as "no gateway running", so the
reload silently never happens and the caller reports success.

## What this repo is

**The common half of a two-layer fleet** — what is true of every Hermes agent.
`README.md` owns the layer contract and the agent-repo contract; this file does
not restate them. Flag drift between that prose and the code, in either
direction.

**Stage:** prototype, not shipped, no dates. One operator running a handful of
Hermes agents — rentals, house-hunting, admin, and one belonging to a different
person — as containers on a single Linux host.

**Distribution model:** a typed Python 3.11+ application, used from a git clone
and symlink during development and published as a checksummed zipapp on tags.
A registry file maps an agent name to its repo. JSON envelopes are the machine
contract; human output remains the compatibility default.

**Trust boundary (known and accepted):** an agent's dotenv sits on the host side
of the container mount, owned by the operator's uid. The container boundary does
not hide it from the operator, and the READMEs say so rather than leaving it
implied. Latch credentials decide which Mac an agent can drive; the Mac
authorises each action, so the approval surface stays on that machine.

## Review priority

Subtractive remedies outrank additive ones. The falsifiable gate for this repo
is what the tool *resolves or executes*: a hardcoded home, an agent's name
reaching a code path, a credential, or a recipe only one agent would ever run.

Three things are deliberately **not** violations of that gate, and flagging them
is a false finding: agent names used illustratively in comments, docs and test
fixtures; the Plow-platform commands this repo owns on purpose (the Latch and
connector probes, the plugin and activation fetches), which are specific to Plow
rather than to any one agent; and a suggestion that would make this tool *more*
general than one operator's fleet needs, which is the bloat rather than the fix.

The failure class this tool exists to prevent is **two gateways against one
home** — the incident and its numbers are in `README.md` § Why it exists.
Anything that reopens that path is blocking.

**Repo-specific contrast pairs:**

| Fleet-CLI DON'T (suppress / flag-as-shape) | Fleet-CLI DO (real finding) |
|---|---|
| Ask for a fallback when `docker`, `compose` or the relay fails. Refusing loudly is the design — a host-side answer is exactly the evidence entering the container's namespace was meant to stop accepting. | Flag an error that is **swallowed or misreported**: a failed `compose ps` read as "no gateway running", a dead credential and a dead network collapsed into one message, a probe that reports success without having run. |
| Demand a cloud lifecycle backend or plugin abstraction before the cloud variants are ready. This remains one operator's local fleet. | Flag a **new route to a container transition** that does not go through the single transition seam, or a `compose run` that does not require `--entrypoint`. Both reopen the second-gateway class above. |
| Suggest guards for an agent count, host count or concurrency this fleet will not reach. | Flag a **write into an agent's home that skips the ownership check**. Proving Compose agrees with the descriptor is not enough — a descriptor copied from a sibling satisfies that perfectly. It is self-consistent and wrong, and the write lands on the sibling. |
| Treat doc-only edits to `README.md` / `docs/` as low-value churn. | Flag **prose↔code drift**. The README owns the agent-repo contract; a behaviour change that leaves its table describing the old behaviour is the canonical regression here. |
| Propose vendoring an upstream artifact to avoid a fetch. | Flag any ref that is **not exact**: a git artifact (plugin, skill) must be a 40-char SHA, a container image a `sha256:` digest — never a tag or a branch. A moving ref silently re-points a running agent on the next upstream push, and these carry the chat token and drive a filesystem. |
| — | Flag a **path that can traverse out of the agent's home**. Destinations are joined onto it and the result is `rm -rf`'d during a swap, so a traversing `--dest` is a delete primitive aimed at the operator's home. Reject by component, not substring — a legitimate `..foo` must still install. |
| — | Flag any **secret reaching argv, a URL, or a log line**. Credentials belong in the agent's dotenv, read through the home mount; compose passes none, and a token travels in a header rather than a path. |
| Ask a test to pin a project or container NAME. The name is the identity; two names may share one checkout so one repo can serve two people. | Flag anything that acts on a container **without identifying it first**. `AGENT_PROJECT` derives from the agent name and Docker's namespace is global, so `-p hermes-rowan` addresses production however isolated HOME and the registry are. Proving the *config* is self-consistent does not help — a scratch descriptor is. The running container's `/opt/data` mount must be this agent's home. |
| — | Flag a test that can reach the **real docker daemon**. The suite ran for a day against live compose projects: ~20 `compose restart` calls per run, 917 boots of the rentals gateway and 1,378 of another agent's in one day, plus 207 shutdown notices into an owners' channel. The shadow is on the PROCESS PATH, so building a child env as `f"{mybin}:{os.environ['PATH']}"` is correct and expected — what is suspect is a PATH assembled from scratch, or one that puts a system directory ahead of the inherited value. See `plow-pbc/agent-mgr#13`. |
| — | Flag a refusal whose named remedy is **more destructive than the action it blocked**, or that cannot run. A guard that hands back `docker rm -f` on a live gateway, or points at a command that refuses the same input, is a net regression on the failure it closes. |
| — | Flag an **agent-specific fact reaching a code path here**: a hardcoded `~/.hermes-<something>`, a PMS token, a lock or property-search call, a recipe only one agent would run. The test is whether a second agent would want it — not whether it mentions an agent, which comments and fixtures do freely. Its sibling half: flag a fact a **sibling repo owns** per `plow-hermes-agent` README § The repos — the base's boot paths (`plow-hermes-agent`), a seed-skill tree enumerated here rather than pinned (`hermes-plow-chat`), the relay or the cloud registry (`plow`) — where the test is instead who else would have to change if the fact changed. |

**Update cadence:** edit when the stage changes — a second operator, a second
host, or Plow starts consuming lifecycle operations. Product and architecture
edits belong in `README.md`, not here.

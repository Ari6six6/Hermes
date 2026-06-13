# The Sandbox Twin

Point Hermes at a target — a web service — and it clones that service into a
**runtime twin**: a faithful, local, *runnable* copy. The agent then builds a
coding solution against the twin instead of the live system, with correctness
measured against what the real service actually does.

## Why a twin, not a "recording"

The point isn't to save a few responses — it's to stand up a copy of the system
that *runs*. To the agent and the code it writes, the twin behaves like the
target's API — but it's offline, safe, and yours.

## Two kinds of twin, one interface

The twin is an *interface*: an HTTP endpoint that behaves like the target. There
are two ways to stand it up, and recon decides which:

- **Behavioral twin** — recorded real responses, served by
  `hermes/twin/server.py`. For **opaque / bespoke** services whose logic is
  hidden. Faithful for observable behavior, approximate for hidden internals.
- **Reconstructed twin** — the *real software* running in the sandbox. For
  **known open-source stacks** (WordPress, Drupal, Django, ...): recon fingerprints
  the stack, the builder pulls the public source + runtime off the web and stands
  up an actual instance in the box. Near-perfect, because the twin literally *is*
  the software.

The thesis and antithesis don't care which is behind the endpoint.

## Three roles (all the same weights, different phase/prompt/tools)

1. **Recon / Builder** — fingerprint the live target (read-only), decide
   behavioral vs reconstructed, gather what's needed (recordings, or public
   source + runtime), stand up the twin in the box, and *prove it behaves like the
   target* before handing off.
2. **Thesis** — build the coding solution to meet the mission, against the twin.
3. **Antithesis** — try to break it; prove it fails the winning condition,
   diffing against the twin.

The roles *are* the compartmentalization: only **recon** ever touches the live
target, and only read-only; **thesis/antithesis** are sealed off in build mode and
see only the twin.

## Winning condition = proven functional code

The plain-English winning condition is the human-readable goal. "Winning" is
decided by a **proof harness**: real code/tests that run the solution against the
twin and genuinely pass, under the codebase's anti-fabrication guards (a test that
can't fail is worthless; quote real output or it didn't happen). The antithesis's
job is to make that proof fail; synthesis is when it can't. English states the
goal; passing code *is* the win.

## Why it's compartmentalized (the live-server rule)

Hermes, like most models, is trained not to manipulate live servers — a safety
property worth keeping. So the system is split so that by the time the model is
*building*, it is provably working against a sealed twin, never the live target,
and it is told so plainly (`hermes/prompts/build_mode.md`).

```
  LIVE TARGET ──(benign clone, operator-driven, on the phone)──> SEALED TWIN ──> BUILD
     │              read-only · capped · polite                   runtime copy     agent
   touched only by the clone layer, never by the building agent              (safe exec env)
```

- **Cloning is not an agent tool.** The agent never decides to poke a live
  service. The operator kicks it off (`project build <name> <url>`), on the phone,
  where every byte is visible.
- **Cloning is benign by construction:** read-only GETs only, a hard request cap,
  a polite delay between requests.
- **When the agent needs a case the twin lacks**, it calls `twin_expand`, which
  routes back to the same benign clone layer to learn it read-only and fold it in.
  The building agent still never touches the live target.

## The accuracy rule: the twin never invents a response

This is the heart of it. For a request the twin has really seen, it replays the
target's real response **byte-for-byte** (`X-Twin: exact`). For anything else it
returns a **504 twin-miss** (`X-Twin: miss`) — never a fabricated body. So
everything the agent builds against is something the real service really did, and
parity is ironclad where it's claimed. Coverage grows by *learning more real
exchanges* (expand), never by guessing.

The honest edge: from outside you can clone observable behavior — reads, response
shapes, status/error semantics — with near-perfect fidelity. You cannot perfectly
reconstruct **hidden server-side logic behind writes** (what a POST does to a
database you can't see). The twin models the *response* faithfully and flags where
the *internal effect* is only approximate. Winning conditions land cleanest when
they target observable behavior.

## Pieces

| Component | File | Role |
|-----------|------|------|
| Model | `hermes/twin/model.py` | The sealed model: manifest + `exchanges.jsonl` + captured spec + stack fingerprint. Exact-match lookup, route map. |
| Clone engine | `hermes/twin/clone.py` | The live-touching component. Autonomous, comprehensive, benign; injectable `fetch`. `clone()` + `expand()`. |
| Recon | `hermes/twin/recon.py` | Deterministic stack fingerprinting (server/runtime/framework/CMS + version). Decides behavioral vs reconstructed twin. |
| Runtime twin | `hermes/twin/server.py` | Self-contained stdlib HTTP server. Exact-replay or miss. Runs standalone on the box: `python3 server.py <model-dir> <port>`. |
| Agent tools | `hermes/tools/twin.py` | `twin_request` / `twin_map` / `twin_stack` / `twin_expand`. Register only when a sealed twin exists. |
| Build framing | `hermes/prompts/build_mode.md` | Injected into the system prompt: "you are building against a safe twin, here is the mission + winning condition." |
| CLI | `hermes/cli.py` | `project build <name> <url>`; `build win|clone|show|clear`. |

## Operator flow

```
project build shopapi https://api.example.com    # clone -> seal a runtime twin
mission edit                                       # what to build
build win Reimplement /products so responses byte-match the twin
run go                                             # the agent builds against the twin
```

Mission (what to build) and winning condition (how we know it's done) are two
distinct, plain-English fields, both set by the operator.

## What's next

1. **The recon/builder agent role**: turn recon from a deterministic helper into
   an agent phase that fingerprints, gathers (recordings or public source +
   runtime), stands up the twin in the box, and proves it behaves like the target.
2. **`build serve`** (behavioral twin): deploy the standalone twin to the GPU box
   on `localhost:<twin_port>` so the solution and its tests hit it like the real
   API.
3. **The dialectic build loop**: thesis builds against the twin; antithesis
   diffs the solution against it, with the anti-collusion rule — a STANDS verdict
   is invalid without a real executed command + its actual output. Extends the
   independent-verifier pass in `hermes/agent.py`. "Winning" = the proof harness
   genuinely passes.
4. **`repo` mode**: clone + build + run a reference codebase to produce the twin.

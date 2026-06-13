# The Sandbox Twin

Point Hermes at a target — a web service — and it clones that service into a
**runtime twin**: a faithful, local, *runnable* copy. The agent then builds a
coding solution against the twin instead of the live system, with correctness
measured against what the real service actually does.

## Why a twin, not a "recording"

The point isn't to save a few responses — it's to stand up a copy of the system
that *runs*. The clone engine gathers as much as it responsibly can (the API spec,
discovery endpoints, a same-origin crawl), and `hermes/twin/server.py` serves that
model as a real local HTTP service. To the agent and the code it writes, the twin
behaves like the target's API — but it's offline, safe, and yours.

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
| Model | `hermes/twin/model.py` | The sealed model: manifest + `exchanges.jsonl` + captured spec. Exact-match lookup, route map. |
| Clone engine | `hermes/twin/clone.py` | The one live-touching component. Autonomous, comprehensive, benign; injectable `fetch`. `clone()` + `expand()`. |
| Runtime twin | `hermes/twin/server.py` | Self-contained stdlib HTTP server. Exact-replay or miss. Runs standalone on the box: `python3 server.py <model-dir> <port>`. |
| Agent tools | `hermes/tools/twin.py` | `twin_request` / `twin_map` / `twin_expand`. Register only when a sealed twin exists. |
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

## What's next (not in this slice)

1. **The dialectic build loop**: thesis builds against the twin; antithesis
   attacks by diffing the reimplementation against the twin, with the
   anti-collusion rule — a STANDS verdict is invalid without a real executed
   command + its actual output. Extends the independent-verifier pass in
   `hermes/agent.py`.
2. **`build serve`**: deploy the standalone twin to the GPU box and run it on
   `localhost:<twin_port>` so the agent's reimplementation and tests hit it like
   the real API.
3. **`repo` mode**: clone + build + run a reference codebase to produce the twin.

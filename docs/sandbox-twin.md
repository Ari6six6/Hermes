# The Sandbox Twin

Point Hermes at a target — a web service — and it clones that service into a
**runtime twin**: a faithful, local, *runnable* copy. The agent then builds a
coding solution against the twin instead of the live system, with correctness
measured against what the real service actually does.

## Why a twin, not a "recording"

The point isn't to save a few responses — it's to stand up a copy of the system
that *runs*. To the agent and the code it writes, the twin behaves like the
target's API — but it's offline, safe, and yours.

## The twin is the reconstructed real software

We don't chase closed-source black boxes. The twin is the **real software running
in the sandbox**: for a known open-source stack (WordPress, Drupal, Django, Rails,
Laravel, Express, ...), recon fingerprints it, the builder pulls the public source
+ runtime off the web, and stands up an actual instance in the box. Near-perfect,
because the twin literally *is* the software.

Captured responses are not a substitute twin — they are **ground-truth samples**:
real input→output pairs from the target that the builder uses to *prove the
reconstruction behaves like the original*, and that the antithesis uses as ammo.
(`hermes/twin/server.py` can replay those samples as a reference responder for
diffing; it is not the twin.)

## Three roles (all the same weights, different phase/prompt/tools)

1. **Recon / Builder** — fingerprint the live target (read-only), decide
   behavioral vs reconstructed, gather what's needed (recordings, or public
   source + runtime), stand up the twin in the box, and *prove it behaves like the
   target* before handing off.
2. **Thesis** — build the coding solution to meet the mission, against the twin.
3. **Antithesis** — try to break it; prove it fails the winning condition,
   diffing against the twin.

The roles *are* the compartmentalization, and **the seal is the boundary that
enforces it**: while the twin is OPEN the agent has the recon tools to get to know
the target thoroughly (plain GET requests — map directories/endpoints, find source
and dependency files, list subdomains); the moment it's SEALED those tools are gone
and only the frozen twin remains.

## Winning condition = proven functional code

The plain-English winning condition is the human-readable goal. "Winning" is
decided by a **proof harness**: real code/tests that run the solution against the
twin and genuinely pass, under the codebase's anti-fabrication guards (a test that
can't fail is worthless; quote real output or it didn't happen). The antithesis's
job is to make that proof fail; synthesis is when it can't. English states the
goal; passing code *is* the win.

### "Told my guy it worked and pissed off" — the gate against it

The failure mode this whole system exists to kill: declaring success without doing
the work. A first concrete guard ships now (`hermes/agent.py`): in build mode (a
sealed twin), if the agent changes code and calls `finish_run` **without ever
querying the twin** to check it, the finish is bounced (`prompts/build_proof.md`)
— go prove parity with a real query, don't claim it. The full antithesis pass
(independent re-run + diff against the twin, anti-collusion: no STANDS without real
executed output) builds on the existing verifier in a later slice.

## Why it's compartmentalized (the live-server rule)

Hermes, like most models, is trained not to manipulate live servers — a safety
property worth keeping. So the system is split so that by the time the model is
*building*, it is provably working against a sealed twin, never the live target,
and it is told so plainly (`hermes/prompts/build_mode.md`).

```
  LIVE TARGET ──(read-only clone, operator-driven, on the phone)──> SEALED TWIN ──> BUILD
     │              GET · capped · polite                            runtime copy     agent
   touched only by the clone step, never by the building agent                (safe exec env)
```

- **Cloning is not an agent tool.** The agent never decides to reach a live
  service on its own. The operator kicks it off (`project build <name> <url>`), on
  the phone, where every byte is visible.
- **Cloning is read-only by construction:** GETs only, a hard request cap, a
  polite delay between requests.
- **When the agent needs a case the twin lacks**, it calls `twin_expand`, which
  routes back to the same clone step to learn it and fold it in.
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
| Clone engine | `hermes/twin/clone.py` | The live-touching component. Autonomous, comprehensive, read-only; injectable `fetch`. `clone()` + `expand()`. |
| Recon | `hermes/twin/recon.py` | Stack fingerprinting + recon helpers (subdomains, exposed-source, dir-scan, robots/sitemap mining). |
| Recon tools | `hermes/tools/recon.py` | The recon agent's eyes: `recon_subdomains` / `recon_sources` / `recon_dirscan`. Register only while the twin is OPEN. |
| Builder tools | `hermes/tools/builder.py` | The builder's hands: `twin_record` / `twin_clone` / `twin_seal`. Register only while the twin is OPEN. |
| Recon/build framing | `hermes/prompts/recon_build.md` | Injected while the twin is OPEN: "get to know the target, stand up the twin, prove it, seal it." |
| Runtime twin | `hermes/twin/server.py` | Self-contained stdlib HTTP server. Exact-replay or miss. Runs standalone on the box: `python3 server.py <model-dir> <port>`. |
| Build tools | `hermes/tools/twin.py` | `twin_request` / `twin_map` / `twin_stack` / `twin_expand` / `twin_reground`. Register only when a sealed twin exists. |
| Build framing | `hermes/prompts/build_mode.md` | Injected once sealed: "build against the safe twin; show, don't claim; here's the mission + winning condition." |
| Anti-bail gate | `hermes/agent.py` + `prompts/build_proof.md` | Bounces a build-mode finish that changed code but never queried the twin. |
| CLI | `hermes/cli.py` | `project build <name> <url>`; `build win|clone|seal|show|clear`. |

## The phases, and the seal between them

The seal is the boundary. A twin is **OPEN** during recon/build and **SEALED**
during build, and the registry + system prompt swap with it:

| | Twin OPEN (recon/build) | Twin SEALED (build) |
|---|---|---|
| Role | recon / builder | thesis, then antithesis |
| Prompt | `recon_build.md` | `build_mode.md` |
| Tools | recon (`recon_*`) + builder (`twin_record/clone/seal`) | `twin_request/map/stack/expand/reground` |
| Live target | read-only, to learn & build the twin | never (only `twin_expand`/`twin_reground`, narrowly) |
| Anti-bail gate | off | on |

## Operator flow

```
project build shopapi https://api.example.com    # create project, seed an OPEN twin
mission edit                                       # what to build
build win Reimplement /products so responses match the twin
run get to know the target and stand up the twin  # recon/builder agent -> twin_seal
run build /products to meet the winning condition # thesis builds against the sealed twin
```

`project build` seeds the twin but leaves it open so the **recon/builder agent**
stands it up and seals it (`twin_seal`). For a quick path without the agent, `build
seal` freezes the seed as-is. Mission (what to build) and winning condition (how we
know it's done) are two distinct, plain-English fields.

## What's next

1. **`build serve`**: deploy the standalone twin to the GPU box on
   `localhost:<twin_port>` so the solution and its tests hit it like the real API.
2. **The dialectic build loop**: thesis builds against the twin; antithesis diffs
   the solution against it, with the anti-collusion rule — a STANDS verdict is
   invalid without a real executed command + its actual output. Extends the
   independent-verifier pass in `hermes/agent.py`. "Winning" = the proof harness
   genuinely passes.
3. **`repo` mode**: clone + build + run a reference codebase to produce the twin.

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

Two more roles bracket the thesis in build mode (same weights, different
phase/prompt; both on by default, see `hermes/agent.py`). A **planner**
(`prompts/planner.md`, `plan_build_tasks`) runs before the thesis and turns the
mission + request into an ordered checklist of verifiable checkpoints the
builder executes against. A **referee** (`prompts/referee.md`,
`referee_on_deadlock`) is invoked only on deadlock — the `verify_rounds` are
spent but the antithesis keeps failing a solution the doer keeps re-finishing —
and makes the binding call with fresh eyes and the real sandbox, overruling
either side, but only granting a PASS backed by real executed evidence.

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

## The accuracy rule: the twin is the real running software

This is the heart of it. The twin isn't a recording — it is the genuine
reconstructed stack, running in a container, so `twin_request` returns whatever
that software really does. Fidelity comes from reconstructing the real OS /
runtime / app at the versions recon found, not from memorizing responses.

The recorded request/response samples are kept as **ground truth**: the diff-only
reference responder (`server.py`) replays them byte-for-byte (`X-Twin: exact`, or
a `504 X-Twin: miss` for anything unseen) so a candidate can be compared against
what the real service actually did. `twin_diff` drives the reconstruction toward
matching them; coverage grows by *learning more real exchanges* (`twin_expand` /
`twin_reground`), never by guessing.

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
| Builder tools | `hermes/tools/builder.py` | The builder's hands: `twin_record` / `twin_clone` / `twin_diff` / `build_run` / `build_recipe` / `twin_seal`. Register only while the twin is OPEN. |
| Recon/build framing | `hermes/prompts/recon_build.md` | Injected while the twin is OPEN: "get to know the target, stand up the twin, prove it, seal it." |
| Blueprint | `twin/recipe.jsonl` + manifest | The portable reconstruction blueprint, on the phone: the ordered `build_run` steps plus the recon fingerprint/services/topography. `build serve` replays it onto any box to respin the runtime server; `build blueprint` shows it. |
| Reference responder | `hermes/twin/server.py` | Self-contained stdlib HTTP server that exact-replays recorded samples or misses. **Not the twin** — a diff-only reference for comparing a candidate against ground truth offline. Runs standalone: `python3 server.py <model-dir> <port>`. |
| Sandbox | `hermes/sandbox/` | The local box Hermes runs on, where the twin container lives. `local_endpoint()` runs commands here (no SSH); `capabilities()` probes the container runtime and `/dev/kvm`. `sandbox status/provision`. |
| Deploy | `hermes/twin/deploy.py` | `build serve` — boots a **local container** from a base image and replays the blueprint recipe *inside it* (`docker exec` per step) to stand the **real reconstructed server** up, published on `127.0.0.1:<twin_port>`. There is no recorded-response fallback: a twin is the real running software, or `build serve` says there's no recipe yet and points at `run build`. |
| Antithesis | `hermes/prompts/antithesis.md` + `agent._verify(build=True)` | Diffs the solution against the twin; anti-collusion — a PASS with no executed evidence is rejected as FAIL. |
| Build tools | `hermes/tools/twin.py` | `twin_request` / `twin_map` / `twin_stack` / `twin_expand` / `twin_reground`. Register only when a sealed twin exists. |
| Build framing | `hermes/prompts/build_mode.md` | Injected once sealed: "build against the safe twin; show, don't claim; here's the mission + winning condition." |
| Anti-bail gate | `hermes/agent.py` + `prompts/build_proof.md` | Bounces a build-mode finish that changed code but never queried the twin. |
| CLI | `hermes/cli.py` | `project build <name> <url>`; `build win|clone|seal|serve|blueprint|show|clear`. |

## The phases, and the seal between them

The seal is the boundary. A twin is **OPEN** during recon/build and **SEALED**
during build, and the registry + system prompt swap with it:

| | Twin OPEN (recon/build) | Twin SEALED (build) |
|---|---|---|
| Role | recon / builder | thesis, then antithesis |
| Prompt | `recon_build.md` | `build_mode.md` |
| Tools | recon (`recon_*`) + builder (`twin_record/clone/diff/seal`) | `twin_request/map/stack/expand/reground` |
| Live target | read-only, to learn & build the twin | never (only `twin_expand`/`twin_reground`, narrowly) |
| Anti-bail gate | off | on |

## Operator flow

```
sandbox provision                                 # ensure a container runtime is on this box (once)
project build shopapi https://shop.example.com    # create project, seed an OPEN twin
mission edit                                       # the task (win = match the target, baked in)
run build                                          # refinement pass: diff vs target, close the gap, seal
build serve                                        # boot a local container and run the reconstruction in it
run build /products to meet the mission            # thesis builds against the sealed twin at localhost
```

Hermes runs on the VPS, so the twin is a container on the **same box** — reached
at `127.0.0.1:<twin_port>`, no SSH hop and no tunnel.

`run build` reopens the twin and runs a recon/build pass; run it again to tighten
the match further. `build serve` ensures the reconstruction is live on the box. The
build's winning condition is baked in — match the target — so the operator only
sets the mission (the task). For a quick seal without the agent, `build seal`.

## One flow: observe → reconstruct → refine

There is one path, not two. You point at a live URL; recon identifies the real
stack (OS → runtime → app + versions); the builder pulls the matching open-source
pieces and stands up the genuine software in the box. Pulling source is *inside*
this flow, not a separate mode.

`run build` is the refinement loop, and each invocation is another pass: it reopens
the twin and runs a recon/build pass that **`twin_diff`**s the reconstruction
against the live target and closes the gap (match / drifted / missing), then seals.
Divergence is the score; the goal is all-match. First pass is the expensive one
(reconstruct from scratch); later passes only close the remaining diff.

### Cost & resources (operating notes)

- The twin runs on the **persistent VPS sandbox host**, not the GPU box, so it
  costs **RAM/CPU/disk on a cheap always-on box** — never VRAM, and nothing on the
  expensive rented GPU. The two are decoupled: tear the GPU down between sessions
  and the twin keeps running.
- `build serve` runs it as a **container** that stays up between runs (and across
  GPU teardown) until you respin or stop it. A ~4–8GB Ubuntu VPS is plenty for a
  lean stack; heavier stacks (app + database) want the headroom. Re-`build serve`
  to respin from the recipe on a fresh or wiped box.
- The real cost is **agent turns** (one GPU inference each), so a from-scratch
  reconstruction is the expensive event. The cost lever is the **recipe**:
  `build_run` captures each working reconstruction step into `twin/recipe.jsonl`,
  so a later pass or a fresh box replays the recipe (`build_recipe`) instead of the
  agent re-deriving the build — the expensive derivation is paid once.
  - Network policy: reconstruction happens **inside the twin container**, which
    has network, so `build_run`'s installs/clones (`apt`, `pip`, `npm`, `git
    clone`, …) run there freely — the container boundary is the isolation, not a
    network cage. The separate GPU box keeps its own deny-list (raw egress and
    target traffic stay off it) for the general `remote_shell` compute work.
- Context: build work is log/diff heavy; **~16k tokens is a floor, 32k
  comfortable**. The package budget already scales to the served context and
  truncates tool output, and the differential approach carries forward only the
  remaining divergences rather than the whole history.

## What's next

1. **Firecracker microVM (phase 2).** When the box exposes `/dev/kvm`, boot the
   reconstructed image as a microVM instead of a container for stronger isolation
   (`hermes/sandbox` already probes `kvm`). Cheap VPSes usually can't, so the
   container path is the default.
2. **End-to-end shakeout** on a real target + VPS: drive `sandbox provision` →
   recon → `build_run` (in the container) → seal → `build serve` →
   thesis/antithesis once against an actual service, and tune the prompts from
   what the 36B model actually does.
3. **Builder-side proof gate**: a harder gate could require the builder to show a
   real reference-vs-twin match before it's allowed to seal.

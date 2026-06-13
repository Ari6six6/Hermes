# The Parity Oracle

Goal: point Hermes at a target — a web service, eventually a repo — and have it
build a working reimplementation, with correctness measured against the *real*
target's behavior rather than the agent's own say-so. Two same-weight agents
(thesis and antithesis) argue, and the oracle is the ground truth that keeps the
argument honest. Where the antithesis can point at the real target's behavior,
the loop is bulletproof; where it can't, you're back to two copies of one mind
nodding at each other — so the more of a build that can be expressed as "match
the target," the more real the whole thing is.

## The compartmentalization rule (why capture is separate)

Hermes — like most models — is trained not to manipulate live servers. That's a
safety property worth keeping, not defeating. So the system is split so that by
the time the model is *building*, it is provably working against a **sealed,
complete sandbox replica** of the target, never a live environment, and it is
told so plainly.

```
  LIVE TARGET ──(benign, operator-driven, on the phone)──> SEALED REPLICA ──> BUILD LOOP
     │                  capture / clone engine                  oracle           thesis
     │                  read-only, bounded, polite              (frozen)      vs antithesis
   touched exactly once, by the operator                    never live, ever
```

- **Capture is not an agent tool.** The agent never decides to go poke a live
  service. The operator seeds the oracle with a CLI action (`target capture`),
  on the phone, where every byte that crosses the line is visible — the same
  principle as the no-internet-on-the-GPU rule.
- **Capture is benign by construction.** Read-only methods only (GET/HEAD), a
  hard cap on request count, a polite delay between requests. It records what the
  service openly returns; it does not fuzz, authenticate, or mutate.
- **Once sealed, the bundle is frozen.** The build loop only ever sees a replay
  of the recording. The oracle tools say so in their own description: *recorded
  replica, not live.*

## Pieces (this slice)

| Component | File | Role |
|-----------|------|------|
| Oracle bundle | `hermes/oracle.py` | The sealed recording: manifest + `probes.jsonl`. `replay()` is the ground-truth lookup. |
| Clone engine | `hermes/capture.py` | The one live-touching component. Benign, operator-driven, injectable `fetch` for testing. |
| Replay tools | `hermes/tools/oracle.py` | `oracle_query` / `oracle_list` — read-only replay the agent uses while building. Register only when a sealed bundle exists. |
| CLI surface | `hermes/cli.py` | `target set | win | capture | show | clear`. |

### Bundle layout (inside a project's `oracle/`)

```
manifest.json   source URL, mode, plain-English win condition, sealed flag, counts
probes.jsonl    one recorded request/response pair per line
```

Requests are matched by a canonical key (method + path + order-insensitive query
+ body), so query-string ordering and header noise don't cause spurious misses.

## Operator flow

```
target set https://api.example.com      # begin an (unsealed) bundle
target win Reimplement /users so responses byte-match the real API
target capture /users /users/1 /search?q=a   # benign read-only clone, then SEAL
target show                              # source, state, probe count, win condition
```

The winning condition is plain English, set by the operator at the start — it is
the acceptance criterion the build is judged against.

## Two target modes

- **`url` (built):** a live web service. The live endpoint is the ground truth;
  capture records it; the build runs against the recording. This is the cleaner
  bootstrap — no build step for the reference.
- **`repo` (planned):** clone + build + run the reference in the sandbox to
  produce the recording. Same sealed-replica contract, harder bootstrap.

## What's next (not in this slice)

1. **The dialectic build loop** (`hermes build`): thesis builds against the
   oracle; antithesis attacks by replaying oracle probes and diffing the
   reimplementation, with the anti-collusion rule — a STANDS verdict is invalid
   without a real executed command + its actual output. This extends the existing
   independent-verifier pass in `hermes/agent.py`.
2. **Build-mode compartmentalization in the prompt**: during a build run, swap in
   a system prompt that states the agent is working against a sealed replica, and
   withhold the live-network tools so the only target window is the replay oracle.
3. **`repo` mode** capture (clone/build/run the reference to record it).
4. **Differential probes**: let the antithesis synthesize new inputs within the
   recorded surface and require parity on them.

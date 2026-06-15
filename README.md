# Hermes

A package-per-prompt agent shell for Termux. It rents nothing and hides
nothing: you rent a GPU on [Vast.ai](https://vast.ai), Hermes provisions
[Hermes-4.3-36B](https://huggingface.co/NousResearch/Hermes-4.3-36B) on it via
vLLM, and gives you an agent that lives across two machines:

- **your phone** (Termux) — where the operator is, where every project lives,
  and the **only place with internet access**;
- **the GPU box** — the model's home and the agent's disposable compute
  sandbox. Internet from there is discouraged by design — a deny-list (plus a
  kernel-level `unshare -n` speed bump when the box allows it) and, above all,
  an honest ask: the box *can* reach the network, we ask the agent not to, so
  all egress stays visible on the phone;
- **the sandbox host** (optional) — a small, persistent VPS you register with
  `sandbox add`, where the **runtime twin** of a target service runs inside a
  container: a real, isolated, always-on clone you (and the code the agent
  writes) can hit on localhost, decoupled from the rented-on-demand GPU. See
  [docs/sandbox-twin.md](docs/sandbox-twin.md);
- **your servers** (optional) — real machines you register with `host add`.
  The agent reaches them from the phone: reads run free, anything mutating
  asks you first.

## The stateful machine

Every prompt you send starts a **fresh chat**. The agent has no rolling
conversation — instead it receives a *package* assembled from project state:

```
# MISSION            <- mission.md, yours to edit
# PROMPT HISTORY     <- your prompts (never the model's old replies)
# RUN SUMMARIES      <- short summaries the agent wrote about its own runs
# NOTES              <- facts the agent chose to remember
# WORKSPACE          <- listing of its file area
# CURRENT REQUEST    <- what you just typed
```

Every section has a hard budget that scales with the served context window, so
the prompt can never creep. Within a run the agent loops freely over native
tool calls (vLLM's `--tool-call-parser hermes` — the format the model was
trained on) until it answers and files its `finish_run` summary.

## The doer doesn't grade its own homework

A sandbox is only worth having if the model is forced to *listen* to it. Left
alone, a small model will write code, write a test that can't fail, run that
test in the real sandbox, and declare victory — verification theater. So when a
run that wrote code (`write_file` / `edit_file` / `remote_write`) tries to
finish and a GPU box is attached, Hermes spins a separate **verification pass**:
fresh context, a skeptical prompt, the *same real sandbox*. It re-runs the
actual code itself and returns `VERDICT: PASS` or `FAIL`. A FAIL — ground truth
from the box, not the doer's opinion — bounces the run back with the real error
to fix; only a PASS lets it finish. It fails closed (no clear PASS = FAIL) and
is bounded (`verify_rounds`, default 2). Turn it off with
`config set verify_code_runs false`. Two earlier guards back it up: a finish
that pasted code but wrote/ran nothing gets bounced (`phantom_nudges`), and
every tool's real output (exit codes included) is echoed to your phone so a
fabricated "it passed" can't hide next to what the command actually printed.

In **build mode** (a sealed twin) two more roles bracket the doer, both on by
default and the same weights wearing different hats. A **planner** runs first
(`plan_build_tasks`): before any code, an independent pass turns the mission and
your request into an ordered checklist of verifiable checkpoints the builder
executes against and the antithesis checks. And when the builder and the
antithesis **deadlock** — the verify rounds are spent but the antithesis is still
failing a solution the doer keeps re-finishing — a **referee** is brought in
(`referee_on_deadlock`): fresh eyes, the real sandbox, and the authority to
overrule either side, but a PASS that overturns the antithesis needs real
executed evidence or the antithesis stands. So the loop is thesis → antithesis,
with a planner up front and a referee only on conflict — never a standing
overseer taxing every turn.

## Install (Termux)

```sh
pkg install python openssh git
git clone <this repo> && cd Hermes
pip install -e .
hermes
```

## Workflow

```text
hermes
> config set vast_api_key <your key>
> project new myproject
> mission edit                  # tell the agent what this project is
# rent a GPU in the Vast.ai console, then:
> gpu attach                    # auto-discovers via the API
                                # (or: gpu attach ssh -p PORT root@HOST)
> gpu serve                     # detects GPUs, picks a tier, launches vLLM,
                                # tunnels port 8000, waits until ready
> run fix the parser in workspace/scraper.py and test it on the box
> host add web ssh://root@203.0.113.7 my blog server      # optional
> run why is nginx returning 502s on web? check the logs and config
> gpu down                      # stop vLLM + optionally PAUSE the box (keeps the
>                               # disk: weights, built llama.cpp, the twin)
> gpu up                        # later: resume the paused box (no re-download/rebuild)
```

For risky server surgery the agent can `replicate` files from a host into
the GPU sandbox, reproduce and fix the problem there, and only then ask you
to apply the verified change back to the real machine.

`config set backend mock` lets you exercise the whole loop with no GPU.

## Models

`gpu serve` opens a picker — Hermes isn't the only mind you can run:

| # | model | runtime | notes |
|---|---|---|---|
| 1 | **Hermes-4.3-36B** (FP8) | vLLM | the ready, battle-tested default |
| 2 | **Qwen3.6-27B** (Alibaba, official · FP8) | vLLM | fits a 32GB card |
| 3 | **Qwen3.6-27B** (HauhauCS Balanced, uncensored · Q5_K_P GGUF) | llama.cpp | fits a single 24GB card |
| 4 | **Qwen3.6-40B** (DavidAU Opus-Deckard Heretic, uncensored · Q5_K_M GGUF) | llama.cpp | ~28GB; wants 32GB+ or 2 GPUs |

The catalog lives in `hermes/models.py` — each row carries everything that
differs between models (weights, runtime, tool-call parser, VRAM floor, context
tiers, the identity the system prompt announces), so adding a model is a row,
not a refactor. Each model serves on its *native* runtime: FP8 safetensors
(Hermes, official Qwen) run on vLLM; GGUF builds run on `llama-server` (built
with CUDA on the box, OpenAI-compatible, tool calls via the model's own chat
template) rather than vLLM's slower experimental GGUF path. The chosen model
persists in config and the agent is told which weights are behind it.

**Per-model build.** The agent loop, package and toolset were tuned around
Hermes, but what makes tool-calling reliable differs per model, so each row
also carries a tuned *build profile*: its sampling (the quantized/uncensored
builds get `min_p` + a presence penalty; thinking models keep Qwen's published
reasoning sampler), a completion budget sized to its reasoning length, how hard
to bounce prose-only turns (`stall_nudges`), which reasoning tags to strip, a
short tool-call discipline note appended to its system prompt, and whether its
runtime honours a forced `tool_choice` (vLLM does; llama.cpp under `--jinja`
doesn't, so the loop adapts). Picking a model at `gpu serve` applies its
profile; **Hermes's profile equals the app defaults**, so the baseline path is
unchanged.

> The GGUF paths need the CUDA *toolkit* on the box (to build llama.cpp) — rent
> a CUDA-devel image, not a runtime-only one. And the uncensored finetunes are
> community builds: sanity-check their tool-calling before trusting them with
> host writes.

## GPU tiers

`gpu serve` reads `nvidia-smi` and adapts — context length scales with total
VRAM. Hermes (FP8 36B):

| total VRAM | context | example boxes |
|---|---|---|
| < 44 GB | refused | weights alone need ~37 GB |
| 44–56 GB | 16k (tight) | 1× 48GB card, 2× 24GB |
| 56–96 GB | 32–64k | A100 80GB, RTX 6000 Pro 96GB |
| 96–168 GB | 128–192k | H200 140GB |
| 168+ GB | 256k | 2× RTX 6000 Pro |

Qwen (Q5 GGUF, ~19GB weights) drops the floor to a single 24GB card and tiers
its context up to 128k as VRAM allows. Override either with
`config set max_model_len <n>`. Architecture notes: Hopper, Ada and Blackwell
run FP8 natively; Ampere (A100/A40/3090) falls back to weight-only FP8 (Marlin)
— works, somewhat slower. Pre-Ampere is not supported. The agent is told its
context size and the package budgets shrink automatically on small tiers.

## What the agent can do

| tool | runs on | gate |
|---|---|---|
| read/write/edit/list files | phone, project dir | free inside the project |
| `local_shell` | phone | **always asks you y/n** |
| `remote_shell`, `remote_read/write` | GPU box | free — it's the sandbox; network commands blocked |
| `host_shell`, `host_read/write` | **your servers** (via phone) | reads free; anything mutating asks you y/n |
| `http_request`, `web_search` | **phone** | GET free; POST etc. ask you |
| `write_note`, `finish_run` | phone | free |
| `list_toolbox` / `equip_tool` | — | library tools load on demand |
| `forge_tool` | phone | you review the source before it loads |

The toolbox ships ready-made tools (`download_file`, `transfer`,
`replicate`, `todo`, `json_query`, `extract_code`, `base64_codec`) whose
schemas don't bloat the prompt until equipped. `extract_code` pulls just the
code out of a page the agent found online — the `<script>`/`<pre>`/`<code>`
blocks and markdown fences that `http_request`'s readable-text mode drops or
flattens — and `base64_codec` encodes/decodes base64 (binary-safe, tolerant
of url-safe alphabets and missing padding). Forged tools are plain Python files in `<project>/tools/`,
loaded only after you approve the exact source (re-approval on any change).
Host tools only appear once you've registered a server.

**The rule:** anything internet happens on the phone. The GPU box gets files
pushed to it (`transfer`, `replicate`), not a network connection out. This is
enforced by trust, not a cage — the box has to reach the internet to install
vLLM and pull the weights, so egress always exists; a root agent can route
around any in-box block. So instead of lying to the model that it's
impossible, the prompt is honest: *you can, we're asking you not to, here's
why.* The deny-list (and `unshare -n` where available) is a speed bump that
stops accidents, not a wall.

**Two safety polarities, on purpose.** The GPU box is disposable, so its
gate is a deny-list speed bump: everything runs free except known network
commands (and, where the container allows `unshare`, those commands also lose
the network at the kernel — still escapable by root, just harder). Your
servers are real, so their gate fails closed: only commands positively
classified as read-only (`cat`, `journalctl`, `systemctl status`,
`docker logs`, ...) run free — everything else, and every file write, shows
you the exact command and waits for y/n.

## Layout

```
~/.hermes/            config.json (0600) · persona.md · gpu.json · hosts.json · logs
~/hermes-projects/<name>/
  mission.md          your standing orders (edit anytime)
  notes.md            the agent's memory notes
  history.jsonl       your prompts
  workspace/          the agent's file area
  tools/              forged tools + approval manifest
  runs/NNNN/          transcript.jsonl + summary.md per run
```

Everything is plain text — `nano mission.md` works fine. `persona.md` in
`~/.hermes/` is appended to the (deliberately lean) system prompt; keep it
short, the philosophy lives there now.

## Development

```sh
pip install -e ".[dev]"
python -m pytest tests/
```

Tests cover package assembly and budgets, path-escape defenses, the tool
registry (including forging/approval), the full agent loop against a scripted
mock backend, the GPU tier planner, the read-only command classifier and
host-tool gates (adversarial cases included), and the replicate relay.

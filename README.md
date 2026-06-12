# Hermes

A package-per-prompt agent shell for Termux. It rents nothing and hides
nothing: you rent a GPU on [Vast.ai](https://vast.ai), Hermes provisions
[Hermes-4.3-36B](https://huggingface.co/NousResearch/Hermes-4.3-36B) on it via
vLLM, and gives you an agent that lives across two machines:

- **your phone** (Termux) — where the operator is, where every project lives,
  and the **only place with internet access**;
- **the GPU box** — the model's home and the agent's disposable compute
  sandbox. Network access from there is blocked by design — at the kernel
  level (`unshare -n`) when the box allows it, by deny-list otherwise;
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
> gpu down                      # stop vLLM + optionally stop the instance
```

For risky server surgery the agent can `replicate` files from a host into
the GPU sandbox, reproduce and fix the problem there, and only then ask you
to apply the verified change back to the real machine.

`config set backend mock` lets you exercise the whole loop with no GPU.

## GPU tiers

`gpu serve` reads `nvidia-smi` and adapts — quantization is FP8 everywhere,
context length scales with total VRAM:

| total VRAM | context | example boxes |
|---|---|---|
| < 44 GB | refused | weights alone need ~37 GB |
| 44–56 GB | 16k (tight) | 1× 48GB card, 2× 24GB |
| 56–96 GB | 32–64k | A100 80GB, RTX 6000 Pro 96GB |
| 96–168 GB | 128–192k | H200 140GB |
| 168+ GB | 256k | 2× RTX 6000 Pro |

Override with `config set max_model_len <n>`. Architecture notes: Hopper, Ada
and Blackwell run FP8 natively; Ampere (A100/A40/3090) falls back to
weight-only FP8 (Marlin) — works, somewhat slower. Pre-Ampere is not
supported. The agent is told its context size and the package budgets shrink
automatically on small tiers.

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

**The hard rule:** anything internet happens on the phone. The GPU box gets
files pushed to it (`transfer`, `replicate`), never a network connection out.

**Two safety polarities, on purpose.** The GPU box is disposable, so its
gate is a deny-list: everything runs free except known network commands
(and, where the container allows `unshare`, commands physically lose the
network). Your servers are real, so their gate fails closed: only commands
positively classified as read-only (`cat`, `journalctl`, `systemctl status`,
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

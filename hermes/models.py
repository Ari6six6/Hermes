"""The model catalog — the one place that knows what's different between the
models Hermes can drive.

Hermes was built around NousResearch Hermes-4.3-36B (FP8 safetensors on vLLM),
but nothing in the agent loop is actually tied to it: the wire protocol is
plain OpenAI chat-completions. So "supporting another model" reduces to a row
in this table — its weights, how vLLM should quantize/parse it, how much VRAM
it needs, and how far its context stretches. `gpu serve` lets the operator pick
a row; everything model-specific downstream (the tier planner, the vLLM launch
command, the system-prompt identity) reads it from here.

Two polarities on purpose:
  - `ready=True`  — battle-tested, the path the app was tuned on.
  - `ready=False` — wired but experimental (e.g. GGUF on vLLM is single-GPU and
    slower than native FP8); the picker flags it so the operator knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSpec:
    key: str  # short id stored in config + used as the served model name root
    label: str  # what the picker shows
    repo: str  # HF repo id (positional vllm arg, unless GGUF)
    identity: str  # how the model is told to refer to itself in the system prompt
    min_total_gb: int  # VRAM floor — weights + runtime overhead
    max_model_len: int  # the longest context the model itself supports
    context_tiers: list  # [(total_gb_threshold, max_model_len), ...] first fit wins
    context_beyond: int  # context when VRAM exceeds every tier threshold
    weights_note: str  # shown while the box downloads ("~37GB", ...)
    served_name: str  # the `model` string the OpenAI client must send
    server: str = "vllm"  # runtime: "vllm" (FP8 safetensors) or "llama_cpp" (GGUF)
    quantization: str = "fp8"
    tool_call_parser: str = "hermes"
    gguf_file: str | None = None  # filename within `repo` when the model is GGUF
    tokenizer: str | None = None  # override tokenizer source (GGUF sometimes needs it)
    ready: bool = True
    notes_extra: list = field(default_factory=list)  # extra serve-time warnings

    @property
    def is_gguf(self) -> bool:
        return self.gguf_file is not None


# Hermes 4.3 supports up to 512K; FP8 36B weights are ~37GB, so 44GB is the floor.
HERMES = ModelSpec(
    key="hermes",
    label="Hermes-4.3-36B (NousResearch) · FP8",
    repo="NousResearch/Hermes-4.3-36B",
    identity="Hermes (NousResearch Hermes-4.3-36B)",
    min_total_gb=44,
    max_model_len=524288,
    context_tiers=[
        (56, 16384),
        (72, 32768),
        (96, 65536),
        (120, 131072),
        (168, 196608),
    ],
    context_beyond=262144,
    weights_note="first run downloads ~37GB of FP8 weights",
    served_name="NousResearch/Hermes-4.3-36B",
    quantization="fp8",
    tool_call_parser="hermes",
    ready=True,
)

# Qwen3.6-27B (HauhauCS balanced uncensored finetune), served from a Q5_K_P
# GGUF on its *native* runtime — llama.cpp's llama-server, not vLLM (whose GGUF
# path is experimental and slower). llama-server speaks the same OpenAI wire
# protocol, downloads the GGUF itself (`--hf-repo/--hf-file`), splits across
# GPUs, and emits OpenAI tool calls from the model's own chat template via
# `--jinja`. ~19GB of Q5 weights fits a single 24GB card.
QWEN = ModelSpec(
    key="qwen",
    label="Qwen3.6-27B (HauhauCS Balanced, uncensored) · Q5_K_P GGUF",
    repo="HauhauCS/Qwen3.6-27B-Uncensored-HauhauCS-Balanced",
    identity=(
        "Qwen3.6-27B (the HauhauCS Balanced uncensored finetune), running as "
        "the mind of the Hermes agent system"
    ),
    min_total_gb=22,  # a 24GB card reports ~23GB; ~19GB of Q5 weights fit
    max_model_len=131072,
    context_tiers=[
        (28, 16384),
        (40, 32768),
        (56, 65536),
        (96, 98304),
    ],
    context_beyond=131072,
    weights_note="first run downloads the ~19GB Q5_K_P GGUF",
    served_name="qwen3.6-27b",
    server="llama_cpp",
    quantization="gguf",
    gguf_file="Qwen3.6-27B-Uncensored-HauhauCS-Balanced-Q5_K_P.gguf",
    ready=False,
    notes_extra=[
        "First serve builds llama.cpp with CUDA on the box (needs the CUDA "
        "toolkit / nvcc — use a CUDA-devel image, not runtime-only).",
        "Community uncensored finetune — sanity-check its tool-calling discipline "
        "before trusting it with host writes.",
    ],
)

# Order is the picker order; HERMES first as the ready default.
CATALOG: dict[str, ModelSpec] = {HERMES.key: HERMES, QWEN.key: QWEN}
DEFAULT_KEY = HERMES.key


def model_list() -> list[ModelSpec]:
    return list(CATALOG.values())


def get_spec(key: str) -> ModelSpec:
    return CATALOG.get(key or DEFAULT_KEY, HERMES)


def resolve(cfg) -> ModelSpec:
    """The model the config currently points at (defaults to Hermes)."""
    return get_spec(cfg.get("model_id", DEFAULT_KEY))

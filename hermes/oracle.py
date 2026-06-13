"""The parity oracle: a sealed, recorded model of a target service.

The build loop NEVER touches the live target. A benign, operator-driven capture
(`hermes.capture`, on the phone) records the target's observable behavior into a
*bundle* — request/response fixtures plus metadata — and seals it. From then on
the agent works only against this bundle: a complete sandbox replica, not a live
environment, and it is told so plainly.

`replay()` answers "what did the real service return for this input?" from the
recording alone. That recorded answer is the ground truth the antithesis diffs a
reimplementation against — parity is measured against a frozen fixture, not a
machine someone else is running right now.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit


def request_key(method: str, path: str, query: str = "", body: str | None = None) -> str:
    """A canonical identity for a request, stable across header noise and query
    ordering. `path` may be a full URL or a bare path; an explicit `query`
    overrides any query embedded in `path`."""
    method = (method or "GET").upper()
    split = urlsplit(path)
    p = split.path or "/"
    raw_query = query or split.query or ""
    params = sorted(parse_qsl(raw_query, keep_blank_values=True))
    qn = urlencode(params)
    b = (body or "").strip()
    return f"{method} {p}?{qn}\n{b}"


@dataclass
class Probe:
    """One recorded request -> response pair from the target."""
    method: str
    path: str
    status: int
    response_body: str
    query: str = ""
    request_headers: dict = field(default_factory=dict)
    request_body: str | None = None
    content_type: str = ""
    response_headers: dict = field(default_factory=dict)
    captured_at: str = ""

    def key(self) -> str:
        return request_key(self.method, self.path, self.query, self.request_body)

    def label(self) -> str:
        q = f"?{self.query}" if self.query else ""
        return f"{self.method} {self.path}{q}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Probe":
        known = {f for f in Probe.__dataclass_fields__}  # type: ignore[attr-defined]
        return Probe(**{k: v for k, v in d.items() if k in known})


class OracleBundle:
    """A directory holding the sealed recording of a target service.

    Layout (inside the project's `oracle/`):
      manifest.json   — source, mode, win condition, sealed flag, counts
      probes.jsonl    — one recorded request/response pair per line
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    # -- layout ------------------------------------------------------------
    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def probes_path(self) -> Path:
        return self.root / "probes.jsonl"

    def exists(self) -> bool:
        return self.manifest_path.exists()

    # -- manifest ----------------------------------------------------------
    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def write_manifest(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(data, indent=2) + "\n")

    def is_sealed(self) -> bool:
        return bool(self.read_manifest().get("sealed"))

    @property
    def source(self) -> str:
        return self.read_manifest().get("source", "")

    @property
    def win_condition(self) -> str:
        return self.read_manifest().get("win_condition", "")

    # -- lifecycle ---------------------------------------------------------
    def init(self, source: str, mode: str = "url", win_condition: str = "") -> None:
        """Begin (or reset) an unsealed bundle for a target. Capture appends to
        it; seal() freezes it. Re-initializing clears any prior recording."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.probes_path.write_text("")
        self.write_manifest({
            "source": source,
            "mode": mode,
            "win_condition": win_condition.strip(),
            "sealed": False,
            "probe_count": 0,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "sealed_at": "",
        })

    def set_win_condition(self, text: str) -> None:
        manifest = self.read_manifest()
        manifest["win_condition"] = text.strip()
        self.write_manifest(manifest)

    def add_probe(self, probe: Probe) -> None:
        """Append a recorded probe. Refused once the bundle is sealed — a sealed
        replica is frozen, so the builder can trust it never shifts under it."""
        if self.is_sealed():
            raise ValueError("bundle is sealed — capture is closed")
        self.root.mkdir(parents=True, exist_ok=True)
        if not probe.captured_at:
            probe.captured_at = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.probes_path.open("a") as f:
            f.write(json.dumps(probe.to_dict(), ensure_ascii=False) + "\n")

    def probes(self) -> list[Probe]:
        if not self.probes_path.exists():
            return []
        out = []
        for line in self.probes_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Probe.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return out

    def seal(self) -> None:
        """Freeze the recording. After this the bundle is the sandbox replica:
        read-only ground truth, never appended to again."""
        manifest = self.read_manifest()
        manifest["sealed"] = True
        manifest["sealed_at"] = time.strftime("%Y-%m-%d %H:%M")
        manifest["probe_count"] = len(self.probes())
        self.write_manifest(manifest)

    # -- replay (the ground-truth lookup) ----------------------------------
    def replay(self, method: str, path: str, query: str = "",
               body: str | None = None) -> Probe | None:
        """Return the recorded response for this exact request, or None. This is
        a lookup in the frozen recording — it never reaches the network."""
        wanted = request_key(method, path, query, body)
        for probe in self.probes():
            if probe.key() == wanted:
                return probe
        return None

    def summary(self) -> str:
        manifest = self.read_manifest()
        if not manifest:
            return "(no target set)"
        state = "sealed" if manifest.get("sealed") else "OPEN (not yet sealed)"
        lines = [
            f"target: {manifest.get('source', '?')}  [{manifest.get('mode', 'url')}]",
            f"state:  {state}  ·  {manifest.get('probe_count', len(self.probes()))} probe(s)",
        ]
        win = manifest.get("win_condition", "")
        lines.append("win:    " + (win if win else "(none set)"))
        return "\n".join(lines)

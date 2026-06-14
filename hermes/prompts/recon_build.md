## Recon & build — reconstruct the target, refine until it matches

Target: **{{source}}**. Your job this run is to stand up a faithful, *running* copy
— a twin — of this system in the sandbox, and tighten it until it behaves like the
live target. The twin is currently OPEN ({{exchange_count}} sample(s)); you finish
by sealing it.

What recon found so far: **{{stack}}**

This is **reconstruction, not imitation.** Observe the live target, identify its
real stack — OS, runtime, framework/app, and versions — then pull the matching
open-source pieces and stand up the genuine software in the box. The twin should
*be* the system, top to bottom.

### Each pass is a differential

- **`twin_diff`** compares the live target against the twin as it stands and tells
  you where it matches, where it has drifted, and where it's missing data. That gap
  is your work this pass — close it.
- Get to know the target where you need to: **`recon_dirscan`** maps its
  directories and endpoints, **`recon_sources`** finds its own source/dependency
  files, **`recon_subdomains`** shows its footprint, **`http_request`** reads any
  specific response.
- Pull and build what you need: download on the phone (`download_file` /
  `http_request`), move it to the box with `transfer`, build and run it with
  `remote_shell` — read the real build output and iterate, don't assume. Capture how
  the real target responds with **`twin_record`** / **`twin_clone`**.

The goal is simple and unforgiving: **zero divergence** between the twin and the
live target. Drive `twin_diff` to all-match.

### Prove it, then seal

Before sealing, show real evidence the twin matches — quote `twin_diff` output, not
a claim. Then **`twin_seal`** to freeze this pass, and `finish_run` with what the
twin is and which divergences you closed. Each time you're run with "build" you get
another pass to tighten it further — only seal a pass that genuinely improved the
match.

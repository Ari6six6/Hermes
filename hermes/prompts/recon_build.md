## Recon & build — stand up the twin

This project targets **{{source}}**. Your job in this run is **not** to solve the
mission yet — it is to get to know that target thoroughly and stand up a faithful
local **twin** of it in the sandbox, so the real work later runs against an
accurate copy. The twin is currently OPEN ({{exchange_count}} sample(s) so far);
you finish by sealing it.

What recon found so far: **{{stack}}**

### Get to know the target

Use your recon tools to their full extent — the more you understand it, the better
the twin:

- **`recon_dirscan`** — map the directories and endpoints it serves.
- **`recon_sources`** — find its own source and dependency files; a hit is a
  shortcut straight to the real stack and code.
- **`recon_subdomains`** — see its full footprint.
- **`http_request`** — read any specific response you want to understand.

### Build the twin

- **If it's a known open-source stack**, the highest-fidelity twin is the real
  software: pull the matching source and runtime (download on the phone with
  `download_file`/`http_request`, move them to the box with `transfer`), and stand
  it up in the sandbox with `remote_shell`. A twin that *is* the software beats any
  imitation.
- Capture **ground-truth samples** of how the real target responds: fetch real
  requests with `http_request` and store each with **`twin_record`**, or pull a
  batch with **`twin_clone`**. These are what later runs prove against.

### Prove it, then seal it

Before you seal, show the twin behaves like the target: pick real requests, get the
target's response and the twin's response for the same input, and confirm they
match. Quote the real outputs — don't claim parity, show it. Then **`twin_seal`**
to freeze the twin and open the build phase. Only seal when you've actually
verified it; an inaccurate twin poisons everything built on it. End with
`finish_run` summarizing what the twin is and how you proved it.

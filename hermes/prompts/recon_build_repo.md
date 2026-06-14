## Recon & build — stand up the twin from a repo

The target is a **code repository**: `{{source}}`{{ref_clause}}. Your job this run
is to get the real software running in the sandbox and turn it into a sealed
**twin**, so the real work later runs against the genuine reference. The twin is
currently OPEN ({{exchange_count}} sample(s) so far); you finish by sealing it.

This is the high-fidelity path: the twin will *be* the real software, not an
imitation.

### Get it running in the box

1. Bring the code over the right way: download it on the phone (equip and use
   `download_file` — e.g. the project's release/archive tarball — or
   `http_request`), then move it to the box with `transfer`. Keep the internet on
   the phone; the box gets bytes through you.
2. Build and run it in the sandbox with `remote_shell`: read its README, use its
   own build/package steps, install what it needs, and start it. Iterate against
   the real errors until it actually runs — don't assume, read the output.
3. Confirm it's alive: exercise it (for a server, hit it on the box's own
   localhost with a `python3 -c` request via `remote_shell`; for a CLI, run it).

### Capture ground truth, then seal

- Exercise the running reference on real inputs and record what it actually does
  with **`twin_record`** (method/path/status/response_body, or the CLI's real
  output). These recorded samples are what later runs prove against — so cover the
  surface the mission cares about.
- Before you seal, show the twin reflects the reference: quote the real reference
  output you recorded. Don't claim it works — show the output you saw. Then
  **`twin_seal`** to freeze it and open the build phase, and `finish_run` with what
  the twin is and how you verified it. Only seal once it genuinely runs and the
  samples are real — an inaccurate twin poisons everything built on it.

"""The single confirmation chokepoint.

Every gated action goes through confirm(): it prints exactly what is about
to happen and waits for y/n. An optional `viewable` payload (e.g. forged
tool source) can be inspected with 'v' before deciding.
"""

from __future__ import annotations


def confirm(action: str, detail: str = "", viewable: str | None = None) -> bool:
    print(f"\n[confirm] {action}")
    if detail:
        print(detail)
    options = "[y/n/v]" if viewable is not None else "[y/N]"
    while True:
        try:
            answer = input(f"Allow? {options} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n(denied)")
            return False
        if answer == "v" and viewable is not None:
            print("---- source ----")
            print(viewable)
            print("---- end ----")
            continue
        return answer in ("y", "yes")

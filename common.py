import sys

# Shared by main.py (the watcher) and resolve_modules.py (module flag
# resolution) — kept dependency-free so resolve_modules.py doesn't need
# watchdog installed just to print a status line.


class Color:
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    RESET  = "\033[0m"

def log(msg: str, color: str = Color.RESET) -> None:
    # stderr, always: resolve_modules.py's stdout is reserved for its one
    # line of actual output (the resolved flags), often captured with
    # `$(...)` in a shell script — log noise must never land there.
    print(f"{color}[watcher] {msg}{Color.RESET}", file=sys.stderr)

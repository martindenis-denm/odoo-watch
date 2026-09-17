import argparse
import ast
import os
import re
import sys
import time
from pathlib import Path

from common import Color, log
from resolve_modules import parse_module_names, get_odoo_major_version, resolve_module_flags

try:
    from watchdog.observers import Observer
except ImportError:
    sys.exit("Missing dependency — run:  pip install watchdog")

try:
    import websockets  # noqa: F401 (livereload.py needs it; validated here for the friendly error)
except ImportError:
    sys.exit("Missing dependency — run:  pip install websockets")

from livereload import LiveReload
from manager import Manager, TRACKED_EXTENSIONS
from rpc import OdooRPC
from runner import OdooRunner

# ── Module discovery ───────────────────────────────────────────────────────────

_IGNORE_DIRS = {".git", "__pycache__", "node_modules", "documentation", "i18n"}

def is_installable(manifest_path: str) -> bool:
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = ast.literal_eval(f.read())
        return bool(manifest.get("installable", True))
    except (OSError, SyntaxError, ValueError):
        return True

def discover_modules(watch_dirs: list[str]) -> dict[str, str]:
    """Map module technical name -> absolute module directory, for every
    installable module found under the watched directories."""
    modules: dict[str, str] = {}
    for wd in watch_dirs:
        root = os.path.abspath(os.path.expanduser(wd))
        if os.path.isfile(os.path.join(root, "__manifest__.py")):
            modules[os.path.basename(root)] = root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
            if "__manifest__.py" in filenames:
                name = os.path.basename(dirpath)
                if is_installable(os.path.join(dirpath, "__manifest__.py")):
                    modules[name] = dirpath
                else:
                    log(f"Skipping module '{name}': manifest marks it not installable", Color.DIM)
                dirnames[:] = []  # modules don't nest
    return modules

# ── odoo-bin command construction ──────────────────────────────────────────────

_ALL_DEV_MODES = {"access", "qweb", "reload", "xml"}

def normalize_dev_mode(cmd: str) -> str:
    """Make sure Odoo's cache-bypass for views/assets (xml) is on, and that
    Odoo's own .py watcher (reload) is on — the wrapper doesn't track .py
    itself, Odoo's own FSWatcher + SIGHUP + phoenix re-exec already does
    that, natively, and there's no reason to reimplement it."""
    m = re.search(r'(--dev(?:=|\s+))(\S+)', cmd)
    if not m:
        return cmd.rstrip() + " --dev=xml,reload"

    modes = {x.strip() for x in m.group(2).split(",") if x.strip()}
    if "all" in modes:
        modes = set(_ALL_DEV_MODES)
    modes.add("xml")
    modes.add("reload")
    new_value = ",".join(sorted(modes))
    return cmd[:m.start(2)] + new_value + cmd[m.end(2):]

def build_odoo_cmd(db_name: str, addons_path: str, http_port: int, demo_flag: str) -> str:
    """Build the odoo-bin invocation from its parts rather than taking a
    ready-made command string — addons_path arrives as one comma-separated
    string (that's how odoo-server.sh already builds it), split it back out
    into its individual paths and rejoin them explicitly."""
    paths = [p.strip() for p in addons_path.split(",") if p.strip()]
    cmd = (
        f"./odoo-bin --addons-path={','.join(paths)} --limit-memory-hard -1 "
        f"--db-filter={db_name} -d {db_name} --http-port={http_port} "
        f"--http-interface '127.0.0.1' --dev=all"
    )
    if demo_flag:
        cmd += f" {demo_flag}"
    return normalize_dev_mode(cmd)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="odoo-watch — auto-restart/live-sync on module changes")
    ap.add_argument("--db", required=True, metavar="DBNAME")
    ap.add_argument("--addons-path", required=True, metavar="PATH[,PATH...]",
                     help="Comma-separated addons paths, same as odoo-bin's own --addons-path")
    ap.add_argument("--demo-flag", default="", metavar="FLAG",
                     help="Raw flag to pass through as-is, e.g. --with-demo or --without-demo=1")
    ap.add_argument("--http-port", type=int, default=8069, metavar="PORT")
    ap.add_argument("--odoo-path", required=True, metavar="ODIR")
    ap.add_argument("--watch-path", nargs="*", default=[], metavar="WDIR",
                     help="Directories to watch for live module sync (optional). If empty, "
                          "Odoo just runs normally — its own --dev=reload watchdog still "
                          "handles .py changes, there's just no .xml/RPC sync loop on top")
    ap.add_argument("--modules", nargs="*", default=[], metavar="MODULE",
                     help="Module names to install/upgrade/reinit before starting "
                          "(comma or space separated) — the flag is resolved from each "
                          "module's current DB state, you don't pick -i/-u/--reinit yourself. "
                          "To resolve flags without starting anything, use resolve_modules.py instead.")
    ap.add_argument("--debounce", type=float, default=1, metavar="SEC")
    ap.add_argument("--reload-port", type=int, default=35729, metavar="PORT")
    ap.add_argument("--rpc-login", default="admin", metavar="LOGIN")
    ap.add_argument("--rpc-password", default="admin", metavar="PASSWORD")
    args = ap.parse_args()

    odoo_path = os.path.expanduser(args.odoo_path)
    module_names = parse_module_names(args.modules)

    watch_dirs = []
    for d in args.watch_path:
        d = d.strip()
        if not d:
            continue  # e.g. --watch-path "" from an unset $repo_path in odoo-server.sh
        expanded_d = os.path.expanduser(d)
        if not os.path.isdir(expanded_d):
            sys.exit(f"[watcher] Watch path not found: {expanded_d}")
        watch_dirs.append(expanded_d)

    db_name = args.db
    port = args.http_port
    cmd = build_odoo_cmd(db_name, args.addons_path, port, args.demo_flag)

    if module_names:
        version = get_odoo_major_version(odoo_path)
        flags = resolve_module_flags(db_name, module_names, version)
        if flags:
            cmd = f"{cmd} {flags}"
            # log(f"Boot will also run: {flags}", Color.YELLOW)

    runner = OdooRunner(cmd, odoo_path, port)
    runner.start()

    try:
        if not watch_dirs:
            # Nothing to watch: just run Odoo normally. Its own --dev=reload
            # watchdog (always on, see normalize_dev_mode) still restarts on
            # .py changes on its own — there's no .xml/RPC sync loop to add
            # on top without a --watch-path to scope it to.
            log("No --watch-path given, running odoo normally. Ctrl+C to stop.", Color.GREEN)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            return

        module_dirs = discover_modules(watch_dirs)

        # No startup sweep here on purpose: --modules already resolved every
        # module reachable from MODULE_NAMES (named modules and their
        # dependencies) to either "uninstalled" or "installed" in the boot
        # above. From here on, everything is reactive and module-per-module —
        # a live file change upgrades exactly the one module it belongs to,
        # nothing more, whether or not you touched anything is what decides
        # whether anything happens.
        rpc = OdooRPC(port, db_name, args.rpc_login, args.rpc_password)
        rpc.wait_ready()
        rpc.update_module_list()

        reloader = LiveReload(args.reload_port)
        reloader.start()

        manager = Manager(rpc, reloader, args.debounce, module_dirs)
        observer = Observer()
        for watch_dir in watch_dirs:
            for f in Path(watch_dir).rglob("*"):
                if f.suffix in TRACKED_EXTENSIONS:
                    manager.track(str(f))
            observer.schedule(manager, watch_dir, recursive=True)
        observer.start()

        log("Watching for changes. Ctrl+C to stop.", Color.GREEN)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            manager.stop()
            observer.join()
    finally:
        runner.shutdown()


if __name__ == "__main__":
    main()

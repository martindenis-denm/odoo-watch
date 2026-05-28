import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import threading
import urllib.request
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import PatternMatchingEventHandler
except ImportError:
    sys.exit("Missing dependency — run:  pip install watchdog")

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    sys.exit("Missing dependency — run:  pip install websockets")

# ── Logging ────────────────────────────────────────────────────────────────────

class Color:
    DIM    = "\033[2m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    RESET  = "\033[0m"

def log(msg: str, color: str = Color.RESET) -> None:
    print(f"{color}[watcher] {msg}{Color.RESET}")

# ── XML parsing ────────────────────────────────────────────────────────────────

_ASSET_RE = re.compile(
    r'(<asset\b[^>]*>)',
    re.IGNORECASE,
)
_TEMPLATE_RE = re.compile(
    r'(<template\b[^>]*>)',
    re.IGNORECASE,
)
_RECORD_RE = re.compile(
    r'<record\b[^>]*\bid=["\']([^"\']+)["\'][^>]*>(.*?)</record>',
    re.DOTALL | re.IGNORECASE,
)
_ARCH_CONTENT_RE = re.compile(
    r'(<field\b[^>]*\bname=["\']arch["\'][^>]*>).*?(</field>)',
    re.DOTALL | re.IGNORECASE,
)
_XML_COMMENT_RE = re.compile(r'<!--.*?-->', re.DOTALL)

_LIVE_RELOAD_EXTENSIONS = {".js", ".css", ".scss", ".svg"}
_RESTART_EXTENSIONS = {".py"}
_TRACKED_EXTENSIONS = _RESTART_EXTENSIONS | _LIVE_RELOAD_EXTENSIONS | {".xml"}
_IGNORE_PATTERNS = ["*/__pycache__/*", "*/.git/*", "*/node_modules/*", "*/documentation/*", "*/i18n/*"]

def read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""

def xml_snapshot(path: str) -> dict:
    text = _XML_COMMENT_RE.sub('', read(path))
    records = {
        rid: hashlib.sha1(_ARCH_CONTENT_RE.sub(r'\1\2', body).encode()).hexdigest()
        for rid, body in _RECORD_RE.findall(text)
    }
    templates = frozenset(_TEMPLATE_RE.findall(text))
    assets    = frozenset(_ASSET_RE.findall(text))
    return {"records": records, "templates": templates, "assets": assets}

def xml_diff(old: dict, new: dict) -> bool:
    return (
        old["records"] != new["records"]  or
        old["templates"] != new["templates"] or
        old["assets"] != new["assets"]
    )

def file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""

def get_odoo_major_version(odoo_path):
    odoo_dir = os.path.expanduser(odoo_path)
    if not os.path.isdir(odoo_dir):
        sys.exit(f"[watcher] Odoo path not found: {odoo_dir}")

    git_head_path = odoo_dir + "/.git/HEAD"
    with open(git_head_path, 'r') as file:
        file_content = file.read()

    match = re.search(r'(\d+)\.\d+', file_content)
    return int(match.group(1)) if match else 19

def get_values_from_cmd(cmd: str) -> tuple | None:
    port_match = re.search(r'(?:-p|--http-port)\s+(\S+)', cmd)
    port = port_match.group(1) if port_match else 8069

    db_match = re.search(r'(?:-d|--database)\s+(\S+)', cmd)
    db_name = db_match.group(1) if db_match else None

    modules_i_match = re.search(r'(?:-i|--init)\s+(\S+)', cmd)
    modules_u_match = re.search(r'(?:-u|--update)\s+(\S+)', cmd)
    modules_r_match = re.search(r'(?:--reinit)\s+(\S+)', cmd)

    modules = ",".join(filter(None, [
        modules_i_match.group(1) if modules_i_match else None,
        modules_u_match.group(1) if modules_u_match else None,
        modules_r_match.group(1) if modules_r_match else None,
    ])) or None

    return (port, db_name, modules)

def strip_module_flags(cmd: str) -> str:
    cmd = re.sub(r'(?:-i|--init)\s+\S+', '', cmd)
    cmd = re.sub(r'(?:-u|--update)\s+\S+', '', cmd)
    cmd = re.sub(r'--reinit\s+\S+', '', cmd)
    return ' '.join(cmd.split())  # normalize extra whitespace

def resolve_module_flag(db_name: str, modules: str, version: int) -> str:
    """
    Bucket each module by its current state in the DB and return the
    appropriate combination of Odoo CLI flags:

      uninstalled / to install  →  -i
      installed                 →  --reinit (>= v19), -u (< v19)
      to upgrade                →  -u
    """
    module_list = [m.strip() for m in modules.split(",") if m.strip()]
    sql_list    = "'" + "','".join(module_list) + "'"

    try:
        result = subprocess.run(
            ["psql", "-lqt"],
            capture_output=True, text=True, check=True,
        )
        db_names = [line.split("|")[0].strip() for line in result.stdout.splitlines()]
        db_exists = db_name in db_names
    except subprocess.SubprocessError:
        db_exists = False

    if not db_exists:
        log(f"Database '{db_name}' not found → all modules use -i", Color.DIM)
        return f"-i {','.join(module_list)}"

    states: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["psql", "-d", db_name, "-tAc",
             f"SELECT name, state FROM ir_module_module WHERE name IN ({sql_list});"],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) == 2:
                states[parts[0].strip()] = parts[1].strip()
    except subprocess.SubprocessError:
        pass

    to_install: list[str] = []
    to_reinit:  list[str] = []
    to_upgrade: list[str] = []

    for mod in module_list:
        state = states.get(mod, "uninstalled")
        if state in ("uninstalled", "to install"):
            to_install.append(mod)
        elif state == "installed":
            if version >= 19:
                to_reinit.append(mod)
            else:
                to_install.append(mod)
        elif state == "to upgrade":
            to_upgrade.append(mod)
        else:
            log(f"  {mod}: unknown state '{state}' → treated as uninstalled", Color.DIM)
            to_install.append(mod)

    flags: list[str] = []
    if to_install: flags.append(f"-i {','.join(to_install)}")
    if to_reinit:  flags.append(f"--reinit {','.join(to_reinit)}")
    if to_upgrade: flags.append(f"-u {','.join(to_upgrade)}")
    return " ".join(flags)

# ── LiveReload server ──────────────────────────────────────────────────────────

class LiveReload:
    HELLO = json.dumps({
        "command": "hello",
        "protocols": ["http://livereload.com/protocols/official-7"],
        "serverName": "odoo-watch",
    })

    def __init__(self, port: int):
        self.port = port
        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._reconnecting = False

    def start(self):
        self._thread.start()

    def reload(self, path: str):
        if not self.is_active():
            return
        msg = json.dumps({"command": "reload", "path": path, "liveCSS": True})
        asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)

    def is_active(self):
        return self._clients and self._loop

    async def _broadcast(self, msg: str):
        for ws in list(self._clients):
            try:
                await ws.send(msg)
            except Exception:
                self._clients.discard(ws)

    async def _handler(self, ws):
        self._clients.add(ws)
        handshake = False
        try:
            await ws.send(self.HELLO)
            async for _ in ws:
                if not handshake:
                    handshake = True
                    if not self._reconnecting:
                        log(f"Live reload client connected ({len(self._clients)} in total)", Color.CYAN)
                    self._reconnecting = False
        finally:
            self._clients.discard(ws)
            if handshake:
                if ws.close_code == 1001:
                    self._reconnecting = True
                else:
                    log(f"Live reload client disconnected ({len(self._clients)} remaining)", Color.DIM)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            async with ws_serve(self._handler, "127.0.0.1", self.port):
                await asyncio.Future()  # run forever

        self._loop.run_until_complete(_serve())


# ── Odoo runner ────────────────────────────────────────────────────────────────

class OdooRunner:
    def __init__(self, command: str, reloader: LiveReload, odoo_path: str):
        self.command = command
        self.reloader = reloader
        self.odoo_port, self.db_name, self.modules = get_values_from_cmd(self.command)
        self.odoo_path = odoo_path
        self.version = get_odoo_major_version(self.odoo_path)
        self._process: subprocess.Popen | None = None
        self._stop_poll = threading.Event()
        self.is_restarting = False
        self.is_shutting_down = False

    def start(self):
        self.is_restarting = True
        self._process = self._spawn()
        self._poll()
        self.is_restarting = False

    def restart(self):
        if self.is_shutting_down:
            return
        self.is_restarting = True
        self.kill()
        self._wait_for_port_free(self.odoo_port)
        self._process = self._spawn()
        self._poll()
        self.is_restarting = False
        self.reloader.reload("/")

    def _wait_for_port_free(self, port: int, timeout: int = 30):
        for _ in range(timeout):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return
            try:
                time.sleep(1)
            except (KeyboardInterrupt, Exception):
                return # don't block shutdown on a second Ctrl+C
        log(f"Port {port} still in use after {timeout}s", Color.RED)

    def _spawn(self) -> subprocess.Popen:
        cmd = self.command
        if self.db_name and self.modules:
            cmd = f"cd {self.odoo_path} && {strip_module_flags(self.command)} {resolve_module_flag(self.db_name, self.modules, self.version)}"
        process = subprocess.Popen(
            cmd,
            shell=True,
            cwd=os.getcwd(),
            start_new_session=True,
        )
        log(f"Starting Odoo (pid {process.pid})", Color.GREEN)
        return process

    def kill(self) -> None:
        self.is_shutting_down = True
        self._stop_poll.set()

        if not self._process or self._process.poll() is not None:
            self._process = None
            return
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            # self._process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            self._process = None
            self.is_shutting_down = False

    def _poll(self):
        """Wait until Odoo's health endpoint responds"""
        url = f"http://127.0.0.1:{self.odoo_port}/web/health"
        self._stop_poll.clear() # reset for this poll cycle
        for _ in range(120): # wait up to 2 minutes
            if self._stop_poll.wait(timeout=1):
                return # a new restart or shutdown cancelled this poll
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        proc = self._process
                        log(f"Odoo is up and running (pid {proc.pid if proc else '?'})", Color.GREEN)
                        return
            except (KeyboardInterrupt, Exception):
                pass
        log(f"Odoo did not come up within 2 minutes", Color.RED)

# ── Watchdog handler ───────────────────────────────────────────────────────────

class Manager(PatternMatchingEventHandler):
    def __init__(self, runner: OdooRunner, reloader: LiveReload, debounce: float):
        super().__init__(
            patterns=[f"*{ext}" for ext in _TRACKED_EXTENSIONS],
            ignore_patterns=_IGNORE_PATTERNS,
            ignore_directories=True,
            case_sensitive=False,
        )

        self.runner = runner
        self.reloader = reloader
        self._debounce = debounce
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._xml: dict[str, dict] = {}
        self._hashes: dict[str, str]  = {}

    def start(self):
        self.runner.start()
        self.reloader.start()

    def track(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        ext = os.path.splitext(path)[1]
        self._hashes[abs_path] = file_hash(abs_path)
        if ext == ".xml":
            self._xml[abs_path] = xml_snapshot(path)

    def kill(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
                self._pending = None
            self.runner.kill()

    # ── watchdog callback ─────────────────────────────────────────────────────

    def on_modified(self, event) -> None:
        path = os.path.abspath(event.src_path)
        extension = os.path.splitext(path)[1]
        h = file_hash(path)

        if self._hashes.get(path) == h:
            return
        self._hashes[path] = h

        if extension == ".xml":
            new_snapshot = xml_snapshot(path)
            old_snapshot = self._xml.get(
                path, {"records": {}, "templates": frozenset(), "assets": frozenset()}
            )
            self._xml[path] = new_snapshot
            self._schedule("restart" if xml_diff(old_snapshot, new_snapshot) else "reload", path)
        elif extension in _RESTART_EXTENSIONS:
            self._schedule("restart", path)
        elif extension in _LIVE_RELOAD_EXTENSIONS:
            self._schedule("reload", path)

    def _schedule(self, action: str, path: str) -> None:
        with self._lock:
            if action == "reload" and self.runner.is_restarting:
                return # restart already queued; reload would be redundant

            file_name = os.path.basename(path)
            if self._timer:
                self._timer.cancel()
            else:
                if action == "restart":
                    self.runner.is_restarting = True
                    log("Restarting server: change detected in %s." % file_name, Color.YELLOW)
                elif self.reloader.is_active():
                    log("Live reload: change detected in %s." % file_name, Color.CYAN)

            self._timer = threading.Timer(self._debounce, self._fire, args=(action, path))
            self._timer.daemon = True
            self._timer.start()

    def _fire(self, action: str, path: str):
        if action == "reload" and self.runner.is_restarting:
            return # restart already queued; reload would be redundant

        with self._lock:
            self._timer = None

        if action == "restart":
            self.runner.restart()
        else:
            self.reloader.reload(path)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="odoo-watch — auto-restart on module changes")
    ap.add_argument("--cmd", required=True, metavar="CMD")
    ap.add_argument("--odoo-path", required=True, metavar="ODIR")
    ap.add_argument("--watch-path", required=True, nargs="+", metavar="WDIR")
    ap.add_argument("--debounce", type=float, default=1, metavar="SEC")
    ap.add_argument("--reload-port", type=int, default=35729, metavar="PORT")
    args = ap.parse_args()

    for d in args.watch_path:
        expanded_d = os.path.expanduser(d)
        if not os.path.isdir(expanded_d):
            sys.exit(f"[watcher] Watch path not found: {expanded_d}")

    reloader = LiveReload(args.reload_port)
    runner = OdooRunner(args.cmd, reloader, args.odoo_path)
    manager = Manager(runner, reloader, args.debounce)

    observer = Observer()
    for watch_dir in args.watch_path:
        expanded_dir = os.path.expanduser(watch_dir)
        for f in Path(expanded_dir).rglob("*"):
            if f.suffix in _TRACKED_EXTENSIONS:
                manager.track(str(f))
        observer.schedule(manager, expanded_dir, recursive=True)

    observer.start()
    manager.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        manager.kill()
        observer.join()


if __name__ == "__main__":
    main()

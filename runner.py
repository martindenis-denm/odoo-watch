import os
import signal
import subprocess
import threading
import time
import urllib.request

from common import Color, log

# Spawns and supervises the Odoo process itself. Boots exactly once — module
# state (see resolve_modules.py) is resolved via a single psql query before
# this ever runs, so the right -i/-u/--reinit flags are already baked into
# `command` and there's no need to start Odoo just to find that out.
#
# Odoo's own SIGHUP re-exec strips -i/-u from its argv automatically on every
# restart it does from here on (its own --dev=reload watcher, not something
# this wrapper is involved in), but NOT --reinit — if `command` used --reinit,
# it will keep re-running on every future .py-triggered restart for the rest
# of the session. Traded on purpose for a single boot rather than a respawn to
# launder it away.


class OdooRunner:
    def __init__(self, command: str, odoo_path: str, port: int):
        self.command = command
        self.odoo_path = odoo_path
        self.port = port
        self._process: subprocess.Popen | None = None
        self._stop_wait = threading.Event()

    def start(self) -> None:
        self._spawn(self.command)
        self._wait_healthy()

    def _spawn(self, command: str) -> None:
        cmd = f"cd {self.odoo_path} && {command}"
        self._process = subprocess.Popen(cmd, shell=True, start_new_session=True)
        log(f"Starting Odoo (pid {self._process.pid})", Color.GREEN)

    def _wait_healthy(self, timeout: int = 120) -> None:
        # While Odoo isn't listening yet, a connection is refused near-instantly,
        # so a short gap between attempts is enough to stay responsive. But once
        # it IS accepting connections, an early request can sit there for several
        # seconds — generating the routing map for the first time is not cheap —
        # and a short per-attempt timeout would abandon it and fire a new
        # overlapping one before the last is even done, piling up concurrent
        # requests that all finish in a burst (visible as a wall of duplicate
        # /web/health lines in Odoo's own log). A generous per-attempt timeout
        # keeps at most one request in flight at any time instead.
        url = f"http://127.0.0.1:{self.port}/web/health"
        self._stop_wait.clear()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._stop_wait.wait(timeout=1):
                return
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    if r.status == 200:
                        log(f"Odoo is up ({time.time() - t0:.1f}s)", Color.GREEN)
                        return
            except Exception:
                pass
        log("Odoo did not come up within the timeout", Color.RED)

    def _stop(self, sig: signal.Signals, timeout: int = 15) -> None:
        if not self._process or self._process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self._process.pid), sig)
            self._process.wait(timeout=timeout)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def shutdown(self) -> None:
        self._stop_wait.set()
        self._stop(signal.SIGTERM)

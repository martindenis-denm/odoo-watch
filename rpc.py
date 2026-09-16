import http.cookiejar
import json
import time
import urllib.request

from common import Color, log

# Module install/upgrade without a process restart.
#
# Deliberately NOT /xmlrpc/2 or /jsonrpc: both are deprecated as of Odoo 19
# (removal scheduled for 22). The replacement, /json/2, needs a Bearer API key
# rather than a login/password, and generating that first key isn't reachable
# through /json/2 itself (needs one to make one) nor available on every plan.
# Instead this uses the same two routes the Odoo web client itself uses for
# every single call it makes — /web/session/authenticate (login) and
# /web/dataset/call_kw (everything else), session-cookie authenticated. Not
# deprecated, not plan-gated, same (model, method, args, kwargs) shape as the
# old execute_kw, so nothing downstream of OdooRPC.call() had to change.


class OdooRPC:
    def __init__(self, port: int, db: str, login: str, password: str):
        self.url = f"http://127.0.0.1:{port}"
        self.db = db
        self.login = login
        self.password = password
        self._cookiejar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._cookiejar))
        self._authenticated = False

    def _jsonrpc(self, path: str, params: dict, timeout: float = 120):
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}).encode()
        req = urllib.request.Request(
            f"{self.url}{path}", data=payload, headers={"Content-Type": "application/json"},
        )
        with self._opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if "error" in body:
            data = body["error"].get("data") or {}
            raise RuntimeError(data.get("message") or body["error"].get("message"))
        return body.get("result")

    def _authenticate(self):
        result = self._jsonrpc("/web/session/authenticate",
                                {"db": self.db, "login": self.login, "password": self.password})
        if not result or not result.get("uid"):
            raise RuntimeError(f"RPC auth failed for {self.login}@{self.db}")
        self._authenticated = True

    def call(self, model: str, method: str, *args, **kwargs):
        if not self._authenticated:
            self._authenticate()
        return self._jsonrpc("/web/dataset/call_kw",
                              {"model": model, "method": method, "args": list(args), "kwargs": kwargs})

    def wait_ready(self, timeout: int = 120) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self._authenticate()
                return
            except Exception:
                time.sleep(0.5)
        raise TimeoutError("Odoo did not become reachable for RPC")

    def update_module_list(self) -> None:
        self.call('ir.module.module', 'update_list')

    def reset_stuck_modules(self) -> list[int]:
        # button_immediate_* takes a DB-wide lock and refuses to run while ANY
        # module is stuck in to install/to upgrade/to remove — not just the one
        # being touched. button_reset_state clears that (it's what the Apps UI
        # uses for the same situation).
        stuck = self.call('ir.module.module', 'search',
                           [('state', 'in', ['to install', 'to upgrade', 'to remove'])])
        if stuck:
            self.call('ir.module.module', 'button_reset_state')
        return stuck

    def _run_button(self, ids: list[int], method: str, label: str) -> None:
        t0 = time.time()
        try:
            self.call('ir.module.module', method, ids)
        except RuntimeError as e:
            if 'another module operation' not in str(e):
                raise
            log("A module operation was already stuck in the database — resetting and retrying", Color.YELLOW)
            self.reset_stuck_modules()
            self.call('ir.module.module', method, ids)
        dt = time.time() - t0
        note = " (large dependency graph — this is Odoo's own cost, not the watcher's)" if dt > 10 else ""
        log(f"{label} finished in {dt:.1f}s{note}", Color.DIM)

    def upgrade(self, name: str) -> None:
        """Upgrade module `name` in place, no restart. No state is checked —
        --modules already resolved every module reachable from MODULE_NAMES
        (named modules and their dependencies) down to either "uninstalled" or
        "installed" in the single startup boot, so by the time a live file
        change reaches here the module is expected to already be installed.
        If it isn't, Odoo's own button_upgrade says so clearly and this is a
        no-op beyond that — add it to --modules instead of guessing here."""
        ids = self.call('ir.module.module', 'search', [('name', '=', name)])
        if not ids:
            log(f"Module '{name}' isn't known to Odoo — add it to --modules first", Color.YELLOW)
            return
        log(f"Upgrading '{name}' via RPC...", Color.YELLOW)
        self._run_button(ids, 'button_immediate_upgrade', f"Upgrade of '{name}'")

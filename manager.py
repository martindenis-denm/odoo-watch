import hashlib
import os
import re
import threading

from watchdog.events import PatternMatchingEventHandler

from common import Color, log
from rpc import OdooRPC

# Classifies each changed .xml file as either a no-op (arch/template body edit
# — Odoo's own --dev=xml already re-reads these from disk on every request, so
# nothing needs to happen at all) or a module sync (record added or removed,
# new template, or any content change outside a record's arch field — these
# only take effect once Odoo re-loads the module's data, which needs an
# ir.module.module install/upgrade).

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

ASSET_EXTENSIONS = {".js", ".css", ".scss", ".svg"}  # served live by --dev=xml, no action needed
# TRACKED_EXTENSIONS = ASSET_EXTENSIONS | {".xml"}  # .py is Odoo's own --dev=reload watcher's job
TRACKED_EXTENSIONS = {".xml"}  # .py is Odoo's own --dev=reload watcher's job
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

def xml_needs_sync(old: dict, new: dict) -> bool:
    return (
        old["records"] != new["records"] or
        old["templates"] != new["templates"] or
        old["assets"] != new["assets"]
    )

def file_hash(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""

def owning_module(path: str, module_dirs: dict[str, str]) -> str | None:
    path = os.path.abspath(path)
    best_name, best_dir = None, ""
    for name, d in module_dirs.items():
        if (path == d or path.startswith(d + os.sep)) and len(d) > len(best_dir):
            best_name, best_dir = name, d
    return best_name

# ── Watchdog handler ───────────────────────────────────────────────────────────

class Manager(PatternMatchingEventHandler):
    def __init__(self, rpc: OdooRPC, debounce: float, module_dirs: dict[str, str]):
        super().__init__(
            patterns=[f"*{ext}" for ext in TRACKED_EXTENSIONS],
            ignore_patterns=_IGNORE_PATTERNS,
            ignore_directories=True,
            case_sensitive=False,
        )
        self.rpc = rpc
        self.module_dirs = module_dirs
        self._debounce = debounce
        self._lock = threading.Lock()
        self._work_lock = threading.Lock()  # serializes RPC calls so a slow upgrade
                                             # can't overlap another and trip Odoo's
                                             # DB-wide module-operation lock
        self._timer: threading.Timer | None = None
        self._xml: dict[str, dict] = {}
        self._hashes: dict[str, str] = {}
        self._pending_modules: set[str] = set()
        # self._pending_no_action = False

    def track(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        ext = os.path.splitext(path)[1]
        self._hashes[abs_path] = file_hash(abs_path)
        if ext == ".xml":
            self._xml[abs_path] = xml_snapshot(path)

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

    # ── watchdog callbacks ────────────────────────────────────────────────────

    def on_created(self, event):
        self._handle(event.src_path)

    def on_modified(self, event):
        self._handle(event.src_path)

    def on_moved(self, event):
        self._handle(event.dest_path)

    def _handle(self, src_path: str) -> None:
        path = os.path.abspath(src_path)
        ext = os.path.splitext(path)[1]
        h = file_hash(path)
        if self._hashes.get(path) == h:
            return
        self._hashes[path] = h

        if ext == ".xml":
            new_snapshot = xml_snapshot(path)
            old_snapshot = self._xml.get(
                path, {"records": {}, "templates": frozenset(), "assets": frozenset()}
            )
            self._xml[path] = new_snapshot
            if xml_needs_sync(old_snapshot, new_snapshot):
                module = owning_module(path, self.module_dirs)
                if module:
                    self._queue(module=module)
                else:
                    log(f"Couldn't resolve a module for {os.path.basename(path)} — "
                        f"skipping (is it under --watch-path, inside a module folder?)", Color.YELLOW)

    def _queue(self, module: str | None = None) -> None:
        with self._lock:
            if module:
                self._pending_modules.add(module)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            modules = sorted(self._pending_modules)
            self._pending_modules.clear()
            self._timer = None

        with self._work_lock:
            for module in modules:
                try:
                    self.rpc.upgrade(module)
                except Exception as e:
                    log(f"Failed to upgrade '{module}': {e}", Color.RED)
            # if no_action_only:
            #     log("Template/asset change detected — already served live by Odoo, nothing to do", Color.CYAN)

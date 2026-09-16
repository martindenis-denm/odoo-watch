# odoo-watch

Keeps your Odoo module(s) in sync with a running dev server, without ever
doing a full process restart for something that doesn't need one.

| Change | Action | Why |
|---|---|---|
| `.py` | Nothing — handled by Odoo itself | Odoo's own `--dev=reload` watcher already restarts on `.py` changes (`SIGHUP` → in-place `os.execve`); reimplementing that here would just race it |
| `.xml` — `<record>` added/removed, new `<template>`, or any non-arch field content change | Upgrade *that file's module* over RPC, **no restart** | `ir.module.module.button_immediate_upgrade` reloads the module's data in the already-running process |
| `.xml` — arch/template body only | Nothing | Odoo's `--dev=xml` already re-reads views/qweb/assets straight from disk on every request |
| `.js`, `.css`, `.scss`, `.svg` | Nothing | same as above (`--dev=xml` also bypasses the asset cache) |

The watcher adds `--dev=xml,reload` to your Odoo command automatically if
either is missing — `xml` for the live view/asset cache-bypass above,
`reload` so Odoo's own `.py` watcher is definitely on. It only watches `.xml`
files itself; `.py` is entirely Odoo's job.

Module install/upgrade is split into two deliberately separate moments —
**once at startup**, and **reactively per file change** — and they work
differently on purpose:

- **`--modules foo,bar`** (once, at startup) — plain module names. The
  watcher looks up each one's *current* `ir_module_module` state and bakes
  the matching `-i`/`-u`/`--reinit` into the **one and only** server boot —
  same as typing them into `--cmd` by hand, except the flag is chosen for
  you: `uninstalled`/`to install` → `-i`, `installed` → `--reinit` (≥19) or
  `-u` (<19), `to upgrade` → `-u`. This is the only way to get true
  `--reinit` semantics (RPC has no equivalent), and it's also what installs
  a module for the first time — RPC can't do that either, see below.

  These flags never survive past that first boot, though: Odoo's own restart
  (`SIGHUP` → re-exec) strips `-i`/`-u` from its argv automatically, on every
  restart, whoever triggers it — but **not** `--reinit`. Since `.py`-triggered
  restarts are entirely Odoo's own from here on (see above), this wrapper has
  no later hook to launder that away, so if `--reinit` was used it does so
  immediately: one extra respawn right after the boot completes, before any
  file watching starts (you'll see "Dropping one-off --reinit..."). `-i`/`-u`
  need no such thing — Odoo already strips those itself, every time.

- **`--watch-path`** (reactively, during the session) — when a file changes,
  the watcher resolves it to its module and upgrades *only that module* over
  RPC, no restart. Nothing else. No state is looked up and no other module is
  touched — the module is just assumed to already be installed (that's what
  `--modules` is for). If it isn't, Odoo says so clearly
  ("Cannot upgrade module 'x'. It is not installed.") and the watcher logs
  that and moves on; it won't guess and install it for you.

  There's deliberately no startup sweep over every module `--watch-path`
  finds. `--watch-path` is often a whole repo, and forcing every module in it
  through an upgrade on every launch — whether you touched it or not — is
  both slow and surprising (module upgrades in Odoo cascade to every module
  that depends on them, even a small one, so this used to mean re-upgrading
  things you never asked for just because they happened to live in the same
  repo). Now nothing happens unless a file actually changes.

If `--cmd` still has `-i`/`-u`/`--reinit` typed into it by hand, the watcher
strips them and logs why — use `--modules` instead.

Note: an upgrade's cost scales with how many other installed modules depend
on the one being touched, because Odoo's own module upgrade always cascades
to the full downstream-dependent closure — a leaf module upgrades in a
couple of seconds, but a module with a wide dependent graph can take tens of
seconds even for a one-line change. That's an Odoo cost, not something the
watcher adds or can avoid, and it's paid identically whether triggered by
RPC or by `-u`/`--reinit` on a restart. The watcher logs how long each one
took.

## Usage

```bash
python3 main.py \
  --cmd "python3 ./odoo-bin -d mydb ..." \
  --odoo-path /path/to/odoo/bin \
  --watch-path addons/my_module addons/another_module \
  [--modules foo,bar] \
  [--debounce 1] \
  [--rpc-login admin] \
  [--rpc-password admin]
```

`--cmd` must include `-d`/`--database` — it's used both to launch Odoo and to
authenticate the RPC calls that install/upgrade modules live. RPC goes over
`/web/session/authenticate` + `/web/dataset/call_kw` — the same session-cookie
mechanism the Odoo web client itself uses for every call — not `/xmlrpc/2` or
`/jsonrpc`: both are deprecated since Odoo 19, and their replacement
(`/json/2`) needs a Bearer API key rather than a login/password, which isn't
available on every plan and can't bootstrap itself from nothing anyway.

To resolve flags without starting anything — e.g. for a plain (non-watch)
`odoo-bin` launch — use `resolve_modules.py` directly, standalone:

```bash
python3 resolve_modules.py --db mydb --odoo-path /path/to/odoo/bin --modules foo,bar
# -> -i foo --reinit bar
```

It's dependency-free (stdlib + `psql`), so it doesn't need `watchdog` installed
just to print a flag string — that's also what `main.py` imports it for
internally, rather than duplicating the resolution logic.

## Options

| Option | Default | Description |
|---|---|---|
| `--cmd` | *(required)* | Command to start Odoo (must include `-d`/`--database`) |
| `--odoo-path` | *(required)* | Path to the Odoo checkout (`odoo-bin`'s directory) |
| `--watch-path` | *(required)* | Directories to watch (space-separated); modules are discovered under these |
| `--modules` | *(none)* | Module names (comma or space separated) to install/upgrade/reinit once at startup, flag chosen from DB state |
| `--debounce` | `1` | Seconds to wait before acting on a change |
| `--rpc-login` | `admin` | Login used for module install/upgrade RPC calls |
| `--rpc-password` | `admin` | Password for the same |

`resolve_modules.py` (standalone use) takes `--db`, `--odoo-path`, and `--modules`.

## Files

| File | Role |
|---|---|
| `main.py` | The watcher — spawns Odoo, watches `.xml` files, drives RPC upgrades |
| `resolve_modules.py` | Resolves module names to `-i`/`-u`/`--reinit` from their DB state; standalone or imported by `main.py` |
| `common.py` | Shared `Color`/`log` — nothing else lives here |

## Dependencies

```bash
pip install watchdog
```

Only `main.py` needs this; `resolve_modules.py` and `common.py` are stdlib-only.

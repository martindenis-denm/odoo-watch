import argparse
import os
import re
import subprocess

from common import Color, log

# Resolves module names to the odoo-bin flags matching their *current* state
# in the database — so you never have to hand-pick -i/-u/--reinit yourself:
#
#   uninstalled / to install  →  -i
#   installed                 →  --reinit (>= v19), -u (< v19)
#   to upgrade                →  -u
#
# Standalone and dependency-free (stdlib + psql) so it can run standalone —
# e.g. from odoo-server.sh, before Odoo has even started for this session —
# and so main.py can import it without pulling in watchdog for something
# that doesn't need it.
#
# log() (see common.py) writes to stderr; stdout carries only the resolved
# flag string, so `module_flags=$(python3 resolve_modules.py ...)` is safe.

def parse_module_names(raw: list[str]) -> list[str]:
    names = []
    for token in raw:
        names.extend(p.strip() for p in token.split(",") if p.strip())
    return names

def get_odoo_major_version(odoo_path: str) -> int:
    release_file = os.path.join(odoo_path, "odoo", "release.py")
    try:
        with open(release_file, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'version_info\s*=\s*\(\s*(\d+)', text)
        if m:
            return int(m.group(1))
    except OSError:
        pass
    log(f"Could not determine Odoo's major version from {release_file}; assuming 19", Color.DIM)
    return 19

def resolve_module_flags(db_name: str, module_names: list[str], version: int) -> str:
    if not module_names:
        return ""

    try:
        result = subprocess.run(["psql", "-lqt"], capture_output=True, text=True, check=True)
        db_exists = db_name in (line.split("|")[0].strip() for line in result.stdout.splitlines())
    except subprocess.SubprocessError:
        db_exists = False

    if not db_exists:
        # log(f"Database '{db_name}' not found — all of {','.join(module_names)} will use -i", Color.DIM)
        return f"-i {','.join(module_names)}"

    states: dict[str, str] = {}
    sql_list = "'" + "','".join(module_names) + "'"
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
    except subprocess.SubprocessError as e:
        log(f"Could not query module states via psql ({e}) — defaulting to -i", Color.YELLOW)

    to_install: list[str] = []
    to_reinit: list[str] = []
    to_upgrade: list[str] = []
    for mod in module_names:
        state = states.get(mod, "uninstalled")
        if state in ("uninstalled", "to install"):
            to_install.append(mod)
        elif state == "installed":
            (to_reinit if version >= 19 else to_upgrade).append(mod)
        elif state == "to upgrade":
            to_upgrade.append(mod)
        else:
            log(f"Module '{mod}': unexpected state '{state}' — treating as uninstalled", Color.DIM)
            to_install.append(mod)

    flags = []
    if to_install: flags.append(f"-i {','.join(to_install)}")
    if to_reinit:  flags.append(f"--reinit {','.join(to_reinit)}")
    if to_upgrade: flags.append(f"-u {','.join(to_upgrade)}")
    return " ".join(flags)

def main():
    ap = argparse.ArgumentParser(
        description="Resolve module names to the -i/-u/--reinit flags matching their current DB state."
    )
    ap.add_argument("--db", required=True, metavar="DBNAME")
    ap.add_argument("--odoo-path", required=True, metavar="ODIR")
    ap.add_argument("--modules", nargs="*", default=[], metavar="MODULE")
    args = ap.parse_args()

    odoo_path = os.path.expanduser(args.odoo_path)
    version = get_odoo_major_version(odoo_path)
    print(resolve_module_flags(args.db, parse_module_names(args.modules), version))


if __name__ == "__main__":
    main()

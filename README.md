# odoo-watch

Auto-restarts your Odoo server and hot-reloads your browser on module(s) file changes.

| Change | Action |
|---|---|
| `.py` | Server restart |
| `.xml` (records, assets) | Server restart |
| `.xml` (templates only) | Hot reload |
| `.js`, `.css`, `.scss`, `.svg` | Hot reload |

Hot reload requires the [LiveReload browser extension](https://chromewebstore.google.com/detail/livereload-reborn/kdlcalkcmchabpgfhmjannmkbppggkck).

## Usage

```bash
python odoo-watch.py \
  --launch "python odoo-bin -c odoo.conf" \
  --db-name mydb \
  --watch addons/my_module addons/another_module \
  [--modules my_module,another_module] \
  [--odoo-port 8069] \
  [--reload-port 35729] \
  [--version 19] \
  [--debounce 1]
```

## Options

| Option | Default | Description |
|---|---|---|
| `--launch` | *(required)* | Command to start Odoo |
| `--db-name` | *(required)* | Database name |
| `--watch` | *(required)* | Directories to watch (space-separated) |
| `--modules` | | Modules to install/update on restart |
| `--odoo-port` | `8069` | Odoo HTTP port |
| `--reload-port` | `35729` | LiveReload WebSocket port |
| `--version` | `19` | Odoo version (affects `-i`/`-u`/`--reinit` flags) |
| `--debounce` | `1` | Seconds to wait before acting on a change |

## Dependencies

```bash
pip install watchdog websockets
```

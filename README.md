# odoo-watch

Auto-restarts your Odoo server and hot-reloads your browser on module(s) file changes.
Hot reload requires the [LiveReload browser extension](https://chromewebstore.google.com/detail/livereload-reborn/kdlcalkcmchabpgfhmjannmkbppggkck).

| Change | Action |
|---|---|
| `.py` | Server restart |
| `.xml` (records, assets) | Server restart |
| `.xml` (templates only) | Hot reload |
| `.js`, `.css`, `.scss`, `.svg` | Hot reload |

## Usage

```bash
python3 main.py \
  --cmd "python3 ./odoo-bin ..." \
  --odoo-path /path/to/odoo/bin \
  --watch-path addons/my_module addons/another_module \
  [--reload-port 35729] \
  [--debounce 1]
```

## Options

| Option | Default | Description |
|---|---|---|
| `--cmd` | *(required)* | Command to start Odoo |
| `--odoo-path` | *(required)* | Database name |
| `--watch-path` | *(required)* | Directories to watch (space-separated) |
| `--reload-port` | `35729` | LiveReload WebSocket port |
| `--debounce` | `1` | Seconds to wait before acting on a change |

## Dependencies

```bash
pip install watchdog websockets
```

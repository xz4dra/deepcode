# DeepCode API

DeepCode starts a small JSON API on `127.0.0.1` at launch. It can inspect and
modify the current workspace, open files in the editor, save documents, and run
editor commands.

The API has no third-party dependencies and is disabled with `--no-api`.

## Connection

At startup DeepCode writes connection details to:

```text
~/.config/deepcode/api.json
```

Example:

```json
{
  "ok": true,
  "service": "deepcode",
  "version": "1.0",
  "host": "127.0.0.1",
  "port": 8765,
  "url": "http://127.0.0.1:8765",
  "workspace": "/home/user/project",
  "token": "…",
  "config": "/home/user/.config/deepcode/api.json",
  "running": true,
  "pid": 12345
}
```

Every endpoint except `GET /health` requires the token:

```http
Authorization: Bearer <token>
```

You can also use:

```http
X-DeepCode-Token: <token>
```

Inside the editor, open the command palette and run:

- `API: Connection Info`
- `API: Copy Token`

## Launch options

```bash
deepcode                     # current directory, API on port 8765
deepcode /path/to/project    # open a project
deepcode /path/to/file.py    # open one file
deepcode --api-port 9000     # choose the API port
deepcode --api-port 0        # choose a random free port
deepcode --no-api            # disable the API
```

Environment variables:

```bash
DEEPCODE_API=0 deepcode                 # disable the API
DEEPCODE_API_PORT=9000 deepcode         # choose the port
DEEPCODE_API_TOKEN=secret deepcode      # use a fixed token
DEEPCODE_CONFIG_DIR=/tmp/dc deepcode    # alternate config directory
```

If the requested port is busy, DeepCode falls back to a random free port and
reports the actual port in `api.json`.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check, no authentication |
| `GET` | `/` | API index and endpoint list |
| `GET` | `/config` | Workspace, port, config path |
| `GET` | `/files?path=.` | List a directory |
| `GET` | `/file?path=main.py` | Read a file |
| `PUT` | `/file` | Write a file |
| `POST` | `/file` | Create a file |
| `POST` | `/folder` | Create a folder |
| `POST` | `/rename` | Rename or move an entry |
| `POST` | `/move` | Alias for `/rename` |
| `POST` | `/delete` | Delete a file or folder |
| `POST` | `/duplicate` | Duplicate a file or folder |
| `GET` | `/search?q=text` | Search file paths |
| `GET` | `/state` | Open tabs and editor state |
| `POST` | `/open` | Open a file in the editor |
| `POST` | `/save` | Save the active file or all files |
| `POST` | `/command` | Run an editor command |

## Examples

### Health

```bash
curl http://127.0.0.1:8765/health
```

### List files

```bash
TOKEN=$(python3 -c 'import json,pathlib;print(json.loads((pathlib.Path.home()/".config/deepcode/api.json").read_text())["token"])')

curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/files?path=."
```

### Create a folder

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"src/components"}' \
  http://127.0.0.1:8765/folder
```

### Create a file

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"src/components/button.py","content":"class Button:\n    pass\n"}' \
  http://127.0.0.1:8765/file
```

### Read a file

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/file?path=main.py"
```

### Write a file

```bash
curl -X PUT \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"notes.txt","content":"hello\n"}' \
  http://127.0.0.1:8765/file
```

### Rename or move

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"old.py","new_path":"src/new.py"}' \
  http://127.0.0.1:8765/rename
```

### Delete

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"tmp","recursive":true}' \
  http://127.0.0.1:8765/delete
```

### Duplicate

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"main.py"}' \
  http://127.0.0.1:8765/duplicate
```

### Search paths

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/search?q=button"
```

### Open a file in the editor

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path":"main.py"}' \
  http://127.0.0.1:8765/open
```

### Save

```bash
# Active file
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{}' \
  http://127.0.0.1:8765/save

# All files
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"all":true}' \
  http://127.0.0.1:8765/save
```

### Run an editor command

Commands are the same IDs used by the command palette, for example:
`save`, `save_all`, `close_tab`, `close_all`, `new_file`, `new_folder`,
`toggle_sidebar`, `toggle_word_wrap`, `find`, `replace`, `goto_line`,
`duplicate_line`, `toggle_comment`, `undo`, `redo`.

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"save_all"}' \
  http://127.0.0.1:8765/command
```

`quit` and `command_palette` are intentionally not callable through the API.

## Errors

Errors are JSON objects with an HTTP status:

```json
{"ok": false, "error": "Path escapes the workspace"}
```

Common statuses:

- `400` — invalid request or path
- `401` — missing/invalid token
- `403` — path outside the workspace
- `404` — file/endpoint not found
- `409` — target exists or directory is not empty
- `413` — request or file too large
- `503` — editor bridge unavailable

## Security

- The server binds to `127.0.0.1` only.
- All data endpoints require a random bearer token.
- The token file is created with permissions `0600`.
- File operations are restricted to the workspace directory; paths escaping
  the workspace are rejected.
- Do not expose the port through a proxy or bind to `0.0.0.0`.

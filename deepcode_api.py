"""Local HTTP API and filesystem service for DeepCode.

The API is intentionally dependency-free: it uses only the Python standard
library.  It binds to 127.0.0.1 by default and requires a bearer token.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

API_VERSION = "1.0"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_READ_BYTES = 16 * 1024 * 1024

_SEARCH_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
}


class APIError(Exception):
    """An error that should be reported to an API client."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class NotFoundError(APIError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 404)


class ConflictError(APIError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 409)


@dataclass(frozen=True)
class FileInfo:
    path: str
    name: str
    is_dir: bool
    size: int
    modified: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "is_dir": self.is_dir,
            "size": self.size,
            "modified": self.modified,
        }


class WorkspaceAPI:
    """Safe file operations rooted at one workspace directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()

    # -- path helpers ---------------------------------------------------

    def resolve(self, value: str | Path, *, root_ok: bool = True) -> Path:
        raw = str(value).strip()
        if raw in {"", "."}:
            if root_ok:
                return self.root
            raise APIError("The workspace root cannot be used here")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve(strict=False)
        if path != self.root and self.root not in path.parents:
            raise APIError("Path escapes the workspace", 403)
        return path

    def relative(self, path: Path) -> str:
        try:
            relative = path.relative_to(self.root)
        except ValueError as error:
            raise APIError("Path escapes the workspace", 403) from error
        value = relative.as_posix()
        return value or "."

    def _relative_info(self, path: Path) -> FileInfo:
        try:
            stat = path.stat()
        except OSError as error:
            raise NotFoundError(f"Cannot stat {path.name}: {error}") from error
        return FileInfo(
            path=self.relative(path),
            name=path.name,
            is_dir=path.is_dir(),
            size=0 if path.is_dir() else stat.st_size,
            modified=stat.st_mtime,
        )

    def _unique_path(self, source: Path) -> Path:
        if source.is_dir():
            base = source.name
            parent = source.parent
            candidate = parent / f"{base} copy"
            counter = 2
            while candidate.exists():
                candidate = parent / f"{base} copy {counter}"
                counter += 1
            return candidate

        suffix = source.suffix
        stem = source.name[: -len(suffix)] if suffix else source.name
        parent = source.parent
        candidate = parent / f"{stem} copy{suffix}"
        counter = 2
        while candidate.exists():
            candidate = parent / f"{stem} copy {counter}{suffix}"
            counter += 1
        return candidate

    # -- read -----------------------------------------------------------

    def list(self, path: str | Path = ".") -> list[dict[str, Any]]:
        target = self.resolve(path)
        if not target.exists():
            raise NotFoundError(f"Directory not found: {path}")
        if not target.is_dir():
            raise APIError(f"Not a directory: {path}")
        try:
            children = sorted(
                target.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as error:
            raise APIError(f"Cannot list directory: {error}") from error
        return [self._relative_info(child).to_dict() for child in children]

    def read(self, path: str | Path) -> dict[str, Any]:
        target = self.resolve(path)
        if not target.exists():
            raise NotFoundError(f"File not found: {path}")
        if target.is_dir():
            raise APIError(f"Not a file: {path}")
        try:
            if target.stat().st_size > MAX_READ_BYTES:
                raise APIError("File is too large for the API", 413)
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise APIError(f"Cannot read file: {error}") from error
        info = self._relative_info(target).to_dict()
        info["content"] = content
        return info

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        query = query.strip().lower()
        if not query:
            return []
        limit = max(1, min(limit, 2000))
        results: list[dict[str, Any]] = []

        for directory, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in _SEARCH_SKIP_DIRS and not name.startswith(".")
            ]
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    relative = self.relative(path)
                except APIError:
                    continue
                if query not in relative.lower():
                    continue
                results.append(self._relative_info(path).to_dict())
                if len(results) >= limit:
                    return results
        return results

    # -- write ----------------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
            os.replace(temporary, target)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def write(self, path: str | Path, content: str) -> dict[str, Any]:
        target = self.resolve(path)
        if target.is_dir():
            raise APIError(f"Path is a directory: {path}")
        try:
            self._atomic_write(target, content)
        except OSError as error:
            raise APIError(f"Cannot write file: {error}") from error
        return self._relative_info(target).to_dict()

    def create_file(
        self,
        path: str | Path,
        content: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        target = self.resolve(path)
        if target.exists() and not overwrite:
            raise ConflictError(f"File already exists: {path}")
        if target.is_dir():
            raise APIError(f"Path is a directory: {path}")
        try:
            self._atomic_write(target, content)
        except OSError as error:
            raise APIError(f"Cannot create file: {error}") from error
        return self._relative_info(target).to_dict()

    def create_folder(self, path: str | Path) -> dict[str, Any]:
        target = self.resolve(path, root_ok=False)
        if target.exists():
            raise ConflictError(f"Folder already exists: {path}")
        try:
            target.mkdir(parents=True)
        except OSError as error:
            raise APIError(f"Cannot create folder: {error}") from error
        return self._relative_info(target).to_dict()

    def rename(self, path: str | Path, new_path: str | Path) -> dict[str, Any]:
        source = self.resolve(path, root_ok=False)
        target = self.resolve(new_path, root_ok=False)
        if not source.exists():
            raise NotFoundError(f"Path not found: {path}")
        if target.exists():
            raise ConflictError(f"Target already exists: {new_path}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
        except OSError as error:
            raise APIError(f"Cannot rename: {error}") from error
        return self._relative_info(target).to_dict()

    def move(self, path: str | Path, new_path: str | Path) -> dict[str, Any]:
        return self.rename(path, new_path)

    def delete(self, path: str | Path, recursive: bool = False) -> dict[str, Any]:
        target = self.resolve(path, root_ok=False)
        if not target.exists():
            raise NotFoundError(f"Path not found: {path}")
        if target.is_dir():
            has_children = any(target.iterdir())
            if has_children and not recursive:
                raise ConflictError(
                    "Directory is not empty (pass recursive=true to delete)"
                )
            try:
                shutil.rmtree(target)
            except OSError as error:
                raise APIError(f"Cannot delete folder: {error}") from error
        else:
            try:
                target.unlink()
            except OSError as error:
                raise APIError(f"Cannot delete file: {error}") from error
        return {"path": str(path), "deleted": True}

    def duplicate(self, path: str | Path) -> dict[str, Any]:
        source = self.resolve(path, root_ok=False)
        if not source.exists():
            raise NotFoundError(f"Path not found: {path}")
        target = self._unique_path(source)
        try:
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        except OSError as error:
            raise APIError(f"Cannot duplicate: {error}") from error
        return self._relative_info(target).to_dict()


# ---------------------------------------------------------------------------
# Editor bridge
# ---------------------------------------------------------------------------


class EditorBridge(Protocol):
    def snapshot(self) -> dict[str, Any]: ...

    def open(self, path: str) -> dict[str, Any]: ...

    def save(self, all_tabs: bool = False) -> dict[str, Any]: ...

    def command(self, command_id: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class LocalAPIServer:
    """Small authenticated HTTP server for the current workspace."""

    def __init__(
        self,
        workspace: Path | str,
        *,
        bridge: EditorBridge | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: str | None = None,
        config_dir: Path | str | None = None,
    ) -> None:
        self.api = WorkspaceAPI(workspace)
        self.bridge = bridge
        self.host = host
        self.requested_port = int(port)
        self.token = (
            token
            or os.environ.get("DEEPCODE_API_TOKEN")
            or secrets.token_urlsafe(32)
        )
        self.config_dir = Path(
            config_dir
            or os.environ.get("DEEPCODE_CONFIG_DIR")
            or (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "deepcode")
        ).expanduser()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.info: dict[str, Any] = {}

    # -- lifecycle ------------------------------------------------------

    def start(self) -> dict[str, Any]:
        handler = self._make_handler()
        try:
            self.httpd = ThreadingHTTPServer(
                (self.host, self.requested_port), handler
            )
        except OSError:
            if self.requested_port == 0:
                raise
            # The requested port is busy: fall back to an ephemeral one.
            self.httpd = ThreadingHTTPServer((self.host, 0), handler)
        self.httpd.daemon_threads = True
        actual_port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="deepcode-api",
            daemon=True,
        )
        self.thread.start()
        self.info = {
            "ok": True,
            "service": "deepcode",
            "version": API_VERSION,
            "host": self.host,
            "port": actual_port,
            "url": f"http://{self.host}:{actual_port}",
            "workspace": str(self.api.root),
            "token": self.token,
            "config": str(self._config_path()),
        }
        self._write_config(running=True)
        return self.info

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None
        if self.info:
            self._write_config(running=False)

    def _config_path(self) -> Path:
        return self.config_dir / "api.json"

    def _write_config(self, *, running: bool) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            payload = dict(self.info)
            payload["running"] = running
            payload["pid"] = os.getpid()
            path = self._config_path()
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.chmod(path, 0o600)
        except OSError:
            # A read-only home directory must not prevent the editor from
            # starting; the token is also available in the palette.
            pass

    # -- HTTP handler ---------------------------------------------------

    def _make_handler(self):
        api_server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DeepCodeAPI/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if header.lower().startswith("bearer "):
                    supplied = header[7:].strip()
                else:
                    supplied = self.headers.get("X-DeepCode-Token", "")
                return hmac.compare_digest(supplied, api_server.token)

            def _body(self) -> dict[str, Any]:
                length_text = self.headers.get("Content-Length", "0")
                try:
                    length = int(length_text)
                except ValueError as error:
                    raise APIError("Invalid Content-Length") from error
                if length > MAX_BODY_BYTES:
                    raise APIError("Request body is too large", 413)
                raw = self.rfile.read(length) if length else b""
                if not raw:
                    return {}
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise APIError("Invalid JSON body") from error
                if not isinstance(data, dict):
                    raise APIError("JSON body must be an object")
                return data

            def _query(self, name: str, default: str = "") -> str:
                values = parse_qs(urlparse(self.path).query).get(name)
                return values[0] if values else default

            def _route(self, method: str) -> None:
                parsed = urlparse(self.path)
                route = parsed.path.rstrip("/") or "/"
                if route == "/health":
                    self._json(
                        200,
                        {
                            "ok": True,
                            "service": "deepcode",
                            "version": API_VERSION,
                            "port": int(api_server.httpd.server_address[1]),
                        },
                    )
                    return

                if not self._authorized():
                    self._json(401, {"ok": False, "error": "Unauthorized"})
                    return

                data: dict[str, Any] = {}
                if method in {"POST", "PUT", "DELETE"}:
                    data = self._body()

                api = api_server.api
                bridge = api_server.bridge

                if method == "GET" and route == "/":
                    self._json(
                        200,
                        {
                            "ok": True,
                            "service": "deepcode",
                            "version": API_VERSION,
                            "workspace": str(api.root),
                            "endpoints": [
                                "GET /health",
                                "GET /files?path=.",
                                "GET /file?path=FILE",
                                "GET /search?q=TEXT",
                                "GET /state",
                                "GET /config",
                                "PUT /file",
                                "POST /file",
                                "POST /folder",
                                "POST /rename",
                                "POST /move",
                                "POST /delete",
                                "POST /duplicate",
                                "POST /open",
                                "POST /save",
                                "POST /command",
                            ],
                        },
                    )
                    return

                if method == "GET" and route == "/config":
                    self._json(
                        200,
                        {
                            "ok": True,
                            "workspace": str(api.root),
                            "port": int(api_server.httpd.server_address[1]),
                            "config": str(api_server.config_dir / "api.json"),
                        },
                    )
                    return

                if method == "GET" and route == "/files":
                    entries = api.list(self._query("path", "."))
                    self._json(200, {"ok": True, "entries": entries})
                    return

                if method == "GET" and route == "/file":
                    file_info = api.read(self._query("path"))
                    self._json(200, {"ok": True, "file": file_info})
                    return

                if method == "GET" and route == "/search":
                    results = api.search(self._query("q"))
                    self._json(200, {"ok": True, "results": results})
                    return

                if method == "GET" and route == "/state":
                    if bridge is None:
                        raise APIError("Editor bridge is unavailable", 503)
                    self._json(200, {"ok": True, "state": bridge.snapshot()})
                    return

                if method == "PUT" and route == "/file":
                    path = data.get("path")
                    content = data.get("content", "")
                    if not isinstance(path, str) or not isinstance(content, str):
                        raise APIError("'path' and 'content' strings are required")
                    file_info = api.write(path, content)
                    self._json(200, {"ok": True, "file": file_info})
                    return

                if method == "POST" and route == "/file":
                    path = data.get("path")
                    content = data.get("content", "")
                    overwrite = bool(data.get("overwrite", False))
                    if not isinstance(path, str) or not isinstance(content, str):
                        raise APIError("'path' and 'content' strings are required")
                    file_info = api.create_file(path, content, overwrite)
                    self._json(201, {"ok": True, "file": file_info})
                    return

                if method == "POST" and route == "/folder":
                    path = data.get("path")
                    if not isinstance(path, str):
                        raise APIError("'path' string is required")
                    folder = api.create_folder(path)
                    self._json(201, {"ok": True, "folder": folder})
                    return

                if method == "POST" and route == "/rename":
                    path = data.get("path")
                    new_path = data.get("new_path")
                    if not isinstance(path, str) or not isinstance(new_path, str):
                        raise APIError("'path' and 'new_path' strings are required")
                    result = api.rename(path, new_path)
                    self._json(200, {"ok": True, "entry": result})
                    return

                if method == "POST" and route == "/move":
                    path = data.get("path")
                    new_path = data.get("new_path")
                    if not isinstance(path, str) or not isinstance(new_path, str):
                        raise APIError("'path' and 'new_path' strings are required")
                    result = api.move(path, new_path)
                    self._json(200, {"ok": True, "entry": result})
                    return

                if method == "POST" and route == "/delete":
                    path = data.get("path")
                    if not isinstance(path, str):
                        raise APIError("'path' string is required")
                    result = api.delete(path, bool(data.get("recursive", False)))
                    self._json(200, {"ok": True, "result": result})
                    return

                if method == "POST" and route == "/duplicate":
                    path = data.get("path")
                    if not isinstance(path, str):
                        raise APIError("'path' string is required")
                    result = api.duplicate(path)
                    self._json(201, {"ok": True, "entry": result})
                    return

                if method == "POST" and route == "/open":
                    path = data.get("path")
                    if not isinstance(path, str):
                        raise APIError("'path' string is required")
                    if bridge is None:
                        raise APIError("Editor bridge is unavailable", 503)
                    resolved = api.resolve(path)
                    if not resolved.is_file():
                        raise NotFoundError(f"File not found: {path}")
                    self._json(200, {"ok": True, "result": bridge.open(str(resolved))})
                    return

                if method == "POST" and route == "/save":
                    if bridge is None:
                        raise APIError("Editor bridge is unavailable", 503)
                    self._json(
                        200,
                        {
                            "ok": True,
                            "result": bridge.save(bool(data.get("all", False))),
                        },
                    )
                    return

                if method == "POST" and route == "/command":
                    command = data.get("command")
                    if not isinstance(command, str):
                        raise APIError("'command' string is required")
                    if bridge is None:
                        raise APIError("Editor bridge is unavailable", 503)
                    self._json(200, {"ok": True, "result": bridge.command(command)})
                    return

                raise NotFoundError(f"Unknown endpoint: {method} {route}")

            def _handle(self, method: str) -> None:
                try:
                    self._route(method)
                except APIError as error:
                    self._json(
                        error.status, {"ok": False, "error": str(error)}
                    )
                except Exception as error:  # pragma: no cover - defensive
                    self._json(500, {"ok": False, "error": str(error)})

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PUT(self) -> None:
                self._handle("PUT")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        return Handler

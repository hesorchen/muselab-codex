"""Async process and JSONL protocol client for ``codex app-server``."""

import asyncio
import fcntl
import inspect
import itertools
import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class ServerRequest:
    """One app-server initiated request, including its correlation id."""

    id: object
    method: str
    params: JsonObject


ServerRequestHandler = Callable[
    [ServerRequest],
    JsonObject | Awaitable[JsonObject],
]

_QUEUE_CLOSED = object()
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
}
_EMPTY_INPUT_METHODS = {"item/tool/requestUserInput"}


class AppServerError(RuntimeError):
    """Base class for bounded, user-content-free app-server diagnostics."""


class AppServerProtocolError(AppServerError):
    """The subprocess emitted a message that is not valid app-server JSONL."""


class AppServerProcessError(AppServerError):
    """The app-server process is unavailable or exited unexpectedly."""


class AppServerTimeoutError(AppServerError):
    """An app-server request exceeded the client-side response deadline."""


class AppServerResponseError(AppServerError):
    """An app-server request returned a JSON-RPC error response."""

    def __init__(self, method: str, code: object):
        self.method = method
        self.code = code
        super().__init__(f"{method} failed with app-server error {code!r}")


class CodexAppServer:
    """Own one app-server process and multiplex its bidirectional messages.

    The default command targets the stable stdio transport. Tests can inject a
    fake command without patching asyncio's subprocess implementation.
    """

    def __init__(
        self,
        *,
        command: Sequence[str] = ("codex", "app-server", "--stdio"),
        request_handler: ServerRequestHandler | None = None,
        environment: Mapping[str, str] | None = None,
        initialize_capabilities: Mapping[str, Any] | None = None,
        strip_openai_api_key: bool = True,
        initialize_timeout: float = 15.0,
        request_timeout: float = 60.0,
        client_version: str = "0.1.0",
    ):
        if not command:
            raise ValueError("app-server command cannot be empty")
        self.command = tuple(command)
        self.request_handler = request_handler
        self.environment = dict(environment or {})
        self.initialize_capabilities = dict(initialize_capabilities or {})
        self.strip_openai_api_key = strip_openai_api_key
        self.initialize_timeout = initialize_timeout
        self.request_timeout = request_timeout
        self.client_version = client_version

        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._server_request_tasks: set[asyncio.Task] = set()
        self._pending: dict[int, tuple[asyncio.Future, str]] = {}
        self._notifications: asyncio.Queue = asyncio.Queue()
        self._notifications_closed = False
        self._request_ids = itertools.count(1)
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._terminal_error: AppServerError | None = None
        self._stderr_line_count = 0
        self.initialize_result: JsonObject | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def stderr_line_count(self) -> int:
        """Number of stderr lines observed, without retaining their content."""
        return self._stderr_line_count

    async def __aenter__(self) -> "CodexAppServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> JsonObject:
        if self.running:
            raise AppServerProcessError("app-server is already running")
        if self._process is not None:
            raise AppServerProcessError("closed app-server clients cannot be restarted")

        child_env = os.environ.copy()
        child_env.update(self.environment)
        if self.strip_openai_api_key:
            child_env.pop("OPENAI_API_KEY", None)

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise AppServerProcessError("unable to start app-server") from exc

        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="codex-app-server-stdout")
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name="codex-app-server-stderr")
        try:
            initialize_params: JsonObject = {
                "clientInfo": {
                    "name": "muselab_codex",
                    "title": "muselab-codex",
                    "version": self.client_version,
                },
            }
            if self.initialize_capabilities:
                initialize_params["capabilities"] = self.initialize_capabilities
            result = await self.request(
                "initialize",
                initialize_params,
                timeout=self.initialize_timeout,
            )
            if not isinstance(result, dict):
                raise AppServerProtocolError("initialize returned a non-object result")
            self.initialize_result = result
            await self.notify("initialized")
            return result
        except BaseException:
            await self.close()
            raise

    async def request(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        self._ensure_running()
        request_id = next(self._request_ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (future, method)
        try:
            await self._send({
                "method": method,
                "id": request_id,
                "params": params or {},
            })
        except BaseException:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

        wait_timeout = self.request_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(asyncio.shield(future), wait_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            future.cancel()
            raise AppServerTimeoutError(f"{method} timed out") from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        self._ensure_running()
        message: JsonObject = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def next_notification(self, *, timeout: float | None = None) -> JsonObject:
        try:
            if timeout is None:
                item = await self._notifications.get()
            else:
                item = await asyncio.wait_for(self._notifications.get(), timeout)
        except TimeoutError as exc:
            raise AppServerError("timed out waiting for app-server notification") from exc
        if item is _QUEUE_CLOSED:
            self._notifications.put_nowait(_QUEUE_CLOSED)
            raise self._terminal_error or AppServerProcessError("app-server notification stream closed")
        return item

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        process = self._process

        for task in tuple(self._server_request_tasks):
            task.cancel()
        if self._server_request_tasks:
            await asyncio.gather(*self._server_request_tasks, return_exceptions=True)

        if process is not None and process.stdin is not None:
            process.stdin.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await process.stdin.wait_closed()

        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._reader_task, self._stderr_task) if task is not None),
            return_exceptions=True,
        )

        self._fail_pending(AppServerProcessError("app-server client closed"))
        self._close_notifications()

    def _ensure_running(self) -> None:
        if not self.running or self._closing:
            raise self._terminal_error or AppServerProcessError("app-server is not running")

    async def _send(self, message: JsonObject) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerProcessError("app-server stdin is unavailable")
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AppServerProcessError("app-server stdin closed") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise AppServerProtocolError("app-server emitted malformed JSONL") from exc
                if not isinstance(message, dict):
                    raise AppServerProtocolError("app-server emitted a non-object message")
                self._dispatch(message)

            if not self._closing:
                returncode = await process.wait()
                raise AppServerProcessError(
                    f"app-server exited unexpectedly with status {returncode}")
        except asyncio.CancelledError:
            raise
        except AppServerError as exc:
            if not self._closing:
                self._terminal_error = exc
                self._fail_pending(exc)
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.terminate()
        finally:
            self._close_notifications()

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        try:
            while await process.stderr.readline():
                self._stderr_line_count += 1
        except asyncio.CancelledError:
            raise

    def _dispatch(self, message: JsonObject) -> None:
        if isinstance(message.get("method"), str):
            if "id" in message:
                task = asyncio.create_task(self._handle_server_request(message))
                self._server_request_tasks.add(task)
                task.add_done_callback(self._server_request_tasks.discard)
            else:
                self._notifications.put_nowait(message)
            return

        if "id" in message and ("result" in message or "error" in message):
            pending = self._pending.pop(message["id"], None)
            if pending is None:
                return
            future, method = pending
            if future.done():
                return
            if "error" in message:
                error = message.get("error")
                code = error.get("code") if isinstance(error, dict) else "unknown"
                future.set_exception(AppServerResponseError(method, code))
            else:
                future.set_result(message.get("result"))
            return

        raise AppServerProtocolError("app-server emitted an unrecognized message")

    async def _handle_server_request(self, message: JsonObject) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            await self._send_error(request_id, -32600, "Invalid app-server request")
            return

        try:
            if self.request_handler is None:
                if method in _APPROVAL_METHODS:
                    result: JsonObject = {"decision": "decline"}
                elif method == "item/permissions/requestApproval":
                    result = {"permissions": {}, "scope": "turn"}
                elif method == "mcpServer/elicitation/request":
                    result = {"action": "cancel"}
                elif method in _EMPTY_INPUT_METHODS:
                    result = {"answers": {}}
                else:
                    await self._send_error(request_id, -32601, "Client method not supported")
                    return
            else:
                result = self.request_handler(ServerRequest(
                    id=request_id,
                    method=method,
                    params=params,
                ))
                if inspect.isawaitable(result):
                    result = await result
                if not isinstance(result, dict):
                    raise TypeError("server request handler must return an object")
            await self._send({"id": request_id, "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(AppServerError):
                await self._send_error(request_id, -32000, "Client request handler failed")

    async def _send_error(self, request_id: object, code: int, message: str) -> None:
        await self._send({
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    def _fail_pending(self, error: AppServerError) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future, _method in pending:
            if not future.done():
                future.set_exception(error)

    def _close_notifications(self) -> None:
        if not self._notifications_closed:
            self._notifications_closed = True
            self._notifications.put_nowait(_QUEUE_CLOSED)


class CodexSharedAppServer(CodexAppServer):
    """Connect over a Unix WebSocket shared with ``codex --remote``.

    The listener is supervised by muselab. WebSocket compression is disabled:
    this matches Codex's local control-socket clients and avoids negotiating an
    extension that the Unix listener currently rejects during the handshake.
    """

    def __init__(
        self,
        *,
        codex_bin: str,
        socket_path: Path,
        listener_timeout: float = 10.0,
        **kwargs: Any,
    ):
        self.codex_bin = codex_bin
        self.socket_path = Path(socket_path).expanduser().resolve()
        self.listener_timeout = listener_timeout
        self._listener: asyncio.subprocess.Process | None = None
        self._listener_lock_fd: int | None = None
        self._listener_socket_identity: tuple[int, int] | None = None
        self._websocket = None
        super().__init__(
            command=(codex_bin, "app-server", "--listen", f"unix://{self.socket_path}"),
            **kwargs,
        )

    @property
    def running(self) -> bool:
        return (
            self._websocket is not None
            and self._reader_task is not None
            and not self._reader_task.done()
            and not self._closing
        )

    @property
    def remote_url(self) -> str:
        return f"unix://{self.socket_path}"

    async def start(self) -> JsonObject:
        if self.running:
            raise AppServerProcessError("app-server is already running")
        await self._start_listener()
        try:
            self._websocket = await websockets.unix_connect(
                str(self.socket_path),
                uri="ws://localhost",
                compression=None,
                # This is a local Unix control connection. The websockets
                # default sends a ping every 20s and closes after one missed
                # pong; app-server can delay control frames while a turn is
                # busy, turning an otherwise healthy local listener into a
                # spurious "websocket closed unexpectedly" failure. Kernel
                # socket closure already gives us reliable liveness here.
                ping_interval=None,
                open_timeout=self.initialize_timeout,
            )
            self._reader_task = asyncio.create_task(
                self._read_websocket(), name="codex-app-server-websocket")
            initialize_params: JsonObject = {
                "clientInfo": {
                    "name": "muselab_codex",
                    "title": "muselab-codex",
                    "version": self.client_version,
                },
            }
            if self.initialize_capabilities:
                initialize_params["capabilities"] = self.initialize_capabilities
            result = await self.request(
                "initialize", initialize_params, timeout=self.initialize_timeout)
            if not isinstance(result, dict):
                raise AppServerProtocolError("initialize returned a non-object result")
            self.initialize_result = result
            await self.notify("initialized")
            return result
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            for task in tuple(self._server_request_tasks):
                task.cancel()
            if self._server_request_tasks:
                await asyncio.gather(
                    *self._server_request_tasks, return_exceptions=True)
            websocket = self._websocket
            self._websocket = None
            if websocket is not None:
                await websocket.close()
            if self._reader_task is not None and not self._reader_task.done():
                self._reader_task.cancel()
            if self._reader_task is not None:
                await asyncio.gather(self._reader_task, return_exceptions=True)
            self._fail_pending(AppServerProcessError("app-server client closed"))
            self._close_notifications()
        finally:
            await self._close_listener()

    async def _send(self, message: JsonObject) -> None:
        websocket = self._websocket
        if websocket is None or self._closing:
            raise AppServerProcessError("app-server websocket is unavailable")
        try:
            async with self._write_lock:
                await websocket.send(json.dumps(message, separators=(",", ":")))
        except ConnectionClosed as exc:
            raise AppServerProcessError("app-server websocket closed") from exc

    async def _read_websocket(self) -> None:
        websocket = self._websocket
        assert websocket is not None
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
                    raise AppServerProtocolError(
                        "app-server emitted malformed websocket JSON") from exc
                if not isinstance(message, dict):
                    raise AppServerProtocolError("app-server emitted a non-object message")
                self._dispatch(message)
            if not self._closing:
                raise AppServerProcessError("app-server websocket closed unexpectedly")
        except asyncio.CancelledError:
            raise
        except (AppServerError, ConnectionClosed) as exc:
            if not self._closing:
                error = exc if isinstance(exc, AppServerError) else AppServerProcessError(
                    "app-server websocket closed unexpectedly")
                self._terminal_error = error
                self._fail_pending(error)
        finally:
            self._close_notifications()

    async def _start_listener(self) -> None:
        if self._listener is not None and self._listener.returncode is None:
            return
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.parent.chmod(0o700)
        self._acquire_listener_lock()
        self.socket_path.unlink(missing_ok=True)
        try:
            self._listener = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "app-server",
                "--listen",
                self.remote_url,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError) as exc:
            self._release_listener_lock()
            raise AppServerProcessError("unable to start shared app-server listener") from exc

        deadline = asyncio.get_running_loop().time() + self.listener_timeout
        while not self.socket_path.exists():
            if self._listener.returncode is not None:
                await self._close_listener()
                raise AppServerProcessError("shared app-server listener exited during startup")
            if asyncio.get_running_loop().time() >= deadline:
                await self._close_listener()
                raise AppServerTimeoutError("shared app-server listener timed out")
            await asyncio.sleep(0.05)
        try:
            stat = self.socket_path.stat()
        except OSError as exc:
            await self._close_listener()
            raise AppServerProcessError(
                "shared app-server socket disappeared during startup") from exc
        self._listener_socket_identity = (stat.st_dev, stat.st_ino)

    async def _close_listener(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None and listener.returncode is None:
            listener.terminate()
            try:
                await asyncio.wait_for(listener.wait(), timeout=2.0)
            except TimeoutError:
                listener.kill()
                await listener.wait()
        try:
            if self._listener_socket_identity is not None:
                try:
                    stat = self.socket_path.stat()
                except FileNotFoundError:
                    pass
                else:
                    if (stat.st_dev, stat.st_ino) == self._listener_socket_identity:
                        self.socket_path.unlink(missing_ok=True)
        finally:
            self._listener_socket_identity = None
            self._release_listener_lock()

    def _acquire_listener_lock(self) -> None:
        if self._listener_lock_fd is not None:
            return
        lock_path = self.socket_path.with_suffix(self.socket_path.suffix + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise AppServerProcessError(
                "another muselab-codex instance owns this app-server socket"
            ) from exc
        self._listener_lock_fd = fd

    def _release_listener_lock(self) -> None:
        fd = self._listener_lock_fd
        self._listener_lock_fd = None
        if fd is None:
            return
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

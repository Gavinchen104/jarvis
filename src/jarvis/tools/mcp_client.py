"""Synchronous wrapper around an MCP server reached over stdio.

The MCP Python SDK is asyncio-based, but JARVIS is a single synchronous
long-running process (DESIGN.md §7.1). Rather than make the whole codebase
async, this client runs the MCP session inside a dedicated background thread
with its own event loop. The session stays alive there for the process
lifetime; synchronous callers dispatch coroutines onto that loop and block
for the result.

Task 3 scope: subprocess spawn + session lifecycle (start/stop) only.
list_tools() and call_tool() land in tasks 4 and 5.
"""

import asyncio
import shlex
import threading

from jarvis.config import settings


class MCPClient:
    """Owns one MCP server subprocess and its client session.

    Usage:
        with MCPClient() as client:
            ...        # tasks 4-5 add list_tools() / call_tool()
    """

    def __init__(self, command: str | None = None) -> None:
        self._cmd = shlex.split(command or settings.search_mcp_command)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None  # mcp.ClientSession, set once initialized
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._start_error: BaseException | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 30.0) -> None:
        """Spawn the server, initialize the session. Blocks until ready."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="mcp-client", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise RuntimeError(
                f"MCP server {self._cmd!r} did not become ready within {timeout}s"
            )
        if self._start_error is not None:
            raise RuntimeError(
                f"MCP server {self._cmd!r} failed to start: {self._start_error}"
            ) from self._start_error

    def stop(self) -> None:
        """Signal shutdown and join the background thread."""
        if self._loop is not None and self._shutdown is not None:
            self._loop.call_soon_threadsafe(self._shutdown.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> "MCPClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # --- background thread / event loop ------------------------------------

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        """Open the stdio transport + session, hold it open until shutdown."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._shutdown = asyncio.Event()
        params = StdioServerParameters(command=self._cmd[0], args=self._cmd[1:])
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._shutdown.wait()
        except BaseException as exc:  # noqa: BLE001 - report back to start()
            self._start_error = exc
            self._ready.set()  # unblock start() so it can raise

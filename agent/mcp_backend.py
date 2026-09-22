"""The graph tools, reached over MCP instead of over pyTigerGraph.

TigerGraph MCP (github.com/tigergraph/tigergraph-mcp) exposes the database as tools.
The agent's ten investigation queries are already installed GSQL, so the whole surface
maps onto one MCP tool, `tigergraph__run_installed_query`, whose arguments are exactly
`runInstalledQuery(name, params)`.

That means MCPBackend is not a third implementation of the ten tools. It is
TigerGraphBackend with its connection swapped for a shim that speaks MCP, so the
response shaping -- which is the part with the bugs in it -- is written once.

  uv run python run.py --backend mcp

The MCP server reads TG_HOST / TG_USERNAME / TG_PASSWORD from the same .env the direct
connection uses, except that it spells the graph TG_GRAPHNAME where pyTigerGraph spells
it TG_GRAPH; the launcher below passes both.
"""
from __future__ import annotations
import asyncio, json, os, pathlib, re, shutil, sys, threading

from backend import TigerGraphBackend

TOOL = "tigergraph__run_installed_query"
_JSON_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.S)


def server_command() -> str:
    """Where tigergraph-mcp actually is.

    It installs as a console script beside the interpreter running this, and a
    subprocess launched from a script does not inherit the venv's bin on PATH -- so
    looking it up on PATH finds nothing and the session times out with no explanation.
    """
    beside = pathlib.Path(sys.executable).with_name("tigergraph-mcp")
    return str(beside) if beside.exists() else (shutil.which("tigergraph-mcp")
                                                or "tigergraph-mcp")


class MCPClient:
    """A stdio MCP session driven from synchronous code.

    The MCP client is async and the investigation loop is not, so the session lives in
    its own event loop on a daemon thread and calls are handed to it one at a time. The
    session has to stay open across calls -- both stdio_client and ClientSession are
    async context managers -- so a parked coroutine holds them and waits on `stop`.
    """

    def __init__(self, command: str | None = None, args=(), env=None, timeout=120):
        command = command or server_command()
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self.timeout = timeout
        self._session = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp").start()

        params = StdioServerParameters(command=command, args=list(args),
                                       env={**os.environ, **(env or {})})
        ready, self._stop = asyncio.Event(), asyncio.Event()
        self._error: BaseException | None = None

        async def serve():
            try:
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        self._session = s
                        ready.set()
                        await self._stop.wait()
            except BaseException as e:      # noqa: BLE001 - re-raised on the caller's thread
                self._error = e
            finally:
                ready.set()

        self._serving = asyncio.run_coroutine_threadsafe(serve(), self._loop)
        asyncio.run_coroutine_threadsafe(ready.wait(), self._loop).result(timeout)
        if self._session is None:
            raise RuntimeError(
                f"tigergraph-mcp did not start from {command!r}. Check that it is "
                f"installed (`uv add tigergraph-mcp`) and that TG_HOST is set in .env."
            ) from self._error

    def call(self, name: str, arguments: dict):
        res = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop).result(self.timeout)
        return _unwrap(res)

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)


def _unwrap(res):
    """tigergraph-mcp answers with one TextContent holding a fenced JSON envelope
    followed by a human-readable rendering of the same thing. Only the envelope is
    machine-readable, and only its `data.result` is the query's own output."""
    text = "".join(getattr(c, "text", "") for c in res.content)
    m = _JSON_BLOCK.search(text)
    if not m:
        raise RuntimeError(f"unparseable MCP response: {text[:400]}")
    env = json.loads(m.group(1))
    if not env.get("success", False):
        raise RuntimeError(f"{env.get('operation')}: {env.get('error') or env.get('summary')}")
    return (env.get("data") or {}).get("result")


class _Conn:
    """The slice of the pyTigerGraph connection surface the backend actually uses."""

    def __init__(self, client: MCPClient, graph: str):
        self.client, self.graphname = client, graph

    def runInstalledQuery(self, name, params=None):
        return self.client.call(TOOL, {"query_name": name, "params": params or {},
                                       "graph_name": self.graphname})


class MCPBackend(TigerGraphBackend):
    """Same ten tools, same response shaping, reached through the MCP server."""
    name = "mcp"

    def __init__(self, log=None, graph=None, command=None):
        graph = graph or os.getenv("TG_GRAPH", "FraudInvestigation")
        # the server spells it TG_GRAPHNAME; pyTigerGraph spells it TG_GRAPH.
        self.client = MCPClient(command=command, env={"TG_GRAPHNAME": graph})
        super().__init__(_Conn(self.client, graph), log)

    def close(self):
        self.client.close()


def demo():
    """Self-check for the envelope parser, which is the only part that runs without a
    live workspace. A real round trip needs TG_HOST set and the queries installed."""
    class R:
        def __init__(self, t): self.content = [type("C", (), {"text": t})()]

    ok = R('```json\n{"success": true, "operation": "run_installed_query", '
           '"data": {"result": [{"txns": [1, 2]}]}}\n```\n\n**Success**')
    assert _unwrap(ok) == [{"txns": [1, 2]}]

    bad = R('```json\n{"success": false, "operation": "run_installed_query", '
            '"error": "query not installed"}\n```')
    try:
        _unwrap(bad)
        raise AssertionError("a failed envelope must raise")
    except RuntimeError as e:
        assert "query not installed" in str(e), e

    try:
        _unwrap(R("no json here"))
        raise AssertionError("an unparseable response must raise")
    except RuntimeError as e:
        assert "unparseable" in str(e), e
    print("mcp_backend.py: envelope parsing checks passed")


if __name__ == "__main__":
    demo()

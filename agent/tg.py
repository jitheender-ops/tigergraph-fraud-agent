"""TigerGraph connection. Reads .env; works with Savanna or Community Edition."""
import os
from dotenv import load_dotenv

load_dotenv()
GRAPH = os.getenv("TG_GRAPH", "FraudInvestigation")


def connect(graph: str | None = None):
    import pyTigerGraph as tg
    host = os.getenv("TG_HOST")
    if not host:
        raise RuntimeError(
            "TG_HOST is not set. Copy .env.example to .env and fill in your TigerGraph "
            "Savanna workspace details, or run with --backend duckdb.")
    kwargs = dict(host=host, graphname=graph or GRAPH,
                  username=os.getenv("TG_USERNAME", "tigergraph"),
                  password=os.getenv("TG_PASSWORD", ""))
    if os.getenv("TG_SECRET"):
        kwargs["gsqlSecret"] = os.getenv("TG_SECRET")
    if os.getenv("TG_RESTPP_PORT"):
        kwargs["restppPort"] = os.getenv("TG_RESTPP_PORT")
    if os.getenv("TG_GS_PORT"):
        kwargs["gsPort"] = os.getenv("TG_GS_PORT")
    conn = tg.TigerGraphConnection(**kwargs)
    if os.getenv("TG_SECRET"):
        conn.getToken(os.getenv("TG_SECRET"))
    return conn

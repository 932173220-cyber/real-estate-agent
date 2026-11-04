"""Small CLI for agent-memory.

Usage:
    agent-memory serve [--host 0.0.0.0] [--port 8000] [--db PATH]
    agent-memory add "text to remember" [--namespace NS] [--ttl SECONDS]
    agent-memory search "what did I say about X" [--namespace NS] [-k 5]
    agent-memory list [--namespace NS] [--limit 20]
    agent-memory stats
    agent-memory prune
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from agent_memory.store import MemoryStore


def _store_from_args(args: argparse.Namespace) -> MemoryStore:
    path = args.db or os.environ.get("AGENT_MEMORY_DB", "agent_memory.db")
    return MemoryStore(path)


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. `pip install agent-memory[server]`", file=sys.stderr)
        return 2

    os.environ["AGENT_MEMORY_DB"] = args.db or os.environ.get("AGENT_MEMORY_DB", "agent_memory.db")
    uvicorn.run("agent_memory.server:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    metadata = json.loads(args.metadata) if args.metadata else None
    mem = store.add(args.content, namespace=args.namespace, metadata=metadata, ttl=args.ttl)
    print(json.dumps(mem.to_dict(), indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    results = store.search(
        args.query,
        namespace=args.namespace,
        limit=args.k,
        diversity=args.diversity,
    )
    for r in results:
        print(f"[{r.score:.3f}] {r.memory.content}")
        if args.verbose:
            print(f"        id={r.memory.id} ns={r.memory.namespace} meta={r.memory.metadata}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    for m in store.list(namespace=args.namespace, limit=args.limit):
        print(f"{m.id}  {m.content}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    ok = store.delete(args.id)
    print("deleted" if ok else "not found")
    return 0 if ok else 1


def cmd_stats(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    print(
        json.dumps(
            {
                "total": store.count(),
                "namespaces": store.namespaces(),
                "embedder": store.embedder.name,
                "dim": store.embedder.dim,
                "db": store.path,
            },
            indent=2,
        )
    )
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    print(f"pruned {store.prune()} memories")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-memory", description="Persistent memory for AI agents.")
    p.add_argument(
        "--db", help="Path to the SQLite database. Default: $AGENT_MEMORY_DB or agent_memory.db"
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="Run the REST server.")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("add", help="Add a memory.")
    a.add_argument("content")
    a.add_argument("--namespace", "-n")
    a.add_argument("--ttl", type=float)
    a.add_argument("--metadata", help="Inline JSON metadata.")
    a.set_defaults(func=cmd_add)

    q = sub.add_parser("search", help="Search memories.")
    q.add_argument("query")
    q.add_argument("--namespace", "-n")
    q.add_argument("-k", type=int, default=5)
    q.add_argument("--diversity", type=float, default=0.0)
    q.add_argument("--verbose", "-v", action="store_true")
    q.set_defaults(func=cmd_search)

    ls = sub.add_parser("list", help="List memories.")
    ls.add_argument("--namespace", "-n")
    ls.add_argument("--limit", type=int, default=20)
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("delete", help="Delete a memory by id.")
    d.add_argument("id")
    d.set_defaults(func=cmd_delete)

    st = sub.add_parser("stats", help="Show store stats.")
    st.set_defaults(func=cmd_stats)

    pr = sub.add_parser("prune", help="Delete expired memories.")
    pr.set_defaults(func=cmd_prune)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""A minimal MCP server over stdio (standard library only).

Implements the parts of the Model Context Protocol that a tools-only server needs:
`initialize`, `ping`, `tools/list`, and `tools/call`, as newline-delimited JSON-RPC 2.0
on stdin and stdout. Each request runs on its own thread, so a long call (the
coordinator's `wait_for_change`) never blocks the workers' calls to the same server.
"""

import asyncio
import inspect
import json
import sys
import threading
import traceback
import types
import typing
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import IO, Any, Literal, Union, get_args, get_origin

DEFAULT_PROTOCOL = "2025-06-18"


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]
    # Parameters given as models (classes with json_schema() and build()).
    models: dict[str, Any]


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and hasattr(tp, "json_schema") and hasattr(tp, "build")


def _schema(tp: Any) -> tuple[dict[str, Any], bool]:
    """JSON schema for a parameter type, and whether it accepts null."""
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        schema, _ = _schema(args[0]) if len(args) == 1 else ({}, False)
        return schema, len(args) < len(get_args(tp))
    if origin is Literal:
        return {"type": "string", "enum": list(get_args(tp))}, False
    if origin is list:
        (item,) = get_args(tp) or (str,)
        return {"type": "array", "items": _schema(item)[0]}, False
    if _is_model(tp):
        return tp.json_schema(), False
    simple = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if tp in simple:
        return {"type": simple[tp]}, False
    return {}, False


class StdioServer:
    def __init__(self, name: str, version: str, instructions: str = "") -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools: dict[str, Tool] = {}
        self._write_lock = threading.Lock()

    # --- Registration -----------------------------------------------------------

    def tool(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            hints = typing.get_type_hints(fn)
            props: dict[str, Any] = {}
            required: list[str] = []
            models: dict[str, Any] = {}
            for pname, param in inspect.signature(fn).parameters.items():
                tp = hints.get(pname, str)
                schema, nullable = _schema(tp)
                if _is_model(tp):
                    models[pname] = tp
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
                elif param.default is not None:
                    schema = {**schema, "default": param.default}
                if nullable and "type" in schema:
                    schema = {**schema, "type": [schema["type"], "null"]}
                props[pname] = schema
            description = inspect.cleandoc(fn.__doc__ or "")
            self.tools[fn.__name__] = Tool(
                name=fn.__name__,
                description=description,
                input_schema={"type": "object", "properties": props, "required": required},
                fn=fn,
                models=models,
            )
            return fn

        return register

    async def list_tools(self) -> list[Tool]:
        return list(self.tools.values())

    # --- Calls ------------------------------------------------------------------

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool and return an MCP tools/call result."""
        tool = self.tools.get(name)
        if tool is None:
            return _error_result(f"unknown tool {name!r}")
        args = dict(arguments or {})
        for pname, model in tool.models.items():
            if pname in args:
                issues: list[Any] = []
                value = model.build(args[pname], (), issues)
                if value is None:
                    return _text_result(
                        {"ok": False, "errors": [i.to_dict() for i in issues]}, is_error=False
                    )
                args[pname] = value
        try:
            result = tool.fn(**args)
            if inspect.iscoroutine(result):
                result = asyncio.run(result)
        except TypeError as exc:
            return _error_result(f"bad arguments for {name}: {exc}")
        except Exception as exc:  # report, never crash the server
            traceback.print_exc(file=sys.stderr)
            return _error_result(f"{type(exc).__name__}: {exc}")
        return _text_result(result)

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; None for notifications."""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if msg_id is None:
            return None  # notifications/initialized, notifications/cancelled, ...
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self.tools.values()
            ]
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
        if method == "tools/call":
            result = self.call(str(params.get("name")), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    # --- Transport --------------------------------------------------------------

    def serve(self, stdin: IO[str] | None = None, stdout: IO[str] | None = None) -> None:
        inp = stdin or sys.stdin
        out = stdout or sys.stdout

        def send(reply: dict[str, Any]) -> None:
            line = json.dumps(reply, ensure_ascii=False)
            with self._write_lock:
                out.write(line + "\n")
                out.flush()

        def work(message: dict[str, Any]) -> None:
            try:
                reply = self.handle(message)
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                reply = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32603, "message": str(exc)},
                }
            if reply is not None:
                send(reply)

        with ThreadPoolExecutor(max_workers=64) as pool:
            for raw in inp:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "parse error"},
                        }
                    )
                    continue
                if isinstance(message, dict):
                    pool.submit(work, message)


def _text_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "isError": is_error,
    }


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}

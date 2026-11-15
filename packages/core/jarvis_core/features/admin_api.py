"""``jarvis.api_describe / api_read / api_write``: the routes the web UI uses, for Jarvis (issue #14).

Everything Arsen can do in the UI that has no precise tool - rename a board, file a chat in a
folder, edit a skill, merge two people in the knowledge graph, run triage now - Jarvis does by
calling the same REST handler in process (``httpx.ASGITransport``): the same validation, the same
events, the same answer as the UI gets. Nothing is copied, so a route added later works the day
it is marked.

Which routes are reachable is a declaration on each route (``api.deps.agent``), read back from
``app.openapi()``: unmarked is unreachable, ``none`` is never (credentials, approvals, media),
``prefer`` sends settings and schedules to their precise tools so there is one trail and one probe
path, and a DELETE - or a body value the route names as risky - asks first.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

import httpx
from pydantic import BaseModel, Field
from starlette.routing import Match

from jarvis_core.engine.current import current_conversation_id
from jarvis_core.features.admin import Plan, who_now
from jarvis_core.tools.builtin import BuiltinProvider, CallRisk, tool
from jarvis_proto import ChangeField, ToolResult

if TYPE_CHECKING:
    from fastapi.routing import APIRoute

    from jarvis_core.app import Core

log = logging.getLogger(__name__)

MARKER = "x-jarvis-agent"
_BASE = "http://core.internal"
# A response longer than this is cut; the loop's own admission limit handles what is left.
_MAX_RESPONSE_CHARS = 60_000


class _DescribeArgs(BaseModel):
    path: str = Field(default="", description="'' = every route Jarvis may use; '/api/boards' = that family in detail")


class _ReadArgs(BaseModel):
    path: str = Field(description="GET path with its query, e.g. '/api/kg/entities?q=Ivan'")


class _WriteArgs(BaseModel):
    method: Literal["POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(description="e.g. '/api/boards/b_1'")
    body: dict[str, Any] | list[Any] | None = Field(default=None, description="JSON body, as the route describes it")
    reason: str = Field(default="", description="One line: why (shown on the change card).")


class ApiTools(BuiltinProvider):
    name = "jarvis-api"

    def __init__(self, core: Core) -> None:
        self.core = core
        self._client: httpx.AsyncClient | None = None
        super().__init__()

    @property
    def configured(self) -> bool:
        admin = self.core.settings.agent_admin
        return admin.enabled and admin.api_enabled and getattr(self.core, "app", None) is not None

    # -- routes ------------------------------------------------------------------------------

    def _routes(self) -> list[APIRoute]:
        app = getattr(self.core, "app", None)
        return api_routes(app.routes if app is not None else [])

    def route_for(self, method: str, path: str) -> tuple[APIRoute | None, dict[str, Any]]:
        """The route a request would reach and its marker ({} = unmarked). Matched by Starlette
        itself, never by comparing strings, so a path parameter cannot smuggle a different route."""
        bare = path.split("?", 1)[0]
        scope = {
            "type": "http",
            "method": method.upper(),
            "path": bare,
            "root_path": "",
            "query_string": b"",
            "headers": [],
        }
        for route in self._routes():
            match, _ = route.matches(scope)
            if match is Match.FULL:
                extra = route.openapi_extra or {}
                return route, dict(extra.get(MARKER) or {})
        return None, {}

    def refusal(self, method: str, path: str) -> str | None:
        """Why Jarvis may not make this request, or None."""
        if not path.startswith("/api/"):
            return f"{path} is not one of the core's /api routes"
        route, marker = self.route_for(method, path)
        if route is None:
            return f"no route {method} {path.split('?', 1)[0]} (jarvis.api_describe lists them)"
        access = marker.get("access")
        if marker.get("prefer"):
            return f"not through the API: use {marker['prefer']} for this"
        if access not in ("read", "write"):
            return f"{method} {path.split('?', 1)[0]} is not available to Jarvis"
        if method.upper() != "GET" and access != "write":
            return f"{path.split('?', 1)[0]} may only be read"
        return None

    # -- dispatch ----------------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            app = self.core.app
            assert app is not None, "jarvis.api_* needs the app (create_app sets it)"
            self._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE, timeout=600)
        return self._client

    async def dispatch(self, method: str, path: str, body: Any = None, *, actor: str) -> tuple[int, Any]:
        """The request through the real app: middleware, token, validation, handler, events."""
        headers = {"X-Jarvis-Actor": actor}
        if self.core.config.token:
            headers["Authorization"] = f"Bearer {self.core.config.token}"
        resp = await self._http().request(method.upper(), path, json=body, headers=headers)
        try:
            data: Any = resp.json() if resp.content else None
        except ValueError:
            data = resp.text[:2000]
        return resp.status_code, data

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- risk ------------------------------------------------------------------------------

    def _risky(self, method: str, path: str, body: Any) -> str | None:
        _route, marker = self.route_for(method, path)
        if marker.get("risk") or method.upper() == "DELETE":
            return f"{method.upper()} {path.split('?', 1)[0]} removes something that cannot be brought back"
        for field, values in (marker.get("risk_when") or {}).items():
            if isinstance(body, dict) and body.get(field) in values:
                return f"{method.upper()} {path.split('?', 1)[0]} with {field}={body.get(field)!r} removes things"
        return None

    def _self_harm(self, method: str, path: str, body: Any) -> str | None:
        """Deleting the conversation this run is speaking in - by path or in a bulk body."""
        own = current_conversation_id.get()
        if not own:
            return None
        bare = path.split("?", 1)[0]
        deleting = method.upper() == "DELETE" or (isinstance(body, dict) and body.get("action") == "delete")
        named = bare.rstrip("/").endswith(f"/conversations/{own}") or (
            isinstance(body, dict) and own in (body.get("ids") or [])
        )
        if deleting and named:
            return "that is the conversation you are speaking in; it cannot be deleted from inside itself"
        return None

    async def assess(self, name: str, arguments: dict[str, Any]) -> CallRisk | None:
        if name != "jarvis.api_write":
            return None
        method, path, body = str(arguments.get("method", "")), str(arguments.get("path", "")), arguments.get("body")
        if self.refusal(method, path) or self._self_harm(method, path, body):
            return None  # refused at call time with the reason
        why = self._risky(method, path, body)
        if why is None:
            return None
        before = await self._before(method, path)

        async def propose() -> ToolResult:
            who = await who_now(self.core)
            rec = await self.core.admin.propose(
                Plan(self.core.settings, []),
                who=who,
                reason=str(arguments.get("reason", "")),
                request={"method": method.upper(), "path": path, "body": body},
                area="api",
                target=f"{method.upper()} {path.split('?', 1)[0]}",
            )
            rec = (
                await self.core.admin.changes.update(
                    rec.id, fields=[ChangeField(path=f"{method.upper()} {path}", old=before, new=body, op="remove")]
                )
                or rec
            )
            return ToolResult.data(
                f"Proposed, NOT done ({rec.id}): {why}. Nobody can say yes in this run, so it waits for Arsen's "
                "Apply. Say so in your report."
            ).model_copy(update={"change": rec.model_dump(mode="json")})

        preview = [
            ChangeField(path=f"{method.upper()} {path.split('?', 1)[0]}", old=_brief(before), new=body, op="remove")
        ]
        return CallRisk(reason=why, risk=["delete"], preview=preview, propose=propose)

    async def _before(self, method: str, path: str) -> Any:
        """What the path holds now, when it can be read: what a delete would lose, for the card."""
        if method.upper() == "POST" or self.refusal("GET", path) is not None:
            return None
        try:
            status, data = await self.dispatch("GET", path, actor="jarvis")
        except Exception:
            return None
        return data if status == 200 else None

    # -- tools ------------------------------------------------------------------------------

    @tool(
        "jarvis.api_describe",
        description=(
            "The core's own routes - what the web UI does - that Jarvis may call with jarvis.api_read and "
            "api_write, for anything without a dedicated tool (boards, folders, chats, skills, knowledge, "
            "meetings, triage/RSVP runs). With a path: its parameters and body."
        ),
        args=_DescribeArgs,
        read_only=True,
        idempotent=True,
        owner_only=True,
    )
    async def _describe(self, path: str = "") -> ToolResult:
        assert self.core.app is not None
        spec = self.core.app.openapi()
        lines: list[str] = []
        want = path.strip().split("?", 1)[0].rstrip("/")
        for route_path, ops in sorted(spec.get("paths", {}).items()):
            for method, op in ops.items():
                marker = op.get(MARKER) or {}
                access = marker.get("access")
                if access not in ("read", "write") and not marker.get("prefer"):
                    continue
                if want and not route_path.startswith(want):
                    continue
                summary = (op.get("summary") or op.get("description") or "").split("\n")[0]
                tag = f" [use {marker['prefer']}]" if marker.get("prefer") else ""
                tag += " [asks first]" if marker.get("risk") or method == "delete" else ""
                tag += (
                    " [asks when " + ", ".join(f"{k}={v}" for k, v in marker["risk_when"].items()) + "]"
                    if marker.get("risk_when")
                    else ""
                )
                lines.append(f"{method.upper()} {route_path}{tag}" + (f" — {summary}" if summary and want else ""))
                if want:
                    lines += _detail(spec, op)
        if not lines:
            return ToolResult.failure(f"no route Jarvis may use under {path!r}")
        return ToolResult.data("\n".join(lines), count=len(lines))

    @tool(
        "jarvis.api_read",
        description="GET one of the core's routes (see jarvis.api_describe) and return its JSON.",
        args=_ReadArgs,
        read_only=True,
        idempotent=True,
        owner_only=True,
    )
    async def _read(self, path: str) -> ToolResult:
        if why := self.refusal("GET", path):
            return ToolResult.failure(why)
        status, data = await self.dispatch("GET", path, actor="jarvis")
        if status >= 400:
            return ToolResult.failure(f"HTTP {status}: {_text(data, 2000)}")
        return ToolResult.data(_text(data, _MAX_RESPONSE_CHARS))

    @tool(
        "jarvis.api_write",
        description=(
            "POST/PUT/PATCH/DELETE one of the core's routes (see jarvis.api_describe), exactly as the UI does. "
            "Deletes ask Arsen first."
        ),
        args=_WriteArgs,
        owner_only=True,
    )
    async def _write(
        self, method: str, path: str, body: Any = None, reason: str = "", idempotency_key: str = ""
    ) -> ToolResult:
        if why := self.refusal(method, path):
            return ToolResult.failure(why)
        if why := self._self_harm(method, path, body):
            return ToolResult.failure(why)
        who = await who_now(self.core, idempotency_key)
        before = await self._before(method, path) if method.upper() in ("PATCH", "PUT", "DELETE") else None
        status, data = await self.dispatch(method, path, body, actor=f"jarvis:{who.run_id or '-'}")
        if status >= 400:
            # The validator's own words (EC-5); nothing was changed by a refused request.
            return ToolResult.failure(f"HTTP {status}, nothing was changed: {_text(data, 3000)}")
        rec = await self.core.admin.changes.record(
            area="api",
            target=f"{method.upper()} {path.split('?', 1)[0]}",
            who=who,
            fields=[
                ChangeField(
                    path=f"{method.upper()} {path.split('?', 1)[0]}",
                    old=_brief(before),
                    new=body,
                    op="remove" if method.upper() == "DELETE" else "set",
                )
            ],
            reason=reason,
            note=f"HTTP {status}",
            request={"method": method.upper(), "path": path, "body": body},
        )
        text = f"HTTP {status}" + (f"\n{_text(data, _MAX_RESPONSE_CHARS)}" if data is not None else "")
        return ToolResult.data(text).model_copy(update={"change": rec.model_dump(mode="json")})


def api_routes(routes: Any) -> list[APIRoute]:
    """Every APIRoute in the app. This FastAPI keeps an included router as an opaque wrapper in
    ``app.routes`` (see tests/test_api_contract._get_paths), so the walk follows ``original_router``."""
    from fastapi.routing import APIRoute

    out: list[APIRoute] = []
    for route in routes or []:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            out += api_routes(inner.routes)
        elif isinstance(route, APIRoute):
            out.append(route)
    return out


def _text(data: Any, limit: int) -> str:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def _brief(value: Any) -> Any:
    """A before-value small enough for a card: a long object keeps its name and id."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= 600:
        return value
    if isinstance(value, dict):
        return {k: value[k] for k in ("id", "name", "title", "kind") if k in value} or text[:600] + "…"
    return text[:600] + "…"


def _resolve(spec: dict[str, Any], node: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "…"
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].rsplit("/", 1)[-1]
            return _resolve(spec, spec.get("components", {}).get("schemas", {}).get(name, {}), depth + 1)
        return {k: _resolve(spec, v, depth + 1) for k, v in node.items() if k not in ("title",)}
    if isinstance(node, list):
        return [_resolve(spec, v, depth + 1) for v in node]
    return node


def _detail(spec: dict[str, Any], op: dict[str, Any]) -> list[str]:
    out: list[str] = []
    params = [p for p in op.get("parameters", []) if p.get("in") in ("path", "query")]
    if params:
        out.append(
            "  params: "
            + ", ".join(
                f"{p['name']} ({p['in']}{', required' if p.get('required') else ''}: {p.get('schema', {}).get('type', 'any')})"
                for p in params
            )
        )
    body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if body:
        resolved = _resolve(spec, body)
        props = resolved.get("properties") if isinstance(resolved, dict) else None
        if props:
            out.append(
                "  body: "
                + ", ".join(
                    f"{k}: {_type(v)}" + (" (required)" if k in (resolved.get("required") or []) else "")
                    for k, v in props.items()
                )
            )
        else:
            out.append(f"  body: {json.dumps(resolved, ensure_ascii=False)[:400]}")
    return out


def _type(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "any"
    if "enum" in schema:
        return "|".join(repr(v) for v in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(_type(s) for s in schema["anyOf"])
    return str(schema.get("type", "object"))

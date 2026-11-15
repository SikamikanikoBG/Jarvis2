"""Paths into the settings document, and what a change along one does (docs/stories/14_agent_admin.md).

A path is dotted over the JSON shape of :class:`Settings`: ``voice.max_turn_s``,
``roles.chat.base_url``, ``budgets.scheduled.max_steps``. An item of a list is picked by the
field the list's ``x-key`` marker names - ``mcp_servers[name=fetch].enabled``,
``mail_accounts[address=a@gmail.com].app_password`` - or by position (``[2]``) where the list has
no key; a dict key that is not a plain word goes in brackets (``triage.account_rules[a@b.bg]``).

Everything here is pure and works on the dumped JSON plus the pydantic types, so the core, its
tools and the break-glass script (``jarvis_core.admin_revert``) read paths the same way. The risk
of a change is read from the markers on the fields (``settings.marks``), never from names.
"""

from __future__ import annotations

import copy
import difflib
import inspect
import re
import types
from dataclasses import dataclass, field
from functools import cache
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from jarvis_proto import settings as settings_module
from jarvis_proto.settings import Settings

#: What a secret looks like when it is not shown. A value that starts with it, written back, means
#: "keep the saved one" - so a model that edits a whole mail account it read masked does not
#: overwrite the password with the mask (the convention /mail/test already uses).
MASK = "••••"

_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class PathError(ValueError):
    """A path that does not name a setting; the message says what is wrong and what is near."""


@dataclass(frozen=True, slots=True)
class Step:
    """One hop: a field or dict key (``name``), a keyed list item, or a list position."""

    name: str | None = None
    key: tuple[str, str] | None = None  # (field, value) for [field=value]
    index: int | None = None


@dataclass(slots=True)
class Leaf:
    """One field that differs between two settings documents."""

    path: str
    old: Any
    new: Any
    op: str = "set"  # set | add | remove
    risk: set[str] = field(default_factory=set)
    secret: bool = False


def parse(path: str) -> list[Step]:
    steps: list[Step] = []
    i, text = 0, path.strip()
    if not text:
        return steps
    while i < len(text):
        if text[i] == ".":
            i += 1
            continue
        if text[i] == "[":
            end = text.find("]", i)
            if end < 0:
                raise PathError(f"unclosed '[' in {path!r}")
            inner = text[i + 1 : end].strip()
            i = end + 1
            if "=" in inner:
                k, v = inner.split("=", 1)
                steps.append(Step(key=(k.strip(), v.strip())))
            elif re.fullmatch(r"-?\d+", inner):
                steps.append(Step(index=int(inner)))
            elif inner:
                steps.append(Step(name=inner))
            else:
                raise PathError(f"empty [] in {path!r}")
            continue
        m = re.match(r"[^.\[]+", text[i:])
        assert m is not None
        steps.append(Step(name=m.group(0).strip()))
        i += m.end()
    return steps


def unwrap(tp: Any) -> Any:
    """``X | None`` → ``X``."""
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return unwrap(args[0])
    return tp


def _is_model(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, BaseModel)


def _extra(model: type[BaseModel], name: str) -> dict[str, Any]:
    info = model.model_fields[name]
    extra = info.json_schema_extra
    return extra if isinstance(extra, dict) else {}


def _seg(name: str) -> str:
    return f".{name}" if _WORD.match(name) else f"[{name}]"


def _join(prefix: str, name: str) -> str:
    seg = _seg(name)
    return (seg[1:] if seg.startswith(".") else seg) if not prefix else prefix + seg


@dataclass(slots=True)
class Resolved:
    """What a path points at in the model: its type, the risk classes along it, its list key."""

    tp: Any
    risk: set[str]
    key: str | None  # the x-key of the list this path IS (when it is a list)


def resolve(path: str | list[Step], root: type[BaseModel] = Settings) -> Resolved:
    steps = parse(path) if isinstance(path, str) else path
    tp: Any = root
    risk: set[str] = set()
    key: str | None = None
    walked = ""
    for step in steps:
        tp = unwrap(tp)
        origin = get_origin(tp)
        if step.name is not None and _is_model(tp):
            fields = tp.model_fields
            if step.name not in fields:
                near = difflib.get_close_matches(step.name, list(fields), n=1, cutoff=0.6)
                hint = f"; did you mean {_join(walked, near[0])!r}?" if near else ""
                raise PathError(f"no setting {_join(walked, step.name)!r}{hint}")
            extra = _extra(tp, step.name)
            risk |= set(extra.get("x-risk") or [])
            key = extra.get("x-key")
            tp = fields[step.name].annotation
            walked = _join(walked, step.name)
            continue
        if step.name is not None and origin is dict:
            tp = get_args(tp)[1]
            walked = _join(walked, step.name)
            key = None
            continue
        if (step.key is not None or step.index is not None) and origin is list:
            if step.key is not None and key and step.key[0] != key:
                raise PathError(f"items of {walked!r} are picked by {key}, e.g. {walked}[{key}=…]")
            tp = get_args(tp)[0]
            walked += f"[{step.key[0]}={step.key[1]}]" if step.key else f"[{step.index}]"
            key = None
            continue
        raise PathError(f"{walked or 'settings'!r} has nothing under {step.name or step.key or step.index!r}")
    return Resolved(tp=tp, risk=risk, key=key)


def _find(items: list[Any], step: Step, where: str) -> int:
    if step.index is not None:
        if -len(items) <= step.index < len(items):
            return step.index % len(items)
        raise PathError(f"{where} has {len(items)} items; there is no [{step.index}]")
    assert step.key is not None
    k, v = step.key
    for i, item in enumerate(items):
        if isinstance(item, dict) and str(item.get(k, "")).strip().lower() == v.strip().lower():
            return i
    known = [str(it.get(k)) for it in items if isinstance(it, dict)]
    raise PathError(f"no item {k}={v!r} in {where}" + (f" (there are: {', '.join(known)})" if known else ""))


def _descend(container: Any, step: Step, where: str) -> tuple[Any, Any]:
    """(the container, the key or index in it) for ``step``."""
    if step.name is not None:
        if not isinstance(container, dict):
            raise PathError(f"{where} is not a section")
        if step.name not in container:
            # A dict keyed by an enum (roles, budgets) or a free name (account_rules).
            match = next((k for k in container if str(k).lower() == step.name.lower()), None)
            if match is None:
                raise PathError(f"no {_join(where, step.name)!r}")
            return container, match
        return container, step.name
    if not isinstance(container, list):
        raise PathError(f"{where} is not a list")
    return container, _find(container, step, where)


def get_at(dump: dict[str, Any], path: str) -> Any:
    resolve(path)  # a path that names no setting fails with the near miss
    node: Any = dump
    where = ""
    for step in parse(path):
        holder, k = _descend(node, step, where or "settings")
        node = holder[k]
        where = _join(where, str(step.name)) if step.name is not None else f"{where}[{k}]"
    return node


def set_at(dump: dict[str, Any], path: str, value: Any) -> None:
    steps = parse(path)
    if not steps:
        raise PathError("a path is required")
    resolve(steps)
    node: Any = dump
    for step in steps[:-1]:
        holder, k = _descend(node, step, "settings")
        node = holder[k]
    last = steps[-1]
    if last.name is not None and isinstance(node, dict):
        holder, k = (node, last.name) if last.name in node else _maybe(node, last.name)
        holder[k] = value
        return
    holder, k = _descend(node, last, "settings")
    holder[k] = value


def _maybe(node: dict[str, Any], name: str) -> tuple[dict[str, Any], str]:
    """A new key in a dict section (a budget for a kind not stored yet), case-matched if present."""
    match = next((k for k in node if str(k).lower() == name.lower()), None)
    return node, match if match is not None else name


def add_at(dump: dict[str, Any], path: str, item: Any) -> None:
    """Append ``item`` to the list at ``path`` (a keyed list refuses a second item with the same key)."""
    res = resolve(path)
    if get_origin(unwrap(res.tp)) is not list:
        raise PathError(f"{path!r} is not a list; set it instead")
    items = get_at(dump, path)
    if res.key and isinstance(item, dict):
        want = str(item.get(res.key, "")).strip().lower()
        if any(isinstance(i, dict) and str(i.get(res.key, "")).strip().lower() == want for i in items):
            raise PathError(f"{path} already has {res.key}={item.get(res.key)!r}; change it with its path")
    items.append(item)


def remove_at(dump: dict[str, Any], path: str) -> None:
    steps = parse(path)
    if not steps:
        raise PathError("a path is required")
    resolve(steps)
    node: Any = dump
    for step in steps[:-1]:
        holder, k = _descend(node, step, "settings")
        node = holder[k]
    holder, k = _descend(node, steps[-1], "settings")
    del holder[k]


# --- diff, secrets -----------------------------------------------------------------------


def _secret_in(tp: Any, value: Any) -> bool:
    """Whether ``value`` (of type ``tp``) carries a non-empty secret anywhere inside."""
    tp = unwrap(tp)
    if _is_model(tp) and isinstance(value, dict):
        for name, info in tp.model_fields.items():
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            if "secret" in (extra.get("x-risk") or []) and value.get(name):
                return True
            if _secret_in(info.annotation, value.get(name)):
                return True
        return False
    origin = get_origin(tp)
    if origin is list and isinstance(value, list):
        return any(_secret_in(get_args(tp)[0], v) for v in value)
    if origin is dict and isinstance(value, dict):
        return any(_secret_in(get_args(tp)[1], v) for v in value.values())
    return False


def diff(
    old: Any,
    new: Any,
    tp: Any = Settings,
    *,
    prefix: str = "",
    risk: frozenset[str] = frozenset(),
    key: str | None = None,
) -> list[Leaf]:
    """Every leaf that differs, with the risk classes along its path."""
    tp = unwrap(tp)
    secret = "secret" in risk
    if old == new:
        return []
    if _is_model(tp) and isinstance(old, dict) and isinstance(new, dict):
        out: list[Leaf] = []
        for name, info in tp.model_fields.items():
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            out += diff(
                old.get(name),
                new.get(name),
                info.annotation,
                prefix=_join(prefix, name),
                risk=risk | frozenset(extra.get("x-risk") or []),
                key=extra.get("x-key"),
            )
        return out
    origin = get_origin(tp)
    if origin is dict and isinstance(old, dict) and isinstance(new, dict):
        sub = get_args(tp)[1]
        out = []
        for k in sorted({*old, *new}, key=str):
            path = _join(prefix, str(k))
            if k not in old:
                out.append(Leaf(path, None, new[k], "add", set(risk), secret or _secret_in(sub, new[k])))
            elif k not in new:
                out.append(Leaf(path, old[k], None, "remove", {*risk, "delete"}, secret or _secret_in(sub, old[k])))
            else:
                out += diff(old[k], new[k], sub, prefix=path, risk=risk)
        return out
    if origin is list and key and isinstance(old, list) and isinstance(new, list):
        sub = get_args(tp)[0]

        def ident(item: Any) -> str:
            return str(item.get(key, "")).strip().lower() if isinstance(item, dict) else repr(item)

        before = {ident(i): i for i in old}
        after = {ident(i): i for i in new}
        out = []
        for item in new:
            k = ident(item)
            path = f"{prefix}[{key}={item.get(key) if isinstance(item, dict) else k}]"
            if k not in before:
                out.append(Leaf(path, None, item, "add", set(risk), secret or _secret_in(sub, item)))
            else:
                out += diff(before[k], item, sub, prefix=path, risk=risk)
        for item in old:
            k = ident(item)
            if k not in after:
                path = f"{prefix}[{key}={item.get(key) if isinstance(item, dict) else k}]"
                out.append(Leaf(path, item, None, "remove", {*risk, "delete"}, secret or _secret_in(sub, item)))
        return out
    return [Leaf(prefix, old, new, "set", set(risk), secret or _secret_in(tp, old) or _secret_in(tp, new))]


def is_masked(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MASK)


def masked(value: str | None) -> str:
    return f"{MASK} set ({len(value)} chars)" if value else ""


def mask(value: Any, tp: Any = Settings, *, secret: bool = False) -> Any:
    """``value`` with every secret inside it masked; ``secret`` = the value itself is one."""
    tp = unwrap(tp)
    if secret:
        if isinstance(value, dict):
            return {k: masked(str(v)) if v else v for k, v in value.items()}
        return masked(str(value)) if value else value
    if _is_model(tp) and isinstance(value, dict):
        out = dict(value)
        for name, info in tp.model_fields.items():
            if name not in out:
                continue
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, dict) else {}
            out[name] = mask(out[name], info.annotation, secret="secret" in (extra.get("x-risk") or []))
        return out
    origin = get_origin(tp)
    if origin is list and isinstance(value, list):
        return [mask(v, get_args(tp)[0]) for v in value]
    if origin is dict and isinstance(value, dict):
        return {k: mask(v, get_args(tp)[1]) for k, v in value.items()}
    return value


def mask_at(path: str, value: Any) -> Any:
    """``value`` as it may be shown, given the path it sits at."""
    try:
        res = resolve(path)
    except PathError:
        return value
    return mask(value, res.tp, secret="secret" in res.risk)


def keep_masked(new: Any, old: Any, tp: Any = Settings) -> Any:
    """``new`` with every masked secret replaced by the value it stands for in ``old``."""
    tp = unwrap(tp)
    if is_masked(new):
        return old if isinstance(old, str) else ""
    if isinstance(new, dict) and isinstance(old, dict):
        if _is_model(tp):
            fields = tp.model_fields
            return {k: keep_masked(v, old.get(k), fields[k].annotation if k in fields else Any) for k, v in new.items()}
        sub = get_args(tp)[1] if get_origin(tp) is dict else Any
        return {k: keep_masked(v, old.get(k), sub) for k, v in new.items()}
    if isinstance(new, list) and isinstance(old, list) and get_origin(tp) is list:
        sub = get_args(tp)[0]
        out = []
        for i, item in enumerate(new):
            twin = _twin(item, old, i)
            out.append(keep_masked(item, twin, sub))
        return out
    return new


def _twin(item: Any, old: list[Any], i: int) -> Any:
    """The item in ``old`` that ``item`` is a new version of: same identity field, else same place."""
    if isinstance(item, dict):
        for k in ("name", "address", "id"):
            if k in item:
                return next((o for o in old if isinstance(o, dict) and o.get(k) == item[k]), None)
    return old[i] if i < len(old) else None


# --- what a field means --------------------------------------------------------------------


@cache
def _comments() -> dict[str, str]:
    """``Class.field`` → the comment block above the field in jarvis_proto/settings.py.

    The settings are documented in comments, not ``Field(description=)`` (none has one), so the
    meaning Jarvis reads is the one a developer reads - no second copy to keep in step. A wheel
    without sources answers nothing, and settings.get then gives values only."""
    try:
        lines = inspect.getsource(settings_module).splitlines()
    except (OSError, TypeError):
        return {}
    out: dict[str, str] = {}
    cls: str | None = None
    block: list[str] = []
    for line in lines:
        if m := re.match(r"^class (\w+)\(", line):
            cls, block = m.group(1), []
            continue
        if cls is None:
            continue
        if m := re.match(r"^    #:? ?(.*)$", line):
            block.append(m.group(1).strip())
            continue
        if m := re.match(r"^    (\w+): ", line):
            if block:
                out[f"{cls}.{m.group(1)}"] = " ".join(b for b in block if b)
            block = []
            continue
        if line.strip() and not line.startswith("        "):
            block = []
    return out


def meaning(path: str) -> str:
    """What the setting at ``path`` is for, from its comment (or the class docstring of a section)."""
    steps = parse(path)
    if not steps:
        return ""
    try:
        parent = resolve(steps[:-1]) if len(steps) > 1 else Resolved(Settings, set(), None)
        here = resolve(steps)
    except PathError:
        return ""
    owner = unwrap(parent.tp)
    if get_origin(owner) is list:
        owner = unwrap(get_args(owner)[0])
    last = steps[-1]
    text = _comments().get(f"{owner.__name__}.{last.name}", "") if _is_model(owner) and last.name else ""
    inner = unwrap(here.tp)
    if get_origin(inner) is list:
        inner = unwrap(get_args(inner)[0])
    if not text and _is_model(inner) and inner.__doc__:
        text = " ".join(inner.__doc__.split())
    return text


def section_fields(path: str) -> list[str]:
    """The field names directly under a section path ([] for a leaf)."""
    inner = unwrap(resolve(path).tp)
    return list(inner.model_fields) if _is_model(inner) else []


def deep(value: Any) -> Any:
    return copy.deepcopy(value)

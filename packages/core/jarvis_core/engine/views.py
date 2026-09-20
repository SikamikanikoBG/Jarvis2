"""A long tool result as the model should see it: sections with ``@refs``, never a blind head.

Until 2026-09-20 a result that did not fit rode as its first N characters plus a marker, and
the rest was reachable by character offset. For prose that is a page torn in half; for the JSON
every host and MCP tool returns it is worse - the first 700 characters of an ``outlook_list``
are one hex ``store_id``, and a 12k head of a 43k folder tree told the model so little that it
went fishing with sixty search/read calls (2026-09-16). The remedy then was to admit results
whole up to 48k chars, which is how a scheduled step came to carry 118k tokens of results and
how one run's prefix evicts another's from the KV pool.

This module gives every result the shape the browser extension already gave pages: an
**outline** of sections, each with a ref (``@a254a08e1df37916.3``), a size and a one-line
preview, and a body that ``jarvis.result_read`` hands back by ref. A JSON list becomes one
section per item (values common to every item hoisted into an envelope section, opaque ids
shortened in the preview, whole in the body); a JSON object without a list becomes one section
per key; text splits at its headings, else into paragraph chunks. The same view serves three
moments in a result's life, at three sizes: when it enters the prompt (``admit``), when the
run's results outgrow their budget or it belongs to an earlier turn (``aged``), and when the
whole step must be cut to the lane's window. A view only ever shrinks - the DB keeps every byte.
"""

from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from jarvis_proto import Message, Role

#: What vLLM puts in front of every tool call id; the model never needs to read it twice.
ID_PREFIX = "chatcmpl-tool-"
#: A result from an earlier step or turn rides as a view of this size.
AGED_VIEW_CHARS = 1_200
#: Results at or under this are left whole when aged: a view would cost more than it saves.
VIEW_MIN_CHARS = 2_000
#: Text without headings is cut into sections of about this many characters.
CHUNK_CHARS = 3_000

_LINE_WIDTH = 140  # an outline line, ref excluded
_INLINE_WIDTH = 100  # a nested value shown on its parent's line
_TAIL_MIN_LINES = 8  # fewer outline lines than this and the outline is grouped into ranges instead
_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")
_OPAQUE = re.compile(r"[0-9A-Fa-f_\-]{32,}")
_MARKER = re.compile(r"\n?\[(@[^\s:\]]+): ([\d,]+) chars in (\d+) sections?; shown: [^\]]*\]$")
_TITLE_KEYS = (
    "subject", "title", "name", "headline", "summary", "text", "path", "url", "from", "sender", "to",
    "received", "date", "modified", "start", "when", "size", "unread", "flagged", "kind", "type", "status",
    "count", "total", "id", "entry_id",
)  # fmt: skip


# --- refs ---------------------------------------------------------------------------------


def short_ref(tool_call_id: str | None) -> str:
    """``chatcmpl-tool-a254a08e1df37916`` → ``@a254a08e1df37916``; anything else keeps its id."""
    rid = tool_call_id or "result"
    if rid.startswith(ID_PREFIX):
        rid = rid[len(ID_PREFIX) :]
    return "@" + rid


def parse_ref(ref: str) -> tuple[list[str], list[int | tuple[int, int]]]:
    """``@a1b2.3.1-4`` → (the ids it may stand for, [3, (1, 4)]). Raises ValueError on a bad path."""
    body = ref.strip().lstrip("@")
    base, _, rest = body.partition(".")
    path: list[int | tuple[int, int]] = []
    for part in rest.split(".") if rest else []:
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if m is None:
            raise ValueError(f"bad section path in {ref!r}: sections are numbers like .3 or ranges like .3-5")
        a = int(m.group(1))
        path.append((a, int(m.group(2))) if m.group(2) else a)
    return ([base, ID_PREFIX + base] if base else []), path


# --- the document -------------------------------------------------------------------------


@dataclass
class Section:
    ref: str
    title: str
    body: str
    start: int = -1  # offsets into the raw text, when the section IS a slice of it (text docs)
    end: int = -1

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class Doc:
    ref: str
    total_chars: int
    shape: str  # "list" | "object" | "text"
    sections: list[Section]
    preamble: str = ""  # a [partial ...] head line the result came with


def parse(text: str, ref: str) -> Doc:
    """Split a result into sections. JSON by structure, everything else by headings/paragraphs."""
    head, body = "", text
    if text.startswith("[partial:"):
        head, _, body = text.partition("\n")
    data = _json(body)
    doc = (
        _json_doc(data, ref, len(text)) if data is not None else _text_doc(body, ref, len(text), len(text) - len(body))
    )
    doc.preamble = head
    return doc


def _json(s: str) -> Any | None:
    s = s.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _json_doc(data: Any, ref: str, total: int) -> Doc:
    envelope: dict[str, Any] = {}
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        key, found = _main_list(data)
        if key is None:
            sections = [
                Section(f"{ref}.{i}", f"{k}: {_preview(v, _LINE_WIDTH - len(k) - 2)}", _compact({k: v}))
                for i, (k, v) in enumerate(data.items(), 1)
            ]
            return Doc(ref, total, "object", sections)
        items = list(found or [])
        envelope = {k: v for k, v in data.items() if k != key}
    else:
        return _text_doc(_scalar(data), ref, total, 0)

    common: dict[str, Any] = {}
    if len(items) >= 2 and all(isinstance(it, dict) for it in items):
        for k in items[0]:
            if not all(k in it for it in items):
                continue
            seen = {json.dumps(it[k], sort_keys=True, ensure_ascii=False) for it in items}
            # Identical on every item: said once in the envelope. Two items may agree by chance,
            # so with only two the value must be long enough to be worth the move.
            if len(seen) == 1 and (len(items) >= 3 or len(next(iter(seen))) >= 12):
                common[k] = items[0][k]
        if common:
            items = [{k: v for k, v in it.items() if k not in common} for it in items]

    sections: list[Section] = []
    if envelope or common:
        lines = []
        if envelope:
            lines.append(_compact(envelope))
        if common:
            lines.append("common to every item:\n" + _compact(common))
        title = _title(envelope) if envelope else "envelope"
        if common:
            title += f"; common: {', '.join(common)}"
        sections.append(Section(f"{ref}.0", title[:_LINE_WIDTH], "\n".join(lines)))
    for i, item in enumerate(items, 1):
        body = _compact(item) if isinstance(item, dict | list) else _scalar(item)
        sections.append(Section(f"{ref}.{i}", _title(item), body))
    return Doc(ref, total, "list", sections)


def _main_list(d: dict[str, Any]) -> tuple[str | None, list[Any] | None]:
    """The list this object is really about: the longest list of at least two items that are
    worth a section each - dicts, or values of some substance. A list of 362 timestamps is not
    it (homelab.get_history, 2026-09-20: the series dict was the content, the axis got the
    sections); such an object is better read key by key."""
    best: tuple[str | None, list[Any] | None] = (None, None)
    best_len = -1
    for k, v in d.items():
        if not (isinstance(v, list) and len(v) >= 2):
            continue
        n = len(json.dumps(v, ensure_ascii=False))
        if not all(isinstance(x, dict) for x in v) and n / len(v) < 40:
            continue
        if n > best_len:
            best, best_len = (k, v), n
    return best


def _text_doc(text: str, ref: str, total: int, offset: int) -> Doc:
    # Segments are lines, except that a line longer than two chunks (minified anything) is cut
    # into chunk-sized pieces so no section can be a single unreadable wall.
    segs: list[tuple[str, int]] = []
    p = 0
    for ln in text.split("\n"):
        if len(ln) > 2 * CHUNK_CHARS:
            segs.extend((ln[k : k + CHUNK_CHARS], p + k) for k in range(0, len(ln), CHUNK_CHARS))
        else:
            segs.append((ln, p))
        p += len(ln) + 1
    heads = [i for i, (ln, _) in enumerate(segs) if _HEADING.match(ln)]
    if len(heads) >= 2:
        bounds = heads if heads[0] == 0 or not any(s.strip() for s, _ in segs[: heads[0]]) else [0, *heads]
    else:
        bounds = _chunk_bounds(segs)
    sections: list[Section] = []
    for j, b in enumerate(bounds):
        e = bounds[j + 1] if j + 1 < len(bounds) else len(segs)
        if e <= b:
            continue
        start = segs[b][1]
        end = segs[e - 1][1] + len(segs[e - 1][0])
        body = text[start:end].strip("\n")
        if not body.strip():
            continue
        m = _HEADING.match(segs[b][0]) if b in heads else None
        title = m.group(2).strip() if m else _first_line(body)
        sections.append(Section(f"{ref}.{len(sections) + 1}", title[:_LINE_WIDTH], body, offset + start, offset + end))
    if not sections:
        sections.append(Section(f"{ref}.1", _first_line(text), text, offset, offset + len(text)))
    return Doc(ref, total, "text", sections)


def _chunk_bounds(segs: list[tuple[str, int]]) -> list[int]:
    """Greedy: a chunk closes at the first blank line once it holds a chunk's worth, or hard at two."""
    bounds = [0]
    size = 0
    for i, (ln, _) in enumerate(segs):
        size += len(ln) + 1
        if i + 1 < len(segs) and ((size >= CHUNK_CHARS and not ln.strip()) or size >= 2 * CHUNK_CHARS):
            bounds.append(i + 1)
            size = 0
    return bounds


def _first_line(text: str) -> str:
    for ln in text.split("\n"):
        if ln.strip():
            return _short(ln, 90)
    return "(blank)"


# --- compact rendering of JSON ------------------------------------------------------------


def _compact(v: Any) -> str:
    return "\n".join(_lines(v, 0))


def _lines(v: Any, indent: int) -> list[str]:
    pad = "  " * indent
    if isinstance(v, dict):
        if not v:
            return [pad + "{}"]
        out: list[str] = []
        for k, x in v.items():
            if isinstance(x, dict | list) and x:
                one = _inline(x)
                if one is not None and len(one) <= _INLINE_WIDTH:
                    out.append(f"{pad}{k}: {one}")
                else:
                    out.append(f"{pad}{k}:")
                    out.extend(_lines(x, indent + 1))
            elif isinstance(x, str) and "\n" in x:
                out.append(f"{pad}{k}: |")
                out.extend(f"{pad}  {ln}" for ln in x.split("\n"))
            else:
                out.append(f"{pad}{k}: {_scalar(x)}")
        return out
    if isinstance(v, list):
        if not v:
            return [pad + "[]"]
        one = _inline(v)
        if one is not None and len(one) <= _INLINE_WIDTH:
            return [pad + one]
        out = []
        for x in v:
            if isinstance(x, dict | list) and x:
                sub = _lines(x, indent + 1)
                out.append(f"{pad}- {sub[0].lstrip()}")
                out.extend(sub[1:])
            else:
                out.append(f"{pad}- {_scalar(x)}")
        return out
    return [pad + _scalar(v)]


def _inline(v: Any) -> str | None:
    """One line for a shallow value, or None when it has depth or multi-line text."""
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if isinstance(x, dict | list):
                if x:
                    return None
                parts.append(f"{k}: {'{}' if isinstance(x, dict) else '[]'}")
            elif isinstance(x, str) and "\n" in x:
                return None
            else:
                parts.append(f"{k}: {_scalar(x)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict | list):
                if x:
                    return None
                parts.append("{}" if isinstance(x, dict) else "[]")
            elif isinstance(x, str) and "\n" in x:
                return None
            else:
                parts.append(_scalar(x))
        return "[" + ", ".join(parts) + "]"
    return _scalar(v)


def _scalar(x: Any) -> str:
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)


def _short(v: Any, width: int) -> str:
    """One line, whitespace folded, opaque ids shortened to their tail, clipped to ``width``."""
    s = " ".join(_scalar(v).split())
    s = _OPAQUE.sub(lambda m: "…" + m.group(0)[-8:], s)
    return s if len(s) <= width else s[: max(1, width - 1)] + "…"


def _preview(v: Any, width: int) -> str:
    if isinstance(v, dict):
        return _title(v, width)
    if isinstance(v, list):
        return f"[{len(v)} items] " + _short(v, max(10, width - 12))
    return _short(v, width)


def _title(item: Any, width: int = _LINE_WIDTH) -> str:
    """The fields that say what an item IS, in a fixed preference order, as far as one line holds."""
    if isinstance(item, dict):
        keys = [k for k in _TITLE_KEYS if k in item] + [k for k in item if k not in _TITLE_KEYS]
        parts: list[str] = []
        used = 0
        for k in keys:
            v = item[k]
            if isinstance(v, dict | list):
                if not v:
                    continue
                s = f"{k}: [{len(v)}]" if isinstance(v, list) else f"{k}: {{{len(v)} fields}}"
            elif v is None or v == "":
                continue
            else:
                s = f"{k}: {_short(v, 60)}"
            if used + len(s) + 2 > width:
                if not parts:
                    parts.append(s[: width - 1] + "…")
                break
            parts.append(s)
            used += len(s) + 2
        return ", ".join(parts) or "{}"
    return _preview(item, width)


# --- rendering under a limit --------------------------------------------------------------


def marker(ref: str, total: int, n: int, shown: str) -> str:
    """The last line of every view: what this is, how much of it is here, how to get the rest."""
    return (
        f"[{ref}: {total:,} chars in {n} section{'s' if n != 1 else ''}; shown: {shown}. "
        f'jarvis.result_read(ref="{ref}.N") reads section N, "{ref}.N-M" a range; '
        f'jarvis.result_search(ref="{ref}", pattern=...) finds inside]'
    )


def _kb(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def outline_line(s: Section) -> str:
    size = f"[{_kb(s.size)}] " if s.size >= 2000 else ""
    return f"{s.ref} {size}{s.title}"


def render(doc: Doc, limit: int) -> str:
    """The view of ``doc`` that fits ``limit`` characters (give or take a line).

    Whole and compact when that fits (a JSON list a third shorter than its minified self, ids
    and all); else the outline, all of it when it fits, else its first lines and a note naming
    the sections not shown; else - the aged sizes - the sections grouped into ranges, one line
    each, so even 1,200 characters say what a 300-folder tree contained.
    """
    n = len(doc.sections)
    ref = doc.ref
    if n == 0:
        return (doc.preamble + "\n" if doc.preamble else "") + "(nothing)"
    tail = marker(ref, doc.total_chars, n, "outline")
    room = limit - len(tail) - len(doc.preamble) - 2
    if doc.shape != "text":
        whole = "\n".join(f"{s.ref}\n{s.body}" for s in doc.sections)
        head = f"{ref}: {n} sections, whole:"
        if len(whole) + len(head) + 1 <= room:
            return _assemble(doc, [head, whole], f"whole ({doc.shape}, compact)")
    head = f"{ref}: {n} sections ({doc.total_chars:,} chars) — outline:"
    lines = [outline_line(s) for s in doc.sections]
    room -= len(head) + 1
    fit = _fit_lines(lines, room)
    if fit >= n:
        return _assemble(doc, [head, *lines], "outline")
    if fit >= _TAIL_MIN_LINES and fit >= n // 3:
        # Enough of it fits to show real titles; the rest is named as a range.
        k = fit - 1
        first_left, last = _num(doc.sections[k]), _num(doc.sections[-1])
        note = f"… +{n - k} more sections: {ref}.{first_left}-{last}"
        return _assemble(doc, [head, *lines[:k], note], f"the first {k} of {n} sections")
    # Grouped: only a small part would fit, so every section is covered by a range line instead -
    # a memory of the whole (the aged view of a 300-folder tree), not of its first ten entries.
    groups = max(2, min(n, room // (len(ref) + 125)))
    while True:
        per = math.ceil(n / groups)
        grouped: list[str] = []
        for a in range(0, n, per):
            chunk = doc.sections[a : a + per]
            first, last = chunk[0], chunk[-1]
            size = sum(s.size for s in chunk)
            span = f"{first.ref}-{_num(last)}" if len(chunk) > 1 else first.ref
            text = first.title if len(chunk) == 1 else f"{_short(first.title, 50)} … {_short(last.title, 50)}"
            grouped.append(f"{span} [{_kb(size)}] {text}")
        if _fit_lines(grouped, room) >= len(grouped) or groups <= 2:
            break
        groups -= 1  # fewer, wider ranges until every line fits: coverage over detail
    return _assemble(doc, [head, *grouped], "grouped outline")


def _num(s: Section) -> str:
    """A section's own number, as its ref says (.0 is the envelope when there is one)."""
    return s.ref.rsplit(".", 1)[-1]


def _fit_lines(lines: list[str], room: int) -> int:
    used = 0
    for i, ln in enumerate(lines):
        used += len(ln) + 1
        if used > room:
            return i
    return len(lines)


def _assemble(doc: Doc, parts: list[str], shown: str) -> str:
    body = "\n".join(parts)
    if doc.preamble:
        body = doc.preamble + "\n" + body
    return body + "\n" + marker(doc.ref, doc.total_chars, len(doc.sections), shown)


# --- messages -----------------------------------------------------------------------------

# Views of persisted messages, by (message id, size, limit): earlier turns are re-viewed on
# every step of every run, and parsing a 100k JSON result each time would cost more than the
# tokens it saves. Messages never change once stored, so the key is safe. Bounded, FIFO.
_CACHE: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_CACHE_MAX = 2_000


def view_of(message: Message, limit: int, *, min_len: int = 0) -> Message:
    """``message`` as the model should see it within ``limit`` chars. Idempotent: a view can only
    shrink further, never be re-parsed as a document; a short message is returned as is."""
    content = message.content
    m = _MARKER.search(content)
    if m is not None:
        if len(content) <= limit:
            return message
        return message.model_copy(update={"content": _shrink(content, m, limit)})
    if len(content) <= max(limit, min_len):
        return message
    # The content hash guards the key: ids repeat across tests and across a re-imported V1
    # conversation, and a wrong view served from cache would be a silent lie.
    key = (f"{message.id or ''}|{message.tool_call_id or ''}|{hash(content)}", len(content), limit)
    rendered = _CACHE.get(key)
    if rendered is None:
        rendered = render(parse(content, short_ref(message.tool_call_id)), limit)
        _CACHE[key] = rendered
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return message.model_copy(update={"content": rendered})


def _shrink(content: str, m: re.Match[str], limit: int) -> str:
    """A smaller view of a view: its first lines, the same marker with an honest 'shown'."""
    ref, total, n = m.group(1), int(m.group(2).replace(",", "")), int(m.group(3))
    lines = content[: m.start()].split("\n")
    tail = marker(ref, total, n, "outline")
    keep = max(1, _fit_lines(lines, limit - len(tail) - 60))
    kept = lines[:keep]
    if keep < len(lines):
        kept.append(f"… +{len(lines) - keep} more lines of this view")
    return "\n".join(kept) + "\n" + marker(ref, total, n, f"first {keep} lines of the view")


def admit(message: Message, admit_chars: int) -> Message:
    """A tool result as it enters the prompt: whole up to ``admit_chars``, else its view."""
    return view_of(message, admit_chars)


def aged(message: Message) -> Message:
    """A tool result from an earlier step or turn: its small view, unless it is small already."""
    return view_of(message, AGED_VIEW_CHARS, min_len=VIEW_MIN_CHARS)


def is_view(message: Message) -> bool:
    return message.role is Role.TOOL and _MARKER.search(message.content) is not None


# --- reading back -------------------------------------------------------------------------


def read(text: str, base_ref: str, path: list[int | tuple[int, int]], limit: int) -> str:
    """What ``jarvis.result_read`` returns for a ref: the whole (viewed if long), one section
    (viewed if long, with sub-refs), or a range of sections up to the limit."""
    doc = parse(text, base_ref)
    if not path:
        return text if len(text) <= limit else render(doc, limit)
    for step in path[:-1]:
        if isinstance(step, tuple):
            raise ValueError(f"a range ({step[0]}-{step[1]}) can only be the last part of a ref")
        doc = _sub_doc(doc, step)
    last = path[-1]
    if isinstance(last, tuple):
        a, b = last
        lo, hi = _section_index(doc, a), _section_index(doc, b)
        if hi < lo:
            lo, hi = hi, lo
        out: list[str] = []
        used = 0
        for s in doc.sections[lo : hi + 1]:
            piece = f"{s.ref}\n{s.body}"
            if out and used + len(piece) + 1 > limit:
                out.append(
                    f"[stopped before {s.ref} to stay under {limit:,} chars; continue with {s.ref}-{doc.sections[hi].ref.rsplit('.', 1)[-1]}]"
                )
                break
            out.append(piece)
            used += len(piece) + 1
        return "\n".join(out)
    s = doc.sections[_section_index(doc, last)]
    if len(s.body) <= limit:
        return f"{s.ref} ({len(s.body):,} chars):\n{s.body}"
    return render(parse(s.body, s.ref), limit)


def _sub_doc(doc: Doc, index: int) -> Doc:
    s = doc.sections[_section_index(doc, index)]
    return parse(s.body, s.ref)


def _section_index(doc: Doc, number: int) -> int:
    """Sections are numbered as their refs say (.0 is the envelope when there is one)."""
    for i, s in enumerate(doc.sections):
        if s.ref.rsplit(".", 1)[-1] == str(number):
            return i
    first = doc.sections[0].ref.rsplit(".", 1)[-1] if doc.sections else "1"
    last = doc.sections[-1].ref.rsplit(".", 1)[-1] if doc.sections else "0"
    raise IndexError(f"{doc.ref} has sections {first}-{last}, not {number}")


def locate(doc: Doc, offset: int, needle: str) -> Section | None:
    """The section a match falls in: by offset for text docs, by content for JSON docs."""
    for s in doc.sections:
        if s.start >= 0 and s.start <= offset < s.end:
            return s
    low = needle.lower()
    if not low.strip():
        return None
    for s in doc.sections:
        if low in s.body.lower():
            return s
    return None

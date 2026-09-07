"""OneNote (desktop) as host tools: read the hierarchy, read a page, search, create, append.

Ported from V1's 1,172-line `tools/onenote`, keeping only what the model actually uses and the
two hard-won facts:

* Office Click-to-Run leaves the OneNote type library unregistered, so IDispatch fails; the
  connection goes through comtypes' vtable interface (`GetModule((TYPELIB_GUID, 1, 1))`).
* `UpdatePageContent` silently truncates a large payload, so content is written in batches.

Everything runs on the COM worker thread, like Outlook.
"""

from __future__ import annotations

import html
import re
import sys
from collections.abc import Callable
from typing import Any
from xml.etree import ElementTree

from jarvis_host.com import ComWorker

ONE_NS = "http://schemas.microsoft.com/office/onenote/2013/onenote"
NS = f"{{{ONE_NS}}}"
TYPELIB_GUID = "{0EA692EE-BB50-4E3C-AEF0-356D91732725}"
APP_CLSID = "{DC67E480-C3CB-49F8-8232-60B0C2056C8E}"

HS_SECTIONS = 3
HS_PAGES = 4
NPS_BLANK_WITH_TITLE = 1
PI_BASIC = 0
# XMLSchema enum: 0 = xs2007, 1 = xs2010, 2 = xs2013. It must match ONE_NS above or the parser
# finds nothing; FindPages takes it as an INT (a string there is a live COM TypeError).
XS_2013 = 2

# One UpdatePageContent call must stay small (V1 measured silent truncation above ~6 kB).
CHUNK_CHARS = 6000
MAX_PAGE_CHARS = 20_000


class OneNoteError(RuntimeError):
    """Anything OneNote refuses to do, phrased for the model."""


class NotFound(OneNoteError):
    pass


def onenote_dispatch() -> Any:
    """Create the OneNote Application object on the COM thread (vtable, not IDispatch)."""
    if sys.platform != "win32":
        raise OneNoteError("OneNote COM is only available on Windows")
    import comtypes.client  # pyright: ignore[reportMissingImports]

    # The COM worker thread already called CoInitialize (jarvis_host/com.py); comtypes reuses
    # that apartment, so this only builds the interface class and the object.
    try:
        module = comtypes.client.GetModule((TYPELIB_GUID, 1, 1))
    except Exception as exc:  # the type library is missing → OneNote desktop is not installed
        raise OneNoteError(f"OneNote type library unavailable ({exc}); is OneNote desktop installed?") from exc
    return comtypes.client.CreateObject(APP_CLSID, interface=module.IApplication)


# --- markdown → OneNote XML -------------------------------------------------------------------


def _cdata(text: str) -> str:
    """CDATA cannot hold ']]>'; split the section instead of dropping the text."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def _inline(text: str) -> str:
    """Inline markdown → the HTML subset OneNote accepts inside <one:T>."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r'<span style="font-weight:bold">\1</span>', out)
    out = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r'<span style="font-style:italic">\1</span>', out)
    out = re.sub(r"~~(.+?)~~", r'<span style="text-decoration:line-through">\1</span>', out)
    out = re.sub(r"`([^`]+?)`", r'<span style="font-family:Consolas">\1</span>', out)
    return out


def _oe(inner: str) -> str:
    return f"<one:OE><one:T><![CDATA[{inner}]]></one:T></one:OE>"


def _table_oe(rows: list[list[str]]) -> str:
    width = max((len(r) for r in rows), default=0)
    if not width:
        return ""
    cols = "".join(f'<one:Column index="{i}" width="120" />' for i in range(width))
    body = []
    for row in rows:
        cells = "".join(
            f"<one:Cell><one:OEChildren>{_oe(_cdata(_inline(row[i] if i < len(row) else '')))}</one:OEChildren></one:Cell>"
            for i in range(width)
        )
        body.append(f"<one:Row>{cells}</one:Row>")
    return (
        f'<one:OE><one:Table bordersVisible="true"><one:Columns>{cols}</one:Columns>'
        f"{''.join(body)}</one:Table></one:OE>"
    )


def markdown_to_oe(content: str) -> list[str]:
    """Markdown → a list of OneNote OE fragments (headings, lists, quotes, code, tables, text)."""
    parts: list[str] = []
    lines = (content or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):  # fenced code block, verbatim
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            for c in code:
                escaped = html.escape(c, quote=False).replace(" ", "&nbsp;")
                parts.append(_oe(_cdata(f'<span style="font-family:Consolas;background:#F5F5F5">{escaped}</span>')))
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip()):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not re.fullmatch(r"[\s:|-]+", lines[i].strip().strip("|")):
                    rows.append(cells)
                i += 1
            parts.append(_table_oe(rows))
            continue
        i += 1
        if not stripped:
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            size = {1: "16pt", 2: "14pt", 3: "12pt", 4: "11pt"}[len(heading.group(1))]
            body = _inline(heading.group(2))
            parts.append(_oe(_cdata(f'<span style="font-weight:bold;font-size:{size}">{body}</span>')))
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            parts.append(
                f'<one:OE><one:List><one:Bullet bullet="2" /></one:List>'
                f"<one:T><![CDATA[{_cdata(_inline(bullet.group(1)))}]]></one:T></one:OE>"
            )
            continue
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            parts.append(
                f'<one:OE><one:List><one:Number numberSequence="1" /></one:List>'
                f"<one:T><![CDATA[{_cdata(_inline(numbered.group(1)))}]]></one:T></one:OE>"
            )
            continue
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            body = _inline(quote.group(1))
            parts.append(_oe(_cdata(f'<span style="font-style:italic;color:#666666">| {body}</span>')))
            continue
        parts.append(_oe(_cdata(_inline(stripped))))
    return [p for p in parts if p]


def batch(parts: list[str], max_chars: int = CHUNK_CHARS) -> list[list[str]]:
    """Group OE fragments so no single UpdatePageContent payload can be truncated."""
    out: list[list[str]] = []
    current: list[str] = []
    size = 0
    for part in parts:
        if current and size + len(part) > max_chars:
            out.append(current)
            current, size = [], 0
        current.append(part)
        size += len(part)
    if current:
        out.append(current)
    return out or [[]]


def page_text(xml: str, max_chars: int = MAX_PAGE_CHARS) -> tuple[str, bool]:
    """Page XML → plain text, in document order. Returns (text, truncated)."""
    root = ElementTree.fromstring(xml)
    lines: list[str] = []
    for node in root.iter(f"{NS}T"):
        raw = node.text or ""
        raw = re.sub(r"<br\s*/?>", "\n", raw)
        raw = re.sub(r"<[^>]+>", "", raw)
        text = html.unescape(raw).strip()
        if text:
            lines.append(text)
    joined = "\n".join(lines)
    return (joined[:max_chars], True) if len(joined) > max_chars else (joined, False)


# --- the backend (COM thread) ------------------------------------------------------------------


class OneNoteBackend:
    """Synchronous OneNote operations. Every method must run on the COM thread."""

    def __init__(self, dispatch: Callable[[], Any] = onenote_dispatch) -> None:
        self._dispatch = dispatch
        self._app: Any = None

    def _session(self) -> Any:
        if self._app is None:
            self._app = self._dispatch()
        return self._app

    def reset(self) -> None:
        self._app = None

    # -- hierarchy ---------------------------------------------------------------------------

    def _hierarchy(self, start_id: str = "", scope: int = HS_PAGES) -> ElementTree.Element:
        try:
            xml = self._session().GetHierarchy(start_id, scope)
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"reading the OneNote hierarchy failed: {exc}") from exc
        return ElementTree.fromstring(xml)

    def tree(self, with_pages: bool = True) -> dict[str, Any]:
        """Notebooks → sections (through section groups) → pages, each with its id and path."""
        root = self._hierarchy("", HS_PAGES if with_pages else HS_SECTIONS)
        notebooks: list[dict[str, Any]] = []
        for nb in root.findall(f"{NS}Notebook"):
            nb_name = nb.get("name", "")
            sections: list[dict[str, Any]] = []
            for sec, group in _iter_sections(nb):
                sec_name = sec.get("name", "")
                path = "/".join(p for p in (nb_name, group, sec_name) if p)
                pages = [
                    {"id": p.get("ID", ""), "name": p.get("name", ""), "path": f"{path}/{p.get('name', '')}"}
                    for p in sec.findall(f"{NS}Page")
                ]
                sections.append({"id": sec.get("ID", ""), "name": sec_name, "path": path, "pages": pages})
            notebooks.append({"id": nb.get("ID", ""), "name": nb_name, "sections": sections})
        return {"notebooks": notebooks}

    def _resolve(self, path: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """'Notebook/Section[/Group]/Page' → (section, page); either may be None."""
        parts = [p.strip() for p in (path or "").strip("/").split("/") if p.strip()]
        if not parts:
            raise OneNoteError("path is empty (expected 'Notebook/Section' or 'Notebook/Section/Page')")
        tree = self.tree(with_pages=True)
        wanted = [p.lower() for p in parts]
        for nb in tree["notebooks"]:
            if nb["name"].lower() != wanted[0]:
                continue
            for sec in nb["sections"]:
                sec_parts = [p.lower() for p in sec["path"].split("/")]
                if sec_parts == wanted[: len(sec_parts)]:
                    rest = wanted[len(sec_parts) :]
                    if not rest:
                        return sec, None
                    page = next((p for p in sec["pages"] if p["name"].lower() == rest[0]), None)
                    if page is not None:
                        return sec, page
        return None, None

    def _page_id(self, page: str) -> str:
        """A page id ('{...}{1}{E19...}') or a path. Returns the id."""
        if page.startswith("{"):
            return page
        _, found = self._resolve(page)
        if found is None:
            raise NotFound(f"page {page!r} not found (use onenote_tree to see the paths)")
        return found["id"]

    # -- read --------------------------------------------------------------------------------

    def read(self, page: str, max_chars: int = MAX_PAGE_CHARS) -> dict[str, Any]:
        page_id = self._page_id(page)
        try:
            xml = self._session().GetPageContent(page_id, PI_BASIC)
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"reading the page failed: {exc}") from exc
        text, truncated = page_text(xml, max_chars)
        title = text.split("\n", 1)[0] if text else ""
        return {"page_id": page_id, "title": title, "text": text, "truncated": truncated}

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Pages whose title or text matches, through OneNote's own index (FindPages)."""
        q = (query or "").strip()
        if not q:
            raise OneNoteError("query is empty")
        # IApplication::FindPages(startNodeID, searchString, [out] xml, unindexed, display, schema):
        # the [out] parameter is the return value under comtypes, so five arguments go in and the
        # last one is the schema enum, not a string.
        try:
            xml = self._session().FindPages("", q, False, False, XS_2013)
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"searching OneNote failed: {exc}") from exc
        root = ElementTree.fromstring(xml)
        hits: list[dict[str, str]] = []
        for nb in root.findall(f"{NS}Notebook"):
            for sec, group in _iter_sections(nb):
                for page in sec.findall(f"{NS}Page"):
                    parts = (nb.get("name", ""), group, sec.get("name", ""), page.get("name", ""))
                    hits.append({"id": page.get("ID", ""), "path": "/".join(p for p in parts if p)})
                    if len(hits) >= max(1, min(limit, 100)):
                        return {"query": q, "pages": hits}
        return {"query": q, "pages": hits}

    # -- write -------------------------------------------------------------------------------

    def create(self, section: str, title: str, content: str = "") -> dict[str, Any]:
        if not title.strip():
            raise OneNoteError("title is empty")
        found, page = self._resolve(section)
        if found is None:
            raise NotFound(f"section {section!r} not found (use onenote_tree to see the paths)")
        if page is not None:
            raise OneNoteError(f"{section!r} is a page, not a section; pass the section path")
        app = self._session()
        try:
            page_id = app.CreateNewPage(found["id"], NPS_BLANK_WITH_TITLE)
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"creating the page failed: {exc}") from exc
        batches = batch(markdown_to_oe(content)) if content.strip() else [[]]
        first = _outline(batches[0]) if batches[0] else ""
        xml = (
            f'<?xml version="1.0"?><one:Page xmlns:one="{ONE_NS}" ID="{page_id}">'
            f"<one:Title><one:OE><one:T><![CDATA[{_cdata(html.escape(title, quote=False))}]]></one:T></one:OE></one:Title>"
            f"{first}</one:Page>"
        )
        self._update(xml)
        for rest in batches[1:]:
            self._update(
                f'<?xml version="1.0"?><one:Page xmlns:one="{ONE_NS}" ID="{page_id}">{_outline(rest)}</one:Page>'
            )
        return {"page_id": page_id, "section": found["path"], "title": title, "chunks": len(batches)}

    def append(self, page: str, content: str) -> dict[str, Any]:
        if not content.strip():
            raise OneNoteError("nothing to append")
        page_id = self._page_id(page)
        batches = batch(markdown_to_oe(content))
        for part in batches:
            self._update(
                f'<?xml version="1.0"?><one:Page xmlns:one="{ONE_NS}" ID="{page_id}">{_outline(part)}</one:Page>'
            )
        return {"page_id": page_id, "appended_chars": len(content), "chunks": len(batches)}

    def move(self, page: str, section: str) -> dict[str, Any]:
        """Move a page into another section (MergeToSection). The page keeps its content and id."""
        page_id = self._page_id(page)
        found, target_page = self._resolve(section)
        if found is None:
            raise NotFound(f"section {section!r} not found (use onenote_tree to see the paths)")
        if target_page is not None:
            raise OneNoteError(f"{section!r} is a page, not a section; pass the section path")
        app = self._session()
        # MergeToSection gained a third argument (delete the source page) in later builds; call
        # the long form first and fall back rather than guessing the installed version.
        try:
            try:
                app.MergeToSection(page_id, found["id"], True)
            except TypeError:
                app.MergeToSection(page_id, found["id"])
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"moving the page failed: {exc}") from exc
        return {"page_id": page_id, "section": found["path"], "moved": True}

    def _update(self, xml: str) -> None:
        try:
            self._session().UpdatePageContent(xml, 0)
        except Exception as exc:
            self.reset()
            raise OneNoteError(f"writing to the page failed: {exc}") from exc


def _iter_sections(notebook: ElementTree.Element) -> list[tuple[ElementTree.Element, str]]:
    """Sections directly under a notebook, plus those inside section groups (group name kept)."""
    out = [(sec, "") for sec in notebook.findall(f"{NS}Section")]
    for group in notebook.findall(f"{NS}SectionGroup"):
        name = group.get("name", "")
        if name.startswith("OneNote_RecycleBin"):
            continue
        out.extend((sec, name) for sec in group.findall(f"{NS}Section"))
    return out


def _outline(parts: list[str]) -> str:
    return f"<one:Outline><one:OEChildren>{''.join(parts)}</one:OEChildren></one:Outline>"


class OneNoteService:
    """``OneNoteBackend`` marshalled through the COM worker with per-call deadlines."""

    TIMEOUTS: dict[str, float] = {"tree": 90, "read": 60, "search": 90, "create": 90, "append": 90, "move": 90}

    def __init__(self, backend: OneNoteBackend, worker: ComWorker) -> None:
        self.backend = backend
        self.worker = worker

    async def call(self, name: str, *args: Any) -> Any:
        fn = getattr(self.backend, name)
        return await self.worker.acall(
            lambda: fn(*args), label=f"onenote.{name}", timeout_s=self.TIMEOUTS.get(name, 60.0)
        )

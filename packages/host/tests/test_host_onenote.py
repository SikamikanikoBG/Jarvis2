"""OneNote host tools against a fake COM application (no OneNote, no Windows)."""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from jarvis_host.onenote import ONE_NS, NotFound, OneNoteBackend, OneNoteError, batch, markdown_to_oe, page_text

HIERARCHY = f"""<?xml version="1.0"?>
<one:Notebooks xmlns:one="{ONE_NS}">
  <one:Notebook name="Work" ID="{{nb-work}}">
    <one:Section name="Meetings" ID="{{sec-meetings}}">
      <one:Page name="Weekly review" ID="{{page-weekly}}" />
      <one:Page name="Retro" ID="{{page-retro}}" />
    </one:Section>
    <one:SectionGroup name="Projects" ID="{{grp-projects}}">
      <one:Section name="Jarvis" ID="{{sec-jarvis}}">
        <one:Page name="Design" ID="{{page-design}}" />
      </one:Section>
    </one:SectionGroup>
    <one:SectionGroup name="OneNote_RecycleBin" ID="{{grp-bin}}">
      <one:Section name="Deleted Pages" ID="{{sec-bin}}"><one:Page name="Old" ID="{{page-old}}" /></one:Section>
    </one:SectionGroup>
  </one:Notebook>
</one:Notebooks>"""

PAGE = f"""<?xml version="1.0"?>
<one:Page xmlns:one="{ONE_NS}" ID="{{page-weekly}}">
  <one:Title><one:OE><one:T><![CDATA[Weekly review]]></one:T></one:OE></one:Title>
  <one:Outline><one:OEChildren>
    <one:OE><one:T><![CDATA[<span style="font-weight:bold">Decisions</span>]]></one:T></one:OE>
    <one:OE><one:T><![CDATA[Ship V2 &amp; keep V1 running<br/>Second line]]></one:T></one:OE>
    <one:OE><one:T><![CDATA[]]></one:T></one:OE>
  </one:OEChildren></one:Outline>
</one:Page>"""


class FakeApp:
    """Records what OneNote would be asked to do; returns the fixtures above."""

    def __init__(self) -> None:
        self.updates: list[str] = []
        self.created: list[tuple[str, int]] = []
        self.searches: list[str] = []
        self.merges: list[tuple[str, str]] = []
        self.fail_update = False

    def GetHierarchy(self, start_id: str, scope: int) -> str:
        return HIERARCHY

    def GetPageContent(self, page_id: str, info: int) -> str:
        if page_id != "{page-weekly}":
            raise RuntimeError(f"no such page: {page_id}")
        return PAGE

    def FindPages(self, start: str, query: str, unindexed: bool, display: bool, schema: int) -> str:
        # Exactly the arity AND the types the real interface has (the schema is an enum): both
        # were live COM errors before the fake insisted on them.
        assert isinstance(schema, int) and not isinstance(schema, bool), "schema must be the XMLSchema enum"
        self.searches.append(query)
        return HIERARCHY

    def CreateNewPage(self, section_id: str, style: int) -> str:
        self.created.append((section_id, style))
        return "{page-new}"

    def UpdatePageContent(self, xml: str, date: int) -> None:
        if self.fail_update:
            raise RuntimeError("The object is in use")
        self.updates.append(xml)

    def MergeToSection(self, source_id: str, section_id: str) -> None:
        # This build has the TWO-argument form: the three-argument call must fall back, not fail.
        self.merges.append((source_id, section_id))


@pytest.fixture
def app() -> FakeApp:
    return FakeApp()


@pytest.fixture
def backend(app: FakeApp) -> OneNoteBackend:
    return OneNoteBackend(lambda: app)


def test_tree_flattens_section_groups_and_skips_the_recycle_bin(backend: OneNoteBackend):
    tree = backend.tree()
    nb = tree["notebooks"][0]
    assert nb["name"] == "Work"
    assert [s["path"] for s in nb["sections"]] == ["Work/Meetings", "Work/Projects/Jarvis"]
    assert [p["path"] for p in nb["sections"][0]["pages"]] == ["Work/Meetings/Weekly review", "Work/Meetings/Retro"]
    assert nb["sections"][1]["pages"][0]["id"] == "{page-design}"


def test_read_by_path_and_by_id_strips_markup_and_reports_truncation(backend: OneNoteBackend):
    res = backend.read("Work/Meetings/Weekly review")
    assert res["page_id"] == "{page-weekly}" and res["title"] == "Weekly review"
    # HTML gone, entities decoded, <br/> became a newline, empty blocks dropped.
    assert res["text"] == "Weekly review\nDecisions\nShip V2 & keep V1 running\nSecond line"
    assert res["truncated"] is False
    assert backend.read("{page-weekly}")["text"] == res["text"]
    short = backend.read("{page-weekly}", 10)
    assert short["truncated"] is True and len(short["text"]) == 10
    with pytest.raises(NotFound):
        backend.read("Work/Meetings/Nope")


def test_search_returns_paths_and_refuses_an_empty_query(backend: OneNoteBackend, app: FakeApp):
    res = backend.search("budget", limit=2)
    assert app.searches == ["budget"] and res["query"] == "budget"
    assert [h["path"] for h in res["pages"]] == ["Work/Meetings/Weekly review", "Work/Meetings/Retro"]
    with pytest.raises(OneNoteError, match="query is empty"):
        backend.search("  ")


def test_create_page_writes_title_then_content_and_names_the_section(backend: OneNoteBackend, app: FakeApp):
    res = backend.create("Work/Projects/Jarvis", "Design notes", "# Heading\n- one\n- two")
    assert app.created == [("{sec-jarvis}", 1)]
    assert res["page_id"] == "{page-new}" and res["section"] == "Work/Projects/Jarvis" and res["chunks"] == 1
    xml = app.updates[0]
    assert "<one:Title>" in xml and "Design notes" in xml
    assert 'font-weight:bold;font-size:16pt">Heading' in xml and xml.count('<one:Bullet bullet="2" />') == 2
    # A page path where a section is expected is refused, not silently written somewhere.
    with pytest.raises(OneNoteError, match="is a page, not a section"):
        backend.create("Work/Meetings/Retro", "X", "")
    with pytest.raises(NotFound):
        backend.create("Work/Nope", "X", "")
    with pytest.raises(OneNoteError, match="title is empty"):
        backend.create("Work/Meetings", "   ", "")


def test_long_content_is_written_in_chunks_so_onenote_cannot_truncate_it(backend: OneNoteBackend, app: FakeApp):
    body = "\n".join(f"line {i} " + "x" * 200 for i in range(120))
    res = backend.create("Work/Meetings", "Big", body)
    assert res["chunks"] > 1 and len(app.updates) == res["chunks"]
    assert all(len(u) < 12_000 for u in app.updates)
    # Every line survived; nothing was dropped between the chunks.
    assert sum(u.count("line ") for u in app.updates) == 120


def test_append_adds_to_an_existing_page_and_refuses_empty_content(backend: OneNoteBackend, app: FakeApp):
    res = backend.append("Work/Meetings/Weekly review", "- follow up with Rumen")
    assert res["page_id"] == "{page-weekly}" and res["chunks"] == 1
    assert 'ID="{page-weekly}"' in app.updates[0] and "follow up with Rumen" in app.updates[0]
    assert "<one:Title>" not in app.updates[0]  # the title is left alone
    with pytest.raises(OneNoteError, match="nothing to append"):
        backend.append("{page-weekly}", "   ")


def test_move_falls_back_to_the_two_argument_merge_and_refuses_a_page_target(backend: OneNoteBackend, app: FakeApp):
    res = backend.move("Work/Meetings/Retro", "Work/Projects/Jarvis")
    assert app.merges == [("{page-retro}", "{sec-jarvis}")]
    assert res == {"page_id": "{page-retro}", "section": "Work/Projects/Jarvis", "moved": True}
    with pytest.raises(OneNoteError, match="is a page, not a section"):
        backend.move("{page-retro}", "Work/Meetings/Retro")
    with pytest.raises(NotFound):
        backend.move("{page-retro}", "Work/Nope")


def test_a_com_failure_is_reported_and_the_session_is_dropped(backend: OneNoteBackend, app: FakeApp):
    app.fail_update = True
    with pytest.raises(OneNoteError, match="writing to the page failed"):
        backend.append("{page-weekly}", "x")
    assert backend._app is None  # next call reconnects


def test_markdown_conversion_covers_the_blocks_a_report_uses():
    oe = "".join(
        markdown_to_oe(
            "## Status\ntext with **bold** and `code`\n1. first\n> quoted\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n```\nx = 1\n```"
        )
    )
    assert 'font-size:14pt">Status' in oe
    assert 'font-weight:bold">bold' in oe and 'font-family:Consolas">code' in oe
    assert '<one:Number numberSequence="1" />' in oe
    assert "| quoted" in oe
    assert '<one:Table bordersVisible="true">' in oe and oe.count("<one:Row>") == 2
    assert "x&nbsp;=&nbsp;1" in oe  # code keeps its spacing
    # A ']]>' in the text cannot break out of the CDATA section: the '>' is escaped first, so the
    # fragment still parses and the text survives.
    fragment = "".join(markdown_to_oe("danger ]]> here"))
    assert "]]&gt;" in fragment
    parsed = ElementTree.fromstring(f'<one:Wrap xmlns:one="{ONE_NS}">{fragment}</one:Wrap>')
    assert (parsed.find(f"{{{ONE_NS}}}OE/{{{ONE_NS}}}T").text or "") == "danger ]]&gt; here"


def test_batching_groups_parts_without_losing_any():
    parts = [f"<one:OE>{i}</one:OE>" for i in range(10)]
    groups = batch(parts, max_chars=40)
    assert sum(len(g) for g in groups) == 10 and len(groups) > 1
    assert batch([]) == [[]]


def test_page_text_handles_a_page_with_no_text():
    text, truncated = page_text(f'<one:Page xmlns:one="{ONE_NS}" ID="x" />')
    assert text == "" and truncated is False

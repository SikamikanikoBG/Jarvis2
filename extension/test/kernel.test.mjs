// The page kernel against a DOM.
//
// Scenarios ported from V1's tests/test_browser_panel_kernel.py and
// tests/test_page_kernel_real_dom.py where they are pure DOM scenarios (no
// layout, no real scrolling, no timing). Each fixture is shaped like the page
// the behaviour failed on:
//   * a conversation list whose rows carry a select-checkbox <label> sharing
//     the row's text (LinkedIn — clicking a name toggled the checkbox)
//   * a composer that only enables Send on an input event with a real inputType
//     (every React/Draft/Quill editor — textContent writes were silent no-ops)
//   * a heading that merely contains the searched text (clicked, did nothing,
//     reported success)
// plus the V2 additions: outline and text formatting, truncation/offset, and
// the model-facing `text` of every result.

import { test } from "node:test";
import assert from "node:assert/strict";
import { page, KERNEL_SRC } from "./dom.mjs";

// ── LinkedIn-shaped conversation list ────────────────────────────────────

const CONVERSATIONS = `
<main>
  <ul>
    <li>
      <label for="cb1" aria-label="Select conversation with Anastasios Kesgiropoulos">
        <input id="cb1" type="checkbox">
      </label>
      <a href="/messaging/thread/2-abc" id="thread-a">
        <span>Anastasios Kesgiropoulos</span>
        <span>Hi Arsen, I saw your post about the homelab</span>
      </a>
    </li>
    <li>
      <label for="cb2" aria-label="Select conversation with Gloria Petrova">
        <input id="cb2" type="checkbox">
      </label>
      <a href="/messaging/thread/2-def" id="thread-b">
        <span>Gloria Petrova</span><span>Are you free Thursday?</span>
      </a>
    </li>
  </ul>
</main>`;

function conversations() {
  const p = page(CONVERSATIONS, { title: "Messaging" });
  p.win.__opened = null;
  for (const a of p.doc.querySelectorAll("a[id^=thread]")) {
    a.addEventListener("click", (e) => { e.preventDefault(); p.win.__opened = a.id; });
  }
  return p;
}

test("the label penalty stays larger than the clickable bonus (the LinkedIn fix)", () => {
  const { op } = page("<p>x</p>");
  const t = op("__tuning");
  assert.ok(t.LABEL_PENALTY > t.CLICKABLE_BONUS, JSON.stringify(t));
});

test("clicking a name opens the thread, not the row's checkbox", () => {
  const p = conversations();
  const res = p.op("click", { selector: "Anastasios Kesgiropoulos" });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.win.__opened, "thread-a");
  assert.equal(p.$("#cb1").checked, false);
  assert.match(res.text, /^Clicked @e\d+ a#thread-a «Anastasios/);
});

test("a real CSS selector still wins outright", () => {
  const p = conversations();
  assert.equal(p.op("click", { selector: "#thread-b" }).ok, true);
  assert.equal(p.win.__opened, "thread-b");
});

test("Playwright pseudo selectors are understood", () => {
  const p = conversations();
  assert.equal(p.op("click", { selector: "a:has-text('Gloria Petrova')" }).ok, true);
  assert.equal(p.win.__opened, "thread-b");
});

test("outline hands out refs that click resolves exactly", () => {
  const p = conversations();
  const out = p.op("read", { mode: "outline" });
  const link = out.elements.find((e) => e.tag === "a" && e.label.includes("Anastasios"));
  assert.ok(link, JSON.stringify(out.elements));
  assert.match(link.ref, /^@e\d+$/);
  assert.ok(out.text.includes(link.ref + " link «Anastasios"), out.text);
  const res = p.op("click", { selector: link.ref });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.win.__opened, "thread-a");
});

test("a stale ref says so instead of hitting the wrong thing", () => {
  const p = conversations();
  const res = p.op("click", { selector: "@e9999" });
  assert.equal(res.ok, false);
  assert.match(res.error, /no longer exists/i);
  assert.match(res.text, /browser\.read mode=outline or browser\.find/);
});

test("locate returns the scored winner and never the label", () => {
  const p = conversations();
  const res = p.op("locate", { selector: "Anastasios" });
  assert.equal(res.found, true);
  assert.match(res.ref, /^@/);
  assert.ok(!res.element.startsWith("label"), res.element);
});

test("candidates say where each link goes", () => {
  const p = page(`
    <main><ul><li>
      <a href="/messaging/thread/2-abc" id="row">Anastasios Kesgiropoulos — Hi Arsen</a>
      <a href="/in/anastasios-k" id="prof">Anastasios Kesgiropoulos</a>
    </li></ul></main>`);
  const res = p.op("locate", { selector: "Anastasios Kesgiropoulos" });
  const hrefs = [res.href, ...(res.others || []).map((c) => c.href)];
  assert.ok(hrefs.some((h) => h && h.includes("/messaging/thread/")), JSON.stringify(res));
  assert.ok(hrefs.some((h) => h && h.includes("/in/")), JSON.stringify(res));
});

test("clicking a heading reaches the row's link", () => {
  const p = page(`
    <main><ul><li>
      <h3>Krete Paal</h3><p>Thanks for getting back to me</p>
      <a href="/messaging/thread/2-krete" id="row">Open</a>
    </li></ul></main>`);
  p.win.__opened = null;
  p.$("#row").addEventListener("click", (e) => { e.preventDefault(); p.win.__opened = "row"; });
  const res = p.op("click", { selector: "Krete Paal" });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.win.__opened, "row");
});

test("an unclickable match is refused instead of doing nothing", () => {
  const p = page("<main><h3>Krete Paal</h3><p>no link anywhere</p></main>");
  const res = p.op("click", { selector: "Krete Paal" });
  assert.equal(res.ok, false);
  assert.match(res.error, /not clickable/);
});

test("a recycled ref is caught before it clicks a stranger", () => {
  const p = conversations();
  const out = p.op("read", { mode: "outline" });
  const link = out.elements.find((e) => e.tag === "a" && e.label.includes("Anastasios"));
  p.$("#thread-a").textContent = "Somebody Else Entirely";
  const res = p.op("click", { selector: link.ref });
  assert.equal(res.ok, false);
  assert.match(res.error, /different content/);
  assert.equal(p.win.__opened, null);
});

// ── rich-text composer ───────────────────────────────────────────────────

const COMPOSER = `
<main>
  <div id="composer" role="textbox" contenteditable="true" aria-label="Write a message"></div>
  <button id="send" disabled>Send</button>
</main>`;

function composer() {
  const p = page(COMPOSER);
  // Faithful to how real editors behave: state only updates on an input event
  // that carries a genuine inputType. A textContent write produces nothing.
  const c = p.$("#composer");
  p.win.__model = "";
  p.win.__sent = undefined;
  c.addEventListener("input", (e) => {
    if (!e.inputType) return;
    p.win.__model = c.textContent;
    p.$("#send").disabled = !p.win.__model.trim();
  });
  p.$("#send").addEventListener("click", () => { p.win.__sent = p.win.__model; });
  return p;
}

test("typing reaches the editor's own state model (input event with inputType)", () => {
  const p = composer();
  const res = p.op("type", { selector: "Write a message", value: "Hi Anastasios, thanks!" });
  assert.equal(res.ok, true, res.text);
  assert.equal(res.contenteditable, true);
  // jsdom has no execCommand, so this is the DOM-write path — and the result says so.
  assert.equal(res.insert_method, "dom_write");
  assert.match(res.text, /rich-text editor, dom_write/);
  assert.equal(p.win.__model, "Hi Anastasios, thanks!");
  assert.equal(p.$("#send").disabled, false);
});

test("typing hands back the Send button and reports it became enabled", () => {
  const p = composer();
  const res = p.op("type", { selector: "Write a message", value: "Hi Krete" });
  const labels = res.controls.map((c) => c.label);
  assert.ok(labels.includes("Send"), JSON.stringify(res.controls));
  assert.match(res.controls[labels.indexOf("Send")].ref, /^@/);
  assert.ok(res.control_changes && res.control_changes[0].disabled_now === false, res.text);
  assert.match(res.text, /«Send» became enabled/);
});

test("read uses the body when <main> is only part of the screen", () => {
  const p = page(`
    <main><ul><li>Krete Paal — Thanks for getting back to me</li>
              <li>Michael Singleton — how do you manage the seam</li></ul></main>
    <section id="thread">
      <p>Hi Arsen, I work with fintech AI and compliance teams on EU AI Act
         readiness. Would a quick 20-min call be useful?</p>
      <div contenteditable="true" aria-label="Write a message">…</div>
    </section>`);
  const res = p.op("read", { mode: "text" });
  assert.ok(res.body.includes("20-min call"), res.body.slice(0, 200));
  assert.ok(res.body.includes("Krete Paal"));
});

test("the composer is discoverable in the outline as a textbox", () => {
  const p = composer();
  const out = p.op("read", { mode: "outline" });
  const c = out.elements.find((e) => e.editable);
  assert.ok(c, JSON.stringify(out.elements));
  assert.ok(c.label.includes("Write a message"));
  assert.match(out.text, /@e\d+ textbox «Write a message» \(empty\)/);
  assert.match(out.text, /@e\d+ button «Send» \(disabled\)/);
});

test("typing then clicking Send completes the round trip", () => {
  const p = composer();
  p.op("type", { selector: "Write a message", value: "On my way" });
  const res = p.op("click", { selector: "Send" });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.win.__sent, "On my way");
});

test("clicking a disabled control explains itself", () => {
  const p = composer();
  const res = p.op("click", { selector: "Send" });
  assert.equal(res.ok, false);
  assert.match(res.error, /disabled/i);
});

test("type refuses a target that is not a field", () => {
  const p = composer();
  const res = p.op("type", { selector: "Send", value: "x" });
  assert.equal(res.ok, false);
  assert.match(res.error, /not a text field/);
});

test("type without a ref asks when it is ambiguous", () => {
  const p = page(`<main><input placeholder="Search"><textarea placeholder="Notes"></textarea></main>`);
  const res = p.op("type", { value: "x" });
  assert.equal(res.ok, false);
  assert.match(res.error, /2 text fields/);
  assert.equal(res.candidates.length, 2);
  assert.match(res.text, /Closest matches/);
});

test("type without a ref uses the only visible field", () => {
  const p = page(`<main><textarea id="t" placeholder="Notes"></textarea></main>`);
  const res = p.op("type", { value: "hello there" });
  assert.equal(res.ok, true, res.text);
  assert.equal(res.how, "focused");
  assert.equal(p.$("#t").value, "hello there");
});

test("a multi-line message keeps its shape", () => {
  const p = composer();
  const value = "Hi Anastasios,\n\nThanks for reaching out.\n\nBest,\nArsen";
  p.op("type", { selector: "Write a message", value });
  assert.equal(p.$("#composer").textContent, value);
});

// ── plain inputs still work the way frameworks expect ────────────────────

test("native setter is used so React-style trackers see the write, and submit submits", () => {
  const p = page(`
    <main><form id="f"><input id="q" placeholder="Search messages"></form></main>`);
  p.win.__seen = [];
  p.win.__sub = 0;
  p.$("#q").addEventListener("input", (e) => p.win.__seen.push(e.target.value));
  p.$("#f").addEventListener("submit", (e) => { e.preventDefault(); p.win.__sub = 1; });
  const res = p.op("type", { selector: "Search messages", value: "invoice", submit: true });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.$("#q").value, "invoice");
  assert.deepEqual(p.win.__seen, ["invoice"]);
  assert.equal(p.win.__sub, 1);
  assert.match(res.text, /Submitted/);
});

test("a div with a role is clicked like a button: mousedown, then click", () => {
  const p = page(`<main><div role="button" tabindex="0" id="w">Publish</div></main>`);
  p.win.__hits = [];
  p.$("#w").addEventListener("mousedown", () => p.win.__hits.push("mousedown"));
  p.$("#w").addEventListener("click", () => p.win.__hits.push("click"));
  const res = p.op("click", { selector: "Publish" });
  assert.equal(res.ok, true, res.text);
  assert.deepEqual(p.win.__hits, ["mousedown", "click"]);
});

// ── find ─────────────────────────────────────────────────────────────────

test("find returns both context and something to click", () => {
  const p = conversations();
  const res = p.op("find", { text: "Thursday" });
  assert.equal(res.found, true);
  assert.ok(res.contexts.length && res.contexts[0].includes("Thursday"));
  assert.match(res.elements[0].ref, /^@/);
  assert.match(res.text, /1 place in the text mention "Thursday"/);
  assert.match(res.text, /\n {2}@e\d+ a#thread-b «Gloria Petrova.*Thursday\?» → https:\/\/example\.test\/messaging\/thread\/2-def/);
});

test("find with nothing matching is an empty result with advice", () => {
  const p = conversations();
  const res = p.op("find", { text: "quarterly budget" });
  assert.equal(res.found, false);
  assert.equal(res.empty, true);
  assert.match(res.text, /Nothing on this page matches/);
});

test("find accepts `query` as well as `text`", () => {
  const p = conversations();
  assert.equal(p.op("find", { query: "Gloria" }).found, true);
  assert.equal(p.op("find", {}).ok, false);
});

// ── reading: shadow DOM, slots, emptiness ────────────────────────────────

test("read sees content inside shadow roots", () => {
  const p = page("<div id=host></div>");
  const sr = p.$("#host").attachShadow({ mode: "open" });
  sr.innerHTML = "<p>inside the shadow root</p>";
  assert.ok(p.op("read", {}).body.includes("inside the shadow root"));
});

test("read sees slotted light DOM (the Reddit post-body bug)", () => {
  const p = page("<div id=card><span>slotted body text</span></div>");
  p.$("#card").attachShadow({ mode: "open" }).innerHTML = "<div><slot></slot></div>";
  const body = p.op("read", {}).body;
  assert.ok(body.includes("slotted body text"), body);
  assert.equal(body.split("slotted body text").length - 1, 1, "walked once, not twice");
});

test("read reports emptiness with context, not silence", () => {
  const p = page("");
  const res = p.op("read", {});
  assert.equal(res.empty, true);
  assert.equal(res.looks_empty, true);
  assert.equal(typeof res.empty_context.ready_state, "string");   // jsdom may still be "loading"
  assert.equal(res.empty_context.dom_nodes, 4);                     // html, head, title, body
  assert.match(res.text, /very little text on this page \(ready_state=\w+, 4 DOM nodes, 0 iframes\)/);
});

test("a short real page is not flagged empty", () => {
  const p = page("<h1>Results</h1><p>Three articles about monitoring your homelab with Prometheus.</p>");
  const res = p.op("read", {});
  assert.ok(!res.empty);
  assert.match(res.text, /interactive elements? on this page — browser\.read mode=outline/);
});

test("password values never reach the model", () => {
  const p = page(`<form><input id="u" aria-label="User" value="arsen">
                        <input id="pw" type="password" aria-label="Password" value="s3cret-hunter2"></form>`);
  const text = p.op("read", {}).text;
  const outline = p.op("read", { mode: "outline" }).text;
  assert.ok(!text.includes("s3cret"), text);
  assert.ok(!outline.includes("s3cret"), outline);
  assert.match(outline, /input\[password\] «Password» = "••••"/);
  assert.match(outline, /input\[text\] «User» = "arsen"/);
});

// ── click: affordance judged by what the DOM did ─────────────────────────

test("inert prose is refused, and the refusal names the way out", () => {
  const p = page(`<h2 id=hot>Publish your comment</h2><a href="/real">Publish</a>`);
  const res = p.op("click", { selector: "#hot" });
  assert.equal(res.ok, false);
  assert.match(res.error, /not clickable/);
  assert.match(res.text, /browser\.read mode=outline/);
});

test("a bare div with a listener is clicked, not refused — and the missing affordance is reported", () => {
  const p = page(`<div id=hot style="width:120px;height:40px"></div><div id=out></div>`);
  p.$("#hot").addEventListener("click", () => { p.$("#out").textContent = "listener ran"; });
  const res = p.op("click", { selector: "#hot" });
  assert.equal(res.ok, true, res.text);
  assert.equal(p.$("#out").textContent, "listener ran");
  assert.equal(res.no_click_affordance, true);
  assert.match(res.text, /verify the outcome/);
});

test("a click that changes nothing is refused even with a listener", () => {
  const p = page(`<div id=hot style="width:120px;height:40px"></div>`);
  p.win.__ran = false;
  p.$("#hot").addEventListener("click", () => { p.win.__ran = true; });   // real handler, zero DOM effect
  const res = p.op("click", { selector: "#hot" });
  assert.equal(p.win.__ran, true);
  assert.equal(res.ok, false);
});

test("click reports editable-field transitions (did my comment post?)", () => {
  const p = page(`<div id=wrap><textarea id=c>my comment</textarea><button id=send>Comment</button></div>`);
  p.$("#send").addEventListener("click", () => { p.$("#c").value = ""; });
  const res = p.op("click", { selector: "#send" });
  assert.equal(res.ok, true, res.text);
  assert.equal(res.editable_fields[0].before, "my comment");
  assert.equal(res.editable_fields[0].changed, true);
  assert.match(res.text, /Field @e\d+: "my comment" → "" \(changed\)/);
});

// ── outline formatting ───────────────────────────────────────────────────

const FORM_PAGE = `
<h1>Search results</h1>
<a href="/a/1">Monitoring with Prometheus</a>
<h2>Leave a comment</h2>
<input aria-label="Search" value="invoice">
<textarea aria-label="Notes"></textarea>
<label><input type="checkbox" checked> Remember me</label>
<select aria-label="Country"><option>Bulgaria</option><option>Germany</option></select>
<button disabled>Comment</button>`;

test("outline: one line per element, document order, with kinds, values, hrefs and states", () => {
  const p = page(FORM_PAGE, { url: "https://example.test/x" });
  const out = p.op("read", { mode: "outline" });
  const lines = out.text.split("\n");
  assert.match(lines[0], /^@e\d+ h1 «Search results»$/);
  assert.match(lines[1], /^@e\d+ link «Monitoring with Prometheus» → https:\/\/example\.test\/a\/1$/);
  assert.match(lines[2], /^@e\d+ h2 «Leave a comment»$/);
  assert.match(out.text, /@e\d+ input\[text\] «Search» = "invoice"/);
  assert.match(out.text, /@e\d+ textarea «Notes» \(empty\)/);
  assert.match(out.text, /@e\d+ checkbox «\[x\] Remember me»/);
  assert.match(out.text, /@e\d+ select «Country» = "Bulgaria"/);
  assert.match(out.text, /@e\d+ button «Comment» \(disabled\)/);
  assert.ok(lines.every((l) => /^@e\d+ /.test(l)), out.text);
  assert.equal(out.elements_capped, false);
});

test("outline: over the cap, fields and buttons survive and the footer says how many were left", () => {
  const links = Array.from({ length: 40 }, (_, i) => `<li><a href="/p/${i}">Post number ${i}</a></li>`).join("");
  const p = page(`<nav><a href="/">Home</a><a href="/jobs">Jobs</a></nav><ul>${links}</ul>
                  <textarea aria-label="Write a comment"></textarea><button>Send</button>`);
  const out = p.op("read", { mode: "outline", max_elements: 6 });
  assert.equal(out.elements.length, 6);
  assert.equal(out.elements_capped, true);
  assert.match(out.text, /\[outline: 6 of 44 interactive elements shown, fields and buttons first/);
  assert.match(out.text, /textbox|textarea «Write a comment»/);
  assert.match(out.text, /button «Send»/);
});

test("outline: nothing interactive is an empty result", () => {
  const p = page("<p>Just prose.</p>");
  const out = p.op("read", { mode: "outline" });
  assert.equal(out.empty, true);
  assert.match(out.text, /no headings or interactive elements/);
});

// ── text formatting, truncation and paging ───────────────────────────────

test("text: blocks become lines and table cells become columns", () => {
  const p = page(`<p>One</p><p>Two <b>bold</b> three</p><table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>`);
  const body = p.op("read", {}).body;
  assert.equal(body, "One\nTwo bold three\na | b\nc | d");
});

test("text: a long page is reachable beyond the first window via offset", () => {
  const rows = Array.from({ length: 27 }, (_, i) =>
    `<tr><td>Article number ${i} about local models and GPUs</td><td>${i * 13} views</td><td>${i * 3} reads</td></tr>`).join("");
  const p = page(`<main><table><tbody>${rows}</tbody></table></main>`);
  const first = p.op("read", { max_chars: 600 });
  assert.equal(first.truncated, true);
  assert.equal(first.next_offset, first.body.length);
  assert.match(first.text, /\[truncated: characters 0-600 of \d+ shown; browser\.read offset=600 continues\]/);
  let seen = first.body;
  let next = first.next_offset;
  let guard = 0;
  while (next !== null && guard++ < 50) {
    const chunk = p.op("read", { max_chars: 600, offset: next });
    seen += chunk.body;
    next = chunk.next_offset;
  }
  assert.ok(seen.includes("Article number 26"));
  assert.equal(seen.length, first.chars_total);
});

test("text: the user's selection is reported first", () => {
  const p = page("<p id=a>Alpha beta gamma</p><p>Delta</p>");
  const range = p.doc.createRange();
  range.selectNodeContents(p.$("#a"));
  const sel = p.win.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  const res = p.op("read", {});
  assert.equal(res.selection, "Alpha beta gamma");
  assert.match(res.text, /^\[selected by the user: "Alpha beta gamma"\]/);
});

// ── scroll ───────────────────────────────────────────────────────────────

test("scroll to a ref brings it into view", () => {
  const p = page(`<main>${"<p>row</p>".repeat(30)}<h2 id="deep">Deep section</h2></main>`);
  const out = p.op("read", { mode: "outline" });
  const heading = out.text.split("\n").find((l) => l.includes("Deep section"));
  const ref = heading.split(" ")[0];
  const res = p.op("scroll", { selector: ref, direction: "element" });
  assert.equal(res.ok, true);
  assert.match(res.text, /^Scrolled @e\d+ h2#deep «Deep section» into view\./);
});

test("scroll without anything scrollable says so", () => {
  const p = page("<p>short</p>");
  const res = p.op("scroll", { direction: "down" });
  assert.equal(res.ok, true);
  assert.equal(res.scrollable, false);
  assert.match(res.text, /nothing to scroll here/);
});

// ── the kernel as a shipped artefact ─────────────────────────────────────

test("kernel.js holds no control characters Chrome could fail to re-parse", () => {
  const bad = [];
  for (let i = 0; i < KERNEL_SRC.length; i++) {
    const c = KERNEL_SRC.charCodeAt(i);
    if (c < 32 && c !== 10 && c !== 13 && c !== 9) bad.push({ i, c });
  }
  assert.deepEqual(bad, []);
  assert.ok(KERNEL_SRC.startsWith("//"), "starts with the header comment");
  assert.ok(/^function pageKernel\(op, p\) \{/m.test(KERNEL_SRC));
});

test("an unknown op is an error, never silence", () => {
  const { op } = page("<p>x</p>");
  const res = op("extract", {});
  assert.equal(res.ok, false);
  assert.match(res.text, /Kernel has no op: extract/);
});

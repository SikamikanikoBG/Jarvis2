// Jarvis side panel — the page kernel (V2).
//
// One function, injected into the page by chrome.scripting.executeScript as
// `func: pageKernel`. Chrome ships pageKernel.toString() to the tab and
// re-parses it there, so:
//   * it must not close over anything in this file — every helper lives inside;
//   * anything the file can hold but a JS parser cannot re-read (a raw NUL byte
//     in a string literal, 21 Aug 2026) silently kills EVERY op — each returns
//     null, no error. Use escapes; test/transport.test.mjs re-parses the source
//     the same way Chrome does.
//
// Two ideas do most of the work (proven on LinkedIn, Medium, Reddit, dev.to and
// SCORM courses in V1):
//   * refs — read/find stamp data-jarvis-ref on what they report, so the model
//     acts on `@e42` instead of inventing CSS for a bundler-generated class;
//   * scoring — when a selector is text, every candidate is scored (exact label,
//     actionability, visibility, tightness) and the runners-up come back with the
//     answer, so a miss teaches the next attempt.
//
// Every result is an object with a model-facing `text` plus the facts the
// background worker needs for routing (ok, found, empty, score, frames_present…).
// The isolated world persists per document, so window.__jarvisRefN survives
// between injections and refs stay stable until the page navigates.

function pageKernel(op, p) {
  p = p || {};
  const REF = "data-jarvis-ref";
  const SIG = "data-jarvis-sig";

  // Scoring weights. The <label> penalty MUST stay larger than the clickable
  // bonus: on list UIs the row's select-checkbox <label> shares the row's text,
  // and letting it win clicked "select this conversation" instead of opening it
  // (LinkedIn, 10 Aug 2026). test/kernel.test.mjs asserts the inequality.
  const CLICKABLE_BONUS = 22;
  const LABEL_PENALTY = 30;

  // jsdom and some embedded engines have no PointerEvent; a MouseEvent with the
  // same init is the honest fallback.
  const PointerCtor = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;

  // Anything a human could plausibly click, type into or toggle. Deliberately
  // wider than "a, button, input": modern apps build their primary controls out
  // of divs with a role, and rich text editors are contenteditable — a narrower
  // list could not see a single chat composer on the web.
  const ACTIONABLE = [
    "a[href]", "button", "input:not([type=hidden])", "textarea", "select",
    "summary", "label", "[contenteditable]:not([contenteditable='false'])",
    "[role=button]", "[role=link]", "[role=tab]", "[role=textbox]",
    "[role=searchbox]", "[role=combobox]", "[role=menuitem]",
    "[role=menuitemcheckbox]", "[role=menuitemradio]", "[role=option]",
    "[role=checkbox]", "[role=radio]", "[role=switch]", "[role=treeitem]",
    "[tabindex]:not([tabindex='-1'])", "[onclick]",
  ].join(", ");

  // `label` belongs here: design systems hide the real radio or checkbox
  // (opacity 0, zero size) and the label is the only thing a human can hit.
  const CLICKABLE =
    "a[href], button, input, summary, label, [role=button], [role=link], [role=tab], " +
    "[role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], " +
    "[role=option], [role=treeitem], [onclick]";

  const EDITABLE =
    "input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=submit])" +
    ":not([type=button]):not([type=reset]):not([type=file]), textarea, " +
    "[contenteditable]:not([contenteditable='false']), [role=textbox], [role=searchbox]";

  const BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|TR|TABLE|THEAD|TBODY|TFOOT|SECTION|ARTICLE|HEADER|FOOTER|NAV|ASIDE|MAIN|BLOCKQUOTE|PRE|FIGURE|FIGCAPTION|DL|DT|DD|FORM|FIELDSET|ADDRESS|HR|DETAILS|SUMMARY|OPTION|LABEL|BODY)$/;

  function clamp(v, lo, hi, dflt) {
    const n = parseInt(v, 10);
    return isNaN(n) ? dflt : Math.max(lo, Math.min(n, hi));
  }

  function tidy(s) {
    return String(s || "")
      .replace(/[ \t\u00a0\r\f\v]+/g, " ")
      .replace(/ *\n */g, "\n")
      .replace(/\n{2,}/g, "\n")
      .trim();
  }

  function safeScroll(el) {
    try { el.scrollIntoView({ block: "center", behavior: "instant" }); } catch (e) {
      try { el.scrollIntoView(); } catch (e2) { /* no layout engine */ }
    }
  }

  // ---- shadow DOM -------------------------------------------------------
  //
  // Every component library ships closed-looking widgets: the button, the
  // table, the whole checkout lives inside a shadow root, and
  // document.querySelectorAll cannot see one word of it.

  function allRoots() {
    const roots = [document];
    const seen = new Set();
    const walk = (root) => {
      let els;
      try { els = root.querySelectorAll("*"); } catch (e) { return; }
      for (let i = 0; i < els.length && i < 8000; i++) {
        const sr = els[i].shadowRoot;
        if (sr && !seen.has(sr)) { seen.add(sr); roots.push(sr); walk(sr); }
      }
    };
    walk(document);
    return roots;
  }

  function deepQueryAll(sel, within) {
    const out = [];
    if (within) {
      try { out.push.apply(out, within.querySelectorAll(sel)); } catch (e) { return out; }
      for (const el of within.querySelectorAll("*")) {
        if (el.shadowRoot) {
          try { out.push.apply(out, el.shadowRoot.querySelectorAll(sel)); } catch (e) {}
        }
      }
      return out;
    }
    for (const root of allRoots()) {
      try { out.push.apply(out, root.querySelectorAll(sel)); } catch (e) {}
    }
    return out;
  }

  // ---- perception -------------------------------------------------------

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    if (parseFloat(s.opacity || "1") === 0) return false;
    return true;
  }

  function inViewport(el) {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  }

  function textOf(el) {
    // innerText is the browser's answer to "what is on screen"; textContent is
    // the fallback for engines without layout (and for SVG).
    const t = el.innerText;
    return (t === undefined || t === null) ? (el.textContent || "") : t;
  }

  function labelOf(el) {
    let s = "";
    if (el.getAttribute) {
      s = el.getAttribute("aria-label") || "";
      if (!s) {
        const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
        s = ids.map((i) => {
          const t = document.getElementById(i);
          return t ? textOf(t) : "";
        }).join(" ");
      }
    }
    if (!s) s = textOf(el);
    // A checkbox's value is "on" — meaningless. Its <label> is its name.
    if (!s && el.tagName === "INPUT" && /^(checkbox|radio)$/i.test(el.type || "")) {
      try { if (el.labels && el.labels[0]) s = textOf(el.labels[0]); } catch (e) {}
      if (!s) s = el.title || el.name || el.id || "";
      return String(s).trim().replace(/\s+/g, " ").slice(0, 200);
    }
    // Never expose password values; they go into the model's context.
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "password") {
      if (!s) s = el.placeholder || el.title ||
          (el.getAttribute && el.getAttribute("alt")) || el.name || "";
    } else if (!s) {
      s = el.value || el.placeholder || el.title ||
          (el.getAttribute && el.getAttribute("alt")) || el.name || "";
    }
    return String(s).trim().replace(/\s+/g, " ").slice(0, 200);
  }

  // Every name an element answers to. An aria-label routinely says something
  // different from the visible text — "Close dialog" on a button that reads
  // "No thanks" — and matching only one of them makes the other unreachable.
  function labelsOf(el) {
    const out = [];
    const add = (v) => {
      const t = String(v || "").trim().replace(/\s+/g, " ").slice(0, 200);
      if (t && out.indexOf(t) < 0) out.push(t);
    };
    if (el.getAttribute) {
      add(el.getAttribute("aria-label"));
      const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
      for (const i of ids) {
        const t = document.getElementById(i);
        if (t) add(textOf(t));
      }
      add(el.getAttribute("alt"));
      add(el.getAttribute("title"));
      add(el.getAttribute("name"));
    }
    add(textOf(el));
    const type = el.tagName === "INPUT" ? (el.type || "").toLowerCase() : "";
    if (type === "checkbox" || type === "radio") {
      try { for (const l of (el.labels || [])) add(textOf(l)); } catch (e) {}
    } else if (type !== "password") {
      add(el.value);
    }
    add(el.placeholder);
    return out;
  }

  function disabled(el) {
    if (!el) return false;
    // `.disabled` reflects the element's OWN attribute only. A control inside a
    // <fieldset disabled> reports false there while the browser ignores every
    // click; :disabled is the state the browser actually applies.
    try {
      if (el.matches && el.matches(":disabled")) return true;
    } catch (e) { /* :disabled invalid for non-form elements in old engines */ }
    return !!(el.disabled ||
              (el.getAttribute && el.getAttribute("aria-disabled") === "true"));
  }

  // An element the page has opted out of hit-testing with pointer-events:none
  // cannot receive a real click; ask the browser what is under that point.
  function pointerTarget(el) {
    try {
      const cs = getComputedStyle(el);
      if (!cs || cs.pointerEvents !== "none") return el;
      const r = el.getBoundingClientRect();
      const hit = el.ownerDocument.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && hit !== el && hit.tagName !== "BODY" && hit.tagName !== "HTML") return hit;
    } catch (e) { /* detached node, zero-size box, or no elementFromPoint */ }
    return el;
  }

  function signature(el) {
    return (el.tagName + "|" + labelOf(el)).slice(0, 60);
  }

  function refOf(el) {
    let r = el.getAttribute(REF);
    if (!r) {
      window.__jarvisRefN = (window.__jarvisRefN || 0) + 1;
      r = "e" + window.__jarvisRefN;
      try { el.setAttribute(REF, r); } catch (e) { return null; }
    }
    // Virtualised lists recycle DOM nodes: the attribute survives while the
    // content is replaced, so a ref can silently come to mean someone else.
    // Stamp what it looked like and check on the way back in.
    const sig = signature(el);
    try { el.setAttribute(SIG, sig); } catch (e) {}
    // Keep the signature off-DOM as well: a re-render takes the attribute with
    // it, and remembering what @e125 *was* lets resolve() find it again by label.
    try {
      window.__jarvisRefSig = window.__jarvisRefSig || {};
      window.__jarvisRefSig[r] = sig;
    } catch (e) {}
    return r;
  }

  function describe(el) {
    // Mint a ref while describing: whatever the model is told about, it can
    // act on next — "Clicked a#x @e12" lets it click @e12 again after a read.
    const r = el.getAttribute ? refOf(el) : null;
    // A label must never come back blank: "Clicked «»" tells the model nothing
    // about WHAT it hit. Fall back through class, size and position so even a
    // bare styled <div> gets an identity.
    let name = labelOf(el).slice(0, 60);
    if (!name) {
      const cls = String(el.className && el.className.baseVal !== undefined
                         ? el.className.baseVal : el.className || "")
        .trim().split(/\s+/).slice(0, 2).join(".");
      if (cls) name = "." + cls;
    }
    if (!name && el.getBoundingClientRect) {
      const b = el.getBoundingClientRect();
      name = "unlabeled " + Math.round(b.width) + "x" + Math.round(b.height) +
             " @" + Math.round(b.left) + "," + Math.round(b.top);
    }
    // Ref first, like every outline line: "@e12 a#thread-b «Gloria Petrova»".
    return (
      (r ? "@" + r + " " : "") +
      el.tagName.toLowerCase() +
      (el.id ? "#" + el.id : "") +
      " «" + name + "»"
    );
  }

  // ---- resolution -------------------------------------------------------

  // Playwright's :has-text()/:contains() are not CSS; querySelector throws on
  // them. Models write them constantly, so translate rather than punish.
  function splitTextPseudo(sel) {
    const m = /^([\s\S]*?):(?:has-text|contains|text)\(\s*(['"]?)([\s\S]*?)\2\s*\)\s*$/i.exec(sel);
    if (!m) return null;
    return { base: (m[1] || "").trim() || "*", text: m[3] };
  }

  function queryAll(css) {
    try {
      document.querySelector(css);           // validate the selector once
    } catch (e) {
      return null;                           // not valid CSS
    }
    return deepQueryAll(css);
  }

  function score(el, want, prefer) {
    if (!el || !el.tagName) return -1;
    const names = labelsOf(el);
    if (!names.length) return -1;
    let sc = -1;
    let lab = "";
    for (const raw of names) {
      const cand = raw.toLowerCase();
      let s;
      if (cand === want) s = 100;
      else if (cand.indexOf(want) === 0) s = 78;
      else if (cand.indexOf(want) >= 0) s = 60;
      else {
        const words = want.split(/\s+/).filter(Boolean);
        if (words.length > 1 && words.every((w) => cand.indexOf(w) >= 0)) s = 45;
        else continue;
      }
      if (s > sc) { sc = s; lab = cand; }
    }
    if (sc < 0) return -1;
    // A tight label beats a sprawling container that merely contains the words.
    sc += Math.round(12 * Math.min(1, want.length / Math.max(1, lab.length)));
    const tag = el.tagName.toLowerCase();
    const role = ((el.getAttribute && el.getAttribute("role")) || "").toLowerCase();
    if (el.matches && el.matches(CLICKABLE)) sc += CLICKABLE_BONUS;
    if (prefer === "editable") {
      if (el.matches && el.matches(EDITABLE)) sc += 45;
      else sc -= 20;
    }
    if (prefer === "clickable" && el.matches && el.matches(EDITABLE) && tag !== "input") sc -= 10;
    // A <label> is a handle on the checkbox behind it. On list UIs — mail,
    // chats, file pickers — that checkbox is "select this row for bulk
    // actions", never "open this row", so it must lose to the real target.
    if (tag === "label") sc -= LABEL_PENALTY;
    if (role === "presentation" || role === "none") sc -= 15;
    if (disabled(el)) sc -= 40;
    if (!visible(el)) sc -= 60;
    else if (inViewport(el)) sc += 6;
    return sc;
  }

  function rank(els, want, prefer) {
    const seen = new Set();
    const out = [];
    for (const el of els) {
      if (!el || seen.has(el)) continue;
      seen.add(el);
      const sc = score(el, want, prefer);
      if (sc < 0) continue;
      out.push({ el: el, score: sc });
    }
    out.sort((a, b) => b.score - a.score);
    return out;
  }

  // A text match often lands on the <span> or <h3> holding the words. The
  // thing that responds to a click is either an ancestor, or — in a list row —
  // a sibling link inside the same row. Clicking the heading itself does
  // nothing at all, silently, which is the worst outcome of the three.
  function promote(el) {
    let n = el;
    let rowLink = null;
    for (let i = 0; n && i < 8; i++, n = n.parentElement) {
      if (n.matches && n.matches(CLICKABLE)) return n;
      if (n !== el && n.tabIndex >= 0) return n;
      if (!rowLink && n.querySelector) {
        const links = n.querySelectorAll("a[href]");
        if (links.length === 1) rowLink = links[0];
      }
    }
    return rowLink || el;
  }

  function clickable(el) {
    if (!el || !el.matches) return false;
    if (el.matches(CLICKABLE) || el.tabIndex >= 0) return true;
    // Custom controls: a <div> wired with addEventListener has no href, role,
    // tabindex or onclick attribute. Two signals catch these without opening
    // the door to clicking arbitrary prose: the page styles it as a pointer
    // target, or it carries interactive ARIA state.
    try {
      if (el.matches("[aria-haspopup],[aria-expanded],[aria-pressed]," +
                     "[aria-selected],[aria-checked],[role=switch]," +
                     "[role=checkbox],[role=radio],[role=combobox]")) return true;
      const cs = (el.ownerDocument.defaultView || window).getComputedStyle(el);
      if (cs && cs.cursor === "pointer") return true;
    } catch (e) { /* cross-origin frame or detached node */ }
    return false;
  }

  function area(el) {
    const r = el.getBoundingClientRect();
    return r.width * r.height;
  }

  // A container row is not itself clickable — its body is. Taking the first
  // clickable descendant picks the select-row CHECKBOX, because that comes
  // first in the markup. Take the biggest one instead: that is the row body.
  function bestInner(el) {
    const inner = deepQueryAll(CLICKABLE, el).filter((n) =>
      visible(n) &&
      !(n.tagName === "INPUT" && /checkbox|radio/.test(n.type || "")) &&
      n.tagName !== "LABEL"
    );
    if (!inner.length) return null;
    inner.sort((a, b) => area(b) - area(a));
    return inner[0];
  }

  // Cookie walls, consent gates and modals sit over the page and swallow the
  // click. Firing anyway and reporting success made the agent believe it had
  // pressed a button it never reached.
  function blockedBy(el) {
    const r = el.getBoundingClientRect();
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return null;
    let top = null;
    try { top = document.elementFromPoint(x, y); } catch (e) { return null; }
    while (top && top.shadowRoot) {
      let inner = null;
      try { inner = top.shadowRoot.elementFromPoint(x, y); } catch (e) {}
      if (!inner || inner === top) break;
      top = inner;
    }
    if (!top || top === el || el.contains(top) || top.contains(el)) return null;
    // Only call it blocked when the thing on top really is an overlay —
    // otherwise ordinary stacking would produce constant false alarms.
    let n = top;
    for (let i = 0; n && i < 6; i++, n = n.parentElement) {
      const st = getComputedStyle(n);
      const z = parseInt(st.zIndex, 10);
      if (st.position === "fixed" || st.position === "sticky" ||
          (st.position === "absolute" && z > 0) || (z > 0 && z >= 10)) {
        return n;
      }
    }
    return null;
  }

  // Climb out of shadow roots as well as elements. `parentElement` is null at a
  // shadow boundary, so a walk that used it alone stopped dead inside every
  // web-component composer — including Reddit's, where the Comment button lives
  // outside the editor's own root.
  function parentAcrossShadow(n) {
    if (!n) return null;
    if (n.parentElement) return n.parentElement;
    const root = n.getRootNode && n.getRootNode();
    return (root && root.host) || null;
  }

  // The buttons that belong to a field — Send, Attach, Cancel. After typing a
  // message the agent used to hunt for Send through guesses and never find it,
  // leaving the text sitting unsent. The controls are right there; hand them over.
  function nearbyControls(el) {
    let n = el;
    for (let i = 0; n && i < 8; i++, n = parentAcrossShadow(n)) {
      const btns = deepQueryAll("button, [role=button], input[type=submit]", n).filter(visible);
      if (btns.length) {
        return btns.slice(0, 8).map((b) => ({
          ref: "@" + refOf(b),
          label: labelOf(b).slice(0, 40),
          disabled: disabled(b),
        }));
      }
    }
    return [];
  }

  // Editable fields near a node, with what they currently hold. Used to report
  // page state as FACT before/after an action — never to decide what an action
  // meant. The model can read a before/after diff perfectly well itself.
  function editableSnapshot(el) {
    const out = [];
    let n = el;
    for (let i = 0; n && i < 8 && out.length === 0; i++, n = parentAcrossShadow(n)) {
      for (const e2 of deepQueryAll(EDITABLE, n).filter(visible)) {
        const t = (e2.value !== undefined ? e2.value : textOf(e2)) || "";
        out.push({ ref: "@" + refOf(e2), text: t.trim() });
        if (out.length >= 4) break;
      }
    }
    return out;
  }

  // Given a form control, find the <label> a person would actually hit.
  // Custom-styled radios and checkboxes hide the real input (opacity 0, a 1px
  // box, off-screen) and paint the label; clicking the hidden input does nothing
  // visible, so the control looked disabled when it was merely invisible.
  function labelFor(el) {
    try {
      const labs = el.labels ? Array.prototype.slice.call(el.labels) : [];
      for (const l of labs) if (visible(l)) return l;
      if (el.id && el.ownerDocument) {
        const l = el.ownerDocument.querySelector(
          'label[for="' + (window.CSS && CSS.escape ? CSS.escape(el.id) : el.id) + '"]');
        if (l && visible(l)) return l;
      }
      const anc = el.closest && el.closest("label");
      if (anc && visible(anc)) return anc;
    } catch (e) { /* detached or exotic node */ }
    return null;
  }

  function unwrapLabel(el) {
    if (!el || el.tagName !== "LABEL") return el;
    const c = el.control ||
      (el.htmlFor ? document.getElementById(el.htmlFor) : null) ||
      el.querySelector("input, textarea, select");
    return c || el;
  }

  // Returns {el, candidates:[{el,score}], how, stale?, recycled?, wasLabel?, recovered?}
  function resolve(sel, prefer) {
    const raw = String(sel == null ? "" : sel).trim();
    if (!raw) return { el: null, candidates: [], how: "empty" };

    // 1. A ref handed out by a previous read/find. Unambiguous by construction.
    if (raw[0] === "@") {
      const id = raw.slice(1).trim();
      let el = null;
      try {
        el = document.querySelector("[" + REF + "=" + JSON.stringify(id) + "]");
        if (!el) {
          // Refs inside shadow roots are stamped there too.
          const deep = deepQueryAll("[" + REF + "=" + JSON.stringify(id) + "]");
          el = deep[0] || null;
        }
      } catch (e) {}
      if (el) {
        const was = el.getAttribute(SIG);
        if (was && was !== signature(el)) {
          return { el: null, candidates: [], how: "ref", stale: true,
                   recycled: was.slice(was.indexOf("|") + 1) };
        }
        return { el: el, candidates: [], how: "ref", stale: false };
      }
      // The ref is gone, but we remember what it pointed at. Re-find that same
      // element by its label so an ordinary re-render stops being a dead end.
      let remembered = null;
      try {
        remembered = (window.__jarvisRefSig || {})[id] || null;
      } catch (e) {}
      if (remembered) {
        const label = remembered.slice(remembered.indexOf("|") + 1);
        const tag = remembered.slice(0, remembered.indexOf("|"));
        if (label) {
          const again = resolve(label, prefer);
          // Only take it when the match is the same kind of element; a button
          // and a link sharing a caption do different things.
          if (again.el && again.el.tagName === tag) {
            again.recovered = sel;
            return again;
          }
          if (again.candidates && again.candidates.length) {
            return { el: null, candidates: again.candidates, how: "ref",
                     stale: true, wasLabel: label };
          }
        }
      }
      return { el: null, candidates: [], how: "ref", stale: true };
    }

    // 2. Explicit engines, so the agent can be precise when it wants to be.
    const eng = /^(css|text|label|role|placeholder|xpath)=([\s\S]+)$/i.exec(raw);
    if (eng) {
      const kind = eng[1].toLowerCase();
      const body = eng[2].trim();
      if (kind === "css") {
        const got = queryAll(body);
        return { el: got && got[0] ? got[0] : null, candidates: [], how: "css" };
      }
      if (kind === "xpath") {
        try {
          const r = document.evaluate(body, document, null, 9 /* FIRST_ORDERED_NODE */, null);
          return { el: r.singleNodeValue, candidates: [], how: "xpath" };
        } catch (e) {
          return { el: null, candidates: [], how: "xpath-error" };
        }
      }
      if (kind === "role") {
        const got = queryAll("[role=" + JSON.stringify(body) + "]") || [];
        return { el: got[0] || null, candidates: [], how: "role" };
      }
      if (kind === "placeholder") {
        const got = (deepQueryAll("[placeholder]") || []).filter(
          (e) => (e.placeholder || "").toLowerCase().indexOf(body.toLowerCase()) >= 0
        );
        return { el: got[0] || null, candidates: [], how: "placeholder" };
      }
      // text= / label=
      const ranked = rank(deepQueryAll(ACTIONABLE), body.toLowerCase(), prefer);
      return {
        el: ranked[0] ? ranked[0].el : null,
        candidates: ranked.slice(0, 5),
        how: kind,
      };
    }

    // 3. Playwright-flavoured pseudo selectors.
    const pseudo = splitTextPseudo(raw);
    if (pseudo) {
      const base = queryAll(pseudo.base) || [];
      const ranked = rank(base, pseudo.text.toLowerCase(), prefer);
      return {
        el: ranked[0] ? ranked[0].el : null,
        candidates: ranked.slice(0, 5),
        how: "has-text",
      };
    }

    // 4. Real CSS. Only trust it if it actually matches something; a stale
    //    class name should fall through to text rather than dead-end.
    const css = queryAll(raw);
    if (css && css.length) {
      const vis = css.filter(visible);
      const pick = (vis.length ? vis : css);
      let el = pick[0];
      if (prefer === "editable") el = pick.find((e) => e.matches(EDITABLE)) || el;
      if (prefer === "clickable") el = pick.find((e) => e.matches(CLICKABLE)) || el;
      return { el: el, candidates: [], how: "css" };
    }

    // 5. Plain text, scored over everything interactive.
    const want = raw.toLowerCase();
    let ranked = rank(deepQueryAll(ACTIONABLE), want, prefer);
    if (ranked.length) {
      return { el: ranked[0].el, candidates: ranked.slice(0, 5), how: "text" };
    }

    // 6. Last resort: any element carrying the text, promoted to whatever
    //    around it is actually clickable.
    const all = [];
    for (const n of deepQueryAll("*")) {
      if (all.length > 4000) break;
      const t = (n.textContent || "").trim().toLowerCase();
      if (!t || t.indexOf(want) < 0) continue;
      if (n.children.length && Array.prototype.some.call(n.children,
        (c) => (c.textContent || "").toLowerCase().indexOf(want) >= 0)) continue; // keep deepest
      all.push(n);
    }
    // Score the nodes that actually carry the text, THEN promote the winner.
    // Promoting first loses every case where the clickable ancestor is
    // labelled something else — a row whose link just says "Open".
    ranked = rank(all, want, prefer);
    if (!ranked.length) return { el: null, candidates: [], how: "deep-text" };
    const promoted = promote(ranked[0].el);
    return {
      el: promoted,
      candidates: ranked.slice(0, 5).map((c) => ({ el: promote(c.el), score: c.score })),
      how: "deep-text",
    };
  }

  function candidateList(r) {
    return (r.candidates || []).map((c) => {
      const out = {
        element: describe(c.el),
        ref: "@" + refOf(c.el),
        score: c.score,
        visible: visible(c.el),
      };
      // Where a link goes is what separates "open this conversation" from
      // "go to this person's profile" — the two carry the same name.
      if (c.el.href) out.href = String(c.el.href).slice(0, 200);
      return out;
    });
  }

  // Attach the model-facing `text` to a structured result that lacks one:
  // the error sentence, then the candidates as a list the model can act on.
  function finalize(res) {
    if (!res || typeof res !== "object" || typeof res.text === "string") return res;
    const lines = [];
    if (res.ok === false) lines.push(res.error || "The page refused.");
    else if (res.note) lines.push(res.note);
    if (res.candidates && res.candidates.length) {
      lines.push("Closest matches (act on one by its ref):");
      for (const c of res.candidates) {
        lines.push("  " + c.element +
          (c.href ? " → " + c.href : "") +
          (c.visible === false ? " (hidden)" : ""));
      }
    }
    res.text = lines.join("\n");
    return res;
  }

  function notFound(sel, r) {
    let error;
    if (r.stale) {
      error = "Ref " + sel + (r.recycled
        ? " now points at different content (it used to be \"" + r.recycled +
          "\") — the list re-rendered underneath it."
        : r.wasLabel
          ? " is gone (it was \"" + r.wasLabel + "\") — the page re-rendered " +
            "and nothing of that kind matches any more."
          : " no longer exists — the page re-rendered.") +
        " Read again (browser.read mode=outline or browser.find) for fresh refs.";
    } else {
      error = "No element matched: " + sel + ".";
    }
    const cands = candidateList(r);
    if (!cands.length && !r.stale) {
      error += " Use a @ref from browser.read mode=outline or browser.find, " +
               "or the exact visible text of the control.";
    }
    return finalize({ ok: false, error: error, how: r.how, candidates: cands });
  }

  // ---- synthetic input --------------------------------------------------

  const KEYCODES = {
    Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, " ": 32,
    ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39,
    Home: 36, End: 35, PageUp: 33, PageDown: 34,
  };

  function fireKey(target, type, key, mods) {
    const code = KEYCODES[key] || (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
    const ev = new KeyboardEvent(type, {
      key: key,
      code: key.length === 1 ? "Key" + key.toUpperCase() : key,
      bubbles: true, cancelable: true, composed: true,
      ctrlKey: !!(mods && mods.ctrl), shiftKey: !!(mods && mods.shift),
      altKey: !!(mods && mods.alt), metaKey: !!(mods && mods.meta),
    });
    // keyCode/which are ignored by the constructor, but plenty of shipped
    // handlers (including "Enter sends") still branch on them.
    try {
      Object.defineProperty(ev, "keyCode", { get: () => code });
      Object.defineProperty(ev, "which", { get: () => code });
    } catch (e) {}
    return target.dispatchEvent(ev);
  }

  // A real pointer sequence, not a bare .click(). Menus, drag handles, custom
  // dropdowns and half of every design system listen on pointerdown/mousedown
  // and never see a lone click event.
  function pointerSeq(el, withPress) {
    const r = el.getBoundingClientRect();
    const cx = Math.round(r.left + r.width / 2);
    const cy = Math.round(r.top + r.height / 2);
    // Dispatch on the TOPMOST element at the click point, like a real mouse —
    // events then bubble through the true hit chain. SCORM/Storyline hotspots
    // attach their listeners to overlay shapes stacked over the resolved node;
    // dispatching on `el` itself never reached them. blockedBy() has already
    // vetoed genuine overlays, so the point target is el or a member of its stack.
    let target = el;
    try {
      let top = document.elementFromPoint(cx, cy);
      while (top && top.shadowRoot) {
        let inner = null;
        try { inner = top.shadowRoot.elementFromPoint(cx, cy); } catch (e) {}
        if (!inner || inner === top) break;
        top = inner;
      }
      if (top && (top === el || el.contains(top) || top.contains(el))) target = top;
    } catch (e) {}
    const base = {
      bubbles: true, cancelable: true, composed: true, view: window,
      clientX: cx, clientY: cy, button: 0,
    };
    const fire = (Ctor, type, extra) => {
      try {
        target.dispatchEvent(new Ctor(type, Object.assign(
          { pointerId: 1, pointerType: "mouse", isPrimary: true }, base, extra || {}
        )));
      } catch (e) {
        try { target.dispatchEvent(new MouseEvent(type, base)); } catch (e2) {}
      }
    };
    fire(PointerCtor, "pointerover");
    fire(MouseEvent, "mouseover");
    fire(PointerCtor, "pointerenter");
    fire(MouseEvent, "mouseenter");
    fire(PointerCtor, "pointermove");
    fire(MouseEvent, "mousemove");
    if (!withPress) return target;
    fire(PointerCtor, "pointerdown", { buttons: 1 });
    fire(MouseEvent, "mousedown", { buttons: 1 });
    try { el.focus({ preventScroll: true }); } catch (e) {}
    fire(PointerCtor, "pointerup");
    fire(MouseEvent, "mouseup");
    return target;
  }

  function typeInto(el, value, opts) {
    opts = opts || {};
    try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) {} }

    // Everything EDITABLE that is not a native <input>/<textarea> is a rich
    // editing host (contenteditable, role=textbox) and gets the execCommand path.
    if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") {
      // Rich editors (Draft, Quill, Lexical, ProseMirror, and every chat
      // composer built on them) keep their own model of the document. Writing
      // textContent updates the pixels and nothing else — Send stays disabled.
      // execCommand is the one path that produces the beforeinput/input pair
      // they all listen for, and execCommand("selectAll") acts on the FOCUSED
      // editing host, which pierces shadow boundaries for free (Reddit's Lexical
      // composer lives in one).
      let ok = false;
      try {
        const lines = String(value).split(/\r?\n/);
        if (!opts.append && textOf(el).trim().length) {
          document.execCommand("selectAll");
          document.execCommand("delete");
        } else if (opts.append) {
          try {
            const s = el.getRootNode().getSelection
              ? el.getRootNode().getSelection() : window.getSelection();
            if (s && s.rangeCount) s.collapseToEnd();
          } catch (e) {}
        }
        // Newlines cannot go through insertText: an editor turns "\n" into
        // whatever its schema feels like, and a two-paragraph message came out
        // with five blank lines. Insert a line at a time and ask for the break.
        ok = true;
        for (let i = 0; i < lines.length; i++) {
          if (i) ok = document.execCommand("insertLineBreak") && ok;
          if (lines[i]) ok = document.execCommand("insertText", false, lines[i]) && ok;
        }
      } catch (e) { ok = false; }
      let uncertain = false;
      if (!ok) {
        el.dispatchEvent(new InputEvent("beforeinput", {
          bubbles: true, cancelable: true, composed: true,
          inputType: "insertText", data: value,
        }));
        if (!opts.append) el.textContent = "";
        el.appendChild(document.createTextNode(value));
        el.dispatchEvent(new InputEvent("input", {
          bubbles: true, composed: true, inputType: "insertText", data: value,
        }));
        uncertain = true;
      }
      const out = { contenteditable: true, text_now: textOf(el).trim().slice(0, 200) };
      if (uncertain) {
        // A fact about HOW the text got there, not a prediction about what
        // happens next: execCommand refused, so the DOM was written directly.
        out.insert_method = "dom_write";
        out.note = "execCommand was refused; the text was written straight into the DOM. " +
                   "Editors that keep their own document model (Lexical, Draft, ProseMirror, " +
                   "Quill) may not see such a write — check that Send became enabled.";
      } else {
        out.insert_method = "exec_command";
      }
      return out;
    }

    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    const next = opts.append ? String(el.value || "") + value : value;
    el.dispatchEvent(new InputEvent("beforeinput", {
      bubbles: true, cancelable: true, composed: true,
      inputType: "insertText", data: value,
    }));
    // The native setter, not el.value — React's value tracker swallows a plain
    // assignment and the component never re-renders.
    if (desc && desc.set) desc.set.call(el, next);
    else el.value = next;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return { contenteditable: false, insert_method: "native_setter",
             text_now: String(el.value || "").slice(0, 200) };
  }

  // ---- scrolling --------------------------------------------------------

  function scrollerOf(el) {
    let n = el;
    while (n && n !== document.body && n !== document.documentElement) {
      const s = getComputedStyle(n);
      if (/(auto|scroll|overlay)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 4) {
        return n;
      }
      n = n.parentElement;
    }
    return null;
  }

  // Chat apps, mail clients and dashboards scroll an inner div while the
  // document itself never moves. Reporting window.scrollY there told the agent
  // "at the bottom" on a list it had barely started reading.
  function mainScroller() {
    const doc = document.scrollingElement || document.documentElement;
    if (doc.scrollHeight > innerHeight + 8) return null; // the window really scrolls
    let best = null, bestArea = 0;
    const nodes = deepQueryAll("div, main, section, ul, ol, [role=main], [role=list], [role=grid]");
    for (let i = 0; i < nodes.length && i < 3000; i++) {
      const n = nodes[i];
      if (n.scrollHeight <= n.clientHeight + 8) continue;
      const s = getComputedStyle(n);
      if (!/(auto|scroll|overlay)/.test(s.overflowY)) continue;
      const r = n.getBoundingClientRect();
      const a = r.width * r.height;
      if (a > bestArea) { bestArea = a; best = n; }
    }
    return best;
  }

  // ---- extraction -------------------------------------------------------

  // A hand-rolled walk rather than a TreeWalker, because a TreeWalker stops
  // dead at a shadow boundary and half of modern UI lives on the other side.
  // Block boundaries become newlines so an article reads as paragraphs, and
  // table cells are joined with " | " so a row stays a row.
  function visibleText(root) {
    const out = [];
    let budget = 40000;
    const push = (node) => {
      if (budget <= 0) return;
      if (node.nodeType === 3) {
        const t = node.nodeValue.replace(/\s+/g, " ");
        if (t.trim()) { out.push(t); budget--; }
        else if (t) out.push(" ");
        return;
      }
      if (node.nodeType !== 1 && node.nodeType !== 11) return;
      let block = false;
      if (node.nodeType === 1) {
        const tag = String(node.tagName || "").toUpperCase();
        if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE") return;
        const s = getComputedStyle(node);
        if (s.display === "none" || s.visibility === "hidden") return;
        if (tag === "BR") { out.push("\n"); return; }
        block = BLOCK.test(tag) || /^(block|flex|grid|table|list-item|flow-root)$/.test(s.display || "");
        if ((tag === "TD" || tag === "TH") && node.previousElementSibling) out.push(" | ");
        // What a field currently holds is on the screen but is not a text
        // node, so a filled-in form used to read back blank. Passwords stay out.
        if (tag === "INPUT") {
          const t = (node.type || "text").toLowerCase();
          if (t === "password" || t === "hidden") return;
          if (t === "checkbox" || t === "radio") {
            out.push(node.checked ? "[x] " : "[ ] ");
            return;
          }
          const v = node.value || node.placeholder;
          if (v) { out.push(String(v).trim()); budget--; }
          return;
        }
        if (tag === "TEXTAREA") {
          const v = node.value || node.placeholder;
          if (v) { out.push(String(v).trim()); budget--; }
          return;
        }
        if (tag === "SELECT") {
          const o = node.options && node.options[node.selectedIndex];
          if (o) { out.push(String(o.text).trim()); budget--; }
          return;
        }
        // A <slot> renders LIGHT-dom nodes assigned to it; its own children
        // are only fallback content. Walk the assigned nodes here, and skip
        // the host's light children below so nothing is read twice — or, as
        // before this fix, by neither side (new Reddit slots every post body).
        if (tag === "SLOT") {
          let assigned = [];
          try { assigned = node.assignedNodes({ flatten: true }); } catch (e) {}
          if (assigned.length) {
            for (const a of assigned) push(a);
            return;
          }
        }
        if (block) out.push("\n");
        if (node.shadowRoot) {
          for (const c of node.shadowRoot.childNodes) push(c);
          if (node.shadowRoot.querySelector("slot")) { if (block) out.push("\n"); return; }
        }
      }
      for (const c of node.childNodes) push(c);
      if (block) out.push("\n");
    };
    push(root);
    return tidy(out.join(""));
  }

  function countElements() {
    let n = 0;
    for (const el of deepQueryAll(ACTIONABLE)) if (visible(el)) n++;
    return n;
  }

  // Every visible interactive element with its facts, as {el, entry}. When
  // there are more than `cap`, the prioritised pick keeps what a task needs —
  // the field to type in and the button to press — over nav bars and the
  // twenty-sixth row of a list.
  function collectItems(root, cap) {
    const nodes = deepQueryAll(ACTIONABLE, root === document ? null : root);
    let kept = [];
    for (const el of nodes) {
      if (!visible(el)) continue;
      const label = labelOf(el);
      const editable = el.matches(EDITABLE);
      if (!label && !editable && el.tagName !== "INPUT") continue;
      kept.push({ el: el, label: label, editable: editable });
    }

    // Custom-styled radios and checkboxes: the input is opacity:0 or 0x0 and
    // the control is drawn on its label, so visible() drops the input while the
    // page plainly shows "[ ] Yes". Surface the visible LABEL, named for the
    // input it drives; clicking a label activates its input natively.
    try {
      const seenEls = new Set(kept.map((k) => k.el));
      for (const inp of deepQueryAll("input[type=radio], input[type=checkbox]")) {
        if (visible(inp) || seenEls.has(inp)) continue;
        let lab = null;
        try { lab = (inp.labels && inp.labels[0]) || inp.closest("label"); } catch (e) {}
        if (!lab || !visible(lab) || seenEls.has(lab)) continue;
        const mark = inp.checked ? "(x)" : "( )";
        kept.push({ el: lab, label: mark + " " + (labelOf(lab) || labelOf(inp) || inp.id || ""),
                    editable: false });
        seenEls.add(lab);
      }
    } catch (e) {}

    // Hotspot pass. E-learning interactions are bare <div>/<img>/<area> with a
    // click handler: no text, no role, no href. Collect visible pointer-styled
    // nodes the first pass missed and give the unlabelled ones a positional name.
    try {
      const seen = new Set(kept.map((k) => k.el));
      const scope = (root === document ? document : root);
      const cand = deepQueryAll(
        "div, span, img, area, li, td, svg, g, path, circle, rect",
        scope === document ? null : scope);
      let added = 0;
      for (let i = 0; i < cand.length && added < 25 && i < 4000; i++) {
        const el = cand[i];
        if (seen.has(el) || !visible(el)) continue;
        let cs;
        try { cs = getComputedStyle(el); } catch (e) { continue; }
        if (!cs || cs.cursor !== "pointer") continue;
        const r = el.getBoundingClientRect();
        if (r.width * r.height > innerWidth * innerHeight * 0.5) continue;
        if (el.querySelector && el.querySelector("a[href],button,input,[role=button]")) continue;
        let lab = labelOf(el) ||
          (el.getAttribute && (el.getAttribute("alt") || el.getAttribute("title") ||
                               el.getAttribute("aria-label"))) || "";
        if (!lab) {
          lab = "hotspot " + Math.round(r.width) + "x" + Math.round(r.height) +
                " at " + Math.round(r.left) + "," + Math.round(r.top);
        }
        kept.push({ el: el, label: lab, editable: false });
        seen.add(el);
        added++;
      }
    } catch (e) { /* never let the hotspot pass break a normal read */ }

    if (kept.length > cap) {
      // Repeated rows are a list's job, not the outline's: a control that
      // appears twenty-six times in the same shape yields its slot to the
      // one-off controls — the composer, Send, the filter.
      const shape = (el) => {
        const cls = (typeof el.className === "string" && el.className.trim())
          ? el.className.trim().split(/\s+/)[0] : "";
        return el.tagName + "|" + (el.getAttribute("role") || "") + "|" + cls +
               "|" + (el.parentElement ? el.parentElement.tagName : "");
      };
      const groups = new Map();
      for (const item of kept) {
        const k = shape(item.el);
        groups.set(k, (groups.get(k) || 0) + 1);
      }
      kept = kept.map((item, i) => {
        const el = item.el;
        let w = 0;
        if (item.editable) w += 100;
        const role = (el.getAttribute("role") || "").toLowerCase();
        if (el.tagName === "BUTTON" || role === "button" ||
            (el.tagName === "INPUT" && /submit|button/.test(el.type || ""))) w += 60;
        else if (el.tagName === "A") w += 10;
        if (!disabled(el)) w += 15;
        if (inViewport(el)) w += 25;
        if (el.tagName === "LABEL") w -= 30;
        if (el.closest && el.closest("nav, header, footer")) w -= 40;
        if ((groups.get(shape(el)) || 0) >= 5) w -= 60;
        return { item: item, w: w, i: i };
      }).sort((a, b) => (b.w - a.w) || (a.i - b.i)).map((x) => x.item);
    }

    const items = [];
    for (const item of kept) {
      if (items.length >= cap) break;
      // Drop wrappers that just repeat a child's label — the inner control is
      // the one that responds.
      const dup = kept.some(
        (o) => o !== item && o.label === item.label && item.el.contains(o.el)
      );
      if (dup && !item.editable) continue;
      const el = item.el;
      const entry = {
        ref: "@" + refOf(el),
        tag: el.tagName.toLowerCase(),
        label: item.label.slice(0, 100),
      };
      const role = el.getAttribute("role");
      if (role) entry.role = role;
      if (el.tagName === "INPUT") entry.type = el.type;
      if (item.editable) entry.editable = true;
      if (disabled(el)) entry.disabled = true;
      if (el.checked !== undefined && el.type && /checkbox|radio/.test(el.type)) {
        entry.checked = !!el.checked;
      }
      if (el.href) entry.href = String(el.href).slice(0, 300);
      if (!inViewport(el)) entry.offscreen = true;
      items.push({ el: el, entry: entry });
    }
    return items;
  }

  function headings() {
    const out = [];
    for (const h of deepQueryAll("h1, h2, h3, h4, h5, h6, [role=heading]")) {
      if (!visible(h)) continue;
      const t = textOf(h).trim().replace(/\s+/g, " ");
      if (!t) continue;
      const lvl = /^H[1-6]$/.test(h.tagName)
        ? parseInt(h.tagName[1], 10)
        : Math.min(6, Math.max(1, parseInt(h.getAttribute("aria-level") || "2", 10) || 2));
      out.push({ el: h, level: lvl, text: t.slice(0, 120) });
      if (out.length >= 60) break;
    }
    return out;
  }

  function fieldValue(el) {
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "password") return el.value ? "••••" : "";
    if (el.value !== undefined && el.tagName !== "SELECT") return String(el.value || "");
    return textOf(el).trim();
  }

  // One outline line: "@e12 kind «label» = "value" → href (disabled)".
  function outlineLine(el, entry) {
    const tag = entry.tag;
    const role = (entry.role || "").toLowerCase();
    const type = (entry.type || "").toLowerCase();
    let kind;
    if (entry.editable) {
      kind = tag === "input" ? "input[" + (type || "text") + "]"
           : tag === "textarea" ? "textarea" : "textbox";
    } else if (tag === "a") kind = "link";
    else if (tag === "button" || role === "button" || (tag === "input" && /submit|button|reset/.test(type))) kind = "button";
    else if (tag === "select") kind = "select";
    else if (type === "checkbox" || role === "checkbox" || role === "switch" || role === "menuitemcheckbox") kind = "checkbox";
    else if (type === "radio" || role === "radio" || role === "menuitemradio") kind = "radio";
    else if (type === "file") kind = "file";
    else if (role) kind = role;
    else kind = tag;

    let label = entry.label;
    if (entry.checked !== undefined && !/^[\(\[][ x][\)\]]/.test(label)) {
      label = (entry.checked ? "[x] " : "[ ] ") + label;
    }
    let line = entry.ref + " " + kind + " «" + label + "»";
    if (entry.editable) {
      const v = fieldValue(el);
      line += v ? ' = "' + v.slice(0, 80).replace(/\n/g, " ") + '"' : " (empty)";
    } else if (tag === "select") {
      const o = el.options && el.options[el.selectedIndex];
      if (o) line += ' = "' + String(o.text).trim().slice(0, 60) + '"';
    }
    if (entry.href) line += " → " + entry.href.slice(0, 160);
    if (entry.disabled) line += " (disabled)";
    if (entry.offscreen) line += " (offscreen)";
    return line;
  }

  function byDocumentOrder(a, b) {
    if (a.el === b.el) return 0;
    let pos = 0;
    try { pos = a.el.compareDocumentPosition(b.el); } catch (e) { return 0; }
    if (pos & 4) return -1;      // b follows a
    if (pos & 2) return 1;       // b precedes a
    return 0;
  }

  // ---- read -------------------------------------------------------------

  function readText(p) {
    const maxChars = clamp(p.max_chars, 500, 40000, 12000);
    const root = document.body || document.documentElement;
    let full = visibleText(root);
    let source = "walker";
    // The walker pierces shadow roots and unwraps form controls — richer than
    // innerText, and more ways to be wrong: one unhandled layout and it returns
    // nothing on a page the user can plainly read ("the browser is broken",
    // 21 Aug). innerText is the browser's own answer; take it when it plainly
    // knows more.
    try {
      const plain = tidy(root.innerText || "");
      if (plain.length > Math.max(200, full.length * 2)) {
        full = plain;
        source = "innerText_fallback";
      }
    } catch (e) {}
    // Paging matters more than it looks: a long page read from the top comes
    // back identical after every scroll, so the agent concludes the page
    // failed to load. Offset lets it walk the whole thing.
    const offset = Math.max(0, Math.min(parseInt(p.offset || 0, 10) || 0, full.length));
    const body = full.slice(offset, offset + maxChars);
    const truncated = offset + body.length < full.length;
    const interactive = countElements();
    const framesPresent = document.querySelectorAll("iframe").length;
    let selection = "";
    try { selection = String(window.getSelection() || "").trim().slice(0, 500); } catch (e) {}

    const out = {
      ok: true, mode: "text",
      url: location.href, title: document.title, ready_state: document.readyState,
      text_source: source,
      body: body,
      chars_total: full.length, offset: offset,
      next_offset: truncated ? offset + body.length : null,
      truncated: truncated,
      interactive_count: interactive,
      frames_present: framesPresent,
      selection: selection,
    };
    const parts = [];
    if (selection) parts.push('[selected by the user: "' + selection + '"]');
    parts.push(body || "(no visible text)");
    if (truncated) {
      parts.push("[truncated: characters " + offset + "-" + (offset + body.length) + " of " +
                 full.length + " shown; browser.read offset=" + out.next_offset + " continues]");
    }
    // 50, not 200: a short page is not an empty one. The banner exists to
    // explain a BLANK read, not to second-guess brief pages.
    if (full.length < 50) {
      out.empty = true;
      out.looks_empty = true;
      out.empty_context = {
        ready_state: document.readyState,
        body_chars: (document.body && document.body.innerText || "").length,
        dom_nodes: document.getElementsByTagName("*").length,
        iframes: framesPresent,
      };
      parts.push("[very little text on this page (ready_state=" + document.readyState + ", " +
                 out.empty_context.dom_nodes + " DOM nodes, " + framesPresent + " iframe" +
                 (framesPresent === 1 ? "" : "s") + "). It may still be rendering — read again in " +
                 "a moment — the content may live in an iframe, or it may really be blank.]");
    } else {
      parts.push("[" + interactive + " interactive element" + (interactive === 1 ? "" : "s") +
                 " on this page — browser.read mode=outline lists them with @refs; " +
                 "browser.find locates one by its text]");
    }
    out.text = parts.join("\n");
    return out;
  }

  function readOutline(p) {
    const cap = clamp(p.max_elements, 5, 400, 120);
    const items = collectItems(document, cap);
    const total = countElements();
    const heads = headings();
    const rows = [];
    for (const h of heads) {
      rows.push({ el: h.el, line: "@" + refOf(h.el) + " h" + h.level + " «" + h.text + "»" });
    }
    for (const it of items) rows.push({ el: it.el, line: outlineLine(it.el, it.entry) });
    rows.sort(byDocumentOrder);
    const capped = items.length >= cap && total > items.length;
    const out = {
      ok: true, mode: "outline",
      url: location.href, title: document.title, ready_state: document.readyState,
      elements: items.map((i) => i.entry),
      elements_total: total,
      elements_capped: capped,
      headings: heads.length,
      frames_present: document.querySelectorAll("iframe").length,
    };
    if (!rows.length) {
      out.empty = true;
      out.text = "[no headings or interactive elements are visible on this page (ready_state=" +
                 document.readyState + ", " + out.frames_present + " iframe" +
                 (out.frames_present === 1 ? "" : "s") + "). It may still be rendering, or its " +
                 "content may live in an iframe; browser.read mode=text shows the text.]";
      return out;
    }
    let text = rows.map((r) => r.line).join("\n");
    if (capped) {
      text += "\n[outline: " + items.length + " of " + total + " interactive elements shown, " +
              "fields and buttons first; browser.find \"words\" locates a specific one]";
    }
    out.text = text;
    return out;
  }

  // ---- dispatch ---------------------------------------------------------

  switch (op) {
    // Exposed for the unit tests: the scoring invariant is asserted, not assumed.
    case "__tuning":
      return { CLICKABLE_BONUS: CLICKABLE_BONUS, LABEL_PENALTY: LABEL_PENALTY };

    case "read": {
      const mode = String(p.mode || "text").toLowerCase();
      return mode === "outline" ? readOutline(p) : readText(p);
    }

    case "find": {
      const needle = String(p.text == null ? (p.query == null ? "" : p.query) : p.text).trim();
      if (!needle) {
        return finalize({ ok: false, error: "browser.find needs a query — a few words that appear on the page." });
      }
      const want = needle.toLowerCase();
      const hay = tidy(document.body ? textOf(document.body) : "").replace(/\s+/g, " ");
      const lower = hay.toLowerCase();
      const contexts = [];
      let total = 0;
      let from = 0;
      for (;;) {
        const i = lower.indexOf(want, from);
        if (i < 0) break;
        total++;
        if (contexts.length < 6) {
          contexts.push(hay.slice(Math.max(0, i - 120), i + needle.length + 120).trim());
        }
        from = i + Math.max(1, want.length);
        if (total > 500) break;
      }
      // Text on its own is not actionable; hand back the controls carrying it
      // so the next call can be a click by ref instead of another guess.
      const ranked = rank(deepQueryAll(ACTIONABLE), want, null).slice(0, 8);
      const elements = ranked.map((c) => {
        const e = {
          ref: "@" + refOf(c.el),
          element: describe(c.el),
          score: c.score,
          visible: visible(c.el),
          disabled: disabled(c.el),
        };
        if (c.el.href) e.href = String(c.el.href).slice(0, 200);
        return e;
      });
      const found = total > 0 || elements.length > 0;
      const out = {
        ok: true, found: found, matches: total, contexts: contexts, elements: elements,
        url: location.href, title: document.title,
      };
      if (!found) {
        out.empty = true;
        out.text = 'Nothing on this page matches "' + needle + '" — not in the visible text and ' +
                   "not on any link, button or field. Try fewer or different words, scroll, or " +
                   "browser.read mode=outline.";
        return out;
      }
      const lines = [];
      if (total) {
        lines.push(total + " place" + (total === 1 ? "" : "s") + ' in the text mention "' + needle + '":');
        for (const c of contexts) lines.push("  …" + c + "…");
      } else {
        lines.push('"' + needle + '" is not in the visible text, but these controls match it:');
      }
      if (elements.length) {
        if (total) lines.push("Matching elements (act on one by its ref):");
        for (const e of elements) {
          lines.push("  " + e.element +
            (e.href ? " → " + e.href : "") +
            (e.disabled ? " (disabled)" : "") +
            (e.visible ? "" : " (hidden)"));
        }
      } else {
        lines.push("No link, button or field carries these words — it is plain text. " +
                   "browser.read mode=outline lists what can be acted on.");
      }
      out.text = lines.join("\n");
      return out;
    }

    // Used by the background worker to find which frame owns a ref/text.
    case "locate": {
      const r = resolve(p.selector, p.prefer);
      if (!r.el) {
        return finalize({ ok: true, found: false, how: r.how, candidates: candidateList(r),
                          note: "No element matched: " + p.selector });
      }
      const out = {
        ok: true,
        found: true,
        how: r.how,
        score: (r.candidates && r.candidates[0] && r.candidates[0].score) || 100,
        element: describe(r.el),
        ref: "@" + refOf(r.el),
        visible: visible(r.el),
        in_viewport: inViewport(r.el),
        disabled: disabled(r.el),
        others: candidateList(r).slice(1),
      };
      if (r.el.href) out.href = String(r.el.href).slice(0, 200);
      out.text = "Found " + out.element + (out.href ? " → " + out.href : "");
      return out;
    }

    case "scroll": {
      let box = null;
      const dir = String(p.direction || (p.selector ? "element" : "down")).toLowerCase();
      if (p.selector) {
        const r = resolve(p.selector, null);
        if (!r.el) return notFound(p.selector, r);
        if (dir === "element") {
          safeScroll(r.el);
          return { ok: true, scrolled_to: describe(r.el), y: Math.round(window.scrollY || 0),
                   text: "Scrolled " + describe(r.el) + " into view." };
        }
        box = scrollerOf(r.el) || r.el;
      }
      if (!box) box = mainScroller();
      const step = parseInt(p.amount || 0, 10) ||
        Math.round((box ? box.clientHeight : innerHeight) * 0.85);
      let res;
      if (box) {
        if (dir === "top") box.scrollTop = 0;
        else if (dir === "bottom") box.scrollTop = box.scrollHeight;
        else if (dir === "up") box.scrollTop -= step;
        else box.scrollTop += step;
        res = {
          container: describe(box),
          y: Math.round(box.scrollTop),
          height: Math.round(box.scrollHeight),
          viewport: Math.round(box.clientHeight),
          at_bottom: box.scrollTop + box.clientHeight >= box.scrollHeight - 4,
        };
      } else {
        const doc = document.scrollingElement || document.documentElement;
        try {
          if (dir === "top") window.scrollTo({ top: 0, behavior: "instant" });
          else if (dir === "bottom") window.scrollTo({ top: doc.scrollHeight, behavior: "instant" });
          else if (dir === "up") window.scrollBy(0, -step);
          else window.scrollBy(0, step);
        } catch (e) {}
        res = {
          container: "window",
          y: Math.round(window.scrollY || 0),
          height: Math.round(doc.scrollHeight || 0),
          viewport: Math.round(innerHeight),
          at_bottom: (window.scrollY || 0) + innerHeight >= (doc.scrollHeight || 0) - 4,
        };
      }
      res.ok = true;
      res.direction = dir;
      res.scrollable = res.height > res.viewport + 8;
      let t = "Scrolled " + dir + (box ? " inside " + res.container : "") +
              " — now at " + res.y + " of " + res.height + " px (viewport " + res.viewport + ")";
      if (!res.scrollable) t += "; nothing to scroll here";
      else if (res.at_bottom) t += "; at the bottom";
      else if (res.y === 0) t += "; at the top";
      res.text = t + ". browser.read mode=text with offset pages long text regardless of scrolling.";
      return res;
    }

    case "click": {
      const r = resolve(p.selector, "clickable");
      if (!r.el) return notFound(p.selector, r);
      let el = unwrapLabel(r.el);
      if (!visible(el) && el !== r.el) el = r.el;
      // A hidden radio/checkbox is driven through its label. Do this before the
      // clickable() and disabled() checks, or a working control gets reported
      // as dead just because the page styles it.
      if (el.tagName === "INPUT" && /checkbox|radio/.test(el.type || "") &&
          !visible(el) && !disabled(el)) {
        const lab = labelFor(el);
        if (lab) el = lab;
      }
      el = pointerTarget(el);
      if (!clickable(el)) {
        const inner = bestInner(el);
        if (inner) el = inner;
      }
      // NO AFFORDANCE IS NOT A VETO. JavaScript cannot see addEventListener
      // handlers, so clickable() can only guess. Perform the click and judge it
      // by what the DOM did afterwards.
      const noAffordance = !clickable(el);
      safeScroll(el);
      const desc = describe(el);
      let blocker = blockedBy(el);
      // A bar PINNED TO A VIEWPORT EDGE (sticky header, cookie footer) is page
      // chrome, not a modal: scroll the target clear of it and carry on.
      if (blocker) {
        const b = blocker.getBoundingClientRect();
        const wide = b.width >= innerWidth * 0.5;
        const atTop = wide && b.top <= 2;
        const atBottom = wide && b.bottom >= innerHeight - 2 && b.top > innerHeight * 0.5;
        if (atTop || atBottom) {
          const r2 = el.getBoundingClientRect();
          const delta = atTop ? -(b.bottom - r2.top + 12) : (r2.bottom - b.top + 12);
          try { (mainScroller() || window).scrollBy(0, delta); } catch (e) {
            try { window.scrollBy(0, delta); } catch (e2) {}
          }
          blocker = blockedBy(el);
        }
      }
      if (blocker) {
        return finalize({
          ok: false,
          error: "Cannot click " + desc + ": " + describe(blocker) +
            " covers it — a cookie wall, consent gate or modal is in the way. " +
            "Dismiss that first (its buttons are listed below), then retry.",
          blocked_by: describe(blocker),
          candidates: deepQueryAll(CLICKABLE, blocker).filter(visible).slice(0, 6)
            .map((b) => ({ ref: "@" + refOf(b), element: describe(b), score: 0, visible: true })),
        });
      }
      if (disabled(el)) {
        return finalize({
          ok: false,
          error: "Element is disabled: " + desc +
            ". Something upstream is unsatisfied — fill the form or type into the editor first.",
        });
      }
      // Observation, not interpretation: snapshot nearby editable fields before
      // and diff them after, so "did my comment post" has a fact to check.
      const before = editableSnapshot(el);
      const urlBefore = location.href;
      let watcher = null;
      if (noAffordance) {
        try {
          watcher = new MutationObserver(function () {});
          watcher.observe(document.documentElement, {
            childList: true, subtree: true, attributes: true, characterData: true,
          });
        } catch (e) { watcher = null; }
      }
      const hit = pointerSeq(el, true);
      // Click the element the pointer actually landed on: events bubble UP, so
      // an overlay's listener never fires when dispatching on `el` beneath it,
      // while a child still bubbles to `el` for native activation.
      (hit || el).click();
      let changedTheDom = false;
      if (watcher) {
        try { changedTheDom = watcher.takeRecords().length > 0; } catch (e) {}
        try { watcher.disconnect(); } catch (e) {}
      }
      if (noAffordance && !changedTheDom && location.href === urlBefore) {
        // Refuse, rather than return ok with a warning nobody reads. What
        // reaches here has no href, role, tabindex, pointer cursor or ARIA
        // state and did nothing — inert prose that merely CONTAINS the text.
        return finalize({
          ok: false,
          action: "click",
          element: desc,
          how: r.how,
          error: "Element is not clickable: " + desc +
            ". It has no href, role, tabindex, pointer cursor or ARIA state and nothing " +
            "changed when it was clicked — it is probably text that just contains your " +
            "words. Pick one of the candidates below, or browser.read mode=outline for " +
            "the links and buttons on this page.",
          candidates: candidateList(r).filter((c) => c.href)
            .concat(candidateList(r).filter((c) => !c.href)).slice(0, 5),
        });
      }
      const res = { ok: true, action: "click", element: desc, how: r.how };
      if (r.recovered) res.recovered = r.recovered;
      if (noAffordance) res.no_click_affordance = true;
      if (before.length) {
        const now = editableSnapshot(el);
        const byRef = {};
        for (const f of now) byRef[f.ref] = f.text;
        const fields = before.map((f) => ({
          ref: f.ref,
          before: f.text.slice(0, 60),
          after: (byRef[f.ref] === undefined ? null : byRef[f.ref].slice(0, 60)),
          changed: byRef[f.ref] === undefined ? true : byRef[f.ref] !== f.text,
        }));
        if (fields.some((f) => f.changed || f.before)) res.editable_fields = fields;
      }
      let t = "Clicked " + desc + ".";
      if (res.recovered) t += " (" + res.recovered + " had been re-rendered; matched the same label again.)";
      if (res.no_click_affordance) {
        t += " The element showed no link/button/role/pointer affordance but the page did " +
             "react — verify the outcome with browser.read.";
      }
      if (res.editable_fields) {
        for (const f of res.editable_fields) {
          t += "\nField " + f.ref + ': "' + f.before + '" → ' +
               (f.after === null ? "(gone)" : '"' + f.after + '"') +
               (f.changed ? " (changed)" : " (unchanged)");
        }
      }
      res.text = t;
      return res;
    }

    case "type": {
      const value = String(p.value == null ? "" : p.value);
      // No selector: the agent has usually just clicked into the field, and
      // failing here only costs a round trip to say what is already obvious.
      let el, r = { how: "focused" };
      if (!String(p.selector || "").trim()) {
        const active = document.activeElement;
        el = (active && active.matches && active.matches(EDITABLE)) ? active : null;
        if (!el) {
          const fields = Array.prototype.slice.call(deepQueryAll(EDITABLE)).filter(visible);
          if (fields.length === 1) el = fields[0];
          else if (fields.length > 1) {
            return finalize({
              ok: false,
              error: "browser.type needs a ref: " + fields.length +
                " text fields are visible and none is focused.",
              candidates: fields.slice(0, 5).map((f) => ({
                ref: "@" + refOf(f), element: describe(f), score: 0, visible: true,
              })),
            });
          }
        }
        if (!el) {
          return finalize({ ok: false, error: "browser.type needs a ref — no text field is focused or visible." });
        }
      } else {
        r = resolve(p.selector, "editable");
        if (!r.el) return notFound(p.selector, r);
        el = unwrapLabel(r.el);
        if (!el.matches(EDITABLE)) {
          const inner = el.querySelector(EDITABLE);
          if (inner) el = inner;
        }
        if (!el.matches(EDITABLE)) {
          return finalize({
            ok: false,
            error: "Resolved " + describe(el) + " but it is not a text field. browser.read " +
              "mode=outline marks fields as input/textarea/textbox — type against one of those refs.",
            candidates: candidateList(r),
          });
        }
        if (disabled(el)) {
          return finalize({ ok: false, error: "Field is disabled: " + describe(el) + "." });
        }
        safeScroll(el);
        pointerSeq(el, true);
      }
      const ctrlsBefore = nearbyControls(el);
      const typed = typeInto(el, value, { append: !!p.append });
      if (p.submit) {
        if (el.form && el.form.requestSubmit) {
          try { el.form.requestSubmit(); } catch (e) { fireKey(el, "keydown", "Enter", {}); }
        } else {
          fireKey(el, "keydown", "Enter", {});
          fireKey(el, "keypress", "Enter", {});
          fireKey(el, "keyup", "Enter", {});
        }
      }
      const ctrls = nearbyControls(el);
      const out = Object.assign(
        { ok: true, action: "type", element: describe(el), submitted: !!p.submit,
          how: r.how, controls: ctrls },
        typed
      );
      if (r.recovered) out.recovered = r.recovered;
      // `controls` carries each button's disabled state — that is the fact.
      // Rich editors gate their submit on their OWN document model, so a Send
      // that stayed disabled after typing is strong evidence the text never
      // landed; one that just became enabled is evidence it did.
      const disabledBefore = {};
      for (const c of ctrlsBefore) disabledBefore[c.ref] = c.disabled;
      const transitions = ctrls
        .filter((c) => disabledBefore[c.ref] !== undefined && disabledBefore[c.ref] !== c.disabled)
        .map((c) => ({ ref: c.ref, label: c.label,
                       disabled_before: disabledBefore[c.ref], disabled_now: c.disabled }));
      if (transitions.length) out.control_changes = transitions;

      let t = "Typed " + value.length + " character" + (value.length === 1 ? "" : "s") +
              " into " + out.element +
              (out.contenteditable ? " (rich-text editor, " + out.insert_method + ")" : "") + ".";
      if (out.recovered) t += " (" + out.recovered + " had been re-rendered; matched the same label again.)";
      if (out.note) t += "\n" + out.note;
      t += '\nField now holds: "' + out.text_now + '"';
      if (out.submitted) t += "\nSubmitted (Enter / form submit).";
      if (ctrls.length) {
        t += "\nButtons next to the field: " + ctrls.map((c) =>
          c.ref + " «" + c.label + "»" + (c.disabled ? " (disabled)" : "")).join(", ");
      }
      if (transitions.length) {
        t += "\n" + transitions.map((c) =>
          c.ref + " «" + c.label + "» became " + (c.disabled_now ? "disabled" : "enabled")).join("; ");
      }
      out.text = t;
      return out;
    }

    default:
      return finalize({ ok: false, error: "Kernel has no op: " + op });
  }
}

if (typeof module === "object" && module && module.exports) module.exports = { pageKernel };

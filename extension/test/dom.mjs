// jsdom harness for the page kernel.
//
// The kernel only ever runs inside a page, and Chrome ships it there as
// pageKernel.toString(). This harness does the same: it reads kernel.js from
// disk and re-parses it inside a jsdom window's own realm (vm context), so
// `document`, `window`, `getComputedStyle`, `InputEvent`… are the page's, not
// Node's — and a source file JavaScript cannot re-parse fails here too.
//
// jsdom has no layout engine. The shims below give it just enough geometry for
// the kernel's visibility and scoring logic to behave like a browser:
//   * innerText            — rendered text with block newlines, hidden nodes dropped
//   * getBoundingClientRect— 100x20 boxes (or the inline width/height), zero when
//                            hidden, stacked in document order so "in viewport"
//                            means "early in the document"
//   * scrollIntoView       — no-op
//   * isContentEditable    — from the contenteditable attribute
// Everything else (MutationObserver, InputEvent, Selection, shadow DOM, slots,
// label activation, requestSubmit) is jsdom's own.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const EXT_DIR = path.resolve(HERE, "..");
export const KERNEL_SRC = fs.readFileSync(path.join(EXT_DIR, "kernel.js"), "utf8");

const BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|TR|TABLE|THEAD|TBODY|SECTION|ARTICLE|HEADER|FOOTER|NAV|ASIDE|MAIN|BLOCKQUOTE|PRE|FORM|FIELDSET|LABEL|BODY|DL|DT|DD|HR|OPTION|SUMMARY|DETAILS|FIGURE)$/;

export function installLayoutShims(win) {
  const { HTMLElement, Element } = win;
  const doc = win.document;

  const hostOf = (n) => n.parentElement || (n.getRootNode && n.getRootNode().host) || null;

  function hidden(el) {
    for (let n = el; n && n.nodeType === 1; n = hostOf(n)) {
      if (n.hidden) return true;
      const cs = win.getComputedStyle(n);
      if (cs.display === "none" || cs.visibility === "hidden") return true;
    }
    return false;
  }

  function render(el) {
    let out = "";
    const walk = (node) => {
      if (node.nodeType === 3) { out += node.nodeValue; return; }
      if (node.nodeType !== 1) return;
      const tag = node.tagName.toUpperCase();
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "TEMPLATE" || tag === "NOSCRIPT") return;
      if (node.hidden) return;
      const cs = win.getComputedStyle(node);
      if (cs.display === "none" || cs.visibility === "hidden") return;
      if (tag === "BR") { out += "\n"; return; }
      if (tag === "SLOT") {
        let assigned = [];
        try { assigned = node.assignedNodes({ flatten: true }); } catch (e) {}
        if (assigned.length) { assigned.forEach(walk); return; }
      }
      const block = BLOCK.test(tag);
      if (block) out += "\n";
      const kids = node.shadowRoot ? node.shadowRoot.childNodes : node.childNodes;
      for (const c of kids) walk(c);
      if (block) out += "\n";
    };
    for (const c of el.childNodes) walk(c);
    return out.replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{2,}/g, "\n").trim();
  }

  Object.defineProperty(HTMLElement.prototype, "innerText", {
    configurable: true,
    get() { return hidden(this) ? "" : render(this); },
    set(v) { this.textContent = String(v); },
  });

  Object.defineProperty(HTMLElement.prototype, "isContentEditable", {
    configurable: true,
    get() {
      const host = this.closest ? this.closest("[contenteditable]") : null;
      if (!host) return false;
      const a = host.getAttribute("contenteditable");
      return a === "" || a === "true" || a === "plaintext-only";
    },
  });

  const px = (v) => {
    const m = /^(\d+(?:\.\d+)?)px$/.exec(v || "");
    return m ? parseFloat(m[1]) : null;
  };
  const rect = (left, top, w, h) => ({
    x: left, y: top, left, top, width: w, height: h, right: left + w, bottom: top + h,
    toJSON() { return { left, top, width: w, height: h }; },
  });

  Element.prototype.getBoundingClientRect = function () {
    if (hidden(this)) return rect(0, 0, 0, 0);
    const root = this === doc.body || this === doc.documentElement;
    const cs = win.getComputedStyle(this);
    const w = px(cs.width) ?? (root ? 1024 : 100);
    const h = px(cs.height) ?? (root ? 2000 : 20);
    let idx = Array.prototype.indexOf.call(doc.getElementsByTagName("*"), this);
    if (idx < 0) idx = 0;                          // inside a shadow root
    return rect(0, idx * 24, w, h);
  };

  Element.prototype.scrollIntoView = function () {};
}

/**
 * Build a page from a body fragment and return the kernel bound to it.
 *   const { op, doc, win } = page("<button>Go</button>");
 *   op("read", { mode: "outline" })
 */
export function page(html, opts = {}) {
  const title = opts.title ?? "Fixture";
  const dom = new JSDOM(
    `<!DOCTYPE html><html><head><title>${title}</title></head><body>${html}</body></html>`,
    { url: opts.url ?? "https://example.test/page", runScripts: "outside-only", pretendToBeVisual: true },
  );
  const win = dom.window;
  installLayoutShims(win);
  const ctx = dom.getInternalVMContext();
  const kernel = vm.runInContext(KERNEL_SRC + "\n;pageKernel", ctx, { filename: "kernel.js" });
  return {
    dom, win, ctx,
    doc: win.document,
    kernel,
    op: (name, params) => kernel(name, params || {}),
    $: (sel) => win.document.querySelector(sel),
  };
}

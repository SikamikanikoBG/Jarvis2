// Jarvis side panel — selection reporter.
//
// The service worker learns about navigation and tab switches from chrome.tabs
// events. The one thing it cannot see without living in the page is what the
// user has *selected*, which is exactly what "summarise this bit" depends on
// (browser.context.selection). This script does one job: debounce
// selectionchange and tell the worker.

(function () {
  let last = "";
  let timer = null;

  function report() {
    let text = "";
    try {
      text = String(window.getSelection() || "").trim();
    } catch (e) {
      return;
    }
    if (text.length > 4000) text = text.slice(0, 4000) + "…";
    if (text === last) return;
    last = text;
    try {
      // The callback swallows "receiving end does not exist" while the worker
      // is asleep — without it Chrome logs an unchecked-lastError warning on
      // every selection on every page.
      chrome.runtime.sendMessage({ type: "selection", text: text }, function () {
        void chrome.runtime.lastError;
      });
    } catch (e) {
      // Worker asleep or extension reloading — the next event will retry.
    }
  }

  document.addEventListener(
    "selectionchange",
    function () {
      clearTimeout(timer);
      timer = setTimeout(report, 400);
    },
    { passive: true }
  );
})();

/* Library install widget: persist method selection across the docs site.
 *
 * Storage key: agentgrep.library-install.method. Mirrors onto
 * <html data-library-install-method=...> so the CSS in widget.css
 * drives panel visibility. The accompanying prehydrate snippet
 * (docs/_ext/widgets/_prehydrate.py) replays that attribute before
 * first paint so initial paint matches the saved selection.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "agentgrep.library-install.method";
  var ATTR = "data-library-install-method";
  var SYNC_EVENT = "agentgrep:library-install:sync";

  function widgets() {
    return document.querySelectorAll(".ag-library-install");
  }

  function selectedMethod(widget) {
    var active = widget.querySelector(
      '.ag-library-install__tab[aria-selected="true"]'
    );
    return active ? active.getAttribute("data-tab-value") : null;
  }

  function syncHtmlAttr() {
    var first = document.querySelector(".ag-library-install");
    if (!first) return;
    var method = selectedMethod(first);
    if (method) {
      document.documentElement.setAttribute(ATTR, method);
    }
  }

  function select(widget, value, opts) {
    opts = opts || {};
    var tab = widget.querySelector(
      '.ag-library-install__tab[data-tab-value="' + value + '"]'
    );
    if (!tab) return;
    var tabs = widget.querySelectorAll(".ag-library-install__tab");
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.setAttribute("tabindex", on ? "0" : "-1");
    });
    var panels = widget.querySelectorAll(".ag-library-install__panel");
    panels.forEach(function (p) {
      var on = p.getAttribute("data-method") === value;
      if (on) {
        p.removeAttribute("hidden");
      } else {
        p.setAttribute("hidden", "");
      }
    });
    syncHtmlAttr();
    if (opts.persist) {
      try {
        localStorage.setItem(STORAGE_KEY, value);
      } catch (_e) {
        /* localStorage may be disabled; ignore. */
      }
    }
    if (opts.broadcast) {
      window.dispatchEvent(new Event(SYNC_EVENT));
    }
  }

  function applySavedState() {
    var saved;
    try {
      saved = localStorage.getItem(STORAGE_KEY);
    } catch (_e) {
      saved = null;
    }
    if (!saved) return;
    widgets().forEach(function (w) {
      select(w, saved, { persist: false, broadcast: false });
    });
  }

  function onClick(event) {
    var tab = event.target.closest(".ag-library-install__tab");
    if (!tab) return;
    var widget = tab.closest(".ag-library-install");
    if (!widget) return;
    var value = tab.getAttribute("data-tab-value");
    if (!value) return;
    select(widget, value, { persist: true, broadcast: true });
  }

  function onBroadcast() {
    applySavedState();
  }

  document.addEventListener("click", onClick);
  window.addEventListener(SYNC_EVENT, onBroadcast);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applySavedState);
  } else {
    applySavedState();
  }
  document.addEventListener("gp-sphinx:navigated", applySavedState);
})();

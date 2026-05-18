/* CLI install widget — SPA-safe tab sync + localStorage persistence.
 *
 * Mirrors the mcp-install widget's state machine, simplified for a
 * single tab axis (method) instead of mcp's client × method × scope
 * matrix.
 *
 * Uses document-level event delegation so listeners survive gp-sphinx
 * SPA navigation (which swaps .article-container via .replaceWith()).
 * Saved localStorage state is re-applied on DOMContentLoaded and on
 * every gp-sphinx:navigated event.
 *
 * Visibility is fully CSS-driven by <html data-cli-install-*> attrs
 * and the @layer cli-install-prehydrate rules in
 * docs/_ext/widgets/_prehydrate.py. This script never mutates the
 * panels' [hidden] attributes — it only keeps tab aria-selected and
 * the <html> data-attrs in sync with the current selection. The CSS
 * handles the rest.
 *
 * Cooldown state is split into three orthogonal axes:
 *   - `cooldown.enabled` ("1" | "0"): master on/off
 *   - `cooldown.type`    ("days" | "bypass"): cooldown flavor when enabled
 *   - `cooldown.days`    int: day count when type=days
 *
 * The checkbox in the method-tab row flips `enabled` only — it does
 * *not* change the install/settings view. The "Configure cooldowns"
 * label is the only entry point to the settings view. Inside settings,
 * touching the radio or days input auto-sets enabled=1.
 *
 * Days slots come in two flavors — uvx, uv-add and pip days bodies
 * embed a duration slot (`P<N>D`, self-refreshing in both uv and
 * pip 26.1+), while pipx days bodies embed an absolute-date slot
 * (pipx 1.8.0's bundled pip <26.1 rejects duration). Both are updated
 * on every days input change.
 *
 * Vanilla JS, no deps.
 */
(function () {
  "use strict";

  var STORAGE = {
    method: "agentgrep.cli-install.method",
    cooldownEnabled: "agentgrep.cli-install.cooldown.enabled",
    cooldownType: "agentgrep.cli-install.cooldown.type",
    cooldownDays: "agentgrep.cli-install.cooldown.days",
  };

  var DEFAULT_COOLDOWN_ENABLED = false;
  var DEFAULT_COOLDOWN_TYPE = "days";
  var DEFAULT_COOLDOWN_DAYS = 7;
  var VALID_COOLDOWN_TYPES = { days: 1, bypass: 1 };

  var SYNC_EVENT = "ag-cli-install:change";

  document.addEventListener("click", onClick);
  document.addEventListener("change", onChange);
  document.addEventListener("input", onInput);
  document.addEventListener("keydown", onKeydown);
  window.addEventListener(SYNC_EVENT, onBroadcast);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applySavedState);
  } else {
    applySavedState();
  }
  document.addEventListener("gp-sphinx:navigated", applySavedState);

  function applySavedState() {
    var widgets = document.querySelectorAll(".ag-cli-install");
    if (!widgets.length) return;
    var savedMethod = localStorage.getItem(STORAGE.method);
    var enabled = readCooldownEnabled();
    var type = readCooldownType();
    var days = readCooldownDays();
    widgets.forEach(function (widget) {
      if (savedMethod) {
        select(widget, "method", savedMethod, { persist: false, broadcast: false });
      }
      // Push the SSR default (or saved value) onto <html data-cli-install-method>
      // so the prehydrate CSS rules have the attr to match against.
      syncHtmlAttrs(widget);
      applyCooldownToWidget(widget, enabled, type, days);
    });
    // Settings view is transient — always reset to install view on
    // first load and every SPA nav.
    setView("install");
  }

  function onClick(e) {
    var action = e.target.closest("[data-action]");
    if (action) {
      var widget = action.closest(".ag-cli-install");
      if (widget) {
        if (handleCooldownAction(widget, action, e)) return;
      }
    }
    var tab = e.target.closest(".ag-cli-install__tab");
    if (!tab) return;
    var tabWidget = tab.closest(".ag-cli-install");
    if (!tabWidget) return;
    select(tabWidget, tab.dataset.tabKind, tab.dataset.tabValue, { persist: true, broadcast: true });
    // Tab clicks are an install-side action — if the user was mid-
    // configuring cooldowns, return to install view so the updated
    // panel is visible.
    setView("install");
  }

  function handleCooldownAction(widget, el, event) {
    var action = el.dataset.action;
    if (action === "cooldown-toggle") {
      // Native checkbox change runs through onChange. Don't double-handle.
      return false;
    }
    if (action === "cooldown-open") {
      var current = document.documentElement.getAttribute("data-cli-install-view");
      setView(current === "settings" ? "install" : "settings");
      event.preventDefault();
      return true;
    }
    if (action === "cooldown-help") {
      setView("settings");
      var details = widget.querySelector(".ag-cli-install__cooldown-explainer");
      if (details) details.open = true;
      event.preventDefault();
      return true;
    }
    if (action === "cooldown-back") {
      setView("install");
      event.preventDefault();
      return true;
    }
    return false;
  }

  function onChange(e) {
    var el = e.target.closest("[data-action]");
    if (!el) return;
    var widget = el.closest(".ag-cli-install");
    if (!widget) return;
    var action = el.dataset.action;
    if (action === "cooldown-toggle") {
      // Master on/off only. Does NOT change view. Does NOT change
      // type or days. Just flips ``cooldown.enabled``.
      setCooldownEnabled(el.checked, { persist: true, broadcast: true });
      return;
    }
    if (action === "cooldown-mode") {
      // Radio selection in settings -> set type, auto-enable.
      setCooldownType(el.value, { persist: true, broadcast: true });
      if (!readCooldownEnabled()) {
        setCooldownEnabled(true, { persist: true, broadcast: true });
      }
      return;
    }
    if (action === "cooldown-days") {
      // Days input in settings -> set days, auto-enable, force type=days.
      var n = clampDays(parseInt(el.value, 10));
      setCooldownDays(n, { persist: true, broadcast: true });
      if (readCooldownType() !== "days") {
        setCooldownType("days", { persist: true, broadcast: true });
      }
      if (!readCooldownEnabled()) {
        setCooldownEnabled(true, { persist: true, broadcast: true });
      }
    }
  }

  function onInput(e) {
    // ``input`` fires on every keystroke for number inputs. Mirror the
    // computed duration + cutoff date into every slot span so the
    // snippet updates in real time even before the user blurs the
    // field. localStorage write happens only on ``change`` (see
    // onChange) to avoid hammering writes.
    var el = e.target.closest('[data-action="cooldown-days"]');
    if (!el) return;
    var widget = el.closest(".ag-cli-install");
    if (!widget) return;
    var n = parseInt(el.value, 10);
    if (!isNaN(n) && n >= 1) {
      updateAllCooldownSlots(n);
      document.documentElement.setAttribute("data-cli-install-cooldown-days", String(n));
    }
  }

  function onKeydown(e) {
    var tab = e.target.closest(".ag-cli-install__tab");
    if (!tab) return;
    var widget = tab.closest(".ag-cli-install");
    if (!widget) return;
    handleKeydown(e, widget, tab);
  }

  function onBroadcast(event) {
    document.querySelectorAll(".ag-cli-install").forEach(function (widget) {
      if (widget === event.detail.origin) return;
      var kind = event.detail.kind;
      if (kind === "cooldown-enabled" || kind === "cooldown-type" || kind === "cooldown-days") {
        applyCooldownToWidget(
          widget,
          readCooldownEnabled(),
          readCooldownType(),
          readCooldownDays()
        );
        return;
      }
      select(widget, kind, event.detail.value, { persist: false, broadcast: false });
    });
  }

  function select(widget, kind, value, opts) {
    var tabSelector =
      '.ag-cli-install__tab[data-tab-kind="' + kind + '"]';
    var tabs = widget.querySelectorAll(tabSelector);
    var hasMatchingTab = false;
    tabs.forEach(function (tab) {
      var match = tab.dataset.tabValue === value;
      if (match) hasMatchingTab = true;
      tab.setAttribute("aria-selected", match ? "true" : "false");
      tab.setAttribute("tabindex", match ? "0" : "-1");
    });
    if (!hasMatchingTab) return;

    syncHtmlAttrs(widget);

    if (opts.persist) {
      localStorage.setItem(STORAGE[kind], value);
    }
    if (opts.broadcast) {
      window.dispatchEvent(
        new CustomEvent(SYNC_EVENT, {
          detail: { origin: widget, kind: kind, value: value },
        })
      );
    }
  }

  // Mirror the widget's current tab state onto <html> for the method
  // axis. The prehydrate CSS rules need both data-cli-install-method
  // and data-cli-install-cooldown-* attrs set for the (method,
  // cooldown) panel rule to match.
  function syncHtmlAttrs(widget) {
    var html = document.documentElement;
    var method = ariaSelected(widget, "method");
    if (method) html.setAttribute("data-cli-install-method", method);
  }

  function ariaSelected(widget, kind) {
    var tab = widget.querySelector(
      '.ag-cli-install__tab[data-tab-kind="' + kind + '"][aria-selected="true"]'
    );
    return tab ? tab.dataset.tabValue : null;
  }

  function handleKeydown(event, widget, tab) {
    var kind = tab.dataset.tabKind;
    var tabSelector =
      '.ag-cli-install__tab[data-tab-kind="' + kind + '"]';
    var tabs = Array.prototype.slice.call(widget.querySelectorAll(tabSelector));
    var current = tabs.indexOf(tab);
    var next = current;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowDown":
        next = (current + 1) % tabs.length;
        break;
      case "ArrowLeft":
      case "ArrowUp":
        next = (current - 1 + tabs.length) % tabs.length;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = tabs.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    tabs[next].focus();
    select(widget, kind, tabs[next].dataset.tabValue, { persist: true, broadcast: true });
  }

  // -------- cooldown helpers --------------------------------------------

  function readCooldownEnabled() {
    var v = localStorage.getItem(STORAGE.cooldownEnabled);
    if (v === "1") return true;
    if (v === "0") return false;
    return DEFAULT_COOLDOWN_ENABLED;
  }

  function readCooldownType() {
    var t = localStorage.getItem(STORAGE.cooldownType);
    return VALID_COOLDOWN_TYPES[t] ? t : DEFAULT_COOLDOWN_TYPE;
  }

  function readCooldownDays() {
    var d = parseInt(localStorage.getItem(STORAGE.cooldownDays), 10);
    return clampDays(d);
  }

  function clampDays(n) {
    if (isNaN(n)) return DEFAULT_COOLDOWN_DAYS;
    if (n < 1) return 1;
    if (n > 365) return 365;
    return n;
  }

  function setCooldownEnabled(enabled, opts) {
    var v = enabled ? "1" : "0";
    document.documentElement.setAttribute("data-cli-install-cooldown-enabled", v);
    if (opts.persist) localStorage.setItem(STORAGE.cooldownEnabled, v);
    document.querySelectorAll(".ag-cli-install").forEach(function (widget) {
      applyCooldownToWidget(widget, enabled, readCooldownType(), readCooldownDays());
    });
    if (opts.broadcast) {
      window.dispatchEvent(
        new CustomEvent(SYNC_EVENT, {
          detail: { origin: null, kind: "cooldown-enabled", value: v },
        })
      );
    }
  }

  function setCooldownType(type, opts) {
    if (!VALID_COOLDOWN_TYPES[type]) return;
    document.documentElement.setAttribute("data-cli-install-cooldown-type", type);
    if (opts.persist) localStorage.setItem(STORAGE.cooldownType, type);
    document.querySelectorAll(".ag-cli-install").forEach(function (widget) {
      applyCooldownToWidget(widget, readCooldownEnabled(), type, readCooldownDays());
    });
    if (opts.broadcast) {
      window.dispatchEvent(
        new CustomEvent(SYNC_EVENT, {
          detail: { origin: null, kind: "cooldown-type", value: type },
        })
      );
    }
  }

  function setCooldownDays(days, opts) {
    var n = clampDays(days);
    document.documentElement.setAttribute("data-cli-install-cooldown-days", String(n));
    if (opts.persist) localStorage.setItem(STORAGE.cooldownDays, String(n));
    updateAllCooldownSlots(n);
    document.querySelectorAll('[data-action="cooldown-days"]').forEach(function (input) {
      if (document.activeElement !== input) input.value = String(n);
    });
    if (opts.broadcast) {
      window.dispatchEvent(
        new CustomEvent(SYNC_EVENT, {
          detail: { origin: null, kind: "cooldown-days", value: n },
        })
      );
    }
  }

  function daysToIsoDate(n) {
    // YYYY-MM-DD in UTC, for pipx (pipx 1.8.0's bundled pip <26.1
    // rejects the duration form).
    var ms = Date.now() - n * 86400000;
    return new Date(ms).toISOString().slice(0, 10);
  }

  function daysToIsoDuration(n) {
    // P<N>D in ISO 8601. Used by uvx, uv-add and pip days panels.
    return "P" + n + "D";
  }

  function updateAllCooldownSlots(n) {
    var duration = daysToIsoDuration(n);
    var date = daysToIsoDate(n);
    document.querySelectorAll(".ag-cli-install [data-cooldown-duration-slot]").forEach(function (slot) {
      slot.textContent = duration;
    });
    document.querySelectorAll(".ag-cli-install [data-cooldown-date-slot]").forEach(function (slot) {
      slot.textContent = date;
    });
  }

  function applyCooldownToWidget(widget, enabled, type, days) {
    var toggle = widget.querySelector(".ag-cli-install__cooldown-toggle");
    if (toggle) toggle.checked = !!enabled;
    widget.querySelectorAll('[data-action="cooldown-mode"]').forEach(function (radio) {
      radio.checked = radio.value === type;
    });
    var daysInput = widget.querySelector('[data-action="cooldown-days"]');
    if (daysInput && document.activeElement !== daysInput) {
      daysInput.value = String(days);
    }
    var duration = daysToIsoDuration(days);
    var date = daysToIsoDate(days);
    widget.querySelectorAll("[data-cooldown-duration-slot]").forEach(function (slot) {
      slot.textContent = duration;
    });
    widget.querySelectorAll("[data-cooldown-date-slot]").forEach(function (slot) {
      slot.textContent = date;
    });
  }

  function setView(view) {
    var prev = document.documentElement.getAttribute("data-cli-install-view");
    if (prev === view) return;
    document.documentElement.setAttribute("data-cli-install-view", view);
  }
})();

"use strict";
(() => {
  const key = "sentinel.newtab.theme";
  const themes = new Set([
    "dark",
    "light",
    "neon",
    "neon-green",
    "neon-red",
    "mono",
    "terminal",
    "terminal-catppuccin",
    "terminal-red",
    "terminal-blue",
    "terminal-purple",
    "terminal-tokyo",
    "terminal-tokyo-dark",
  ]);
  const normalize = (value) => (themes.has(value) ? value : "dark");

  function apply(value) {
    const theme = normalize(value);
    document.documentElement.dataset.theme = theme;
    const select = document.getElementById("theme-select");
    if (select) select.value = theme;
  }

  // Apply before the stylesheet loads to avoid a flash of the default theme.
  let saved = "dark";
  try {
    saved = localStorage.getItem(key);
  } catch {
    // The selector still works for this page if persistence is unavailable.
  }
  apply(saved);

  document.addEventListener("DOMContentLoaded", () => {
    const select = document.getElementById("theme-select");
    if (!select) return;
    select.value = document.documentElement.dataset.theme;
    select.addEventListener("change", () => {
      const theme = normalize(select.value);
      apply(theme);
      try {
        localStorage.setItem(key, theme);
      } catch {
        // A cosmetic preference never prevents navigation or search.
      }
    });
  });

  // Keep already-open New Tab and catalog pages in step with the selection.
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) apply(event.newValue);
  });
})();

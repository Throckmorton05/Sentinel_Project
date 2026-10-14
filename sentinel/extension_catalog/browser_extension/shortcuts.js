"use strict";
(() => {
  const key = "sentinel.newtab.shortcuts";
  const limit = 24;
  const grid = document.getElementById("shortcuts");
  const add = document.getElementById("add-shortcut");
  const message = document.getElementById("shortcut-message");
  const dialog = document.getElementById("shortcut-dialog");
  const form = document.getElementById("shortcut-form");
  const name = document.getElementById("shortcut-name");
  const address = document.getElementById("shortcut-url");
  const error = document.getElementById("shortcut-error");
  const remove = document.getElementById("shortcut-remove");
  let shortcuts = [];
  let editing;

  function website(value) {
    const text = value.trim();
    if (!text || text.length > 2048)
      throw new Error("Enter a website address.");
    // Bare domains use HTTPS. Explicit schemes must be ordinary web links.
    const explicitScheme =
      /^[a-z][a-z\d+.-]*:/i.test(text) && !/^[^/\s]+:\d+(?:[/?#]|$)/.test(text);
    const url = new URL(explicitScheme ? text : `https://${text}`);
    if (
      !["https:", "http:"].includes(url.protocol) ||
      !url.hostname ||
      url.username ||
      url.password ||
      url.href.length > 2048
    )
      throw new Error(
        "Use an http:// or https:// website address without login details.",
      );
    return url.href;
  }

  function read() {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return [];
      const data = JSON.parse(raw);
      if (
        data.version !== 1 ||
        !Array.isArray(data.shortcuts) ||
        data.shortcuts.length > limit
      )
        throw new Error("Invalid shortcuts");
      const seen = new Set();
      return data.shortcuts.map((entry) => {
        if (
          !entry ||
          typeof entry.id !== "string" ||
          !/^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(entry.id) ||
          seen.has(entry.id) ||
          typeof entry.name !== "string" ||
          !entry.name.trim() ||
          entry.name.length > 80 ||
          typeof entry.url !== "string"
        )
          throw new Error("Invalid shortcut");
        seen.add(entry.id);
        return {
          id: entry.id,
          name: entry.name.trim(),
          url: website(entry.url),
        };
      });
    } catch {
      message.textContent =
        "Saved shortcuts could not be loaded. Changes may only be available in this tab.";
      return shortcuts;
    }
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function render() {
    grid.replaceChildren();
    for (const entry of shortcuts) {
      const tile = node("div", "shortcut-tile");
      const link = node("a", "shortcut-link");
      link.href = entry.url;
      link.title = `${entry.name} · ${new URL(entry.url).hostname}`;
      const icon = node(
        "span",
        "shortcut-icon",
        entry.name.slice(0, 1).toLocaleUpperCase(),
      );
      icon.setAttribute("aria-hidden", "true");
      // Chrome supplies the site's favicon without a third-party icon service.
      const favicon = node("img", "shortcut-favicon");
      favicon.alt = "";
      favicon.width = 24;
      favicon.height = 24;
      favicon.hidden = true;
      favicon.addEventListener("load", () => {
        icon.replaceChildren(favicon);
        favicon.hidden = false;
      }, { once: true });
      favicon.addEventListener("error", () => favicon.remove(), { once: true });
      const faviconUrl = new URL(chrome.runtime.getURL("/_favicon/"));
      faviconUrl.searchParams.set("pageUrl", entry.url);
      faviconUrl.searchParams.set("size", "32");
      favicon.src = faviconUrl.href;
      icon.append(favicon);
      link.append(icon, node("span", "shortcut-label", entry.name));
      const edit = node("button", "shortcut-edit", "⋯");
      edit.type = "button";
      edit.setAttribute("aria-label", `Edit ${entry.name} shortcut`);
      edit.title = "Edit shortcut";
      edit.addEventListener("click", () => open(entry));
      tile.append(link, edit);
      grid.append(tile);
    }
    add.disabled = shortcuts.length >= limit;
    add.title = add.disabled
      ? `You can save up to ${limit} shortcuts.`
      : "Add a website shortcut";
  }

  function save(next) {
    shortcuts = next;
    try {
      localStorage.setItem(key, JSON.stringify({ version: 1, shortcuts }));
      message.textContent =
        shortcuts.length >= limit
          ? `You have ${limit} shortcuts. Remove one to add another.`
          : "";
    } catch {
      message.textContent =
        "Your browser could not save this change. It is available in this tab only.";
    }
    render();
    dialog.close();
  }

  function open(entry) {
    editing = entry?.id;
    name.value = entry?.name ?? "";
    address.value = entry?.url ?? "";
    error.textContent = "";
    remove.hidden = !editing;
    document.getElementById("shortcut-title").textContent = editing
      ? "Edit shortcut"
      : "Add shortcut";
    document.getElementById("shortcut-save").textContent = editing
      ? "Save"
      : "Add";
    dialog.showModal();
    name.focus();
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      const current = read();
      const title = name.value.trim();
      if (!title || title.length > 80)
        throw new Error("Enter a name for the shortcut.");
      let url;
      try {
        url = website(address.value);
      } catch {
        throw new Error(
          "Enter an http:// or https:// website address without login details.",
        );
      }
      if (editing && !current.some((entry) => entry.id === editing))
        throw new Error(
          "This shortcut was removed in another tab. Close and try again.",
        );
      if (!editing && current.length >= limit)
        throw new Error(`You can save up to ${limit} shortcuts.`);
      const entry = { id: editing ?? crypto.randomUUID(), name: title, url };
      save(
        editing
          ? current.map((item) => (item.id === editing ? entry : item))
          : [...current, entry],
      );
    } catch (failure) {
      error.textContent = failure.message;
    }
  });
  remove.addEventListener("click", () =>
    save(read().filter((entry) => entry.id !== editing)),
  );
  add.addEventListener("click", () => open());
  document
    .getElementById("shortcut-cancel")
    .addEventListener("click", () => dialog.close());
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) {
      shortcuts = read();
      render();
    }
  });
  shortcuts = read();
  render();
})();

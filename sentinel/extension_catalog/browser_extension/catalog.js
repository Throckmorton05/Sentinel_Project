"use strict";
(() => {
  const endpoint = "http://127.0.0.1:8765/api/browser/extensions";
  const search = document.getElementById("search");
  const categories = document.getElementById("categories");
  const grid = document.getElementById("extensions");
  const status = document.getElementById("catalog-status");
  const required = document.getElementById("required");
  const included = document.getElementById("included-section");
  const reload = document.getElementById("reload");
  const guardianNote = document.getElementById("guardian-note");
  const approvalMessage = document.getElementById("approval-message");
  const dialog = document.getElementById("guardian-dialog");
  const form = document.getElementById("guardian-form");
  const pin = document.getElementById("guardian-pin");
  const dialogError = document.getElementById("guardian-error");
  const submit = document.getElementById("guardian-submit");
  const cancel = document.getElementById("guardian-cancel");
  let entries = [];
  let guardianRequired = false;
  let approvalTarget;
  let approvalOperation;
  let submitting = false;
  let selected = "All";
  let ready = false;
  let controller;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function storeURL(entry) {
    const url = new URL(entry.store_url);
    if (
      url.origin !== "https://chromewebstore.google.com" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      !url.pathname.startsWith("/detail/") ||
      url.pathname.split("/").pop() !== entry.id ||
      !/^[a-p]{32}$/.test(entry.id)
    ) {
      throw new Error("Invalid store link");
    }
    return url.href;
  }

  function render() {
    if (!ready) return;
    const query = search.value.trim().toLocaleLowerCase();
    const matches = entries.filter(
      (entry) =>
        (selected === "All" || entry.category === selected) &&
        [entry.name, entry.developer, entry.description, entry.category]
          .join(" ")
          .toLocaleLowerCase()
          .includes(query),
    );
    grid.replaceChildren();
    for (const entry of matches) {
      const card = element("article", "extension-card");
      const top = element("div", "card-top");
      const icon = element("span", "extension-icon", entry.name.slice(0, 1));
      icon.setAttribute("aria-hidden", "true");
      top.append(icon, element("span", "approved-badge", "Approved"));
      const link = element("a", "store-button", "View in Chrome Web Store ↗");
      link.href = storeURL(entry);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute(
        "aria-label",
        `View ${entry.name} in Chrome Web Store (opens a new tab)`,
      );
      card.append(
        top,
        element("p", "card-category", entry.category),
        element("h2", "", entry.name),
        element("p", "developer", `By ${entry.developer}`),
        element("p", "card-description", entry.description),
        element("p", "permission-note", entry.permissions_note),
      );
      if (guardianRequired) {
        const action = element(
          "button",
          "store-button",
          entry.guardian_approved ? "Revoke approval" : "Add with Guardian PIN",
        );
        action.type = "button";
        action.addEventListener("click", () =>
          openGuardian(entry, entry.guardian_approved ? "revoke" : "approve"),
        );
        link.className = "details-link";
        link.textContent = "View store details ↗";
        card.append(
          element(
            "p",
            "approval-status",
            entry.policy_pending
              ? "Approval change pending"
              : entry.guardian_approved
                ? "Guardian approved"
                : "Guardian approval required",
          ),
          action,
          link,
        );
      } else {
        card.append(link);
      }
      grid.append(card);
    }
    status.textContent = matches.length
      ? `${matches.length} approved ${matches.length === 1 ? "extension" : "extensions"}`
      : entries.length
        ? "No extensions match. Try another search or category."
        : "No optional extensions are approved yet.";
    for (const button of categories.children)
      button.setAttribute(
        "aria-pressed",
        String(button.textContent === selected),
      );
  }

  async function load() {
    if (controller) controller.abort();
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort(), 5000);
    ready = false;
    entries = [];
    grid.replaceChildren();
    categories.replaceChildren();
    required.replaceChildren();
    included.hidden = true;
    status.textContent = "Loading approved extensions…";
    reload.disabled = true;
    try {
      const response = await fetch(endpoint, {
        cache: "no-store",
        credentials: "omit",
        signal: request.signal,
      });
      if (!response.ok) throw new Error("Catalog unavailable");
      const data = await response.json();
      if (
        data.schema_version !== 1 ||
        typeof data.guardian_required !== "boolean" ||
        !Array.isArray(data.extensions) ||
        !Array.isArray(data.required_extensions)
      )
        throw new Error("Invalid catalog");
      for (const entry of data.extensions) {
        for (const field of [
          "id",
          "name",
          "developer",
          "category",
          "description",
          "permissions_note",
          "store_url",
        ])
          if (typeof entry[field] !== "string" || !entry[field].trim())
            throw new Error("Invalid entry");
        storeURL(entry);
        if (
          typeof entry.guardian_approved !== "boolean" ||
          typeof entry.policy_pending !== "boolean"
        )
          throw new Error("Invalid approval state");
      }
      for (const entry of data.required_extensions)
        if (
          typeof entry.name !== "string" ||
          typeof entry.description !== "string"
        )
          throw new Error("Invalid required extension");
      if (controller !== request) return;
      entries = data.extensions;
      guardianRequired = data.guardian_required;
      guardianNote.hidden = !guardianRequired;
      const names = [
        "All",
        ...new Set(entries.map((entry) => entry.category).sort()),
      ];
      if (!names.includes(selected)) selected = "All";
      for (const name of names) {
        const button = element("button", "category-button", name);
        button.type = "button";
        button.addEventListener("click", () => {
          selected = name;
          render();
        });
        categories.append(button);
      }
      for (const entry of data.required_extensions) {
        const card = element("article", "required-card");
        card.append(
          element("span", "included-badge", "Included"),
          element("h3", "", entry.name),
          element("p", "", entry.description),
        );
        required.append(card);
      }
      included.hidden = data.required_extensions.length === 0;
      ready = true;
      render();
    } catch (error) {
      if (controller !== request) return;
      entries = [];
      grid.replaceChildren();
      categories.replaceChildren();
      required.replaceChildren();
      included.hidden = true;
      guardianNote.hidden = true;
      status.textContent =
        "The approved list is unavailable. Try Refresh when Sentinel is running.";
    } finally {
      clearTimeout(timeout);
      if (controller === request) reload.disabled = false;
    }
  }

  function openGuardian(entry, operation) {
    approvalTarget = entry;
    approvalOperation = operation;
    document.getElementById("guardian-title").textContent =
      operation === "approve" ? "Add extension" : "Revoke approval";
    document.getElementById("guardian-description").textContent =
      operation === "approve"
        ? `Add ${entry.name}? Your guardian's approval is required.`
        : `Revoke approval for ${entry.name}? It will be blocked in Guardian mode.`;
    submit.textContent =
      operation === "approve" ? "Approve and add" : "Revoke approval";
    pin.value = "";
    dialogError.textContent = "";
    dialog.showModal();
    pin.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || !approvalTarget) return;
    submitting = true;
    submit.disabled = cancel.disabled = pin.disabled = true;
    dialogError.textContent = "";
    const secret = pin.value;
    pin.value = "";
    const mutation = new AbortController();
    const timeout = setTimeout(() => mutation.abort(), 10000);
    try {
      const response = await fetch(`${endpoint}/${approvalOperation}`, {
        method: "POST",
        credentials: "omit",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          extension_id: approvalTarget.id,
          guardian_pin: secret,
        }),
        signal: mutation.signal,
      });
      const result = await response.json();
      if (!response.ok || !result.ok)
        throw new Error(
          result.error?.message ||
            "Approval could not be confirmed. Try Refresh.",
        );
      dialog.close();
      approvalMessage.textContent =
        result.result.application.status === "applied"
          ? approvalOperation === "approve"
            ? `Approved. Chrome is set to add ${approvalTarget.name}.`
            : `Approval revoked for ${approvalTarget.name}.`
          : "Your change was saved and is waiting to be applied. Try Refresh shortly.";
      await load();
    } catch (error) {
      dialogError.textContent =
        error.name === "AbortError"
          ? "The change could not be confirmed. Refresh the catalog before retrying."
          : error.message;
    } finally {
      clearTimeout(timeout);
      submitting = false;
      submit.disabled = cancel.disabled = pin.disabled = false;
      if (dialog.open) pin.focus();
    }
  });
  cancel.addEventListener("click", () => {
    pin.value = "";
    dialog.close();
  });
  dialog.addEventListener("cancel", (event) => {
    if (submitting) event.preventDefault();
    else pin.value = "";
  });
  dialog.addEventListener("close", () => {
    pin.value = "";
    dialogError.textContent = "";
  });
  search.addEventListener("input", render);
  reload.addEventListener("click", load);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });
  load();
})();

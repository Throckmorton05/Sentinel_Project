// Policy reads and bounded block telemetry; no settings mutations.
const POLICY_safe_steam = "safe_steam";
const POLICY_URL = "http://127.0.0.1:8765/api/safe-steam";

async function readPolicy() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    try {
        const response = await fetch(POLICY_URL, {
            cache: "no-store", credentials: "omit", redirect: "error",
            signal: controller.signal
        });
        if (!response.ok) return true;
        const data = await response.json();
        return data[POLICY_safe_steam] !== false;
    } catch (_) {
        return true;
    } finally {
        clearTimeout(timer);
    }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id === chrome.runtime.id && message?.type === "sentinel.activity.block") {
        if (!sender.url || sender.frameId !== 0) return false;
        let url;
        try { url = new URL(sender.url); } catch (_) { return false; }
        if (url.hostname !== "store.steampowered.com" || url.protocol !== "https:") return false;
        fetch("http://127.0.0.1:8765/api/activity", {
            method: "POST", credentials: "omit", redirect: "error",
            headers: {"Content-Type": "application/json"},
            signal: AbortSignal.timeout(3000),
            body: JSON.stringify({kind: "website_block", event_id: crypto.randomUUID(),
                occurred_at: new Date().toISOString(), url: url.origin + url.pathname,
                reason: String(message.reason || "Restricted content").slice(0, 120)})
        }).then(response => sendResponse({accepted: response.ok}))
          .catch(() => sendResponse({accepted: false}));
        return true;
    }
    if (sender.id !== chrome.runtime.id || message?.type !== "sentinel.policy.get") {
        return false;
    }
    readPolicy().then(enabled => sendResponse({enabled}));
    return true;
});

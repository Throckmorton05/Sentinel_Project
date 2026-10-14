// Only this fixed read is exposed to content scripts; no mutation endpoint.
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
    if (sender.id !== chrome.runtime.id || message?.type !== "sentinel.policy.get") {
        return false;
    }
    readPolicy().then(enabled => sendResponse({enabled}));
    return true;
});

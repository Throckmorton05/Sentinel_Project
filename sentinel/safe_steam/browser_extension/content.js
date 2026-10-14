console.log("[Sentinel Safe Steam] Content script loaded.");

// Terms that identify restricted Steam content descriptors.
const BLOCKED_DESCRIPTOR_TERMS = [
    "nudity",
    "sexual content",
    "sexual acts",
    "sexual material",
    "sexual nature",
    "nsfw",
    "pornography",
    "pornographic",
    "adult content",
    "sex scenes",
    "sexual"
];

// Terms that should block Steam searches/navigation immediately.
const BLOCKED_SEARCH_TERMS = [
    "futa",
    "futanari",
    "hentai",
    "bdsm",
    "sex",
    "sexy",
    "porn",
    "porno",
    "pornographic",
    "pornography",
    "nude",
    "nudity",
    "nsfw",
    "adult",
    "erotic",
    "xxx",
    "lewd"
];

let pageBlocked = false;


function normalizeText(text) {
    return text
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();
}


function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}


// Match complete words/phrases instead of arbitrary substrings.
// This prevents "sex" from matching unrelated words such as "sextant".
function containsBlockedTerm(text, term) {
    const normalizedText = normalizeText(text);
    const normalizedTerm = normalizeText(term);

    if (!normalizedText || !normalizedTerm) {
        return false;
    }

    const pattern = new RegExp(
        `(^|[^a-z0-9])${escapeRegExp(normalizedTerm)}([^a-z0-9]|$)`,
        "i"
    );

    return pattern.test(normalizedText);
}


const BLOCK_OVERLAY_ID = "sentinel-safe-steam-block-overlay";

function blockPage(reason) {
    if (!document.documentElement) return;
    pageBlocked = true;
    let overlay = document.getElementById(BLOCK_OVERLAY_ID);
    if (overlay) return;
    chrome.runtime.sendMessage({type: "sentinel.activity.block", reason: String(reason)}, () => {
        void chrome.runtime.lastError;
    });
    overlay = document.createElement("div");
    overlay.id = BLOCK_OVERLAY_ID;
    overlay.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;background:#111827;color:white;font:18px sans-serif;text-align:center";
    const panel = document.createElement("div");
    const title = document.createElement("h1");
    title.textContent = "Blocked by Sentinel";
    const description = document.createElement("p");
    description.textContent = "This Steam page contains content restricted by Sentinel.";
    panel.append(title, description);
    overlay.appendChild(panel);
    document.documentElement.appendChild(overlay);
}

function unblockPage() {
    document.getElementById(BLOCK_OVERLAY_ID)?.remove();
    pageBlocked = false;
}

// Check Steam's search query directly from the URL.
// Example: /search/?term=futanari
function checkSearchQuery() {
    const params = new URLSearchParams(window.location.search);
    const searchTerm = params.get("term") || "";

    if (!searchTerm) {
        return false;
    }

    console.log(
        "[Sentinel Safe Steam] Steam search query:",
        searchTerm
    );

    for (const term of BLOCKED_SEARCH_TERMS) {
        if (containsBlockedTerm(searchTerm, term)) {
            blockPage(`blocked search term: ${term}`);
            return true;
        }
    }

    return false;
}


// Check high-value page metadata as an additional layer.
function checkPageMetadata() {
    const candidates = [];

    if (document.title) {
        candidates.push(document.title);
    }

    const metaDescription = document.querySelector(
        'meta[name="description"]'
    );

    if (metaDescription?.content) {
        candidates.push(metaDescription.content);
    }

    const ogTitle = document.querySelector(
        'meta[property="og:title"]'
    );

    if (ogTitle?.content) {
        candidates.push(ogTitle.content);
    }

    const metadataText = candidates.join(" ");

    for (const term of BLOCKED_SEARCH_TERMS) {
        if (containsBlockedTerm(metadataText, term)) {
            blockPage(`blocked page term: ${term}`);
            return true;
        }
    }

    return false;
}


function checkDescriptors() {
    const descriptorElement = document.querySelector(
        "#game_area_content_descriptors"
    );

    if (!descriptorElement) {
        console.log(
            "[Sentinel Safe Steam] No content descriptor found."
        );
        return false;
    }

    const rawText = descriptorElement.innerText;
    const text = normalizeText(rawText);

    console.log(
        "[Sentinel Safe Steam] Content descriptor found:"
    );

    console.log(rawText);

    for (const term of BLOCKED_DESCRIPTOR_TERMS) {
        if (containsBlockedTerm(text, term)) {
            blockPage(`blocked descriptor: ${term}`);
            return true;
        }
    }

    console.log("[Sentinel Safe Steam] Content allowed.");

    return false;
}


function runChecks() {
    if (!SentinelPolicy.enabled) {
        unblockPage();
        return;
    }
    if (checkSearchQuery() || checkPageMetadata() || checkDescriptors()) return;
    unblockPage();
}

SentinelPolicy.start(runChecks);
runChecks();
setTimeout(runChecks, 2000);
setTimeout(runChecks, 5000);
let lastUrl = window.location.href;
setInterval(() => {
    if (window.location.href !== lastUrl) {
        lastUrl = window.location.href;
        unblockPage();
        runChecks();
    }
    // Also restores an overlay removed by the site's own DOM updates.
    if (pageBlocked && !document.getElementById(BLOCK_OVERLAY_ID)) runChecks();
}, 500);
const observer = new MutationObserver(() => {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(runChecks, 150);
});
let checkTimer;
observer.observe(document, {childList: true, subtree: true});

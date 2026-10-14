console.log("[Sentinel Safe Crunchyroll] Content script loaded.");

const BLOCKED_ADVISORY_TERMS = [
    "sexual content",
    "sexualized imagery",
    "nudity"
];

// Only sexual categories contribute to the three-advisory threshold.
// Each category counts once, regardless of repetitions in the page's markup.
const NEGATIVE_ADVISORY_TERMS = [
    "sexual content",
    "sexualized imagery",
    "suggestive dialogue",
    "nudity"
];
// Recognize other labels so a nonsexual advisory can be read and allowed.
// These categories do not contribute to the blocking count.
const KNOWN_ADVISORY_TERMS = [
    ...NEGATIVE_ADVISORY_TERMS,
    "violence",
    "profanity",
    "smoking",
    "slurs"
];
const ADVISORY_BLOCK_THRESHOLD = 3;

const BLOCKED_AGE_RATINGS = [
    "16+",
    "18+"
];

const BLOCK_OVERLAY_ID = "sentinel-safe-crunchyroll-block-overlay";

let pageBlocked = false;
let lastUrl = window.location.href;
let checkTimer = null;
let blockedVideos = [];
let previousOverflow = null;

function normalizeText(text) {
    return text
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();
}


function findAdvisoryTerms(advisoryText, terms) {
    const text = normalizeText(advisoryText);
    return terms.filter(term =>
        new RegExp(`(^|[^a-z])${term}([^a-z]|$)`).test(text)
    );
}


function findNegativeAdvisories(advisoryText) {
    return findAdvisoryTerms(advisoryText, NEGATIVE_ADVISORY_TERMS);
}


function stopMediaPlayback() {
    blockedVideos = [];

    const videos = document.querySelectorAll("video");

    for (const video of videos) {
        blockedVideos.push({
            video: video,
            wasMuted: video.muted
        });

        video.pause();
        video.muted = true;

        console.log(
            "[Sentinel Safe Crunchyroll] Video paused and muted."
        );
    }
}


function restoreMediaState() {
    for (const entry of blockedVideos) {
        /*
         * Restore the user's original mute setting.
         *
         * We intentionally do NOT automatically call play().
         * If Sentinel blocked an episode, we don't want leaving the
         * block screen to unexpectedly resume playback.
         */
        if (entry.video && entry.video.isConnected) {
            entry.video.muted = entry.wasMuted;
        }
    }

    blockedVideos = [];
}

function enforceMediaBlock() {
    if (!pageBlocked) {
        return;
    }

    const videos = document.querySelectorAll("video");

    for (const video of videos) {
        if (!blockedVideos.some(entry => entry.video === video)) {
            blockedVideos.push({video, wasMuted: video.muted});
        }
        if (!video.paused) {
            video.pause();
        }

        if (!video.muted) {
            video.muted = true;
        }
    }
}



function blockPage(reason) {
    console.warn(
        "[Sentinel Safe Crunchyroll] Blocking page:",
        reason
    );

    if (!document.documentElement) return;
    if (!pageBlocked) {
        previousOverflow = document.documentElement.style.overflow;
        stopMediaPlayback();
    }
    pageBlocked = true;

    /*
     * Do not destroy Crunchyroll's page.
     *
     * Crunchyroll uses single-page-app navigation, so replacing
     * document.documentElement breaks browser Back/Forward behavior.
     *
     * Instead, place a full-screen overlay over the existing page.
     */
    let overlay = document.getElementById(BLOCK_OVERLAY_ID);

    if (overlay) {
        return;
    }

    chrome.runtime.sendMessage({type: "sentinel.activity.block", reason: String(reason)}, () => {
        void chrome.runtime.lastError;
    });
    overlay = document.createElement("div");
    overlay.id = BLOCK_OVERLAY_ID;

    overlay.style.cssText = `
        position: fixed;
        inset: 0;
        z-index: 2147483647;

        display: flex;
        align-items: center;
        justify-content: center;

        background: #000000;
        color: white;

        font-family: sans-serif;
        text-align: center;
    `;

    overlay.innerHTML = `
        <div style="
            max-width: 650px;
            padding: 40px;
        ">
            <h1 style="
                font-size: 42px;
                margin: 0 0 20px 0;
                color: #F47521;
            ">
                Blocked by Sentinel
            </h1>

            <p style="
                font-size: 18px;
                line-height: 1.5;
                color: #FFFFFF;
                margin: 0;
            ">
                This Crunchyroll title contains content
                restricted by Sentinel.
            </p>
        </div>
    `;
    document.documentElement.style.overflow = "hidden";

    document.documentElement.appendChild(overlay);
}


function unblockPage() {
    const overlay = document.getElementById(BLOCK_OVERLAY_ID);

    if (overlay) {
        overlay.remove();
    }

    if (document.documentElement && previousOverflow !== null) {
        document.documentElement.style.overflow = previousOverflow;
    }
    previousOverflow = null;

    restoreMediaState();

    if (pageBlocked) {
        console.log(
            "[Sentinel Safe Crunchyroll] Page unblocked."
        );
    }

    pageBlocked = false;
}


function findAgeRating(container) {
    if (!container) return null;
    // The badge can be an SVG and therefore absent from innerText.
    const ratingElement = container.querySelector('[data-t^="universal-rating-"]');
    const badge = (ratingElement?.getAttribute("data-t") || "").match(
        /^universal-rating-(\d+)-svg$/
    );
    if (badge) return `${badge[1]}+`;
    const text = normalizeText(container.innerText || "");
    const rating = text.match(/(?:^|[^a-z0-9])(\d{1,2})\+(?=$|[^0-9])/);
    return rating ? `${rating[1]}+` : null;
}


function findContentAdvisory() {
    const elements = document.querySelectorAll(
        "div, section, span, p, h1, h2, h3, h4, h5, h6"
    );
    let candidate = null;
    let shortestText = Infinity;
    for (const element of elements) {
        if (element.closest(`#${BLOCK_OVERLAY_ID}`)) continue;
        const text = normalizeText(element.innerText || "");
        // Both a separate heading and "Content Advisory: descriptors" work.
        if (/^content advisory(?:\s*:|\s|$)/.test(text) && text.length < shortestText) {
            candidate = element;
            shortestText = text.length;
        }
    }
    return candidate;
}


function getContentAdvisory(advisoryElement) {
    // Use the smallest useful advisory container. Do not require an age badge
    // or scan the surrounding synopsis for extra negative-category words.
    let current = advisoryElement;
    for (let depth = 0; depth < 10 && current; depth++) {
        const text = normalizeText(current.innerText || "");
        const advisories = findNegativeAdvisories(text);
        const knownAdvisories = findAdvisoryTerms(text, KNOWN_ADVISORY_TERMS);
        if (text.includes("content advisory") && knownAdvisories.length && text.length < 3000) {
            // Inline advisory text can have its SVG badge as a sibling.
            const parent = current.parentElement;
            const ratingContainer = parent && normalizeText(parent.innerText || "").length < 3000
                ? parent : null;
            return {
                text,
                advisories,
                ageRating: findAgeRating(current) || findAgeRating(ratingContainer)
            };
        }
        current = current.parentElement;
    }
    return null;
}


function checkContentAdvisory() {
    const advisory = getContentAdvisory(findContentAdvisory());
    if (!advisory) return false;

    // Three distinct sexual categories block independently of a rating. The existing
    // 16+/18+ sexual-content rule still catches smaller advisory lists.
    const blockedDescriptor = BLOCKED_ADVISORY_TERMS.find(
        term => advisory.advisories.includes(term)
    );
    if (advisory.advisories.length >= ADVISORY_BLOCK_THRESHOLD) {
        blockPage(`${advisory.advisories.length} sexual content advisories`);
        return true;
    }
    if (BLOCKED_AGE_RATINGS.includes(advisory.ageRating) && blockedDescriptor) {
        blockPage(`Rating: ${advisory.ageRating}; Content Advisory: ${blockedDescriptor}`);
        return true;
    }
    unblockPage();
    return false;
}


function runChecks() {
    if (!SentinelPolicy.enabled) {
        unblockPage();
        return;
    }
    checkContentAdvisory();

    if (pageBlocked) {
        enforceMediaBlock();
    }
}


/*
 * Debounce checks.
 *
 * Crunchyroll changes the DOM frequently while loading, so running the
 * full advisory scan on every single DOM mutation would cause a large
 * number of unnecessary checks.
 */
function scheduleCheck(delay = 150) {
    clearTimeout(checkTimer);

    checkTimer = setTimeout(() => {
        runChecks();
    }, delay);
}


/*
 * Crunchyroll is a dynamic web application, so the advisory might not
 * exist when document_start initially runs.
 */

SentinelPolicy.start(() => scheduleCheck(0));
scheduleCheck(0);

setTimeout(() => scheduleCheck(0), 500);
setTimeout(() => scheduleCheck(0), 1000);
setTimeout(() => scheduleCheck(0), 2000);
setTimeout(() => scheduleCheck(0), 5000);


/*
 * Watch for dynamically inserted Crunchyroll content.
 */

const observer = new MutationObserver(() => {
    scheduleCheck();
});

observer.observe(document, {
    childList: true,
    subtree: true
});


/*
 * Crunchyroll uses SPA-style navigation.
 *
 * This means clicking a series, episode, or browser Back button may
 * change the URL without performing a complete page reload.
 *
 * When that happens:
 *
 *     1. Remove the old Sentinel block.
 *     2. Allow Crunchyroll to render the new page.
 *     3. Re-run the advisory checks.
 */

setInterval(() => {
    if (window.location.href === lastUrl) {
        return;
    }

    lastUrl = window.location.href;

    console.log(
        "[Sentinel Safe Crunchyroll] URL changed:",
        lastUrl
    );

    /*
     * Never let the previous page's block decision carry over to
     * the newly loaded Crunchyroll route.
     */
    unblockPage();

    scheduleCheck(100);

    setTimeout(() => scheduleCheck(0), 500);
    setTimeout(() => scheduleCheck(0), 1000);
    setTimeout(() => scheduleCheck(0), 2000);

}, 250);

setInterval(enforceMediaBlock, 250);

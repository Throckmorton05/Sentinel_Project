// Collect supported searches and foreground website time; never page contents.
const ENDPOINT = 'http://127.0.0.1:8765/api/activity';
const SUPPORTED = new Set(['google.com', 'www.google.com', 'bing.com', 'www.bing.com',
    'duckduckgo.com', 'www.duckduckgo.com', 'html.duckduckgo.com', 'lite.duckduckgo.com',
    'search.yahoo.com', 'search.brave.com', 'youtube.com', 'www.youtube.com', 'm.youtube.com',
    'store.steampowered.com']);
let serial = Promise.resolve();
let dropped = 0;
const seen = new Map();

async function post(event) {
    const response = await fetch(ENDPOINT, {method: 'POST', credentials: 'omit',
        redirect: 'error', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(event), signal: AbortSignal.timeout(3000)});
    if (response.status >= 400 && response.status < 500 && response.status !== 429) {
        dropped++; return; // A permanently invalid event must not stall the queue.
    }
    if (!response.ok) throw new Error('Activity unavailable');
}

async function flush() {
    let {pending = []} = await chrome.storage.session.get('pending');
    pending = pending.filter(e => Date.now() - Date.parse(e.occurred_at) < 86400000);
    while (pending.length) {
        try { await post(pending[0]); } catch (_) { break; }
        pending.shift();
    }
    await chrome.storage.session.set({pending});
    try { await post({kind: 'heartbeat', dropped}); } catch (_) {}
}

function search(details, blocked = false) {
    if (details.frameId !== 0 || details.documentLifecycle === 'prerender') return;
    let url;
    try { url = new URL(details.url); } catch (_) { return; }
    if (!['http:', 'https:'].includes(url.protocol) || !SUPPORTED.has(url.hostname)) return;
    let key = 'q';
    if (url.hostname.includes('youtube.com')) key = 'search_query';
    if (url.hostname === 'search.yahoo.com') key = 'p';
    if (url.hostname === 'store.steampowered.com') key = 'term';
    if (!url.searchParams.get(key)?.trim()) return;
    if (blocked && !['net::ERR_BLOCKED_BY_ADMINISTRATOR', 'net::ERR_BLOCKED_BY_CLIENT'].includes(details.error)) return;
    const signature = `${blocked}:${details.tabId}:${url.hostname}:${url.pathname}:${url.searchParams.get(key)}`;
    if (Date.now() - (seen.get(signature) || 0) < 1500) return;
    seen.set(signature, Date.now());
    if (seen.size > 512) seen.delete(seen.keys().next().value);
    // Send only the query-bearing URL; strip unrelated tracking/query parameters.
    const clean = `${url.origin}${url.pathname}?${new URLSearchParams({[key]: Array.from(url.searchParams.get(key)).slice(0, 512).join('')})}`;
    const event = {kind: blocked ? 'search_block' : 'search', reason: blocked ? details.error : undefined, url: clean, event_id: crypto.randomUUID(),
        occurred_at: new Date().toISOString()};
    serial = serial.catch(() => {}).then(async () => {
        let {pending = []} = await chrome.storage.session.get('pending');
        pending.push(event);
        if (pending.length > 500) { pending.shift(); dropped++; }
        await chrome.storage.session.set({pending});
        await flush();
    });
}
chrome.webNavigation.onCommitted.addListener(details => search(details));
chrome.webNavigation.onHistoryStateUpdated.addListener(details => search(details));
chrome.webNavigation.onErrorOccurred.addListener(details => search(details, true));
// No browsing history or full URL is saved for usage, only the active hostname.
async function focus() {
    let domain = '';
    try {
        const win = await chrome.windows.getLastFocused();
        if (win.focused && await chrome.idle.queryState(300) === 'active') {
            const tabs = await chrome.tabs.query({active: true, windowId: win.id});
            const url = new URL(tabs[0]?.url || '');
            if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password)
                domain = url.hostname;
        }
    } catch (_) {}
    try { await post({kind: 'browser_focus', domain, occurred_at: new Date().toISOString()}); } catch (_) {}
}
function updateFocus() { serial = serial.catch(() => {}).then(focus); }
chrome.tabs.onActivated.addListener(updateFocus);
chrome.tabs.onUpdated.addListener((_id, change) => { if (change.url || change.status) updateFocus(); });
chrome.tabs.onRemoved.addListener(updateFocus);
chrome.windows.onFocusChanged.addListener(updateFocus);
chrome.idle.onStateChanged.addListener(updateFocus);
chrome.alarms.create('sentinel-activity-flush', {periodInMinutes: 0.5});
chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'sentinel-activity-flush') serial = serial.catch(() => {}).then(flush).then(focus);
});
serial = serial.then(flush).then(focus);

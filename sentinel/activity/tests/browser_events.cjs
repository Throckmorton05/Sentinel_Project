// Exercise the service worker with browser event/storage/fetch mocks.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {randomUUID} = require('node:crypto');
const root = require('node:path').resolve(__dirname, '../browser_extension/background.js');
let committed, history, alarm, failed, activated, focused, idleChanged;
let focusWindow = {id: 1, focused: true};
let activeUrl = 'https://www.example.com/path?secret=hidden';
let idleState = 'active';
const posts = [];
const session = {};
let offline = false;
const context = vm.createContext({URL, URLSearchParams, Date, Map, Set, Promise,
    AbortSignal, crypto: {randomUUID},
    fetch: async (url, options) => {
        if (offline) throw new Error('offline');
        posts.push(JSON.parse(options.body)); return {ok: true};
    },
    chrome: {
        storage: {session: {get: async () => ({...session}), set: async value => Object.assign(session, value)}},
        webNavigation: {onCommitted: {addListener: fn => committed = fn},
                        onHistoryStateUpdated: {addListener: fn => history = fn},
                        onErrorOccurred: {addListener: fn => failed = fn}},
        tabs: {query: async () => [{url: activeUrl}],
               onActivated: {addListener: fn => activated = fn},
               onUpdated: {addListener: () => {}}, onRemoved: {addListener: () => {}}},
        windows: {getLastFocused: async () => focusWindow, onFocusChanged: {addListener: fn => focused = fn}},
        idle: {queryState: async () => idleState, onStateChanged: {addListener: fn => idleChanged = fn}},
        alarms: {create: () => {}, onAlarm: {addListener: fn => alarm = fn}}
    }
});
const settle = async () => { await vm.runInContext('serial', context); };
(async () => {
    vm.runInContext(fs.readFileSync(root, 'utf8'), context);
    await settle();
    committed({frameId: 1, tabId: 1, url: 'https://www.google.com/search?q=ignored'});
    committed({frameId: 0, tabId: 1, url: 'https://unrelated.example/search?q=ignored'});
    committed({frameId: 0, tabId: 1, documentLifecycle: 'prerender', url: 'https://www.google.com/search?q=ignored'});
    committed({frameId: 0, tabId: 1, url: 'https://www.google.com/search?q=cats&token=private'});
    history({frameId: 0, tabId: 1, url: 'https://www.google.com/search?q=cats&token=private'});
    await settle();
    let searches = posts.filter(e => e.kind === 'search');
    assert.equal(searches.length, 1);
    assert.equal(searches[0].url, 'https://www.google.com/search?q=cats');
    assert.ok(searches[0].event_id);
    offline = true;
    committed({frameId: 0, tabId: 2, url: 'https://www.youtube.com/results?search_query=birds'});
    await settle();
    assert.equal(session.pending.length, 1);
    const id = session.pending[0].event_id;
    offline = false;
    alarm({name: 'sentinel-activity-flush'});
    await settle();
    assert.equal(session.pending.length, 0);
    assert.equal(posts.filter(e => e.event_id === id).length, 1);
    session.pending = [{kind: 'search', event_id: 'expired', occurred_at: new Date(Date.now() - 2 * 86400000).toISOString()}];
    alarm({name: 'sentinel-activity-flush'}); await settle();
    assert.equal(session.pending.length, 0);
    assert.equal(posts.filter(e => e.event_id === 'expired').length, 0);
    assert.equal(posts.filter(e => e.kind === 'browser_focus').at(-1).domain, 'www.example.com');
    activeUrl = 'https://www.google.com/search?q=blocked&token=private';
    activated(); await settle();
    assert.equal(posts.filter(e => e.kind === 'browser_focus').at(-1).domain, 'www.google.com');
    focusWindow.focused = false;
    focused(); await settle();
    assert.equal(posts.filter(e => e.kind === 'browser_focus').at(-1).domain, '');
    focusWindow.focused = true; idleState = 'idle';
    idleChanged(); await settle();
    assert.equal(posts.filter(e => e.kind === 'browser_focus').at(-1).domain, '');
    failed({frameId: 0, tabId: 1, url: activeUrl, error: 'net::ERR_BLOCKED_BY_ADMINISTRATOR'});
    failed({frameId: 0, tabId: 1, url: 'https://www.google.com/search?q=offline', error: 'net::ERR_NAME_NOT_RESOLVED'});
    await settle();
    const blocks = posts.filter(e => e.kind === 'search_block');
    assert.equal(blocks.length, 1);
    assert.equal(blocks[0].url, 'https://www.google.com/search?q=blocked');
    console.log('Browser collection checks passed.');
})().catch(e => { console.error(e); process.exitCode = 1; });

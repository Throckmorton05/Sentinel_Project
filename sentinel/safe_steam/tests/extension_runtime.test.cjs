// Behavior tests in isolated VM contexts; no external site or browser is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../../..');
let assertions = 0;
function equal(a, b) { assertions++; assert.equal(a, b); }
function sandbox() {
    const timers = [];
    const intervals = [];
    const elements = new Map();
    const video = {muted: false, paused: false, isConnected: true, pause() { this.paused = true; }};
    const advisoryParent = {innerText: 'Content Advisory 16+ Sexual Content', parentElement: null, querySelector() { return null; }};
    const advisory = {innerText: 'Content Advisory', parentElement: advisoryParent, closest() { return null; }, querySelector() { return null; }};
    function element() {
        return {id: '', style: {}, children: [], append(...children) { this.children.push(...children); },
                appendChild(child) { if (child.id) elements.set(child.id, child); this.children.push(child); },
                remove() { elements.delete(this.id); }, closest() { return null; }};
    }
    const doc = {documentElement: element(), title: '', hidden: false,
        addEventListener() {}, createElement: element,
        getElementById(id) { return elements.get(id) || null; },
        querySelector() { return null; },
        querySelectorAll(selector) { return selector === 'video' ? [video] : [advisory]; }};
    doc.documentElement.style.overflow = 'auto';
    const location = {href: 'https://store.steampowered.com/search/?term=hentai', search: '?term=hentai'};
    const ctx = vm.createContext({document: doc, window: {location}, URLSearchParams,
        console: {log() {}, warn() {}, error() {}},
        MutationObserver: class { observe() {} },
        setTimeout(fn, delay) { const id = {fn, delay, cancelled: false}; timers.push(id); return id; },
        clearTimeout(id) { if (id) id.cancelled = true; },
        setInterval(fn, delay) { intervals.push({fn, delay}); },
        chrome: {runtime: {lastError: null, sendMessage(_message, cb) { cb({enabled: true}); }}},
    });
    return {ctx, doc, elements, video, advisory, advisoryParent, location, timers, intervals};
}
function load(ctx, relative) { vm.runInContext(fs.readFileSync(path.join(root, relative), 'utf8'), ctx); }
async function flush() { for (let i=0;i<6;i++) await Promise.resolve(); }
async function policyConsumerTests() {
    for (const folder of ['safe_steam/browser_extension', 'safe_steam/steam_extension_tests', 'safe_crunchyroll/browser_extension']) {
        const s = sandbox();
        load(s.ctx, `sentinel/${folder}/policy.js`);
        equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), true);
        let enabled = false;
        s.ctx.chrome.runtime.sendMessage = (_, cb) => cb({enabled});
        vm.runInContext('SentinelPolicy.start(() => {})', s.ctx);
        await flush();
        equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), false);
        enabled = true;
        s.intervals.find(x => x.delay === 5000).fn(); await flush();
        equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), true);
        enabled = false;
        s.intervals.find(x => x.delay === 5000).fn(); await flush();
        equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), false);
        s.ctx.chrome.runtime.lastError = {message: 'worker offline'};
        s.intervals.find(x => x.delay === 5000).fn(); await flush();
        equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), true);
        s.ctx.chrome.runtime.lastError = null;
        s.ctx.chrome.runtime.sendMessage = () => {}; // A hanging worker has a bounded deadline.
        s.intervals.find(x => x.delay === 5000).fn();
        s.timers.filter(x => x.delay === 3500 && !x.cancelled).at(-1).fn();
        await flush(); equal(vm.runInContext('SentinelPolicy.enabled', s.ctx), true);
    }
}
async function backgroundTests() {
    for (const name of ['safe_steam', 'safe_crunchyroll']) {
        const s = sandbox();
        let listener;
        let requestUrl;
        s.ctx.AbortController = AbortController;
        s.ctx.chrome.runtime.id = 'sentinel-extension';
        s.ctx.chrome.runtime.onMessage = {addListener(fn) { listener = fn; }};
        let value = false;
        s.ctx.fetch = async url => { requestUrl = url; return {ok: true, json: async () => ({[name]: value})}; };
        load(s.ctx, `sentinel/${name}/browser_extension/background.js`);
        async function read() {
            let result;
            equal(listener({type:'sentinel.policy.get', url:'https://untrusted.example'},
                           {id:'sentinel-extension'}, response => {result = response.enabled;}), true);
            await flush(); return result;
        }
        equal(await read(), false);
        equal(requestUrl, `http://127.0.0.1:8765/api/${name.replace('_','-')}`);
        for (value of [true, 'false', 0, null, undefined]) equal(await read(), true);
        s.ctx.fetch = async () => {throw new Error('daemon offline');};
        equal(await read(), true);
        s.ctx.fetch = async () => ({ok:false});
        equal(await read(), true);
        equal(listener({type:'sentinel.policy.get'}, {id:'other'}, () => {throw Error('untrusted reply');}), false);
        equal(listener({type:'sentinel.policy.set', enabled:false}, {id:'sentinel-extension'}, () => {throw Error('write reply');}), false);
    }
}
function filteringTests() {
    for (const folder of ['browser_extension', 'steam_extension_tests']) {
        const s = sandbox();
        s.ctx.SentinelPolicy = {enabled: true, start() {}};
        load(s.ctx, `sentinel/safe_steam/${folder}/content.js`);
        equal(s.elements.has('sentinel-safe-steam-block-overlay'), true);
        s.ctx.SentinelPolicy.enabled = false;
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-steam-block-overlay'), false);
        s.ctx.SentinelPolicy.enabled = true;
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-steam-block-overlay'), true);
        // Allow an unrelated word and an allowed SPA route without destroying the page.
        s.location.search = '?term=sextant';
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-steam-block-overlay'), false);
    }
    const s = sandbox();
    s.ctx.SentinelPolicy = {enabled: true, start() {}};
    load(s.ctx, 'sentinel/safe_crunchyroll/browser_extension/content.js');
    const advisoryCases = [
        ['16+ Sexual Content', true],
        ['14+ Nudity', false],
        ['18+ Sexualized Imagery', true],
        ['16+ Violence', false],
        // The supplied Maken-Ki metadata: no badge, four distinct sexual categories.
        ['Sexual Content, Sexualized Imagery, Suggestive Dialogue, Nudity, Profanity, Slurs', true],
        ['Nudity, Profanity', false],
        ['Nudity, Profanity, Slurs', false],
        ['Nudity, Nudity, Profanity, Profanity', false],
        ['14+ Sexualized Imagery, Nudity', false],
        ['14+ Sexual Content, Nudity, Profanity', false],
        ['Violence, Profanity, Slurs', false],
        ['Sexual Content, Sexualized Imagery', false],
        ['SeXuAl   cOnTeNt, \n PrOfAnItY, SlUrS', false],
        ['Nudity, Profanity, Slurship', false],
        ['Sexual Content, Sexualized Imagery, Nudity', true],
        ['14+ Sexual Content, Suggestive Dialogue, Nudity', true],
        ['Sexualized Imagery, Suggestive Dialogue, Nudity', true],
        ['Sexual Content, Sexual Content, Nudity, Nudity', false],
        ['18+ Violence, Smoking, Profanity, Slurs', false],
        ['16+ Suggestive Dialogue, Violence, Smoking, Profanity, Slurs', false],
        ['SeXuAl   cOnTeNt, \n SuGgEsTiVe DiAlOgUe, NuDiTy', true],
    ];
    // Heading + separate descriptor container; no badge is needed for counting.
    for (const [advisory, blocked] of advisoryCases) {
        s.advisory.innerText = 'Content Advisory';
        s.advisoryParent.innerText = `Content Advisory ${advisory}`;
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-crunchyroll-block-overlay'), blocked);
    }
    // A single inline "Content Advisory: ..." node must also be recognized.
    for (const [advisory, blocked] of advisoryCases) {
        s.advisory.innerText = `Content Advisory: ${advisory}`;
        s.advisoryParent.innerText = s.advisory.innerText;
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-crunchyroll-block-overlay'), blocked);
    }
    // Rating is available only as an SVG sibling, absent from innerText.
    for (const [rating, blocked] of [[16,true], [14,false], [18,true], [12,false]]) {
        s.advisory.innerText = 'Content Advisory: Sexual Content';
        s.advisoryParent.innerText = s.advisory.innerText;
        s.advisoryParent.querySelector = () => ({getAttribute() { return `universal-rating-${rating}-svg`; }});
        vm.runInContext('runChecks()', s.ctx);
        equal(s.elements.has('sentinel-safe-crunchyroll-block-overlay'), blocked);
    }
    s.advisoryParent.querySelector = () => null;
    // A surrounding synopsis cannot supply the other two counted categories.
    s.advisory.innerText = 'Content Advisory: Nudity';
    s.advisoryParent.innerText = 'Content Advisory: Nudity Synopsis: Sexual Content and Sexualized Imagery';
    vm.runInContext('runChecks()', s.ctx);
    equal(s.elements.has('sentinel-safe-crunchyroll-block-overlay'), false);
    s.advisory.innerText = 'Content Advisory';
    s.advisoryParent.innerText = 'Content Advisory 16+ Sexual Content';
    s.video.paused = false;
    vm.runInContext('runChecks()', s.ctx);
    equal(s.video.paused, true); equal(s.video.muted, true);
    s.video.paused = false; s.video.muted = false;
    vm.runInContext('enforceMediaBlock()', s.ctx);
    equal(s.video.paused, true); equal(s.video.muted, true);
    s.ctx.SentinelPolicy.enabled = false;
    vm.runInContext('runChecks()', s.ctx);
    equal(s.elements.has('sentinel-safe-crunchyroll-block-overlay'), false);
    equal(s.video.muted, false); equal(s.video.paused, true);
    equal(s.doc.documentElement.style.overflow, 'auto');
}
(async () => {
    await policyConsumerTests(); await backgroundTests(); filteringTests();
    console.log(`${assertions} extension behavior assertions passed`);
})().catch(error => {console.error(error); process.exitCode = 1;});

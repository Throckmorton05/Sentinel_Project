// A missing/malformed policy is restrictive. Never persist a cached false.
const SentinelPolicy = (() => {
    let enabled = true;
    let pending = false;
    function read() {
        return new Promise(resolve => {
            let settled = false;
            const finish = value => {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            };
            const timer = setTimeout(() => finish(true), 3500);
            try {
                chrome.runtime.sendMessage({type: "sentinel.policy.get"}, response => {
                    if (chrome.runtime.lastError) return finish(true);
                    finish(response?.enabled !== false);
                });
            } catch (_) { finish(true); }
        });
    }
    return {
        get enabled() { return enabled; },
        start(onRefresh) {
            const refresh = async () => {
                if (pending) return;
                pending = true;
                try { enabled = await read(); } finally { pending = false; }
                onRefresh();
            };
            refresh();
            setInterval(refresh, 5000);
            document.addEventListener("visibilitychange", () => {
                if (!document.hidden) refresh();
            });
        }
    };
})();

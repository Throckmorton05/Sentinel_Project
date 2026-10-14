# AdGuard control API

This is sentineld's bounded local AdGuard Home adapter. Connection settings live
in root-only `/etc/sentinel/adguard.toml`, with this structure:

```toml
[adguard]
url = "http://127.0.0.1"
username = "Sentinel"
password = "YOUR_PRIVATE_ADGUARD_PASSWORD"
```

Use `sudoedit /etc/sentinel/adguard.toml` to edit it. It must be root-owned with
mode `0600`. First daemon startup imports the legacy connection settings once;
see [DNS migration and password requirements](../README.md).

`reload_adguard_filters(expected_rules=..., enabled=...)` registers/updates
Sentinel's absolute-path filter subscription, refreshes it with `force: true`,
verifies its rule count and protection state, and clears the DNS cache. Disabling
Sentinel disables only its own subscription. Empty rules do not register an
empty new subscription.

`check_adguard_filter(expected_rules=..., enabled=...)` reads subscription and
backend state without mutating protection. sentineld uses it for periodic drift
checks and retries saved policy when necessary.

Calls use finite connection/read timeouts, refuse remote control URLs and
redirects, close sessions, and do not print responses or credentials. HTTP,
invalid-response, and backend-state failures propagate to the daemon's explicit
pending application status.

Use `sen dns regenerate` as a client. Do not call privileged enforcement from a
desktop UI or let the user edit the generated filter file.

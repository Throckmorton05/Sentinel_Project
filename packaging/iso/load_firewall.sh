#!/bin/sh
# Never called during image customization: service starts only on installed systems.
set -eu
if /usr/sbin/nft list table inet sentinel_dns >/dev/null 2>&1; then
    # One nft batch is atomic; do not flush unrelated tables or briefly open a gap.
    { echo 'delete table inet sentinel_dns'; cat /etc/sentinel/dns-transport.nft; } |
        /usr/sbin/nft -f -
else
    /usr/sbin/nft -f /etc/sentinel/dns-transport.nft
fi

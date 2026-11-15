#!/bin/sh
# Caddy's entrypoint on the droplet: run Caddy, and reload it whenever the
# `certs` service installs a new certificate (first issuance, renewal, or a
# changed hostname list). Caddy reads certificate files only when it loads its
# config, and does not watch them.
#
# The reload goes through Caddy's admin API on localhost:2019 *inside this
# container* (see the Caddyfile's global options), which no other container
# can reach. `--force` reloads even though the Caddyfile itself is unchanged.

CONFIG=/etc/caddy/Caddyfile
CERT=/certs/site.crt

caddy run --config "$CONFIG" --adapter caddyfile &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null; wait "$pid"; exit 0' TERM INT

last=$(cksum "$CERT" 2>/dev/null)
while kill -0 "$pid" 2>/dev/null; do
    sleep 30 & wait $!
    now=$(cksum "$CERT" 2>/dev/null)
    if [ -n "$now" ] && [ "$now" != "$last" ]; then
        echo "run.sh: $CERT changed; reloading Caddy"
        if caddy reload --config "$CONFIG" --adapter caddyfile --force; then
            last=$now
        fi
    fi
done
wait "$pid"

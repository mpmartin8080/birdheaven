#!/bin/sh
# The `certs` service: ONE Let's Encrypt certificate for every site hostname,
# the first as its subject and all of them as SANs, kept renewed. See
# deploy/certs/Dockerfile and ARCHITECTURE.md, "TLS".
#
#   DOMAINS         comma-separated, first = primary  (from BIRD_DEPLOY_SITE_DOMAIN)
#   ACME_EMAIL      ACME account contact (required by lego)
#   ACME_SERVER     optional CA directory URL (e.g. Let's Encrypt staging)
#   ACME_CA_CERT    optional PEM to trust for the CA itself (tests with Pebble)
#
# Volumes:
#   /certs   site.crt + site.key (what Caddy loads), domains (what they cover),
#            lego/ (lego's account and certificate store)
#   /acme    HTTP-01 webroot. lego writes .well-known/acme-challenge/<token>
#            here; Caddy serves it on :80 for every hostname.
#
# Lifecycle:
#   1. No certificate yet: write a short-lived self-signed placeholder covering
#      all names, so Caddy can start (it refuses to without cert files). The
#      container reports healthy once site.crt exists; Caddy waits for that.
#   2. Obtain the real certificate (`lego run`) whenever there is none, or the
#      hostname list has changed since the last one was issued.
#   3. Otherwise `lego renew` twice a day; it renews within 30 days of expiry.
#   4. Install by atomic rename. Caddy's start script (deploy/caddy/run.sh)
#      notices the new file and reloads, so renewals need no restart.
# Failures back off from 15 minutes to 6 hours, to stay well inside Let's
# Encrypt's limit of 5 failed validations per hostname per hour.

set -u

: "${DOMAINS:?DOMAINS is not set}"
# lego refuses to register an ACME account without a contact address.
: "${ACME_EMAIL:?ACME_EMAIL is not set (BIRD_DEPLOY_ACME_EMAIL in deploy/.env.prod)}"
CERTS=/certs
LEGO_DIR=$CERTS/lego
WEBROOT=/acme
LEGO=/lego  # where the goacme/lego image keeps its binary (not on PATH)
RENEW_DAYS=${RENEW_DAYS:-30}
CHECK_EVERY=${CHECK_EVERY:-43200}

log() { echo "certs: $*"; }

DOMAINS=$(echo "$DOMAINS" | tr 'A-Z' 'a-z' | tr -d ' ')
PRIMARY=${DOMAINS%%,*}
WANT=$(echo "$DOMAINS" | tr ',' '\n' | sort -u | tr '\n' ' ' | sed 's/ $//')

set --
for d in $(echo "$DOMAINS" | tr ',' ' '); do set -- "$@" -d "$d"; done
set -- "$@" --email "$ACME_EMAIL"
[ -n "${ACME_SERVER:-}" ] && set -- "$@" --server "$ACME_SERVER"
[ -n "${ACME_CA_CERT:-}" ] && export LEGO_CA_CERTIFICATES="$ACME_CA_CERT"
set -- --accept-tos --path "$LEGO_DIR" --http --http.webroot "$WEBROOT" "$@"

mkdir -p "$LEGO_DIR" "$WEBROOT"
chmod 755 "$WEBROOT"

placeholder() {
    [ -s "$CERTS/site.crt" ] && [ -s "$CERTS/site.key" ] && return 0
    log "no certificate yet; writing a self-signed placeholder for: $WANT"
    san=$(echo "DNS:$DOMAINS" | sed 's/,/,DNS:/g')
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 7 \
        -subj "/CN=$PRIMARY" -addext "subjectAltName=$san" \
        -keyout "$CERTS/site.key.tmp" -out "$CERTS/site.crt.tmp" 2>/dev/null || return 1
    mv "$CERTS/site.key.tmp" "$CERTS/site.key"
    mv "$CERTS/site.crt.tmp" "$CERTS/site.crt"
    echo "placeholder" > "$CERTS/domains"
}

install() {
    src=$LEGO_DIR/certificates/$PRIMARY
    [ -s "$src.crt" ] && [ -s "$src.key" ] || { log "lego produced no certificate files"; return 1; }
    if cmp -s "$src.crt" "$CERTS/site.crt" && [ "$(cat "$CERTS/domains" 2>/dev/null)" = "$WANT" ]; then
        return 0  # nothing new
    fi
    cp "$src.key" "$CERTS/site.key.tmp" && chmod 600 "$CERTS/site.key.tmp"
    cp "$src.crt" "$CERTS/site.crt.tmp" && chmod 644 "$CERTS/site.crt.tmp"
    # Key first, then the certificate: Caddy's watcher keys on the certificate.
    mv "$CERTS/site.key.tmp" "$CERTS/site.key"
    mv "$CERTS/site.crt.tmp" "$CERTS/site.crt"
    echo "$WANT" > "$CERTS/domains"
    log "installed a certificate for: $WANT"
}

have_issued() {
    [ -s "$LEGO_DIR/certificates/$PRIMARY.crt" ] && [ "$(cat "$CERTS/domains" 2>/dev/null)" = "$WANT" ]
}

trap 'log "stopping"; exit 0' TERM INT

placeholder || { log "could not create a placeholder certificate"; exit 1; }

# Give Caddy a moment to start serving the challenge webroot on :80.
sleep 15 & wait $!

backoff=900
while :; do
    if have_issued; then
        log "checking for renewal (within $RENEW_DAYS days of expiry)"
        "$LEGO" "$@" renew --days "$RENEW_DAYS" --no-random-sleep && install
        status=$?
    else
        log "requesting a certificate for: $WANT"
        "$LEGO" "$@" run && install
        status=$?
    fi
    if [ "$status" -eq 0 ]; then
        backoff=900
        sleep "$CHECK_EVERY" & wait $!
    else
        log "failed; retrying in $((backoff / 60)) minutes"
        sleep "$backoff" & wait $!
        backoff=$((backoff * 2))
        [ "$backoff" -gt 21600 ] && backoff=21600
    fi
done

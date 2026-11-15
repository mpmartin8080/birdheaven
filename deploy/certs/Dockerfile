# The `certs` service: lego (an ACME client) issuing ONE Let's Encrypt
# certificate with every site hostname as a SAN, which Caddy then serves. See
# deploy/certs/certs.sh for the lifecycle, and ARCHITECTURE.md, "TLS".
#
# Built on the droplet like the other images (deploy/build.py), so there is
# still no registry. openssl is added only to write the first-boot placeholder
# certificate that lets Caddy start before the real one exists.
#
# Build context is the repository root:
#     docker build -f deploy/certs/Dockerfile -t birdheaven-certs:local .

FROM goacme/lego:v4.28.1@sha256:bc674054d11643fa8c248915574e59fb8856e4d1fd3d5026933521df1782cc47

RUN apk add --no-cache openssl
COPY deploy/certs/certs.sh /usr/local/bin/certs.sh

VOLUME ["/certs", "/acme"]
HEALTHCHECK --interval=5s --timeout=3s --retries=60 CMD test -s /certs/site.crt
ENTRYPOINT ["/bin/sh", "/usr/local/bin/certs.sh"]

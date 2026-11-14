"""Deployment framework: Debian 13 + Docker on a DigitalOcean droplet.

Adapted from ../financesite/deploy. Everything is driven from your workstation:
the image is built on the droplet's Docker daemon over SSH
(`DOCKER_HOST=ssh://...`), kept in that daemon's local image store (no
container registry), and started with `docker compose` against the same
daemon. No source code
is ever copied to the droplet, except the Caddyfile its bind mount needs.

Unlike financesite there is no database and no Volume: videos live in Spaces
(see ARCHITECTURE.md), and Caddy's certificates sit in a named Docker volume.
"""

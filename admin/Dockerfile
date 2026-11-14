# The Bird Heaven admin console.
#
# Not distroless, unlike app/Dockerfile: this container runs ffmpeg/ffprobe to
# process uploads, and Debian's ffmpeg package is the sane way to get them. It
# still runs as an unprivileged user, has no published port (Caddy is the only
# way in), and keeps its state on a dedicated volume at /data.
#
# Build context is the repository root:
#     docker build -f admin/Dockerfile -t birdheaven-admin:local .

FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ADMIN_DATA_DIR=/data ADMIN_STATIC_DIR=/srv/static \
    TMPDIR=/data/work

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin birdadmin \
 && install -d -o 10001 -g 10001 -m 0700 /data

COPY admin/requirements.txt /tmp/requirements.txt
RUN pip install --only-binary :all: -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY admin/birdadmin /srv/birdadmin
COPY admin/static /srv/static

WORKDIR /srv
USER 10001
VOLUME /data
EXPOSE 8000

# One worker: jobs and the passkey file are per-process state.
ENTRYPOINT ["python", "-m", "uvicorn", "birdadmin.main:app", \
            "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
            "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]

# The Bird Heaven web service. Same two-stage, distroless pattern as
# financesite/app/Dockerfile -- see that file for the full reasoning behind the
# base images. In short: no shell, no package manager, non-root (uid 65532),
# pinned by digest. The builder's Python minor version must match the
# distroless runtime's (3.11) so compiled wheels import.
#
# Build context is the repository root, because the image needs webroot/ too:
#     docker build -f app/Dockerfile -t birdheaven-web:local .

FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY app/requirements.txt .
RUN python -m venv /venv \
 && /venv/bin/pip install --only-binary :all: -r requirements.txt

FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

ENV PYTHONPATH=/venv/lib/python3.11/site-packages:/srv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    STATIC_DIR=/srv/webroot

COPY --from=builder /venv/lib/python3.11/site-packages /venv/lib/python3.11/site-packages
COPY app/birdheaven /srv/birdheaven
COPY webroot /srv/webroot

WORKDIR /srv
USER 65532
EXPOSE 8000

# One worker is plenty: the app serves a small page and three cached JSON
# endpoints; the video itself comes from the CDN.
ENTRYPOINT ["/usr/bin/python3.11", "-m", "uvicorn", "birdheaven.main:app", \
            "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
            "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]

# FBT Dashboard API — container image.
#
# Render built this for us invisibly from runtime.txt and a start command in a
# web form. ECS will not: it runs an image and nothing else, so everything that
# was implicit has to be written down here.
#
# Two stages. The first installs the dependencies, which needs a compiler and
# header files for anything without a prebuilt wheel. The second copies only
# the finished virtualenv across. The compiler never reaches the deployed
# image, which is both smaller and one less thing an attacker who lands inside
# the container can use.

# ── stage 1: build the virtualenv ─────────────────────────────────────────
# Pinned to the patch, matching runtime.txt, so a rebuild six months from now
# produces the same interpreter rather than whatever 3.12 has become. Stronger
# still is pinning the digest (python:3.12.7-slim-bookworm@sha256:...), which
# survives a tag being re-pushed; see AWS_PIPELINE_SETUP.md.
FROM python:3.12.7-slim-bookworm AS builder

# Compiler and libpq headers, in case a pinned version has no cp312 wheel on
# the day of the build. Present only in this stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# A virtualenv rather than the system site-packages purely so the next stage
# can copy ONE directory and be certain it has everything.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="/opt/venv/bin:$PATH"

# requirements.txt on its own line, before the source. Docker caches per
# instruction, so editing a router does not re-run a five-minute pip install —
# only a change to requirements.txt does.
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt


# ── stage 2: the image that actually runs ─────────────────────────────────
FROM python:3.12.7-slim-bookworm AS runtime

# Security-relevant patches for the base image's own packages. Kept as its own
# layer so the reason is legible in `docker history`.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# NOT root. The default in a container is uid 0, and a process that is root
# inside the container is one kernel or runtime bug away from being root
# outside it. Nothing this application does needs privilege: it opens one
# high-numbered port, reads its own source, and talks to Postgres.
#
# A fixed high uid rather than letting the system pick, so the number is
# predictable if a volume or a security policy ever has to reference it.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# Never write .pyc files: the root filesystem is read-only in production (see
# ecs/task-definition.json) and the interpreter should not be trying.
ENV PYTHONDONTWRITEBYTECODE=1
# Unbuffered, so a log line reaches CloudWatch when it is written rather than
# when the buffer happens to fill. An audit trail that arrives late — or not at
# all, because the process died with a full buffer — is not an audit trail.
ENV PYTHONUNBUFFERED=1
ENV PYTHONFAULTHANDLER=1
ENV PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# --chown at copy time rather than a RUN chown afterwards: a recursive chown
# rewrites every file into a new layer, doubling the source's contribution to
# the image size for no benefit.
#
# Note what is NOT here: no `COPY . .`. Only the three things the application
# needs at runtime, each named. .dockerignore is the belt; this is the braces.
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser alembic/ ./alembic/
COPY --chown=appuser:appuser alembic.ini ./alembic.ini

USER appuser

EXPOSE 8000

# The container's own liveness check. The ALB target group is what actually
# decides whether traffic reaches this task; this one is what makes ECS replace
# a task that is running but wedged. Both point at /health, which touches no
# database and so cannot report a failure it is not responsible for.
#
# Uses urllib rather than curl because curl is not installed, and installing it
# would add a capable HTTP client to an image whose entire job is to contain one
# Python process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4)"]

# `exec` matters: without it, sh stays as pid 1 and swallows the SIGTERM that
# ECS sends at the start of a graceful shutdown, so the task is SIGKILLed 30
# seconds later mid-request. With it, uvicorn IS pid 1 and drains cleanly.
#
# --proxy-headers / --forwarded-allow-ips: behind an ALB every connection
# arrives from the load balancer, so without these the client address recorded
# for a request is the ALB's. That address goes on every audit row and is what
# the per-IP rate limiter counts — so every user would look like one machine,
# and 164.312(b) would be logging the wrong thing. '*' trusts the immediate
# proxy, which is only correct because nothing but the ALB can reach this port:
# the task's security group must allow ingress from the ALB's alone.
#
# WEB_CONCURRENCY defaults to 1, and that is not arbitrary: app/main.py starts
# an in-process APScheduler, so a second worker is a second copy of the
# notification sweep and the retention purge. Read the scaling note in
# AWS_PIPELINE_SETUP.md before raising it or the service's desired task count.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --proxy-headers --forwarded-allow-ips='*'"]

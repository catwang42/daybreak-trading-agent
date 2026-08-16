# Cloud Run Job image. One process, one daily run, then exit.
FROM python:3.11-slim

# Unbuffered stdio: Cloud Logging only shows what the process has actually
# flushed, and a 15-minute run that logs nothing until it exits is impossible
# to watch. PYTHONDONTWRITEBYTECODE keeps the read-only-ish layer clean.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/report-schema.md config/preferences.md config/

# config/.env is excluded by .dockerignore on purpose: secrets reach the job
# through Secret Manager, never through a layer that anyone with pull access
# can `docker save` and read.

# The app writes reports/ and journal/ next to src/ (config.REPO_ROOT). Create
# them owned by the runtime user so dropping root does not turn the first write
# into a permission error at the end of a run that already spent its tokens.
RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /app/reports /app/journal \
    && chown -R agent:agent /app
USER agent

# The experiment ledger stamps every row with the commit that produced it, and
# that is the whole point of the ledger: a change in the numbers has to be
# attributable to a change in the code. The image carries neither a git binary
# nor a .git directory, so the commit has to come from outside —
# `docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD)`. Cloud Build's
# `--tag` shorthand takes no build args, so deploy/setup.sh sets the same
# variable on the job instead; either way the env var is what the ledger reads.
# An unstamped build still runs; it just files every row under "unknown", which
# no later reader can use.
ARG GIT_COMMIT=unknown
ENV GIT_COMMIT=${GIT_COMMIT}

ENTRYPOINT ["python", "-m", "tradingagent"]
CMD ["--stage", "all"]

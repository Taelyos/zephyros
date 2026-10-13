# The image of the "tests" verification kit (ADR 0017): Python and a pinned pytest, nothing else.
# The filter runs it as a sandbox: no network, read-only root, the files to verify in /work,
# destroyed after the run. It prints a run line and one signed outcome line
# `ZEPHYROS-OUTCOMES {...} <hmac>` with the outcome of each acceptance test asked for in
# /work/zephyros-request.json; the filter checks the pair before believing the results (ATK-22).
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
COPY requirements.txt zephyros_kit.py kit-run /opt/kit/
RUN python -m pip install --no-cache-dir --no-deps --only-binary :all: --require-hashes -r /opt/kit/requirements.txt \
    && chmod 0555 /opt/kit/kit-run \
    && mkdir /work && chown 10000:10000 /work
ENV PYTHONPATH=/opt/kit PYTHONDONTWRITEBYTECODE=1
USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/kit/kit-run"]

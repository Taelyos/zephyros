# The image of the "tests" verification kit (ADR 0017): Python and a pinned pytest, nothing else.
# The filter runs it as a sandbox: no network, read-only root, the files to verify in /work,
# destroyed after the run. It prints a run line and one signed outcome line
# `ZEPHYROS-OUTCOMES {...} <hmac>` with the outcome of each acceptance test asked for in
# /work/zephyros-request.json and the tests it deselected; the filter checks the pair before
# believing the results (ATK-22). The acceptance tests are black box: the plugin refuses to
# import any delivered module into the judging process (ADR 0017).
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
COPY requirements.txt zephyros_kit.py kit-run /opt/kit/
RUN python -m pip install --no-cache-dir --no-deps --only-binary :all: --require-hashes -r /opt/kit/requirements.txt \
    && chmod 0555 /opt/kit/kit-run \
    && mkdir /work && chown 10000:10000 /work
# The tools of the project (ADR 0030), when the operator gives some, are mounted read-only at
# /opt/project-tools, as in the turns: their bin directory comes last on the PATH, so an
# acceptance test runs a tool by its name, and no tool hides the Python that judges.
ENV PYTHONPATH=/opt/kit PYTHONDONTWRITEBYTECODE=1 PATH="${PATH}:/opt/project-tools/bin"
USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/kit/kit-run"]

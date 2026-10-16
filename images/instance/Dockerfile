# The instance image of Zephyros: the upstream Hermes Agent image, pinned by the digest of its
# multi-architecture index (release v0.21.6, upstream commit 818c13be), with five additions:
# a managed-scope policy, a seed configuration, an entrypoint that starts one Hermes process
# as a non-root user without the upstream supervisor, the helper with which it starts a new
# session when the operator asks for one, and the base instruction of the agent. Each turn is one container of this image
# (ADR 0016): `--oneshot=<prompt> --continue`, see src/zephyros/adapters/hermes/engine.py.
FROM nousresearch/hermes-agent@sha256:9774f4f39a9bb8c2f68ce728ed5e99ddbad282163be56764afacf88ed952b784

# /opt/data, the Hermes home, is a volume of the upstream image (owned by 10000, mode 0700), so
# nothing can be baked there: the seed lives next to the policy, and the entrypoint copies it
# into the home at every start.
RUN mkdir -p /opt/zephyros && chmod 0755 /opt/zephyros
COPY --chmod=0644 managed.yaml /etc/hermes/config.yaml
COPY --chmod=0644 seed.yaml /opt/zephyros/seed.yaml
COPY --chmod=0755 entrypoint.sh /opt/zephyros/entrypoint.sh
COPY --chmod=0644 new_session.py /opt/zephyros/new_session.py
# The base instruction of every turn (ADR 0034): the entrypoint hands it to Hermes, which adds it
# to the system prompt of each call, out of the conversation it keeps.
COPY --chmod=0644 instructions.md /opt/zephyros/instructions.md

# The work volume. A new named volume mounted here takes this owner, so the turn can write it.
RUN mkdir -p /work && chown 10000:10000 /work

# The test runner of the work (ADR 0024): a sealed instance installs nothing, so pytest and its
# wheels are here, with the versions and hashes of the "tests" kit, installed by hash only. They
# go to the standalone Python the image ships (/usr/local/bin/python3, 3.14), never into the
# virtual environment of Hermes, whose pins stay its own. /opt/zephyros/bin comes first on the
# PATH, so `python3`, `python` and `pytest` of a turn are that Python; Hermes runs from its own
# absolute path and does not change.
COPY --chmod=0644 requirements-tests.txt /opt/zephyros/requirements-tests.txt
RUN /usr/local/bin/python3 -m pip install --no-cache-dir --no-deps --only-binary :all: \
        --require-hashes --disable-pip-version-check -r /opt/zephyros/requirements-tests.txt \
    && mkdir -p /opt/zephyros/bin \
    && ln -s /usr/local/bin/python3 /opt/zephyros/bin/python3 \
    && ln -s /usr/local/bin/python3 /opt/zephyros/bin/python \
    && ln -s "$(dirname "$(readlink -f /usr/local/bin/python3)")/pytest" /opt/zephyros/bin/pytest \
    && /opt/zephyros/bin/pytest --version
# The file tools of Hermes write where the user of the instance may write (ADR 0031): the
# upstream image keeps them to the home (HERMES_WRITE_SAFE_ROOT=/opt/data), so they could not
# write the work directory. Empty, the variable bounds nothing; the read-only root, the user
# without root and the read-only tools bound what is written.
ENV HERMES_WRITE_SAFE_ROOT=
# The tools of the project (ADR 0030), when the operator gives some, are mounted read-only at
# /opt/project-tools: their bin directory comes last, so a tool runs by its name and never hides
# a program of the image.
ENV PATH=/opt/zephyros/bin:/opt/hermes/bin:/opt/hermes/.venv/bin:/opt/data/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/project-tools/bin

# A login shell (the terminal of Hermes starts one) takes its PATH from /etc/profile, not from
# the image: the profile adds the bin directory of the tools there too.
COPY --chmod=0644 project-tools.sh /etc/profile.d/zephyros-project-tools.sh
USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/zephyros/entrypoint.sh"]

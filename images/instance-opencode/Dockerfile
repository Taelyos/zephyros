# The instance image of Zephyros for a third-party coding agent: OpenCode 1.19.0 (ADR 0029). It
# stands where images/instance stands, under the same container template: user 10000, read-only
# root, the home volume on /opt/data and the work volume on /work, the network of the guard. Each
# turn is one container of this image (ADR 0016): `turn <prompt>`, see entrypoint.sh and
# src/zephyros/adapters/opencode/engine.py.
#
# OpenCode is the build the npm registry publishes for this platform (musl, since the base is
# Alpine), fetched by its exact version and checked against the SHA-512 the registry gives as its
# integrity, then baked in: a sealed instance installs nothing, and the npm client never runs.
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 AS fetch
ARG TARGETARCH
RUN apk add --no-cache curl=8.22.0-r0 \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "$arch" in \
         amd64|x86_64) package=opencode-linux-x64-baseline-musl \
           sha512=a921dc9495cb4cfb39d42c5eff14d0ec3caf1d130696e19480c6edd340ceb733ca83853ad06eead90a986b66f84714ae7a32c882cb6bdd725653309c8166f85a ;; \
         arm64|aarch64) package=opencode-linux-arm64-musl \
           sha512=129a66839cf7319dc216f2788d3b55fee6dfd3d0ead4bca5234585cd1376e963e3e742e1fc8e758628919a16b87b523c9e3e5245a9eb6830df1fa6aa1ef6e9fb ;; \
         *) echo "no OpenCode build for $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/opencode.tgz \
         "https://registry.npmjs.org/$package/-/$package-1.19.0.tgz" \
    && echo "$sha512  /tmp/opencode.tgz" | sha512sum -c - \
    && mkdir -p /out \
    && tar -xzf /tmp/opencode.tgz -C /out --strip-components=2 package/bin/opencode

# The same base as the "tests" kit (Python 3.12 on Alpine 3.24), so a test that passes here runs
# on the Python that judges it. git and curl carry the base and the deliveries to and from the
# staging area of the hub; ripgrep is the search tool of OpenCode, which would otherwise download
# it; libstdc++ and libgcc are what the OpenCode build links. Pinned to the versions of the base
# image's Alpine release, as the guard pins nftables.
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
RUN apk add --no-cache git=2.54.0-r0 curl=8.22.0-r0 bash=5.3.9-r1 ripgrep=15.1.0-r0 \
        libstdc++=15.2.0-r5 libgcc=15.2.0-r5
COPY --from=fetch --chmod=0555 /out/opencode /opt/opencode/bin/opencode

# The test runner of the work (ADR 0024): pytest and its wheels at the versions and hashes of the
# "tests" kit, installed by hash only, so a sealed instance runs the tests it writes.
COPY --chmod=0644 requirements-tests.txt /opt/zephyros/requirements-tests.txt
RUN python3 -m pip install --no-cache-dir --no-deps --only-binary :all: \
        --require-hashes --disable-pip-version-check -r /opt/zephyros/requirements-tests.txt \
    && pytest --version

# The configuration lives in the managed scope of OpenCode (/etc/opencode), which OpenCode reads
# after every other scope: a key set there wins over the same key in the agent's own files. The
# root file system is read-only in a turn, so the agent cannot change it, even inside its turn.
COPY --chmod=0644 opencode.jsonc /etc/opencode/opencode.jsonc
# The base instruction of the agent (ADR 0034), which the managed configuration names.
COPY --chmod=0644 instructions.md /etc/opencode/instructions.md
COPY --chmod=0755 entrypoint.sh /opt/zephyros/entrypoint.sh
COPY --chmod=0644 session.py /opt/zephyros/session.py

# Where OpenCode looks for the configuration of its user (~/.config/opencode) and for a ".opencode"
# directory in its home: two empty directories of the read-only root, not the home volume. There,
# the agent of one turn could leave instructions, agents, commands, skills, MCP servers or plugins
# that every later turn would load; OpenCode adds them to the managed configuration rather than
# replace it (lists such as "instructions" are joined), so the managed scope alone cannot refuse
# them. Hermes turns off its background review for the same reason (images/instance/seed.yaml).
# XDG_CONFIG_HOME and OPENCODE_TEST_HOME, below, point there. OpenCode alone reads the second one
# (its own name for its home), so the commands of the agent keep /opt/data as their home; with
# the first, the user configuration of the other programs of a turn (git, pip) is empty too.
# OpenCode writes a .gitignore in its configuration directory when there is none, and stops on a
# read-only file system: an empty one is there already.
RUN mkdir -p /opt/opencode/config/opencode /opt/opencode/home \
    && touch /opt/opencode/config/opencode/.gitignore

# The home and the work volumes. A new named volume mounted here takes this owner, so the turn
# can write it. The home holds what OpenCode keeps from turn to turn (its sessions, under
# ~/.local/share/opencode), as the Hermes home does for Hermes.
RUN mkdir -p /opt/data /work && chown 10000:10000 /opt/data /work && chmod 0700 /opt/data

# What OpenCode would otherwise do by itself, off: look for a new version, fetch the model list
# of models.dev, share a session, install its default and external plugins, download language
# servers, read the configuration, instructions and skills of other agents (~/.claude, ~/.agents)
# or the configuration a delivered repository carries. None of them is a guarantee of Zephyros:
# the network guard holds whatever OpenCode tries (ADR 0024); these keep a turn from trying.
# The tools of the project (ADR 0030), when the operator gives some, are mounted read-only at
# /opt/project-tools: their bin directory comes last on the PATH, so a tool runs by its name and
# never hides a program of the image.
ENV HOME=/opt/data \
    PATH="${PATH}:/opt/project-tools/bin" \
    TMPDIR=/tmp \
    XDG_CONFIG_HOME=/opt/opencode/config \
    OPENCODE_TEST_HOME=/opt/opencode/home \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_SHARE=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
    OPENCODE_PURE=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_CLAUDE_CODE=1 \
    OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
    OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    OPENCODE_DISABLE_TERMINAL_TITLE=1

# A login shell (the terminal of Hermes starts one) takes its PATH from /etc/profile, not from
# the image: the profile adds the bin directory of the tools there too.
COPY --chmod=0644 project-tools.sh /etc/profile.d/zephyros-project-tools.sh
USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/zephyros/entrypoint.sh"]

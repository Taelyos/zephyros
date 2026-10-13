# The image of the hub and of the container filter: the zephyros package and its locked
# dependencies, nothing else. The two run as separate containers with separate hardening
# (deploy/compose.yaml); the command chooses which one a container is.
# Build from the root of the repository: docker build -f images/zephyros/Dockerfile .
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4 AS build
COPY --from=ghcr.io/astral-sh/uv@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/zephyros UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python3

FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
# The hub runs the delivery loop: it reads and writes git bundles and the pull requests of the
# directory forge, and the git command line is its only way there. The filter runs the same
# image and ignores git. Pinned to the version of the base image's Alpine release, as the
# guard pins nftables.
RUN apk add --no-cache git=2.54.0-r0
COPY --from=build /opt/zephyros /opt/zephyros
# The data directory and the directory of the filter's socket. A new named volume mounted on
# them takes this owner: the filter (uid 0, group 10001, no capability) writes as owner, the hub
# (10001) as group; nobody else.
RUN mkdir -p /var/lib/zephyros /run/zephyros \
    && chown 0:10001 /var/lib/zephyros /run/zephyros \
    && chmod 0770 /var/lib/zephyros /run/zephyros
USER 10001:10001
ENTRYPOINT ["/opt/zephyros/bin/zephyros"]

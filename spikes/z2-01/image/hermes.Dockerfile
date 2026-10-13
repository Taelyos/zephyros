# Zephyros instance image for spike Z2-01: the upstream Hermes Agent image, pinned by the
# digest of its multi-architecture index (release v0.21.6, upstream commit 818c13be), with
# three additions: a managed-scope policy file, a seed configuration, and an entrypoint that
# starts one Hermes process as a non-root user without the upstream s6 supervisor.
FROM nousresearch/hermes-agent@sha256:9774f4f39a9bb8c2f68ce728ed5e99ddbad282163be56764afacf88ed952b784

# /opt/data is a VOLUME in the upstream image, so nothing can be baked there: the seed lives
# next to the policy and the entrypoint copies it into the Hermes home at every start.
RUN mkdir -p /opt/zephyros && chmod 0755 /opt/zephyros
COPY --chmod=0644 managed.yaml /etc/hermes/config.yaml
COPY --chmod=0644 seed.yaml /opt/zephyros/seed.yaml
COPY --chmod=0755 entrypoint.sh /opt/zephyros/entrypoint.sh

# The work volume. A fresh named volume mounted here takes this owner (copy-up).
RUN mkdir -p /work && chown 10000:10000 /work

USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/zephyros/entrypoint.sh"]

# A stand-in for OpenCode, for the runtime tests (ADR 0029): the real entrypoint and the real
# managed configuration of images/instance-opencode, on the same base, with a fake `opencode` in
# the place of the real one. tests/runtime/test_opencode_wiring.py assembles the build context:
# this file, fake_opencode.py, and the two files of the real image.
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
COPY --chmod=0555 fake_opencode.py /opt/opencode/bin/opencode
COPY --chmod=0644 opencode.jsonc /etc/opencode/opencode.jsonc
COPY --chmod=0755 entrypoint.sh /opt/zephyros/entrypoint.sh
RUN mkdir -p /opt/data /work && chown 10000:10000 /opt/data /work && chmod 0700 /opt/data
ENV HOME=/opt/data
USER 10000:10000
WORKDIR /work
ENTRYPOINT ["/opt/zephyros/entrypoint.sh"]

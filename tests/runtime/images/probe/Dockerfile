# The stand-in of the agent engine for the runtime tests: it takes the command of a turn and
# waits, and it carries the tools of the attempts (interpreters, mount, chattr, unshare).
FROM alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache python3=3.12.15-r0 perl=5.40.4-r0 e2fsprogs-extra=1.47.2-r2 \
    util-linux-misc=2.41.6-r1
# Like the instance image: the mount points of the two volumes belong to the user of a turn, so
# that a new volume is created with that owner.
RUN mkdir -p /opt/data /work && chown 10000:10000 /opt/data /work
COPY --chmod=0555 probe.py /usr/local/bin/probe
COPY --chmod=0555 entry.sh /usr/local/bin/entry
ENTRYPOINT ["/usr/local/bin/entry"]

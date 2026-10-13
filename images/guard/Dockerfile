# The network guard of a Zephyros instance. See guard.sh.
FROM alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache nftables=1.1.3-r0
COPY --chmod=0555 guard.sh /usr/local/bin/guard.sh
ENTRYPOINT ["/usr/local/bin/guard.sh"]
CMD ["run"]

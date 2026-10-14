# The network guard of a Zephyros instance. See guard.sh.
#
# The guard runs as user 10002, not root (ADR 0027). Its one privilege, NET_ADMIN in the network
# namespace of its instance, comes from a file capability on nft, which only that user's group
# (and root) may run; the shell and everything else hold no capability. setcap is removed once it
# has written the capability. Changing the owner of a file clears its capabilities, so the owner
# is set first.
FROM alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache nftables=1.1.3-r0 libcap-setcap=2.78-r0 \
 && chown root:10002 /usr/sbin/nft \
 && chmod 0750 /usr/sbin/nft \
 && setcap cap_net_admin+ep /usr/sbin/nft \
 && apk del --no-cache libcap-setcap
COPY --chmod=0555 guard.sh /usr/local/bin/guard.sh
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/guard.sh"]
CMD ["run"]

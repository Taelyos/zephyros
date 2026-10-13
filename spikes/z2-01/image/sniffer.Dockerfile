# Packet capture fixture for spike Z2-01: joins the network namespace of an instance and
# prints every packet that is not the conversation with the fake hub.
FROM alpine@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
RUN apk add --no-cache tcpdump
ENTRYPOINT ["tcpdump"]

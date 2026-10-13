# A listener that records every peer that reaches it (oracle O-NET of the threat model), and
# the capture tool of a network namespace (ATK-83: a network observer on the Hub host).
FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
RUN apk add --no-cache tcpdump
COPY observer.py /observer.py
ENTRYPOINT ["python", "-u", "/observer.py"]

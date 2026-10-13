FROM python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
COPY observer.py /observer.py
ENTRYPOINT ["python", "-u", "/observer.py"]

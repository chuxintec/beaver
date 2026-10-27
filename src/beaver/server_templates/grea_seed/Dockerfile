FROM scratch
COPY bin/role /role
COPY config /app/config
WORKDIR /app
USER 65532:65532
ENTRYPOINT ["/role"]

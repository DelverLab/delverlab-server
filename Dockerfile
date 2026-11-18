FROM eclipse-temurin:27-jre@sha256:acfb350b50e707addf3dcbb47f7052d1882d1e5f1670ce838a9c7838d82d4fe5

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl \
  && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --chown=10001:10001 --chmod=0444 build/libs/server.jar /app/server.jar
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=4s --start-period=30s --retries=12 \
  CMD ["curl", "--fail", "--silent", "--max-time", "3", "http://127.0.0.1:8080/healthz"]
ENTRYPOINT ["java", "-jar", "/app/server.jar"]
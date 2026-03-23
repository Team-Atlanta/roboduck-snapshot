# Builder image — used during the build-target phase.
# Compiles the target project and submits build outputs (build + src).
ARG target_base_image
FROM ${target_base_image}

# bear: intercepts compilation to produce compile_commands.json (needed by infer)
RUN apt-get update && apt-get install -y --no-install-recommends bear \
    && rm -rf /var/lib/apt/lists/*

COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh

COPY oss-crs/scripts/compile_target /usr/local/bin/compile_target

CMD ["compile_target"]

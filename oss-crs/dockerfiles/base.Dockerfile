# Roboduck base image — prepare phase.
# llvm-cov built from source. Infer downloaded as pre-built binary.

###############################################################################
# Stage 1: Build llvm-cov from source
###############################################################################
FROM gcr.io/oss-fuzz-base/base-builder:latest AS llvm-cov-build

RUN apt-get update && apt-get -y install ninja-build libz-dev
RUN mkdir -p /root/build
WORKDIR /root/build
RUN git clone -b llvmorg-20.1.6 --depth 1 https://github.com/llvm/llvm-project.git
WORKDIR /root/build/llvm-project/build
RUN LD=lld CC=clang CXX=clang++ cmake \
    -DCMAKE_BUILD_TYPE=Release \
    -DLIBCLANG_BUILD_STATIC=ON \
    -DLLVM_ENABLE_BINDINGS=OFF \
    -DLLVM_ENABLE_LIBXML2=OFF \
    -DLLVM_ENABLE_LTO=OFF \
    -DLLVM_ENABLE_OCAMLDOC=OFF \
    -DLLVM_ENABLE_PIC=OFF \
    -DLLVM_ENABLE_PROJECTS='clang;lld' \
    -DLLVM_ENABLE_TERMINFO=OFF \
    -DLLVM_ENABLE_WARNINGS=OFF \
    -DLLVM_ENABLE_Z3_SOLVER=OFF \
    -DLLVM_ENABLE_ZLIB=FORCE_ON \
    -DLLVM_ENABLE_ZSTD=OFF \
    -DLLVM_HAVE_LIBXAR=OFF \
    -DLLVM_INCLUDE_BENCHMARKS=OFF \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DLLVM_TOOL_REMARKS_SHLIB_BUILD=OFF \
    -G Ninja \
    ../llvm && \
    ninja llvm-cov

###############################################################################
# Stage 2: Final roboduck image
###############################################################################
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies (no azcopy, no az cli)
RUN apt-get update \
    && apt install -y \
       build-essential curl docker.io docker-buildx git git-lfs unzip \
       pkg-config protobuf-compiler flex bison libnl-route-3-dev \
       software-properties-common openjdk-17-jdk \
       universal-ctags global patchutils rustup musl-tools clang sudo ripgrep wget \
       libssl-dev fuse-overlayfs \
    && rustup default stable \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update --allow-insecure-repositories \
    && apt install -y --allow-unauthenticated python3.13 python3.13-dev python3.13-venv \
    && apt-get autoremove -y \
    && apt-get clean -y \
    && apt-get autoclean -y \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /crs /crs/external/llvm-cov
WORKDIR /crs

# Copy llvm-cov from build stage
COPY --from=llvm-cov-build /root/build/llvm-project/build/bin/llvm-cov external/llvm-cov/llvm-cov
RUN chmod +x external/llvm-cov/llvm-cov

# Install kaitai struct compiler
RUN curl -LO https://github.com/kaitai-io/kaitai_struct_compiler/releases/download/0.10/kaitai-struct-compiler_0.10_all.deb \
    && apt-get update && apt-get install -y ./kaitai-struct-compiler_0.10_all.deb \
    && rm -f kaitai-struct-compiler_0.10_all.deb \
    && rm -rf /var/lib/apt/lists/*

# Build external dependencies and utils (bear, lcov_parser, etc.)
COPY ./utils ./utils
COPY ./external ./external
COPY build.sh ./
RUN ./build.sh

# Infer: download pre-built v1.2.0 from GitHub releases AFTER COPY ./external
# (COPY overwrites external/infer/ — must download after).
# The archive layout under lib/infer/ matches what roboduck expects:
#   external/infer/infer/bin/infer  +  external/infer/facebook-clang-plugins/
# Falls back to empty placeholder if download fails — LAUNCH_INFER will
# degrade gracefully (no static analysis reports, fuzzing unaffected).
# v1.1.0 is the newest release compatible with glibc 2.31 (base-runner is
# Ubuntu 20.04). v1.2.0 requires glibc 2.34+.
ARG INFER_VERSION=v1.1.0
RUN curl -fsSL -o /tmp/infer.tar.xz \
        "https://github.com/facebook/infer/releases/download/${INFER_VERSION}/infer-linux64-${INFER_VERSION}.tar.xz" \
    && tar -Jxf /tmp/infer.tar.xz --strip-components=3 \
        -C external/infer/ "infer-linux64-${INFER_VERSION}/lib/infer/" \
    && rm /tmp/infer.tar.xz \
    && chmod +x external/infer/infer/bin/infer \
    && echo "[base] Infer ${INFER_VERSION} installed successfully." \
    || { echo "[base] WARNING: Could not download infer. Static analysis will be unavailable."; \
         mkdir -p external/infer/infer/bin external/infer/infer/lib; }

RUN git config --system --add safe.directory '*'

# Install Python package + Rust extensions
COPY ./src ./src
COPY ./Cargo.toml ./Cargo.toml
COPY ./pyproject.toml ./pyproject.toml
RUN python3.13 -m venv .venv && .venv/bin/pip install .

# Copy CRS code and configs
COPY ./crs ./crs
COPY ./configs ./configs
COPY ./prompts ./prompts
COPY ./main.py ./main.py
COPY ./run-crs.sh ./run-crs.sh
COPY ./oss-crs/scripts /opt/roboduck-oss-crs/

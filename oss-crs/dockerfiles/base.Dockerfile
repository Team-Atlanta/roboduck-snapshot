# Roboduck base image — prepare phase.
# No Azure dependencies. llvm-cov built from source. Infer skipped (see below).

###############################################################################
# Stage 1: Build infer from source — SKIPPED
#
# WHY: The pinned infer commit (1b1366e6) uses opam dependency resolution that
#      fails with current opam repositories. The original roboduck pulled
#      pre-built infer from Azure Blob Storage, which we stripped.
#
# IMPACT:
#   - LAUNCH_INFER pipeline stage will fail gracefully (no infer binary found)
#   - Static analysis reports from infer won't be generated
#   - Bug-finding still works: fuzzing, LLM analysis (ainalysis), and
#     diff analysis pipelines are unaffected
#   - Affects: crs/modules/infer.py — checks for external/infer/infer/bin/infer
#
# TO FIX: Either:
#   a) Pin opam to an older snapshot (opam repository archive), or
#   b) Update infer to a newer commit with compatible deps, or
#   c) Host pre-built infer binary somewhere accessible (GCS, GitHub release)
###############################################################################

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
       libssl-dev \
    && rustup default stable \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update --allow-insecure-repositories \
    && apt install -y --allow-unauthenticated python3.13 python3.13-dev python3.13-venv \
    && apt-get autoremove -y \
    && apt-get clean -y \
    && apt-get autoclean -y \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /crs /crs/external/infer /crs/external/llvm-cov
WORKDIR /crs

# Infer: NOT included (see Stage 1 comment above)
# Create empty placeholder so code that checks the path doesn't crash
RUN mkdir -p external/infer/infer/bin external/infer/infer/lib

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

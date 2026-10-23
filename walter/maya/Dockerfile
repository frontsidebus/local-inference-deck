# Project Maya (https://github.com/mw00/project-maya): an MoE engine that tiers experts over VRAM, RAM and NVMe, for
# models llama.cpp cannot run here (DeepSeek-V4.1-Flash, GLM-5.3-Flash). Lab only: see walter/maya/README.md.
#
# Everything is pinned: the CUDA base images by digest, Maya and llama.cpp (its ggml, gguf-py) by commit SHA (git checks
# the object hashes; the build refuses another HEAD), the Python packages by sha256 (requirements.lock). Maya's own
# installer (maya.sh / maya.py) is NOT used: it downloads unpinned packages and models and writes into the checkout.
# The image has no git, no .git, no compiler and no network need at runtime; the server's self-update cannot run.
#
# Build (any x86-64 host with Docker; no GPU needed):
#   docker build -t walter-maya:<MAYA_COMMIT[:7]>-<n> walter/maya   (<n>: this directory's revision, see README.md)
# GGML_NATIVE (-march=native) is on, as Maya builds it: build on a CPU with the runtime CPU's vector extensions
# (README.md), or pass --build-arg PORTABLE=ON for an AVX2 baseline.
ARG CUDA_DEVEL=nvidia/cuda:12.8.1-devel-ubuntu24.04@sha256:520292dbb4f755fd360766059e62956e9379485d9e073bbd2f6e3c20c270ed66
ARG CUDA_RUNTIME=nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:ebef3c171eeef0298e4eb2e4be843105edf3b8b0ac45e0b43acee358e8046867

FROM ${CUDA_DEVEL} AS build
ARG MAYA_COMMIT=451f9cb6a21ea65673bdde2e57b4a670ae2b8859
# third_party/ggml/VERSION.txt of that commit (setup.py LLAMA_CPP_COMMIT); Maya's setup fetches it as an unchecked zip
ARG LLAMA_CPP_COMMIT=3cf03257f219afbe7334045ff7c6a06ac68c627d
ARG CUDA_ARCH=86
ARG PORTABLE=OFF
ARG JOBS=8
# git: the fetches below, and CMake applies third_party/ggml/mmq-fused-swiglu.patch with `git apply`
RUN apt-get update && apt-get install -y --no-install-recommends git g++ make cmake ninja-build ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN git init -q /opt/maya && cd /opt/maya \
 && git fetch -q --depth 1 https://github.com/mw00/project-maya.git ${MAYA_COMMIT} && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${MAYA_COMMIT}" \
 && test "$(cut -d' ' -f1 third_party/ggml/VERSION.txt)" = "${LLAMA_CPP_COMMIT}" \
 && git init -q third_party/llama.cpp && cd third_party/llama.cpp \
 && git fetch -q --depth 1 https://github.com/ggml-org/llama.cpp.git ${LLAMA_CPP_COMMIT} && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${LLAMA_CPP_COMMIT}" && rm -rf .git
WORKDIR /opt/maya
# maya.py's compile_engine without the installer: sm_86 only, no tests; maya = GLM engine, maya-ds4 = DeepSeek engine
RUN cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} -DSTRATA_NATIVE_EXPERTS=ON -DSTRATA_BUILD_TESTS=OFF \
      -DSTRATA_PORTABLE=${PORTABLE} -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc -DCUDAToolkit_ROOT=/usr/local/cuda \
      -DSTRATA_GGML_DIR=/opt/maya/third_party/llama.cpp \
 && cmake --build build --target maya maya-ds4 -j ${JOBS} \
 && mkdir -p /out/build /out/data /out/third_party/llama.cpp \
 && cp build/maya build/maya-ds4 /out/build/ \
 && cp -r serve tools VERSION LICENSE LICENSE.deepseek-v41 /out/ \
 && cp data/expert-profile-glm.bin data/expert-profile-ds4.bin /out/data/ \
 && cp -r third_party/llama.cpp/gguf-py third_party/llama.cpp/LICENSE /out/third_party/llama.cpp/ \
 && for d in ggml exllamav3 beellama.cpp; do mkdir -p /out/third_party/$d; \
      cp third_party/$d/LICENSE* /out/third_party/$d/ 2>/dev/null || true; done \
 && rm -rf /out/serve/test_* /out/serve/fixtures /out/serve/mcp_fake_server.py /out/serve/fake_multi_engine.py \
 && find /out -name '__pycache__' -prune -exec rm -rf {} +

FROM ${CUDA_RUNTIME}
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: \
      -r /tmp/requirements.lock \
 && rm -rf /venv/lib/python3*/site-packages/pip* /venv/bin/pip* /tmp/requirements.lock
COPY --from=build /out /opt/maya
# lab_entry.py: Maya's server with only the inference routes reachable (llama-swap's /upstream/<model>/ passes any path)
COPY lab_entry.py /opt/maya/lab_entry.py
# MAYA_UPDATE_CHECK=0: no GitHub release check from the dashboard (egress is blocked anyway)
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MAYA_UPDATE_CHECK=0 TMPDIR=/tmp \
    STRATA_GGUF_PY=/opt/maya/third_party/llama.cpp/gguf-py STRATA_MAX_BODY_MIB=32 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility
# smoke: the server imports, and the engines need no library the image lacks (libcuda comes from the driver at run time)
RUN python3 -m compileall -q /opt/maya/serve /opt/maya/tools /venv/lib/python3*/site-packages \
 && cd /opt/maya && python3 -c "import serve.server" && python3 -c "import runpy; runpy.run_path('lab_entry.py')" \
 && ! ldd build/maya build/maya-ds4 | grep 'not found' | grep -v 'libcuda\.so'
USER 10010:10010
WORKDIR /opt/maya
EXPOSE 8080
ENTRYPOINT ["python3", "/opt/maya/lab_entry.py", "--engine", "strata"]

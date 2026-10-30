FROM python:3.11-slim

# 默认走清华镜像：files.pythonhosted.org 在国内经常 ReadTimeout
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG HF_ENDPOINT=https://hf-mirror.com
ARG PRELOAD_MODELS=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf \
    HF_ENDPOINT=${HF_ENDPOINT}

WORKDIR /app

# torch 的 manylinux wheel 依赖 libgomp；opencv（OCR）还需要 libGL 与 libglib。
# Debian 官方源在境内容器里常连不上，换腾讯云镜像
RUN if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i -e 's|deb.debian.org|mirrors.cloud.tencent.com|g' \
             -e 's|security.debian.org|mirrors.cloud.tencent.com|g' \
             /etc/apt/sources.list.d/debian.sources; \
    else \
      sed -i -e 's|deb.debian.org|mirrors.cloud.tencent.com|g' \
             -e 's|security.debian.org|mirrors.cloud.tencent.com|g' \
             /etc/apt/sources.list; \
    fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

# 用 CPU 版 torch：PyPI 默认 Linux wheel 会带 CUDA 依赖，镜像白白多 2GB
RUN pip install --index-url ${TORCH_INDEX_URL} torch
COPY requirements.txt .
RUN pip install --index-url ${PIP_INDEX_URL} -r requirements.txt

# 模型层放在 COPY . . 之前：只改代码时不用重新下载 2.4GB
COPY tools/preload_models.py tools/preload_models.py
RUN if [ "$PRELOAD_MODELS" = "1" ]; then python -m tools.preload_models; fi

COPY . .

# 镜像内重新生成语料：data/work 不进 git，但原始卷 + 解析产物在仓库里
RUN python -m pipeline.build --paper data/raw/2019_paper.txt \
                             --answer data/raw/2019_answer.txt \
                             --year 2019 --provider mock \
 && python -m pipeline.analysis --draft data/work/2019_draft.jsonl \
        --out data/curated/2019_analysis.jsonl \
        --apply-to data/work/2019_draft.jsonl \
 && python -m pipeline.build --paper data/raw/2020_paper.txt \
                             --answer data/raw/2020_answer.txt \
                             --year 2020 --provider mock \
 && python -m pipeline.build --paper data/raw/2023_paper.txt \
                             --answer data/raw/2023_answer.txt \
                             --year 2023 --provider mock \
 && python -m pipeline.build --paper data/raw/2021_paper.txt \
                             --answer data/raw/2021_answer.txt \
                             --year 2021 --provider mock \
 && python -m pipeline.analysis --draft data/work/2021_draft.jsonl \
        --out data/curated/2021_analysis.jsonl --merge-only \
        --apply-to data/work/2021_draft.jsonl \
 && python -m pipeline.build --paper data/raw/2024_paper.txt \
                             --answer data/raw/2024_answer.txt \
                             --year 2024 --provider mock \
 && python -m pipeline.analysis --draft data/work/2024_draft.jsonl \
        --out data/curated/2024_analysis.jsonl --merge-only \
        --apply-to data/work/2024_draft.jsonl

RUN useradd -m appuser && mkdir -p /app/logs && chown -R appuser:appuser /app/logs
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# 先等 Postgres 就绪（compose 的 healthcheck 之外再兜一层），再启动 API。
# 精排模型占 2GB+，单 worker；并发由进程内锁串行化
CMD ["sh", "-c", "python -m db.wait && exec uvicorn api.app:app --host 0.0.0.0 --port 8000 --workers 1"]

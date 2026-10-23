# -*- coding: utf-8 -*-
"""把检索和精排模型预下载到 HF 缓存，镜像构建时执行，避免容器启动再拉 2.3GB。

仓库常同时放 model.safetensors 和 pytorch_model.bin（后者是前者的重复），
默认跳过 .bin；万一某个模型没有 safetensors，再退回完整下载。
"""
import glob
import os

from huggingface_hub import snapshot_download

MODELS = [
    os.environ.get("KB_EMBED_MODEL", "BAAI/bge-small-zh-v1.5"),
    os.environ.get("KB_RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
]

SKIP_DUPLICATES = ["*.bin", "*.h5", "*.msgpack", "*.onnx", "onnx/*"]


def fetch(name: str) -> str:
    path = snapshot_download(name, max_workers=2,
                             ignore_patterns=SKIP_DUPLICATES)
    if glob.glob(os.path.join(path, "**", "*.safetensors"), recursive=True):
        return path
    # 没有 safetensors 的模型只能用 .bin，重新拉一次完整快照
    print("no safetensors for %s, downloading full snapshot" % name)
    return snapshot_download(name, max_workers=2,
                             ignore_patterns=["*.h5", "*.msgpack",
                                              "*.onnx", "onnx/*"])


def main() -> None:
    for name in MODELS:
        print("ok %s -> %s" % (name, fetch(name)))


if __name__ == "__main__":
    main()

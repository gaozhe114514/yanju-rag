# -*- coding: utf-8 -*-
"""向量召回。开发期用 bge-small-zh-v1.5（CPU 可跑），生产换 bge-m3。

模型下载走 HuggingFace；国内网络可设 HF_ENDPOINT=https://hf-mirror.com。
"""
import os
from typing import List, Sequence, Tuple

import numpy as np

# 国内机器默认走镜像；显式设置的环境变量优先
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def load_pretrained(fn, model_name: str, **kwargs):
    """先读本地缓存，没有再联网。

    已下载的模型每次联网做 HEAD 检查会在国内网络下卡十几秒甚至超时，
    离线优先能让评测和启动稳定在秒级。
    """
    try:
        return fn(model_name, local_files_only=True, **kwargs)
    except (OSError, ValueError):
        return fn(model_name, **kwargs)


class BgeEmbedder:
    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5",
                 query_instruction: str = QUERY_INSTRUCTION,
                 max_length: int = 512, device: str = ""):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.torch = torch
        self.model_name = model_name
        self.query_instruction = query_instruction
        self.max_length = max_length
        self.tokenizer = load_pretrained(AutoTokenizer.from_pretrained, model_name)
        self.model = load_pretrained(AutoModel.from_pretrained, model_name)
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device).eval()

    def embed(self, texts: Sequence[str], is_query: bool = False,
              batch_size: int = 16) -> np.ndarray:
        torch = self.torch
        texts = list(texts)
        if is_query and self.query_instruction:
            texts = [self.query_instruction + t for t in texts]
        out = []
        for i in range(0, len(texts), batch_size):
            batch = self.tokenizer(
                texts[i:i + batch_size], padding=True, truncation=True,
                max_length=self.max_length, return_tensors="pt",
            ).to(self.device)
            with torch.no_grad():
                hidden = self.model(**batch).last_hidden_state
            mask = batch["attention_mask"].unsqueeze(-1).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            out.append(pooled.cpu().numpy())
        return np.vstack(out) if out else np.zeros((0, 0), dtype=np.float32)


class VectorIndex:
    def __init__(self, vectors: np.ndarray):
        v = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        self.vectors = v / np.clip(norms, 1e-9, None)

    def search(self, query_vec: np.ndarray, top_k: int = 50) -> List[Tuple[int, float]]:
        q = np.asarray(query_vec, dtype=np.float32)
        q = q / max(float(np.linalg.norm(q)), 1e-9)
        scores = self.vectors @ q
        idx = np.argsort(-scores)[:top_k]
        return [(int(i), float(scores[i])) for i in idx]

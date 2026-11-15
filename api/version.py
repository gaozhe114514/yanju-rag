# -*- coding: utf-8 -*-
"""版本信息：前后端共用一个 version.json。

这里只用标准库读文件，不依赖 FastAPI，单测可以在只有标准库的环境里跑。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "version.json"


def version_info() -> dict:
    """版本信息只从仓库里的 version.json 读，前后端共用一个事实来源。"""
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": "0.0", "released": "", "name": "言据", "history": []}
    if not isinstance(data, dict):
        return {"version": "0.0", "released": "", "name": "言据", "history": []}
    history = data.get("history")
    if not isinstance(history, list):
        history = []
    return {
        "version": str(data.get("version") or "0.0"),
        "released": str(data.get("released") or ""),
        "name": str(data.get("name") or "言据"),
        "history": [h for h in history if isinstance(h, dict)][:20],
    }

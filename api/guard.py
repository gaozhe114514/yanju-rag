# -*- coding: utf-8 -*-
"""访问口令 + 限流。

公网部署时，谁拿到链接都能消耗 DeepSeek 额度，所以 /ask 默认要求口令；
再按 IP 做滑动窗口限流，另加一个全局每日上限兜底。

实现是单进程内存版，配合 `uvicorn --workers 1` 使用。多 worker / 多实例要换 Redis，
否则每个进程各算各的。
"""
import hmac
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, Mapping, Optional

log = logging.getLogger(__name__)


@dataclass
class Decision:
    ok: bool
    status: int = 200
    message: str = ""
    retry_after: int = 0


class AccessGuard:
    def __init__(self, token: str = "", rate_max: int = 20,
                 window: int = 600, daily_max: int = 0,
                 trust_proxy: bool = False,
                 clock: Callable[[], float] = time.time):
        self.token = token or ""
        self.rate_max = max(0, rate_max)
        self.window = max(1, window)
        self.daily_max = max(0, daily_max)
        self.trust_proxy = trust_proxy
        self.clock = clock
        self._lock = threading.Lock()
        self._hits: Dict[str, deque] = {}
        self._day = date.today()
        self._daily_used = 0

    @classmethod
    def from_env(cls) -> "AccessGuard":
        guard = cls(
            token=os.environ.get("KB_ACCESS_TOKEN", ""),
            rate_max=int(os.environ.get("KB_RATE_MAX", "20")),
            window=int(os.environ.get("KB_RATE_WINDOW", "600")),
            daily_max=int(os.environ.get("KB_DAILY_MAX", "0")),
            trust_proxy=os.environ.get("KB_TRUST_PROXY", "") in ("1", "true", "yes"),
        )
        if not guard.token:
            log.warning("KB_ACCESS_TOKEN 未设置：/ask 不校验口令，只适合本地调试")
        return guard

    @property
    def token_required(self) -> bool:
        return bool(self.token)

    def client_ip(self, headers: Mapping[str, str],
                  peer: Optional[str] = None) -> str:
        # 只有确认前面是自家反向代理时才开 trust_proxy；代理必须用 $remote_addr
        # 覆盖 X-Forwarded-For，否则客户端可以伪造第一个 IP 绕过限流。
        if self.trust_proxy:
            xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[0].strip()
        return peer or "unknown"

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._daily_used = 0

    def _prune(self, now: float) -> None:
        """公网上 IP 可能很多，窗口过期的桶要清掉，避免内存只增不减。"""
        if len(self._hits) < 1000:
            return
        stale = [ip for ip, hits in self._hits.items()
                 if not hits or now - hits[-1] > self.window]
        for ip in stale:
            del self._hits[ip]

    def check(self, headers: Mapping[str, str],
              peer: Optional[str] = None) -> Decision:
        ip = self.client_ip(headers, peer)

        if self.token:
            got = headers.get("x-access-token") or headers.get("X-Access-Token") or ""
            if not hmac.compare_digest(got.encode("utf-8"),
                                       self.token.encode("utf-8")):
                return Decision(False, 401, "访问口令不正确")

        now = self.clock()
        with self._lock:
            self._reset_daily_if_needed()

            if self.rate_max:
                self._prune(now)
                hits = self._hits.setdefault(ip, deque())
                while hits and now - hits[0] > self.window:
                    hits.popleft()
                if len(hits) >= self.rate_max:
                    wait = int(self.window - (now - hits[0])) + 1
                    return Decision(False, 429, "请求过于频繁，请稍后再试", wait)
                hits.append(now)

            if self.daily_max and self._daily_used >= self.daily_max:
                return Decision(False, 429, "今日额度已用完，请明天再试", 3600)
            self._daily_used += 1

        return Decision(True)

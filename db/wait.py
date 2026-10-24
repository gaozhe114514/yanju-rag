# -*- coding: utf-8 -*-
"""容器启动时等 Postgres 就绪，再拉起 API。

compose 的 depends_on: service_healthy 已经覆盖了正常路径；
这里再等一轮，防止数据库刚重启、健康检查还没翻绿就被连接拒绝。
"""
import os
import sys
import time

from db.store import Store


def main() -> None:
    store = Store.from_env()
    if store is None:
        print("未设置 DATABASE_URL，使用内存索引")
        return

    timeout = int(os.environ.get("DB_WAIT_TIMEOUT", "120"))
    deadline = time.time() + timeout
    while True:
        try:
            store.ping()
            print("Postgres 已就绪")
            return
        except Exception as exc:  # psycopg 的异常类型较杂，统一重试
            if time.time() >= deadline:
                sys.exit("等待 Postgres 超时：%s" % exc)
            time.sleep(2)


if __name__ == "__main__":
    main()

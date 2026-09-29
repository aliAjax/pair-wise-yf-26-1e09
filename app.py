"""数字档案长期保存服务入口。

业务按文件拆分：
  db.py        数据表、连接与通用错误/审计
  content.py   按内容摘要共用实体的规则（提交/副本/迁移）
  cleanup.py   存储整理任务（计划-确认两阶段）
  migration.py 历史数据平滑迁移
  store.py     档案、版本、副本、校验修复、迁移业务
  server.py    HTTP/页面接入与路由
"""
from __future__ import annotations

import argparse

from db import DEFAULT_DB
from server import PreservationServer
from store import PreservationStore


def main() -> None:
    parser = argparse.ArgumentParser(description="数字档案长期保存服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    store = PreservationStore(args.db)
    stats = store.init_schema()
    if stats:
        print(f"历史数据已平滑迁移到按摘要共用结构: {stats}")
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    print(f"数字档案服务运行于 http://127.0.0.1:{args.port}")
    server = PreservationServer(("127.0.0.1", args.port), store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

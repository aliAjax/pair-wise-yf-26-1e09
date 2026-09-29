# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

文件内容**按内容摘要（SHA-256）共用存储**：同一份内容无论出现在新版本、新登记的副本还是格式迁移产物里，都只在 `content_blobs` 中保存一份实体，版本清单（`archive_files`）与副本（`copy_files`）只登记 `blob_id` 引用。管理员可以生成残留实体清理计划、确认占用后清理；被引用的实体不会被误删。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

服务地址为 <http://127.0.0.1:8102>，默认数据库 `preservation.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`owner`、`archivist`、`auditor`、`outsider`。API 使用 `X-User-Id`。文件通过 Base64 提交，单文件上限 10 MiB；这是为了保持示例自包含，生产部署应换成对象存储和流式上传。

## 业务文件划分

| 文件 | 职责 |
| --- | --- |
| `db.py` | SQLite 连接、核心数据表、通用错误与审计写入 |
| `content.py` | 按摘要共用实体的规则：清单校验、`intern_blob` 去重登记、残留与占用盘点 |
| `cleanup.py` | 存储整理任务（两阶段：计划 → 确认），建表与状态机 |
| `migration.py` | 历史 V1 数据（内联 BLOB）平滑迁移到共用结构，幂等 |
| `store.py` | 档案、版本提交、副本登记、校验修复、格式迁移业务 |
| `server.py` | HTTP/JSON 路由与页面接入 |
| `app.py` | 命令行入口（启动时自动检测并迁移老库） |

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小；相同摘要的实体直接共用，返回 `shared_blobs`。
- `GET /api/versions/{id}`：查看版本、文件清单（含 `blob_id`）和副本状态。
- `POST /api/versions/{id}/copies`：登记副本，只登记对内容实体的引用，不复制字节。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复（把损坏路径的引用指回健康实体）。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏。共享存储下采用写时复制：生成独立的损坏实体并改指该副本的引用，绝不污染共享实体；仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本；未转换文件零拷贝沿用引用，新内容按摘要共用。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。

### 存储整理

- `GET /api/storage`：存储盘点——实体数、实际占用、被引用/残留占用、去重节省字节。
- `POST /api/storage/cleanups`：生成清理计划，快照当前所有无引用的残留实体及占用空间（仅 owner/archivist）。
- `GET /api/storage/cleanups`、`GET /api/storage/cleanups/{id}`：查看任务与逐条实体明细（`pending`/`deleted`/`skipped_referenced`/`skipped_missing`）。
- `POST /api/storage/cleanups/{id}/confirm`：确认执行。删除在写事务内逐条重新核对引用：计划生成后被新版本、副本或修复重新引用的实体标记为 `skipped_referenced` 保留，外键约束再兜底。因此整理与新提交并发时，被引用实体不会被清空。
- `POST /api/storage/cleanups/{id}/cancel`：取消未确认的计划。

首页 `/` 提供存储盘点、生成计划、确认/取消的操作入口。

## 历史数据迁移

启动（`--init`/正常运行/`seed`）时自动检测老库：`archive_files`/`copy_files` 仍带内联 `content` 列即为 V1。迁移在单事务内完成——抽取去重内容到 `content_blobs`、把两张文件表重建为引用表、保留全部版本/副本/审计元数据，结果登记到 `schema_migrations`，重复执行幂等。迁移后老版本清单、副本核验修复、格式迁移照常可用。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

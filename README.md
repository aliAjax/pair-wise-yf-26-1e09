# 数字档案长期保存服务

仅使用 Python 3.11+ 标准库实现的独立档案保存项目，支持按内容摘要共用存储、清单校验、真实 SHA-256 内容校验、多个离线副本、损坏检测与自动修复、格式迁移、保留期限和访问控制。

## 按内容摘要共用存储

文件字节只保存一份内容实体（`content_blobs`，主键为 SHA-256）。新提交的版本、新登记的副本和格式迁移产生的文件，摘要相同就共用同一份实体，版本清单与副本只登记引用：

- 版本提交时按摘要登记实体，重复内容只增加引用（返回 `shared_blobs` 计数）；
- 副本不再复制字节，直接引用版本清单指向的实体；
- 迁移生成的新版本，未转换文件沿用原引用，转换文件按摘要登记；
- 模拟损坏会把坏字节另存为独立实体并改指，共享实体本身从不被改动。

管理员整理存储：

- `GET /api/storage/overview`：物理实体数/字节、逻辑引用数/字节、共用节省量、残留实体和历史整理任务（auditor 可只读）；
- `GET /api/storage/orphans`：预览无人引用的残留实体与可回收空间（owner/archivist）；
- `POST /api/storage/purge`：确认后清空残留实体（owner/archivist）。清理在 `BEGIN IMMEDIATE` 事务内按两张引用表实时判定，与新提交/新副本/迁移串行互斥，被引用的实体不会被清空。

首页“存储整理”面板可直接完成预览与确认清理。

## 代码结构

- `db_schema.py`：内容实体表、引用表结构与历史库平滑迁移；
- `storage_rules.py`：按摘要共用、引用登记、修复改指等规则；
- `storage_gc.py`：残留实体预览、占用统计与确认清理任务；
- `web/storage.js`：存储整理页面接入；
- `app.py`：HTTP 服务、权限与业务编排。

历史数据库（`archive_files`/`copy_files` 自带 BLOB 的旧结构）在 `--init`/`--seed`/首次启动时自动平滑迁移：重复字节按摘要去重进 `content_blobs`，两张引用表保留原有 ID 重建；迁移后老版本清单、副本修复和格式迁移照常使用，迁移过程幂等。

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

## 主要接口

- `POST /api/archives`：创建受限档案。
- `POST /api/archives/{id}/members`：所有者授予 read/write 权限。
- `POST /api/archives/{id}/versions`：提交文件清单，服务端重新计算哈希和大小。
- `GET /api/versions/{id}`：查看版本、文件清单和副本状态。
- `POST /api/versions/{id}/copies`：创建独立副本内容。
- `POST /api/copies/{id}/verify`：校验副本；发现损坏时从健康副本修复。
- `POST /api/copies/{id}/simulate-corruption`：演示/测试介质损坏，仅 owner 或 archivist 可用。
- `POST /api/versions/{id}/migrate`：生成格式迁移后的新版本并保留派生关系。
- `GET /api/archives/{id}/status`：保留期限、版本状态和审计记录。
- `GET /api/storage/overview` / `GET /api/storage/orphans` / `POST /api/storage/purge`：存储占用、残留实体预览与确认清理。

档案路径拒绝绝对路径和 `..`；同一版本副本位置唯一；没有健康副本时版本标记为 `degraded`；所有变更写入审计日志。

# 湖仓一体化治理平台

面向数据架构师的湖仓一体化治理平台。覆盖数据湖表格式管理、Iceberg/Hudi表优化、小文件合并、数据压缩策略、元数据服务、表快照管理、数据分支合并与数据回滚恢复的完整湖仓治理链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 湖表格式管理 | `tablefmt` | 表格式配置 | 配置编号、表名称、格式类型 |
| 小文件合并 | `compact` | 合并任务 | 任务编号、目标表名、文件范围 |
| 数据压缩策略 | `compress` | 压缩配置 | 配置编号、目标表名、压缩算法 |
| 元数据服务 | `metadata` | 元数据表 | 表编号、表名称、字段清单 |
| 表快照管理 | `snapshot` | 快照记录 | 快照编号、表名称、快照时间 |
| 数据分支合并 | `branch` | 分支记录 | 分支编号、表名称、分支类型 |
| 数据回滚恢复 | `rollback` | 回滚记录 | 记录编号、表名称、回滚版本 |
| 分区演进管理 | `partition` | 演进计划 | 计划编号、表名称、当前分区 |
| 表优化管理 | `iceberg` | 优化任务 | 任务编号、表名称、优化类型 |
| 数据目录同步 | `catalog` | 同步作业 | 作业编号、源路径、目标目录 |
| 数据过期清理 | `cleanup` | 清理策略 | 策略编号、目标表名、过期条件 |
| 湖存储配额 | `quota` | 配额配置 | 配置编号、存储空间、表数量上限 |
| 湖仓血缘追踪 | `lineage` | 血缘记录 | 记录编号、上游表名、下游表名 |
| 治理任务调度 | `schedule` | 治理任务 | 任务编号、任务类型、调度表达式 |
| 表统计信息 | `stats` | 统计报告 | 报告编号、表名称、行总数 |
| 数据迁移管理 | `migrate` | 迁移任务 | 任务编号、源表名、目标表名 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 数据目录同步：字段漂移预检与增量水位

数据目录同步在原有作业清单之外，提供一条可恢复的增量确认链路（后端
`app/services/catalog_sync.py`，接口前缀 `/api/catalog/sync`）：

1. **读取作业生成增量批次**：`POST /sync/jobs/{id}/generate` 读取同步作业，先做字段
   漂移预检（源字段类型 vs 目录字段类型，结论为 一致 / 兼容可接收 / 版本冲突），再生成
   携带「确认边界水位 + 新水位 + 目录元数据快照」的批次。
2. **冲突停边界、兼容可推进**：存在版本冲突时批次为「已阻塞」，增量水位停在确认边界；
   逐条「接收兼容字段」消解冲突后，批次转为「待处理」，确认后才推进到新水位；也可忽略
   批次，水位保持在确认边界，断线重试从该水位继续。
3. **同成同退、幂等确认**：`POST /sync/batches/{id}/confirm` 在同一把锁内完成
   「写目录元数据 + 推进增量水位 + 批次置已确认」；重复提交同一批次只写入一次，
   目录元数据始终只对应一个完整批次。
4. **批量选择与整组提交**：同步清单支持多选作业，`POST /sync/batches/confirm-bulk`
   一次提交多个批次，结果按批次分别保留。
5. **同一版本水位的两方消费**：确认时元数据服务自动把字段结论写入元数据审计清单；
   另一业务面通过 `POST /sync/batches/{id}/business-consume` 消费同一份水位并回写审计
   清单，审计记录支持逐条核验（`POST /sync/audit/{id}/verify`）。
6. **可核对的三类视图**：增量水位（`/sync/watermarks`）、目录元数据
   （`/sync/metadata`）、元数据审计清单（`/sync/audit`）中的批次版本与水位版本一致。

示例数据 `customer_events`：源字段 `customer_id` 为 `VARCHAR(32)`、目录侧为 `STRING`
（兼容可接收），增量字段为 `event_time`；首批次预置 `JSON → STRING` 的版本冲突用于
演示「停在待处理边界 → 接收兼容字段 → 确认推进」的完整流程。

领域：数据工程
子方向：数仓与数据湖

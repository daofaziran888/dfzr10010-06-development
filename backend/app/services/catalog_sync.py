"""数据目录同步领域服务：字段漂移预检、增量批次、增量水位与目录元数据。

设计要点（对应需求中的一致性约束）：

* ``sync_batches`` 是一次增量读取的完整批次，批次同时携带拟写入的目录元数据和
  确认后才推进的新水位；批次未确认前水位停在确认边界，重试只会读到旧水位。
* ``confirm_batch`` 用同一把锁完成「写目录元数据 + 推进增量水位 + 批次置为已确认」，
  三者同成同退；目录元数据始终只对应一个完整批次，不会出现半个批次的元数据。
* 批次确认按 ``(作业, 批次号)`` 幂等：重复提交同一批次直接返回已有结果，
  目录元数据不会多写一条，水位也不会重复推进，断线重试可从确认水位继续。
* 源字段类型与目录字段类型存在版本冲突时，批次停在待处理边界（阻塞）；
  接收兼容字段后批次可确认并推进到新水位。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from app.store import store

SYNC_JOB_MODULE = "catalog"
BATCH_MODULE = "sync_batches"
CATALOG_META_MODULE = "catalog_metadata"
AUDIT_MODULE = "metadata_audit"

# 批次生命周期：待预检 -> 待处理（预检通过，等待确认）/ 已阻塞（存在未处理的版本冲突）
# 已确认（目录元数据与增量水位已同批落地）/ 已忽略（冲突不接收，水位停在确认边界）
BATCH_PENDING = "待处理"
BATCH_BLOCKED = "已阻塞"
BATCH_CONFIRMED = "已确认"
BATCH_IGNORED = "已忽略"

# 字段漂移结论
DRIFT_CONSISTENT = "一致"
DRIFT_COMPATIBLE = "兼容可接收"
DRIFT_CONFLICT = "版本冲突"

# 兼容类型族：同族内的源类型 -> 目录类型视为可接收的兼容字段，跨族且不一致即为版本冲突
TYPE_FAMILIES: list[set[str]] = [
    {"VARCHAR", "STRING", "CHAR", "TEXT"},
    {"INTEGER", "BIGINT", "INT", "SMALLINT", "LONG"},
    {"DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL"},
    {"TIMESTAMP", "DATETIME", "DATE"},
    {"BOOLEAN", "BOOL", "BIT"},
    {"BINARY", "VARBINARY", "BLOB"},
]


def _type_family(type_name: str) -> str | None:
    head = str(type_name or "").strip().upper().split("(", 1)[0]
    for family in TYPE_FAMILIES:
        if head in family:
            return next(iter(family))
    return None


def normalize_type(type_name: str) -> str:
    """去掉长度/精度包装，只保留类型族名，例如 VARCHAR(32) -> VARCHAR。"""
    return str(type_name or "").strip().upper().split("(", 1)[0]


def compare_field_type(source_type: str, catalog_type: str) -> str:
    """字段漂移预检：比较源字段类型与目录字段类型，给出漂移结论。"""
    source_norm = normalize_type(source_type)
    catalog_norm = normalize_type(catalog_type)
    if not source_norm or not catalog_norm:
        return DRIFT_CONFLICT
    if source_norm == catalog_norm:
        return DRIFT_CONSISTENT
    if _type_family(source_norm) == _type_family(catalog_norm):
        return DRIFT_COMPATIBLE
    return DRIFT_CONFLICT


class CatalogSyncService:
    """同步作业读取、批次生成与确认的状态都收口在这一个服务里。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 水位
    def get_watermark(self, job_id: int) -> dict[str, Any] | None:
        job = store.find(SYNC_JOB_MODULE, job_id)
        if job is None:
            return None
        return {
            "作业编号": job.get("作业编号"),
            "增量字段": job.get("增量字段"),
            "确认水位": job.get("确认水位", "0"),
            "水位批次版本": job.get("水位批次版本", "未初始化"),
            "待处理批次号": job.get("待处理批次号"),
        }

    def list_watermarks(self) -> list[dict[str, Any]]:
        return [wm for job in store.rows(SYNC_JOB_MODULE) if (wm := self.get_watermark(int(job["id"])))]

    # -------------------------------------------------------------- 批次清单
    def list_batches(self, job_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        if job_id is not None:
            rows = [row for row in rows if int(row.get("作业id", 0)) == job_id]
        return deepcopy(rows)

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        row = store.find(BATCH_MODULE, batch_id)
        return deepcopy(row) if row else None

    def _find_open_batch(self, job_id: int) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if int(row.get("作业id", 0)) == job_id and row.get("批次状态") in (BATCH_PENDING, BATCH_BLOCKED):
                return row
        return None

    # ---------------------------------------------------------- 读取同步作业
    def read_job(self, job_id: int) -> dict[str, Any] | None:
        job = store.find(SYNC_JOB_MODULE, job_id)
        return deepcopy(job) if job else None

    # ------------------------------------------------ 生成增量批次（含漂移预检）
    def generate_batch(self, job_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            job = store.find(SYNC_JOB_MODULE, job_id)
            if job is None:
                return None, f"同步作业 {job_id} 不存在或已归档"

            # 已经有待确认/阻塞批次时不重复生成：同一确认边界只允许一个在途批次
            open_batch = self._find_open_batch(job_id)
            if open_batch is not None:
                return deepcopy(open_batch), "存在待确认批次，已返回当前在途批次，未重复生成"

            plan = self._source_plan(job)
            current_watermark = str(job.get("确认水位", "0"))
            next_seq = max(
                (int(r.get("批次序号", 0)) for r in store.rows(BATCH_MODULE)
                 if int(r.get("作业id", 0)) == job_id),
                default=0,
            ) + 1
            batch_no = f"{job.get('作业编号')}-B{next_seq:03d}"
            new_watermark = str(plan["watermark"])
            batch_version = f"v{next_seq}"

            # 字段漂移预检：逐字段比对源类型与目录类型
            drift_fields: list[dict[str, Any]] = []
            has_conflict = False
            for field in plan["fields"]:
                conclusion = compare_field_type(field["源字段类型"], field["目录字段类型"])
                drift_fields.append({
                    "字段名": field["字段名"],
                    "增量字段": field.get("增量字段", False),
                    "源字段类型": field["源字段类型"],
                    "目录字段类型": field["目录字段类型"],
                    "漂移结论": conclusion,
                    "核验状态": "待核验",
                })
                if conclusion == DRIFT_CONFLICT:
                    has_conflict = True

            # 目录元数据快照：确认批次时原样写入，保证元数据与水位来自同一确认批次
            catalog_meta_snapshot = {
                "作业编号": job.get("作业编号"),
                "源路径": job.get("源路径"),
                "目标目录": job.get("目标目录"),
                "表名称": plan["table_name"],
                "字段清单": [
                    {"字段名": f["字段名"], "字段类型": f["目录字段类型"]} for f in plan["fields"]
                ],
                "增量字段": job.get("增量字段"),
                "批次版本": batch_version,
            }

            rows = store.rows(BATCH_MODULE)
            batch = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "作业id": job_id,
                "作业编号": job.get("作业编号"),
                "批次号": batch_no,
                "批次序号": next_seq,
                "批次版本": batch_version,
                "批次状态": BATCH_BLOCKED if has_conflict else BATCH_PENDING,
                "增量字段": job.get("增量字段"),
                "确认边界水位": current_watermark,
                "新水位": new_watermark,
                "漂移预检": drift_fields,
                "目录元数据快照": catalog_meta_snapshot,
                "待处理冲突数": sum(1 for f in drift_fields if f["漂移结论"] == DRIFT_CONFLICT),
                "元数据记录id": None,
            }
            rows.append(batch)
            job["待处理批次号"] = batch_no
            if has_conflict:
                job["作业状态提示"] = "字段版本冲突，水位停在确认边界"
            return deepcopy(batch), ("字段漂移预检发现版本冲突，批次停在待处理边界"
                                     if has_conflict else "增量批次已生成，等待确认同步")

    def _source_plan(self, job: dict[str, Any]) -> dict[str, Any]:
        """模拟读取数据目录同步作业在源端的下一批增量数据。

        每个作业按已确认批次序号推进到下一个预置场景，保证演示可重复。
        """
        job_id = int(job["id"])
        confirmed = sum(
            1 for r in store.rows(BATCH_MODULE)
            if int(r.get("作业id", 0)) == job_id and r.get("批次状态") == BATCH_CONFIRMED
        )
        plans = SYNC_SOURCE_PLANS.get(job_id, DEFAULT_SOURCE_PLANS)
        return deepcopy(plans[min(confirmed, len(plans) - 1)])

    # ---------------------------------------------------------- 确认/忽略批次
    def confirm_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"同步批次 {batch_id} 不存在"

            # 幂等：重复提交同一批次只返回首次确认结果，不多写目录记录、不重复推进水位
            if batch.get("批次状态") == BATCH_CONFIRMED:
                return deepcopy(batch), f"批次 {batch['批次号']} 已确认，重复提交未重复写入目录元数据与水位"
            if batch.get("批次状态") == BATCH_IGNORED:
                return None, f"批次 {batch['批次号']} 已忽略，无法再确认"
            if batch.get("批次状态") == BATCH_BLOCKED:
                remaining = sum(1 for f in batch.get("漂移预检", []) if f["漂移结论"] == DRIFT_CONFLICT)
                if remaining:
                    return None, (f"批次仍有 {remaining} 个版本冲突字段未处理，"
                                  "水位停在确认边界，请先接收兼容字段或忽略批次")

            job = store.find(SYNC_JOB_MODULE, int(batch["作业id"]))
            if job is None:
                return None, "同步作业已归档，无法确认批次"

            # 同成同退：目录元数据写入、水位推进、批次确认在同一把锁内顺序完成，
            # 任一步失败都不会留下提前推进的水位（内存实现里通过先组装再落库保证）。
            snapshot = deepcopy(batch["目录元数据快照"])
            meta_rows = store.rows(CATALOG_META_MODULE)
            already = next(
                (r for r in meta_rows
                 if r.get("作业编号") == batch["作业编号"] and r.get("批次版本") == batch["批次版本"]),
                None,
            )
            if already is None:
                meta = {
                    "id": max((int(r.get("id", 0)) for r in meta_rows), default=0) + 1,
                    "同步批次id": batch_id,
                    "确认水位": batch["新水位"],
                    **snapshot,
                }
                meta_rows.append(meta)
                batch["元数据记录id"] = meta["id"]
            else:
                batch["元数据记录id"] = already["id"]

            job["确认水位"] = batch["新水位"]
            job["水位批次版本"] = batch["批次版本"]
            job["待处理批次号"] = None
            job["作业状态提示"] = f"已确认 {batch['批次号']}，水位推进至 {batch['新水位']}"
            batch["批次状态"] = BATCH_CONFIRMED

            # 元数据服务消费同一版本水位：为字段清单保留同步批次并写入审计清单
            self._append_audit(batch, consumer="元数据服务")
            return deepcopy(batch), f"批次 {batch['批次号']} 已确认：目录元数据与增量水位同批落地"

    def ignore_batch(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"同步批次 {batch_id} 不存在"
            if batch.get("批次状态") == BATCH_CONFIRMED:
                return None, f"批次 {batch['批次号']} 已确认，不能忽略"
            if batch.get("批次状态") == BATCH_IGNORED:
                return deepcopy(batch), f"批次 {batch['批次号']} 已处于忽略状态，水位保持在确认边界"
            job = store.find(SYNC_JOB_MODULE, int(batch["作业id"]))
            batch["批次状态"] = BATCH_IGNORED
            if job is not None:
                job["待处理批次号"] = None
                job["作业状态提示"] = f"已忽略 {batch['批次号']}，水位停在 {batch['确认边界水位']}"
            return deepcopy(batch), f"批次 {batch['批次号']} 已忽略，增量水位停在确认边界 {batch['确认边界水位']}"

    # ------------------------------------------- 冲突字段逐条处理（接收兼容字段）
    def resolve_field(self, batch_id: int, field_name: str, receive: bool) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"同步批次 {batch_id} 不存在"
            if batch.get("批次状态") not in (BATCH_BLOCKED, BATCH_PENDING):
                return None, f"批次 {batch.get('批次号')} 已结束，不能再核验字段"
            target = next((f for f in batch["漂移预检"] if f["字段名"] == field_name), None)
            if target is None:
                return None, f"批次中不存在字段 {field_name}"
            if target["漂移结论"] != DRIFT_CONFLICT:
                return None, f"字段 {field_name} 结论为{target['漂移结论']}，无需冲突处理"
            if receive:
                target["漂移结论"] = DRIFT_COMPATIBLE
                target["核验状态"] = "已接收兼容字段"
            else:
                target["核验状态"] = "已标记不接收"
            remaining = sum(1 for f in batch["漂移预检"] if f["漂移结论"] == DRIFT_CONFLICT)
            batch["待处理冲突数"] = remaining
            if remaining == 0 and batch["批次状态"] == BATCH_BLOCKED:
                batch["批次状态"] = BATCH_PENDING
            return deepcopy(batch), (f"字段 {field_name} 已{'接收' if receive else '标记不接收'}，"
                                     f"剩余待处理冲突 {remaining} 个")

    # ----------------------------------------------------------- 目录元数据
    def list_catalog_metadata(self, job_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(CATALOG_META_MODULE)
        if job_id is not None:
            rows = [r for r in rows if int(r.get("作业id", r.get("同步批次id", 0)) or 0) == job_id]
        return deepcopy(rows)

    def get_catalog_metadata(self, meta_id: int) -> dict[str, Any] | None:
        row = store.find(CATALOG_META_MODULE, meta_id)
        return deepcopy(row) if row else None

    # ----------------------------------------------------------- 元数据审计清单
    def _append_audit(self, batch: dict[str, Any], *, consumer: str) -> None:
        rows = store.rows(AUDIT_MODULE)
        # 幂等：同一批次同一消费方只保留一条审计记录
        exists = next(
            (r for r in rows if r.get("同步批次id") == batch["id"] and r.get("消费方") == consumer),
            None,
        )
        if exists is not None:
            return
        record = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "同步批次id": batch["id"],
            "批次号": batch["批次号"],
            "批次版本": batch["批次版本"],
            "作业编号": batch["作业编号"],
            "消费方": consumer,
            "水位版本": batch["新水位"],
            "审计状态": "待核验",
            "字段结论": [dict(f) for f in batch.get("漂移预检", [])],
        }
        rows.append(record)

    def list_audit(self, consumer: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if consumer:
            rows = [r for r in rows if r.get("消费方") == consumer]
        return deepcopy(rows)

    def verify_audit(self, audit_id: int) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            record = store.find(AUDIT_MODULE, audit_id)
            if record is None:
                return None, f"审计记录 {audit_id} 不存在"
            record["审计状态"] = "已核验"
            return deepcopy(record), f"审计记录 {record['批次号']}（{record['消费方']}）已逐条核验"

    def business_consume(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """另一个业务面读取同一份增量水位，并把结果同步到元数据审计清单。"""
        with self._lock:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"同步批次 {batch_id} 不存在"
            if batch.get("批次状态") != BATCH_CONFIRMED:
                return None, "只有已确认批次的水位才能被业务面消费"
            self._append_audit(batch, consumer="业务面")
            record = next(
                r for r in store.rows(AUDIT_MODULE)
                if r.get("同步批次id") == batch_id and r.get("消费方") == "业务面"
            )
            return deepcopy(record), f"业务面已消费批次 {batch['批次号']} 的同一版本水位，结果已写入审计清单"


# 未预置源端场景的作业使用的默认增量计划
DEFAULT_SOURCE_PLANS: list[dict[str, Any]] = [
    {
        "table_name": "default_table",
        "watermark": "2026-11-10 00:00:00",
        "fields": [
            {"字段名": "id", "源字段类型": "BIGINT", "目录字段类型": "BIGINT"},
            {"字段名": "updated_at", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
        ],
    },
]


# 源端增量场景：每个作业预置多批，按确认进度依次读取；customer_events 对应需求示例
SYNC_SOURCE_PLANS: dict[int, list[dict[str, Any]]] = {
    1: [
        {
            "table_name": "customer_events",
            "watermark": "2026-11-10 10:00:00",
            "fields": [
                {"字段名": "customer_id", "源字段类型": "VARCHAR(32)", "目录字段类型": "STRING"},
                {"字段名": "event_time", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "event_type", "源字段类型": "VARCHAR(16)", "目录字段类型": "VARCHAR(16)"},
                {"字段名": "payload", "源字段类型": "JSON", "目录字段类型": "STRING"},
            ],
        },
        {
            "table_name": "customer_events",
            "watermark": "2026-11-11 10:00:00",
            "fields": [
                {"字段名": "customer_id", "源字段类型": "VARCHAR(32)", "目录字段类型": "STRING"},
                {"字段名": "event_time", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "event_type", "源字段类型": "VARCHAR(16)", "目录字段类型": "VARCHAR(16)"},
                {"字段名": "payload", "源字段类型": "STRING", "目录字段类型": "STRING"},
            ],
        },
        {
            "table_name": "customer_events",
            "watermark": "2026-11-12 10:00:00",
            "fields": [
                {"字段名": "customer_id", "源字段类型": "VARCHAR(32)", "目录字段类型": "STRING"},
                {"字段名": "event_time", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "event_type", "源字段类型": "VARCHAR(16)", "目录字段类型": "VARCHAR(16)"},
                {"字段名": "payload", "源字段类型": "STRING", "目录字段类型": "STRING"},
            ],
        },
    ],
    2: [
        {
            "table_name": "order_snapshot",
            "watermark": "2026-11-10 12:00:00",
            "fields": [
                # STRING 与 BIGINT 跨类型族：版本冲突，首批次水位停在确认边界
                {"字段名": "order_id", "源字段类型": "STRING", "目录字段类型": "BIGINT"},
                {"字段名": "updated_at", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "amount", "源字段类型": "DECIMAL(18,2)", "目录字段类型": "DOUBLE"},
            ],
        },
        {
            "table_name": "order_snapshot",
            "watermark": "2026-11-11 12:00:00",
            "fields": [
                {"字段名": "order_id", "源字段类型": "BIGINT", "目录字段类型": "BIGINT"},
                {"字段名": "updated_at", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "amount", "源字段类型": "DECIMAL(18,2)", "目录字段类型": "DOUBLE"},
            ],
        },
    ],
    3: [
        {
            "table_name": "device_metrics",
            "watermark": "2026-11-10 18:00:00",
            "fields": [
                {"字段名": "device_id", "源字段类型": "VARCHAR(24)", "目录字段类型": "VARCHAR(24)"},
                {"字段名": "metric_time", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                # 源端把温度上报成字符串：STRING 与 DOUBLE 跨类型族，构成版本冲突
                {"字段名": "temperature", "源字段类型": "STRING", "目录字段类型": "DOUBLE"},
            ],
        },
        {
            "table_name": "device_metrics",
            "watermark": "2026-11-11 18:00:00",
            "fields": [
                {"字段名": "device_id", "源字段类型": "VARCHAR(24)", "目录字段类型": "VARCHAR(24)"},
                {"字段名": "metric_time", "源字段类型": "TIMESTAMP", "目录字段类型": "TIMESTAMP", "增量字段": True},
                {"字段名": "temperature", "源字段类型": "DOUBLE", "目录字段类型": "DOUBLE"},
            ],
        },
    ],
}


sync_service = CatalogSyncService()

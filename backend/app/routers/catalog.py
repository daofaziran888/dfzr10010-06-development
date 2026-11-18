"""数据目录同步接口：维护同步作业，覆盖启动同步、暂停同步、归档作业等动作。

同步领域接口（字段漂移预检、增量水位、批次确认、目录元数据、元数据审计清单）
统一挂在 ``/api/catalog/sync`` 前缀下，与原有作业清单接口共存。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.catalog import CatalogService
from app.services.catalog_sync import sync_service

router = APIRouter(prefix="/api/catalog", tags=["数据目录同步"])

service = CatalogService()

LIST_FIELDS = ["作业编号", "源路径", "目标目录", "同步模式", "增量字段", "过滤规则", "同步频率", "作业状态"]
STATUSES = ["草稿", "同步中", "已完成", "已暂停"]


class BatchGeneratePayload(BaseModel):
    pass


class FieldResolvePayload(BaseModel):
    field_name: str
    receive: bool = True


class BulkConfirmPayload(BaseModel):
    batch_ids: list[int]


# ---------------------------------------------------------------- 同步领域接口
@router.get("/sync/watermarks")
def list_watermarks() -> dict[str, Any]:
    """读取全部同步作业的增量水位；水位只来自已确认批次。"""
    return {"items": sync_service.list_watermarks(), "total": len(sync_service.list_watermarks())}


@router.get("/sync/batches")
def list_batches(job_id: int | None = Query(default=None, description="按同步作业过滤批次")) -> dict[str, Any]:
    """增量批次清单：支持按作业过滤，整组查看待确认/已阻塞/已确认批次。"""
    items = sync_service.list_batches(job_id)
    return {"items": items, "total": len(items)}


@router.get("/sync/batches/{batch_id}")
def get_batch(batch_id: int) -> dict[str, Any]:
    batch = sync_service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"同步批次 {batch_id} 不存在")
    return batch


@router.post("/sync/jobs/{job_id}/generate", response_model=ActionResult)
def generate_batch(job_id: int) -> ActionResult:
    """读取数据目录同步作业并生成增量批次：先做字段漂移预检，再给出待确认水位。"""
    batch, message = sync_service.generate_batch(job_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    blocked = batch.get("批次状态") == "已阻塞"
    return ActionResult(ok=not blocked, message=message, entry=batch)


@router.post("/sync/batches/{batch_id}/confirm", response_model=ActionResult)
def confirm_batch(batch_id: int) -> ActionResult:
    """确认批次：目录元数据与增量水位同成同退；重复提交同一批次只写入一次。"""
    batch, message = sync_service.confirm_batch(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/sync/batches/confirm-bulk", response_model=ActionResult)
def confirm_batches(payload: BulkConfirmPayload) -> ActionResult:
    """整组提交：多选同步任务一次提交，分别保留每个批次的确认结果。"""
    results: list[dict[str, Any]] = []
    ok_count = 0
    for batch_id in payload.batch_ids:
        batch, message = sync_service.confirm_batch(batch_id)
        if batch is not None:
            ok_count += 1
        results.append({"batch_id": batch_id, "ok": batch is not None, "message": message,
                        "entry": batch})
    return ActionResult(
        ok=ok_count > 0,
        message=f"整组提交完成：成功 {ok_count} 个，共 {len(payload.batch_ids)} 个，结果分别保留",
        entry={"results": results, "success": ok_count, "total": len(payload.batch_ids)},
    )


@router.post("/sync/batches/{batch_id}/ignore", response_model=ActionResult)
def ignore_batch(batch_id: int) -> ActionResult:
    """忽略存在版本冲突的批次：增量水位停在确认边界，不写目录元数据。"""
    batch, message = sync_service.ignore_batch(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/sync/batches/{batch_id}/fields/resolve", response_model=ActionResult)
def resolve_field(batch_id: int, payload: FieldResolvePayload) -> ActionResult:
    """逐条核验冲突字段：接收兼容字段后冲突消解，批次可继续确认并推进水位。"""
    batch, message = sync_service.resolve_field(batch_id, payload.field_name, payload.receive)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/sync/batches/{batch_id}/business-consume", response_model=ActionResult)
def business_consume(batch_id: int) -> ActionResult:
    """另一个业务面读取同一份增量水位，把消费结果同步到元数据审计清单。"""
    record, message = sync_service.business_consume(batch_id)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.get("/sync/metadata")
def list_catalog_metadata(job_id: int | None = Query(default=None)) -> dict[str, Any]:
    """目录元数据清单：每条元数据都能回溯到唯一的已确认批次与水位版本。"""
    items = sync_service.list_catalog_metadata(job_id)
    return {"items": items, "total": len(items)}


@router.get("/sync/metadata/{meta_id}")
def get_catalog_metadata(meta_id: int) -> dict[str, Any]:
    meta = sync_service.get_catalog_metadata(meta_id)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"目录元数据 {meta_id} 不存在")
    return meta


@router.get("/sync/audit")
def list_audit(consumer: str | None = Query(default=None, description="元数据服务 / 业务面")) -> dict[str, Any]:
    """元数据审计清单：两个消费方读取同一版本水位的结论都保留在这里。"""
    items = sync_service.list_audit(consumer)
    return {"items": items, "total": len(items)}


@router.post("/sync/audit/{audit_id}/verify", response_model=ActionResult)
def verify_audit(audit_id: int) -> ActionResult:
    """逐条核验审计清单：字段结论回写后由人工确认审计状态。"""
    record, message = sync_service.verify_audit(audit_id)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


# ---------------------------------------------------------------- 原有作业接口
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号检索"),
    status: str | None = Query(default=None, description="草稿、同步中、已完成、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤数据目录同步列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条同步作业明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"同步作业 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条同步作业，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="同步作业已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条同步作业执行启动同步、暂停同步、归档作业；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据目录同步清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "catalog", "total": total, "items": items}

# -*- coding: utf-8 -*-
"""波段回测 endpoints：运行库查询、异步运行、报告下载。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from api.v1.schemas.swing_backtest import (
    SwingRunDetail,
    SwingRunRequest,
    SwingRunStatus,
    SwingRunSummary,
    SwingRunsResponse,
)
from src.services.swing_backtest_service import (
    CacheUnavailableError,
    InvalidRunRequestError,
    RunBusyError,
    RunNotFoundError,
    get_report_file,
    get_run_detail,
    get_run_status,
    list_runs,
    submit_run,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_MEDIA_TYPES = {
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


@router.get(
    "/runs",
    response_model=SwingRunsResponse,
    summary="列出波段回测运行",
)
def list_swing_runs() -> SwingRunsResponse:
    return SwingRunsResponse(items=[SwingRunSummary(**item) for item in list_runs()])


@router.post(
    "/runs",
    response_model=SwingRunStatus,
    status_code=202,
    summary="基于本地缓存触发波段回测",
    description="只读本地 BaoStock 缓存，不发起网络抓取；同一进程串行执行。",
)
def create_swing_run(request: SwingRunRequest) -> SwingRunStatus:
    try:
        status = submit_run(
            strategy=request.strategy,
            start=request.start,
            end=request.end,
            cost_tier=request.cost_tier,
            mode=request.mode,
            max_codes=request.max_codes,
            aum=request.aum,
        )
        return SwingRunStatus(**status)
    except InvalidRunRequestError as error:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": str(error)})
    except (CacheUnavailableError, RunBusyError) as error:
        raise HTTPException(status_code=409, detail={"error": "conflict", "message": str(error)})
    except Exception as error:  # noqa: BLE001
        logger.error("触发波段回测失败: %s", error, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"触发回测失败: {error}"},
        )


@router.get(
    "/runs/{run_id}/status",
    response_model=SwingRunStatus,
    summary="查询波段回测运行状态",
)
def get_swing_run_status(run_id: str) -> SwingRunStatus:
    try:
        return SwingRunStatus(**get_run_status(run_id))
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(error)})


@router.get(
    "/runs/{run_id}",
    response_model=SwingRunDetail,
    summary="获取波段回测详情",
)
def get_swing_run(
    run_id: str,
    trade_limit: int = Query(200, ge=1, le=500, description="返回的最近交易数"),
) -> SwingRunDetail:
    try:
        return SwingRunDetail(**get_run_detail(run_id, trade_limit=trade_limit))
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(error)})
    except Exception as error:  # noqa: BLE001
        logger.error("读取波段回测详情失败 %s: %s", run_id, error, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"error": "internal_error", "message": f"读取回测详情失败: {error}"},
        )


@router.get(
    "/runs/{run_id}/files/{filename}",
    summary="下载波段回测报告文件",
)
def download_swing_run_file(run_id: str, filename: str) -> FileResponse:
    try:
        path = get_report_file(run_id, filename)
    except RunNotFoundError as error:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": str(error)})
    return FileResponse(path, media_type=_MEDIA_TYPES.get(path.suffix.lower()), filename=path.name)

"""FastAPI 入口：工地作业车危险区穿越审计服务。"""
from __future__ import annotations

from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import Database
from .schemas import CoOccupancyQuery, CorrectionBatch, PermitCreate, PermitReviewQuery, PermitRevoke, PointBatch, ProximityQuery, RiskBudgetQuery, SpeedingReviewQuery, ZoneCreate
from .service import AuditService, ConflictError, NotFoundError, ValidationError

settings = get_settings()
db = Database(settings)
service = AuditService(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    db.close()


app = FastAPI(
    title="工地作业车危险区穿越审计服务",
    version="1.8.0",
    lifespan=lifespan,
    description=(
        "登记带洞米制多边形危险区及其 UTC 半开生效时段；每次登记原子产生全局递增修订号，"
        "各区保留不可变历史版本。批量提交车辆轨迹点，按最新配置或指定修订号查询"
        "各危险区的穿越区间与累计时长。支持现场采集点误录后的整组原子更正"
        "（撤销/改写、乐观版本、不可变前后值履历），车次轨迹按版本管理，"
        "每批真正录入新点或成功更正原子保留该版本全量有效点集，"
        "穿越审计可同时指定轨迹版本与区域修订号复现历史现场。"
        "多车同时占用审计按同一已提交读视图核算指定危险区在一段时间内"
        "被多辆作业车同时占用的时段，返回达到阈值的区间及区间内恒定的车次集合。"
        "近距离相遇复核按同一已提交读视图比较两车同时位于区内时的平面距离，"
        "按无序车次对返回距离严格小于阈值的正时长区间及区间最小距离。"
        "单车危险区风险预算复核按同一已提交读视图核算一次作业内各区占用，"
        "重叠区域同时占用只按最高权重计入，返回恒权重区间、各段与总计加权秒数"
        "及累计值首次达到预算的时刻。"
        "单车危险区超速复核按同一已提交读视图核算相邻点线性轨迹的恒定速度，"
        "点间隔超过上限的整段视为无覆盖，重叠区域按最低限速，"
        "返回按时序切分的超速正时长区间（每段速度、适用限速与并列限速区）、"
        "实际使用的版本号、违规总时长与超速距离总量。"
        "作业车进入危险区前须登记覆盖实际占用时段的通行许可：许可按许可 ID、车次、"
        "危险区及带时区 UTC 半开时段登记（登记时危险区须存在，车次可尚无轨迹），"
        "同 ID 同内容重传幂等、异内容冲突，撤销后 ID 不得重用；每次真实登记或撤销"
        "原子递增独立的全局许可修订号并保留不可改历史，撤销须提交所见修订号，"
        "过期或重复撤销拒绝。许可复核按同一已提交读视图（可选区域修订号、轨迹版本、"
        "许可修订号；无许可时许可修订号为 0）核对单车次在各区的实际占用，"
        "重叠许可时段取并集，按区返回无许可的正时长区间、累计秒数与实际使用的三个版本。"
    ),
)


@app.exception_handler(ValidationError)
async def _validation_handler(_: Request, exc: ValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": "validation_error", "detail": str(exc)})


@app.exception_handler(ConflictError)
async def _conflict_handler(_: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "conflict", "detail": str(exc)})


@app.exception_handler(NotFoundError)
async def _not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "service": "danger-zone-crossing-audit",
        "docs": "/docs",
        "health": "/health",
        "endpoints": [
            "POST /api/v1/zones",
            "GET  /api/v1/zones",
            "GET  /api/v1/zones/{zone_id}",
            "GET  /api/v1/revisions",
            "GET  /api/v1/zones/{zone_id}/revisions",
            "POST /api/v1/trips/{trip_id}/points",
            "POST /api/v1/trips/{trip_id}/corrections",
            "GET  /api/v1/trips/{trip_id}",
            "GET  /api/v1/trips/{trip_id}/corrections",
            "GET  /api/v1/trips/{trip_id}/versions",
            "GET  /api/v1/trips/{trip_id}/crossings",
            "POST /api/v1/zones/{zone_id}/co-occupancy",
            "POST /api/v1/zones/{zone_id}/proximity",
            "POST /api/v1/trips/{trip_id}/risk-budget",
            "POST /api/v1/trips/{trip_id}/speeding-review",
            "POST /api/v1/permits",
            "POST /api/v1/permits/{permit_id}/revoke",
            "GET  /api/v1/permits/{permit_id}",
            "GET  /api/v1/permit-revisions",
            "POST /api/v1/trips/{trip_id}/permit-review",
        ],
    }


@app.post("/api/v1/zones", status_code=201)
def create_zone(body: ZoneCreate) -> dict:
    # 响应含本次登记原子产生的全局递增修订号 revision
    return service.register_zone(body.model_dump())


@app.get("/api/v1/zones")
def list_zones(
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="可选修订号：返回各区截至该修订号的最后版本；省略则返回最新配置",
    ),
) -> dict:
    return service.list_zones(revision)


@app.get("/api/v1/zones/{zone_id}")
def get_zone(
    zone_id: str,
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description="可选修订号：返回该区截至该修订号的版本；省略则返回最新版本",
    ),
) -> JSONResponse:
    try:
        detail = service.get_zone(zone_id, revision)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": str(exc)}
        )
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"危险区不存在: {zone_id}"}
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/revisions")
def list_revisions(
    limit: int = Query(default=500, ge=1, le=10000, description="最多返回条数（按修订号倒序）"),
) -> dict:
    """全局修订流水：每次区域登记/覆盖更新产生一条。"""
    return service.list_revisions(zone_id=None, limit=limit)


@app.get("/api/v1/zones/{zone_id}/revisions")
def list_zone_revisions(zone_id: str) -> JSONResponse:
    """单个危险区的不可变修订历史（按修订号升序）。"""
    try:
        return JSONResponse(content=service.list_revisions(zone_id=zone_id))
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": str(exc)}
        )


@app.post("/api/v1/trips/{trip_id}/points")
def submit_points(trip_id: str, body: PointBatch) -> dict:
    result = service.submit_points(trip_id, body.model_dump())
    return result


@app.post("/api/v1/trips/{trip_id}/corrections")
def submit_corrections(trip_id: str, body: CorrectionBatch) -> dict:
    """现场采集点更正：整组（撤销/改写）在单事务内核对版本、存在性与最终点集同刻唯一。"""
    return service.correct_points(trip_id, body.model_dump())


@app.get("/api/v1/trips/{trip_id}/corrections")
def get_corrections(trip_id: str) -> JSONResponse:
    """按车次排列的点更正履历（按版本升序，含不可改前后值）。"""
    detail = service.list_corrections(trip_id)
    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "detail": f"车次不存在（无轨迹数据）: {trip_id}"},
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/trips/{trip_id}/versions")
def get_trip_versions(trip_id: str) -> JSONResponse:
    """车次已保留的轨迹版本流水（按版本升序，含基线版本与各版本有效点数）。"""
    detail = service.list_trip_versions(trip_id)
    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "detail": f"车次不存在（无轨迹数据）: {trip_id}"},
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/trips/{trip_id}")
def get_trip(trip_id: str) -> JSONResponse:
    detail = service.get_trip(trip_id)
    if detail is None:
        return JSONResponse(
            status_code=404, content={"error": "not_found", "detail": f"车次无轨迹数据: {trip_id}"}
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/trips/{trip_id}/crossings")
def get_crossings(
    trip_id: str,
    zone_id: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（含）之后"),
    until: Optional[str] = Query(default=None, description="只统计该 UTC 时刻（不含）之前"),
    revision: Optional[int] = Query(
        default=None,
        ge=1,
        description=(
            "可选区域配置修订号：按各区截至该修订号的最后版本复现核算，"
            "响应回填该 revision；省略则按最新配置，响应语义不变"
        ),
    ),
    track_version: Optional[int] = Query(
        default=None,
        ge=0,
        alias="track_version",
        description=(
            "可选轨迹版本号：按该版本保留的有效点集复现穿越，响应回填实际使用的 "
            "track_version；省略则按当前有效点集，响应语义不变。可与 revision 同时指定"
        ),
    ),
) -> dict:
    return service.query_crossings(
        trip_id,
        zone_id=zone_id,
        since=since,
        until=until,
        revision_raw=revision,
        track_version_raw=track_version,
    )


@app.post("/api/v1/zones/{zone_id}/co-occupancy")
def query_co_occupancy(zone_id: str, body: CoOccupancyQuery) -> dict:
    """多车同时占用审计：区域与全部车次点集从同一已提交读视图读取，
    返回 [since, until) 内达到占用阈值的半开区间及区间内恒定的车次集合。"""
    return service.query_co_occupancy(zone_id, body.model_dump())


@app.post("/api/v1/zones/{zone_id}/proximity")
def query_proximity(zone_id: str, body: ProximityQuery) -> dict:
    """近距离相遇复核：区域与全部车次点集从同一已提交读视图读取，
    只在两车均有轨迹覆盖且同时位于区内时比较同一时刻平面距离，
    按无序车次对返回距离严格小于阈值的正时长区间及区间最小距离。"""
    return service.query_proximity(zone_id, body.model_dump())


@app.post("/api/v1/trips/{trip_id}/risk-budget")
def query_risk_budget(trip_id: str, body: RiskBudgetQuery) -> dict:
    """单车一次作业危险区风险预算复核：区域配置与车次点集从同一已提交读视图读取，
    重叠区域同时占用只按最高权重计入，返回恒权重正时长区间、各段与总计加权秒数，
    以及累计加权秒数首次达到预算的时刻。"""
    return service.query_risk_budget(trip_id, body.model_dump())


@app.post("/api/v1/trips/{trip_id}/speeding-review")
def query_speeding_review(trip_id: str, body: SpeedingReviewQuery) -> dict:
    """单车危险区内超速时段复核：区域配置与车次有效点集从同一已提交读视图读取，
    相邻点按线性轨迹计算恒定速度，点间隔超过上限的整段视为无覆盖，重叠区域按最低限速，
    速度严格大于限速才违规；返回按时序切分的违规正时长区间（每段速度、适用限速与
    并列限速区 ID）、实际使用的区域修订号与轨迹版本，以及违规总时长与超速距离总量。"""
    return service.query_speeding_review(trip_id, body.model_dump())


@app.post("/api/v1/permits", status_code=201)
def register_permit(body: PermitCreate) -> JSONResponse:
    """通行许可登记：许可 ID + 车次 + 危险区 + UTC 半开覆盖时段。

    登记时危险区须存在（车次可尚无轨迹）；同 ID 同内容重传幂等（200，不递增），
    同 ID 异内容或撤销后 ID 重用冲突（409）；真实登记原子分配全局许可修订号（201）。
    """
    status_code, result = service.register_permit(body.model_dump())
    return JSONResponse(status_code=status_code, content=result)


@app.post("/api/v1/permits/{permit_id}/revoke")
def revoke_permit(permit_id: str, body: PermitRevoke) -> dict:
    """撤销通行许可：须提交所见全局许可修订号；过期或重复撤销拒绝（409），
    ID 未登记 404；真实撤销原子递增全局许可修订号，撤销后 ID 不得重用。"""
    return service.revoke_permit(permit_id, body.model_dump())


@app.get("/api/v1/permits/{permit_id}")
def get_permit(permit_id: str) -> JSONResponse:
    """查询单个许可当前状态（含已撤销）及其所在全局许可修订号。"""
    detail = service.get_permit(permit_id)
    if detail is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not_found", "detail": f"通行许可不存在（未登记）: {permit_id}"},
        )
    return JSONResponse(content=detail)


@app.get("/api/v1/permit-revisions")
def list_permit_revisions(
    limit: int = Query(default=500, ge=1, le=10000, description="最多返回条数（按修订号倒序）"),
) -> dict:
    """全局通行许可修订流水：每次真实登记/撤销产生一条，含当前全局许可修订号。"""
    return service.list_permit_revisions(limit=limit)


@app.post("/api/v1/trips/{trip_id}/permit-review")
def query_permit_review(trip_id: str, body: PermitReviewQuery) -> dict:
    """单车次多危险区通行许可复核：区域配置、车次点集与许可状态从同一已提交读视图读取；
    沿用既有轨迹插值、带洞边界与区域生效规则核算实际占用，仅核对该车次该区在指定
    许可修订视图下仍有效的许可（重叠时段取并集）；按区返回无许可正时长区间、累计秒数
    与实际使用的区域修订号、轨迹版本、许可修订号（无许可时为 0）。"""
    return service.query_permit_review(trip_id, body.model_dump())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)

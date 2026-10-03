"""HTTP 请求/响应模型（Pydantic；坐标数值校验在服务层做有限性检查）。"""
from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class CoordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 允许任意数值（含 inf/nan 透传），有限性在服务层显式拒绝并给出明确报错
    x: float
    y: float


class WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(..., description="生效开始 UTC ISO 8601（含）")
    end: str = Field(..., description="生效结束 UTC ISO 8601（不含）")


class ZoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(..., min_length=1)
    # 顶点为 [x, y]，首尾点无需重复
    exterior: List[List[float]] = Field(..., min_length=3)
    holes: List[List[List[float]]] = Field(default_factory=list)
    windows: List[WindowModel] = Field(..., min_length=1)


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="UTC ISO 8601，必须带时区")
    x: float
    y: float


class PointBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: List[TrajectoryPoint] = Field(..., min_length=1)


class RevokeItem(BaseModel):
    """撤销项：该 point_id 从车次有效点集中永久移除。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["revoke"]
    point_id: str = Field(..., min_length=1)


class UpdateItem(BaseModel):
    """更正项：将该 point_id 改写为新的 UTC 时刻与有限平面坐标。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["update"]
    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="更正后的 UTC ISO 8601 时刻，必须带时区")
    x: float
    y: float


CorrectionItem = Annotated[Union[RevokeItem, UpdateItem], Field(discriminator="action")]


class CorrectionBatch(BaseModel):
    """现场采集点更正批：整组在同一事务内核对并生效。"""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(
        ..., ge=0, description="作业人员所见的当前轨迹版本；须与库中当前版本一致"
    )
    reason: str = Field(..., min_length=1, description="非空更正原因")
    items: List[CorrectionItem] = Field(..., min_length=1)


class CoOccupancyQuery(BaseModel):
    """多车同时占用审计请求：指定危险区在 [since, until) 内被多车同时占用的时段。"""

    model_config = ConfigDict(extra="forbid")

    trip_ids: List[str] = Field(
        ..., min_length=2, description="至少两辆互异车次（重复车次在服务层拒绝）"
    )
    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    threshold: int = Field(
        ..., ge=2, description="同时占用阈值：2..车次数（超出车次数在服务层拒绝）"
    )
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算"
    )
    track_versions: Dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict,
        description="可选逐车轨迹版本 {trip_id: 版本号}；未列出的车次取查询时最新已提交状态",
    )


class RiskBudgetQuery(BaseModel):
    """单车一次作业危险区风险预算复核请求。"""

    model_config = ConfigDict(extra="forbid")

    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    zone_weights: Dict[
        Annotated[str, Field(min_length=1)],
        Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)],
    ] = Field(
        ...,
        min_length=1,
        description="非空危险区 ID -> 正且有限风险权重映射；仅这些区参与核算",
    )
    budget_weighted_sec: Annotated[
        float, Field(gt=0, strict=True, allow_inf_nan=False)
    ] = Field(
        ..., description="正且有限的加权秒数预算"
    )
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算"
    )
    track_version: Optional[int] = Field(
        default=None,
        ge=0,
        description="可选轨迹版本号：按该版本保留的有效点集核算，响应回填实际使用版本",
    )


class SpeedingReviewQuery(BaseModel):
    """单车危险区内超速时段复核请求。"""

    model_config = ConfigDict(extra="forbid")

    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    zone_speed_limits: Dict[
        Annotated[str, Field(min_length=1)],
        Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)],
    ] = Field(
        ...,
        min_length=1,
        description="非空危险区 ID -> 正且有限限速（米/秒）映射；仅这些区参与核算",
    )
    max_sample_interval_sec: Annotated[
        float, Field(gt=0, strict=True, allow_inf_nan=False)
    ] = Field(
        ...,
        description="正且有限的最大采样间隔（秒）：相邻点间隔超过该值的整段视为无覆盖",
    )
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算"
    )
    track_version: Optional[int] = Field(
        default=None,
        ge=0,
        description="可选轨迹版本号：按该版本保留的有效点集核算，响应回填实际使用版本",
    )


class ProximityQuery(BaseModel):
    """危险区内近距离相遇复核请求：两车同时位于区内且平面距离严格小于阈值的时段。"""

    model_config = ConfigDict(extra="forbid")

    trip_ids: List[str] = Field(
        ..., min_length=2, description="至少两辆互异车次（重复车次在服务层拒绝）"
    )
    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    max_distance: float = Field(
        ...,
        gt=0,
        allow_inf_nan=False,
        description="米制距离阈值：正且有限，仅严格小于才计入相遇",
    )
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算"
    )
    track_versions: Dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict,
        description="可选逐车轨迹版本 {trip_id: 版本号}；未列出的车次取查询时最新已提交状态",
    )


class PermitCreate(BaseModel):
    """作业车进入危险区通行许可登记请求：许可 ID + 车次 + 危险区 + UTC 半开覆盖时段。"""

    model_config = ConfigDict(extra="forbid")

    permit_id: str = Field(..., min_length=1, description="许可 ID；撤销后永久占用、不得重用")
    trip_id: str = Field(..., min_length=1, description="车次；登记时可尚无轨迹")
    zone_id: str = Field(..., min_length=1, description="危险区 ID；登记时该区须已存在")
    start: str = Field(..., description="许可覆盖起始 UTC ISO 8601（含），必须带时区")
    end: str = Field(..., description="许可覆盖结束 UTC ISO 8601（不含），必须带时区")


class PermitRevoke(BaseModel):
    """通行许可撤销请求：须提交所见全局许可修订号（乐观并发）。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(
        ...,
        ge=0,
        description="提交方所见的当前全局许可修订号；过期（已前进）或重复撤销拒绝",
    )
    reason: Optional[str] = Field(
        default=None, description="可选撤销原因（仅记录用途，不影响修订语义）"
    )


class PermitReviewQuery(BaseModel):
    """单车次多危险区通行许可复核请求：核对实际占用是否被许可覆盖。"""

    model_config = ConfigDict(extra="forbid")

    zone_ids: List[Annotated[str, Field(min_length=1)]] = Field(
        ..., min_length=1, description="非空且互异的危险区 ID 集合（重复在服务层拒绝）"
    )
    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算实际占用"
    )
    track_version: Optional[int] = Field(
        default=None,
        ge=0,
        description="可选轨迹版本号：按该版本保留的有效点集核算，响应回填实际使用版本",
    )
    permit_revision: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "可选全局许可修订号（含 0）：按截至该号的许可状态复现；"
            "省略取同一读视图最新号，无任何许可时为 0"
        ),
    )

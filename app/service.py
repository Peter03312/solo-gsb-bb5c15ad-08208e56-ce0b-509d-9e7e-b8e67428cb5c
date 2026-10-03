"""业务服务：危险区登记（全局修订号）、轨迹点批量提交、穿越审计计算。"""
from __future__ import annotations

import bisect
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Polygon

from .db import Database
from .geometry import GeometryError, build_polygon, merge_intervals, segment_inside_params
from .timeutils import TimeError, clip_intervals_halfopen, merge_windows, parse_utc, subtract_intervals_halfopen, validate_window


class ConflictError(Exception):
    """内容冲突（HTTP 409）。"""


class ValidationError(Exception):
    """语义校验失败（HTTP 400）。"""


class NotFoundError(Exception):
    """资源不存在或修订号超出范围（HTTP 404）。"""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_revision_param(raw: Any) -> int:
    """校验查询参数 revision：必须是 >=1 的整数。"""
    if isinstance(raw, bool):
        raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw).strip()
        try:
            value = int(text)
        except (TypeError, ValueError):
            raise ValidationError(f"revision 必须是正整数，收到: {raw!r}")
    if value < 1:
        raise ValidationError(f"revision 必须是正整数（>=1），收到: {value}")
    return value


def _row_to_zone(row: Any, *, include_revision: bool = True) -> Dict[str, Any]:
    windows_raw = json.loads(row["windows"])
    body: Dict[str, Any] = {
        "zone_id": row["zone_id"],
        "exterior": json.loads(row["exterior"]),
        "holes": json.loads(row["holes"]),
        "windows": [
            {"start": w["start_iso"], "end": w["end_iso"]} for w in windows_raw
        ],
        "created_at": row["created_at"],
        "registered_at": row["registered_at"],
    }
    if include_revision:
        body["revision"] = int(row["revision"])
    return body


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.min_duration = db.settings.min_duration

    # ------------------------------------------------------------------
    # 修订号
    # ------------------------------------------------------------------

    def current_revision(self) -> int:
        return self.db.current_revision()

    def current_permit_revision(self) -> int:
        return self.db.current_permit_revision()

    def _resolve_revision(self, raw: Any) -> int:
        """校验并解析调用方指定的修订号；超出当前范围明确报错 404。"""
        revision = _validate_revision_param(raw)
        head = self.db.current_revision()
        if revision > head:
            raise NotFoundError(
                f"修订号 {revision} 超出当前范围：当前最新修订号为 {head}"
                + ("（尚无任何区域登记）" if head == 0 else "")
            )
        return revision

    # ------------------------------------------------------------------
    # 危险区登记
    # ------------------------------------------------------------------

    def register_zone(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        exterior = payload.get("exterior")
        holes = payload.get("holes") or []
        if not isinstance(holes, list):
            raise ValidationError("holes 必须是环列表")
        try:
            polygon = build_polygon(exterior, holes)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc

        raw_windows = payload.get("windows")
        if not isinstance(raw_windows, list) or not raw_windows:
            raise ValidationError("windows 必须是非空左闭右开时段列表 [{start, end}]")
        windows: List[Dict[str, Any]] = []
        for idx, w in enumerate(raw_windows):
            if not isinstance(w, dict):
                raise ValidationError(f"windows[{idx}] 必须是对象")
            try:
                start = parse_utc(w.get("start"), field=f"windows[{idx}].start")
                end = parse_utc(w.get("end"), field=f"windows[{idx}].end")
                validate_window(start, end)
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc
            windows.append(
                {
                    "start_epoch": start.timestamp(),
                    "end_epoch": end.timestamp(),
                    "start_iso": _iso_z(start),
                    "end_iso": _iso_z(end),
                }
            )

        now_iso = _iso_z(datetime.now(timezone.utc))
        # 单事务：读取既有首版 created_at + 追加不可变版本 + 分配全局修订号，原子提交
        with self.db.transaction() as conn:
            first = self.db.first_zone_revision(conn, zone_id)
            created = first is None
            created_iso = now_iso if created else first["created_at"]
            revision = self.db.insert_zone_revision(
                conn,
                zone_id,
                [list(c) for c in polygon.exterior.coords],
                [[list(c) for c in ring.coords] for ring in polygon.interiors],
                windows,
                created_iso,
                now_iso,
            )

        return {
            "zone_id": zone_id,
            "revision": revision,
            "status": "created" if created else "updated",
            "num_holes": len(polygon.interiors),
            "num_windows": len(windows),
        }

    def get_zone(self, zone_id: str, revision_raw: Any = None) -> Optional[Dict[str, Any]]:
        if revision_raw is None:
            row = self.db.latest_zone_row(zone_id)
            if row is None:
                return None
            return _row_to_zone(row)

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            row = self.db._zone_row_at(conn, zone_id, revision)
        if row is None:
            # 指定版本中该区尚未登记
            raise NotFoundError(
                f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
            )
        return _row_to_zone(row)

    def list_zones(self, revision_raw: Any = None) -> Dict[str, Any]:
        if revision_raw is None:
            rows = self.db.list_latest_zone_rows()
            return {"zones": [_row_to_zone(r) for r in rows]}

        revision = self._resolve_revision(revision_raw)
        with self.db.read_view() as conn:
            rows = self.db._list_zone_rows_at(conn, revision)
        return {
            "revision": revision,
            "zones": [_row_to_zone(r) for r in rows],
        }

    def list_revisions(self, zone_id: Optional[str] = None, limit: int = 500) -> Dict[str, Any]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10000:
            raise ValidationError("limit 必须是 1..10000 的整数")
        if zone_id is None:
            rows = self.db.list_revision_rows(limit=limit)
            payload = {
                "current_revision": self.db.current_revision(),
                "revisions": [self._revision_brief(r) for r in rows],
            }
            return payload
        # 按区查看：区当前必须存在（历史曾登记即视为存在，版本不可删除）
        existing = self.db.latest_zone_row(zone_id)
        if existing is None:
            raise NotFoundError(f"危险区不存在: {zone_id}")
        rows = self.db.list_zone_revision_rows(zone_id)
        return {
            "zone_id": zone_id,
            "revisions": [self._revision_brief(r) for r in rows],
        }

    @staticmethod
    def _revision_brief(row: Any) -> Dict[str, Any]:
        return {
            "revision": int(row["revision"]),
            "zone_id": row["zone_id"],
            "created_at": row["created_at"],
            "registered_at": row["registered_at"],
        }

    # ------------------------------------------------------------------
    # 轨迹点批量提交（幂等 / 冲突）
    # ------------------------------------------------------------------

    def submit_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        points = payload.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationError("points 必须是非空轨迹点列表")

        # ---- 批内解析与校验 ----
        parsed: List[Tuple[str, float, float, float, str]] = []
        # point_id -> (ts,x,y,ts_iso)；ts -> point_id，用于批内一致性检查
        seen_ids: Dict[str, Tuple[float, float, float, str]] = {}
        seen_ts: Dict[float, str] = {}

        for idx, item in enumerate(points):
            if not isinstance(item, dict):
                raise ValidationError(f"points[{idx}] 必须是对象")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"points[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()

            try:
                dt = parse_utc(item.get("ts"), field=f"points[{idx}].ts")
            except TimeError as exc:
                raise ValidationError(str(exc)) from exc

            x_raw, y_raw = item.get("x"), item.get("y")
            if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                raise ValidationError(f"points[{idx}] 坐标必须是数值")
            try:
                x, y = float(x_raw), float(y_raw)
            except (TypeError, ValueError):
                raise ValidationError(
                    f"points[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                )

            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValidationError(
                    f"points[{idx}] 坐标必须有限，禁止 NaN/Infinity: x={x_raw!r}, y={y_raw!r}"
                )

            ts_epoch = dt.timestamp()
            sig = (ts_epoch, x, y)

            # 同 point_id 批内重复：内容一致幂等，不一致冲突
            if point_id in seen_ids:
                if seen_ids[point_id][:3] != sig:
                    raise ConflictError(
                        f"批内冲突: point_id={point_id!r} 出现了不同内容（时刻或坐标不一致）"
                    )
                continue

            # 同车次同刻不同点
            if ts_epoch in seen_ts:
                raise ConflictError(
                    f"批内冲突: 时刻 {_iso_z(dt)} 同时关联了 point_id="
                    f"{seen_ts[ts_epoch]!r} 与 {point_id!r}"
                )

            seen_ids[point_id] = (ts_epoch, x, y, _iso_z(dt))
            seen_ts[ts_epoch] = point_id
            parsed.append((point_id, ts_epoch, x, y, _iso_z(dt)))

        # ---- 单事务：与已存数据核对 + 写入 + 版本推进 + 版本点集快照 ----
        # 全部读取与比对都在 BEGIN IMMEDIATE 后的同一快照/写事务内，杜绝并发下
        # “先读判断后写入”竞态；失败一律回滚，不留任何数据。
        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            existing_rows = self.db._list_trip_points(conn, trip_id)
            # 旧库直接落库的车次（无 trip_versions 行但确有数据）：幂等补齐版本 0 基线快照。
            # 全新车次（无任何点）不登记版本 0，首批新点即版本 1；
            # 升级车次（有 trip_versions 行、基线在当时当前版本）不走此路径。
            legacy_vrow = self.db._trip_version_row(conn, trip_id)
            if legacy_vrow is None and existing_rows:
                self.db.ensure_legacy_baseline(conn, trip_id, now_iso)
            stored_map: Dict[str, Tuple[float, float, float]] = {
                r["point_id"]: (r["ts_epoch"], r["x"], r["y"]) for r in existing_rows
            }
            ts_owner: Dict[float, str] = {r["ts_epoch"]: r["point_id"] for r in existing_rows}

            new_rows: List[Tuple[str, str, float, float, float]] = []
            for point_id, ts_epoch, x, y, _ in parsed:
                # 已撤销 ID 不得经上传重用（撤销不可经上传恢复）
                if self.db._revoked_row(conn, trip_id, point_id) is not None:
                    raise ConflictError(
                        f"point_id={point_id!r} 已被更正撤销，不得经批量上传重用；"
                        "如确需重新采集请使用新的 point_id"
                    )
                stored = stored_map.get(point_id)
                if stored is not None:
                    if stored != (ts_epoch, x, y):
                        raise ConflictError(
                            f"point_id={point_id!r} 已存在且内容不同："
                            f"已存 (ts_epoch={stored[0]}, x={stored[1]}, y={stored[2]})，"
                            f"新传 (ts_epoch={ts_epoch}, x={x}, y={y})"
                        )
                    continue  # 同 ID 同内容：幂等重传
                # 新点：同刻必须仍空闲
                owner = ts_owner.get(ts_epoch)
                if owner is not None:
                    raise ConflictError(
                        f"时刻冲突: point_id={point_id!r} 的时刻与已存 point_id="
                        f"{owner!r} 相同（同车次同刻只能有一个点）"
                    )
                new_rows.append((trip_id, point_id, ts_epoch, x, y))
                # 本批后续点视作已占用
                ts_owner[ts_epoch] = point_id

            if new_rows:
                self.db.insert_points(conn, new_rows)
                # 真正录入新点：版本恰好推进一次（纯幂等重传不推进），
                # 并在同一事务内原子保留足以复原新版本有效点集的全量快照
                vrow = self.db._trip_version_row(conn, trip_id)
                next_version = 1 if vrow is None else int(vrow["version"]) + 1
                self.db.upsert_trip_version(conn, trip_id, next_version, now_iso)
                snapshot_rows = self.db._list_trip_points(conn, trip_id)
                self.db._insert_version_snapshot(
                    conn, trip_id, next_version, "points", now_iso, snapshot_rows
                )

        return {
            "trip_id": trip_id,
            "received": len(points),
            "accepted_new": len(new_rows),
            "idempotent_retransmit": len(points) - len(new_rows),
        }

    # ------------------------------------------------------------------
    # 采集点更正（撤销 / 改写，乐观版本，整组原子 + 不可变履历）
    # ------------------------------------------------------------------

    def correct_points(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        base_version = payload.get("base_version")
        if isinstance(base_version, bool) or not isinstance(base_version, int) or base_version < 0:
            raise ValidationError(f"base_version 必须是 >=0 的整数，收到: {base_version!r}")

        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("reason 必须是非空字符串")
        reason = reason.strip()

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError("items 必须是非空更正项列表")

        # ---- 解析整组：一组互异 point_id，每项撤销或带时区时刻+有限坐标 ----
        touched: List[str] = []
        updates: Dict[str, Tuple[float, float, float, str]] = {}
        revokes: List[str] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"items[{idx}] 必须是对象")
            action = item.get("action")
            point_id = item.get("point_id")
            if not isinstance(point_id, str) or not point_id.strip():
                raise ValidationError(f"items[{idx}].point_id 必须是非空字符串")
            point_id = point_id.strip()
            if point_id in updates or point_id in revokes:
                raise ValidationError(f"items[{idx}].point_id={point_id!r} 在本批更正中重复")
            touched.append(point_id)

            if action == "revoke":
                revokes.append(point_id)
            elif action == "update":
                try:
                    dt = parse_utc(item.get("ts"), field=f"items[{idx}].ts")
                except TimeError as exc:
                    raise ValidationError(str(exc)) from exc
                x_raw, y_raw = item.get("x"), item.get("y")
                if isinstance(x_raw, bool) or isinstance(y_raw, bool):
                    raise ValidationError(f"items[{idx}] 坐标必须是数值")
                try:
                    x, y = float(x_raw), float(y_raw)
                except (TypeError, ValueError):
                    raise ValidationError(
                        f"items[{idx}] 坐标必须是数值: x={x_raw!r}, y={y_raw!r}"
                    )
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValidationError(
                        f"items[{idx}] 坐标必须有限，禁止 NaN/Infinity: "
                        f"x={x_raw!r}, y={y_raw!r}"
                    )
                updates[point_id] = (dt.timestamp(), x, y, _iso_z(dt))
            else:
                raise ValidationError(
                    f"items[{idx}].action 必须是 revoke 或 update，收到: {action!r}"
                )

        # ---- 单事务核对整组并写入 ----
        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            # 既有车次以当前点集为版本 0 基线（兼容直接通过 SQL 落库的旧数据）
            if vrow is None:
                existing = self.db._list_trip_points(conn, trip_id)
                if not existing and not self.db.list_revoked_ids(conn, trip_id):
                    raise NotFoundError(f"车次不存在（无轨迹点，无法更正）: {trip_id}")
                # 幂等补齐版本 0 基线全量点集（可能在先前上传路径已补齐），再推进版本 1
                self.db.ensure_legacy_baseline(conn, trip_id, now_iso)
                current_version = 0
            else:
                current_version = int(vrow["version"])

            # 过期版本：整批拒绝且不留履历
            if base_version != current_version:
                raise ConflictError(
                    f"轨迹版本过期: 提交基于版本 {base_version}，当前版本为 "
                    f"{current_version}；请读取当前点集与版本后重新提交更正"
                )

            current_rows = self.db._list_trip_points(conn, trip_id)
            current_map: Dict[str, Any] = {
                r["point_id"]: r for r in current_rows
            }

            # 点存在性：缺失点或已撤销点一律整批拒绝
            missing = [pid for pid in touched if pid not in current_map]
            if missing:
                revoked_hint = [
                    pid
                    for pid in missing
                    if self.db._revoked_row(conn, trip_id, pid) is not None
                ]
                if revoked_hint:
                    raise ConflictError(
                        "更正点已撤销，不能再次更正: "
                        + ", ".join(repr(p) for p in revoked_hint)
                    )
                raise ConflictError(
                    "更正点在当前有效点集中不存在: "
                    + ", ".join(repr(p) for p in missing)
                )

            # ---- 以整组最终有效点集核对同车次同刻唯一 ----
            # 未触及点保持原值；update 项取新值；revoke 项移除
            final_ts: Dict[float, str] = {}
            for r in current_rows:
                pid = r["point_id"]
                if pid in revokes:
                    continue
                if pid in updates:
                    continue
                final_ts[r["ts_epoch"]] = pid
            for pid, (ts_epoch, _x, _y, _iso) in updates.items():
                owner = final_ts.get(ts_epoch)
                if owner is not None and owner != pid:
                    raise ConflictError(
                        f"更正后同车次同刻冲突: point_id={pid!r} 的新时刻 "
                        f"{updates[pid][3]} 与 point_id={owner!r} 相同"
                    )
                final_ts[ts_epoch] = pid

            # ---- 写入新有效状态：先删除全部受影响点，再插入更新项 ----
            # 先删后插使同刻互换/移位不触发 (trip, ts) 唯一约束；校验已保证最终集合法
            self.db.delete_points(conn, trip_id, touched)
            self.db.insert_points(
                conn,
                [
                    (trip_id, pid, ts_epoch, x, y)
                    for pid, (ts_epoch, x, y, _iso) in updates.items()
                ],
            )
            new_version = current_version + 1
            self.db.upsert_trip_version(conn, trip_id, new_version, now_iso)
            # 同一事务内原子保留新版本全量有效点集快照（撤销点不再包含于其中）
            snapshot_rows = self.db._list_trip_points(conn, trip_id)
            self.db._insert_version_snapshot(
                conn, trip_id, new_version, "correction", now_iso, snapshot_rows
            )
            if revokes:
                self.db.insert_revoked_points(
                    conn, trip_id, revokes, new_version, reason, now_iso
                )

            # ---- 不可改前后值履历（仅本批成功后写入，顺序随请求 items）----
            history_rows: List[Tuple[Any, ...]] = []
            for pid in touched:
                r = current_map[pid]
                if pid in updates:
                    ts_epoch, x, y, _iso = updates[pid]
                    history_rows.append(
                        (trip_id, new_version, reason, pid, "update",
                         r["ts_epoch"], r["x"], r["y"], ts_epoch, x, y, now_iso)
                    )
                else:
                    history_rows.append(
                        (trip_id, new_version, reason, pid, "revoke",
                         r["ts_epoch"], r["x"], r["y"], None, None, None, now_iso)
                    )
            self.db.insert_point_corrections(conn, history_rows)

        return {
            "trip_id": trip_id,
            "base_version": base_version,
            "version": new_version,
            "reason": reason,
            "affected": [self._affected_point(pid, current_map, updates) for pid in touched],
        }

    @staticmethod
    def _point_state(ts_epoch: float, x: float, y: float) -> Dict[str, Any]:
        return {
            "ts": _iso_z(datetime.fromtimestamp(ts_epoch, timezone.utc)),
            "x": x,
            "y": y,
        }

    def _affected_point(
        self,
        point_id: str,
        current_map: Dict[str, Any],
        updates: Dict[str, Tuple[float, float, float, str]],
    ) -> Dict[str, Any]:
        r = current_map[point_id]
        before = self._point_state(r["ts_epoch"], r["x"], r["y"])
        if point_id in updates:
            ts_epoch, x, y, _iso = updates[point_id]
            return {
                "point_id": point_id,
                "action": "update",
                "before": before,
                "after": self._point_state(ts_epoch, x, y),
            }
        return {"point_id": point_id, "action": "revoke", "before": before, "after": None}

    def get_trip(self, trip_id: str) -> Optional[Dict[str, Any]]:
        """车次当前轨迹（更正后有效点集）+ 当前版本；车次从未存在返回 None。"""
        with self.db.read_view() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            rows = self.db._list_trip_points(conn, trip_id)
            if vrow is None and not rows:
                return None
            baseline, _head = self.db._trip_version_info(conn, trip_id)
            version = 0 if vrow is None else int(vrow["version"])
            return {
                "trip_id": trip_id,
                "version": version,
                "baseline_version": version if baseline is None else baseline,
                "point_count": len(rows),
                "points": [
                    {
                        "point_id": r["point_id"],
                        **self._point_state(r["ts_epoch"], r["x"], r["y"]),
                    }
                    for r in rows
                ],
            }

    def list_trip_versions(self, trip_id: str) -> Optional[Dict[str, Any]]:
        """车次已保留的轨迹版本流水（升序），标明基线/新点/更正来源；车次从未存在返回 None。"""
        with self.db.read_view() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            rows = self.db._list_trip_points(conn, trip_id)
            if vrow is None and not rows:
                return None
            log_rows = self.db.list_trip_version_log_rows(conn, trip_id)
            baseline, head = self.db._trip_version_info(conn, trip_id)
            versions: List[Dict[str, Any]] = []
            for lr in log_rows:
                point_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM trip_point_versions "
                        "WHERE trip_id = ? AND version = ?",
                        (trip_id, int(lr["version"])),
                    ).fetchone()["c"]
                )
                versions.append(
                    {
                        "version": int(lr["version"]),
                        "cause": lr["cause"],
                        "point_count": point_count,
                        "created_at": lr["created_at"],
                    }
                )
        return {
            "trip_id": trip_id,
            "version": 0 if vrow is None else int(vrow["version"]),
            "baseline_version": head if baseline is None else baseline,
            "versions": versions,
        }

    def list_corrections(self, trip_id: str) -> Dict[str, Any]:
        """按车次排列的更正履历（按版本升序）；车次从未存在返回 None。"""
        with self.db.read_view() as conn:
            vrow = self.db._trip_version_row(conn, trip_id)
            rows = self.db._list_trip_points(conn, trip_id)
            if vrow is None and not rows:
                return None
            current_version = 0 if vrow is None else int(vrow["version"])
            correction_rows = self.db.list_point_correction_rows(conn, trip_id)

        corrections: List[Dict[str, Any]] = []
        for r in correction_rows:
            after = None
            if r["action"] == "update":
                after = self._point_state(
                    r["after_ts_epoch"], r["after_x"], r["after_y"]
                )
            corrections.append(
                {
                    "version": int(r["version"]),
                    "reason": r["reason"],
                    "point_id": r["point_id"],
                    "action": r["action"],
                    "before": self._point_state(
                        r["before_ts_epoch"], r["before_x"], r["before_y"]
                    ),
                    "after": after,
                    "corrected_at": r["corrected_at"],
                }
            )
        return {
            "trip_id": trip_id,
            "version": current_version,
            "corrections": corrections,
        }

    # ------------------------------------------------------------------
    # 穿越审计
    # ------------------------------------------------------------------

    def query_crossings(
        self,
        trip_id: str,
        zone_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        revision_raw: Any = None,
        track_version_raw: Any = None,
    ) -> Dict[str, Any]:
        try:
            lo = parse_utc(since, field="since").timestamp() if since else None
            hi = parse_utc(until, field="until").timestamp() if until else None
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo is not None and hi is not None and lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        pinned_rev = revision_raw is not None
        revision = self._resolve_revision(revision_raw) if pinned_rev else None

        pinned_track = track_version_raw is not None
        track_version = (
            self._resolve_track_version(trip_id, track_version_raw) if pinned_track else None
        )

        with self.db.read_view() as conn:
            if pinned_track:
                points = self.db._list_trip_point_version_rows(
                    conn, trip_id, track_version
                )
            else:
                points = self.db._list_trip_points(conn, trip_id)
            if len(points) < 2:
                result: Dict[str, Any] = {
                    "trip_id": trip_id,
                    "point_count": len(points),
                    "zones": [],
                }
                if pinned_track:
                    result["track_version"] = track_version
                if pinned_rev:
                    result["revision"] = revision
                return result

            if pinned_rev:
                zone_rows = self.db._list_zone_rows_at(conn, revision)
            else:
                zone_rows = self.db._list_latest_zone_rows(conn)
            if zone_id is not None:
                zone_rows = [r for r in zone_rows if r["zone_id"] == zone_id]
                if not zone_rows:
                    if pinned_rev:
                        # 指定版本中尚未登记的区域不参与核算
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
                        )
                    raise ValidationError(f"危险区不存在: {zone_id}")

            zone_results: List[Dict[str, Any]] = []
            for zrow in zone_rows:
                zone_result = self._crossing_for_zone(zrow, points, lo, hi)
                zone_results.append(zone_result)

        result = {
            "trip_id": trip_id,
            "point_count": len(points),
            "zones": zone_results,
        }
        if pinned_track:
            # 标明本次复现实际使用的轨迹版本
            result["track_version"] = track_version
        if pinned_rev:
            result["revision"] = revision
        return result

    @staticmethod
    def _validate_track_version_value(raw: Any) -> int:
        """校验轨迹版本号格式：必须是 >=0 的整数（非法值 400）。"""
        if isinstance(raw, bool):
            raise ValidationError(f"轨迹版本必须是 >=0 的整数，收到: {raw!r}")
        if isinstance(raw, int):
            version = raw
        else:
            text = str(raw).strip()
            try:
                version = int(text)
            except (TypeError, ValueError):
                raise ValidationError(f"轨迹版本必须是 >=0 的整数，收到: {raw!r}")
        if version < 0:
            raise ValidationError(f"轨迹版本必须是 >=0 的整数，收到: {version}")
        return version

    def _resolve_track_version(self, trip_id: str, raw: Any) -> int:
        """校验穿越查询指定的轨迹版本。

        - 必须是 >=0 的整数；
        - 车次不存在（无任何已保留版本）：404；
        - 早于该车次最早可追溯基线版本或晚于当前版本：404 明确报错。
        """
        version = self._validate_track_version_value(raw)
        with self.db.read_view() as conn:
            baseline, head = self.db._trip_version_info(conn, trip_id)
        return self._check_track_version_range(trip_id, version, baseline, head)

    @staticmethod
    def _check_track_version_range(
        trip_id: str,
        version: int,
        baseline: Optional[int],
        head: Optional[int],
    ) -> int:
        """按 (基线, 当前版本) 核对指定轨迹版本；不存在/早于基线/晚于当前均 404。"""
        if baseline is None:
            raise NotFoundError(f"车次不存在（无任何已保留的轨迹版本）: {trip_id}")
        if version < baseline:
            raise NotFoundError(
                f"轨迹版本 {version} 早于车次 {trip_id} 最早可追溯基线版本 {baseline}；"
                "升级前历史不做推造，无法复现"
            )
        if version > head:
            raise NotFoundError(
                f"轨迹版本 {version} 晚于车次 {trip_id} 当前版本 {head}"
            )
        return version

    def _coverage_intervals(
        self,
        zrow: Any,
        points: List[Any],
        lo: Optional[float],
        hi: Optional[float],
    ) -> List[Tuple[float, float]]:
        """单车道在区域内的占用时间区间（半开 [s, e)，epoch 秒）。

        沿用既有规则：带洞边界（外边界计入、洞及其边界不计）、区域 UTC 半开
        生效窗口、相邻轨迹点线性插值、零时长接触不计、相接区间合并；
        再与查询范围 [lo, hi) 做半开求交。
        """
        polygon = Polygon(
            json.loads(zrow["exterior"]),
            json.loads(zrow["holes"]),
        )
        windows_raw = json.loads(zrow["windows"])
        windows = merge_windows([(w["start_epoch"], w["end_epoch"]) for w in windows_raw])

        # 覆盖参数（时间）区间：遍历相邻点，乱序已在 SQL 层按时刻排序重建
        cover: List[Tuple[float, float]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            params = segment_inside_params(
                polygon, (a["x"], a["y"]), (b["x"], b["y"])
            )
            for pa, pb in params:
                # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                s = t0 if pa == 0.0 else t0 + pa * (t1 - t0)
                e = t1 if pb == 1.0 else t0 + pb * (t1 - t0)
                if e - s > self.min_duration:
                    cover.append((s, e))

        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        cover = merge_intervals(cover, eps=1e-6)
        intervals = clip_intervals_halfopen(cover, windows)
        if lo is not None or hi is not None:
            bounds = [(-float("inf") if lo is None else lo, float("inf") if hi is None else hi)]
            intervals = clip_intervals_halfopen(intervals, bounds)
        intervals = merge_intervals(intervals, eps=1e-6)
        return [(s, e) for s, e in intervals if e - s > self.min_duration]

    def _crossing_for_zone(
        self,
        zrow: Any,
        points: List[Any],
        lo: Optional[float],
        hi: Optional[float],
    ) -> Dict[str, Any]:
        intervals = self._coverage_intervals(zrow, points, lo, hi)

        segments = [
            {
                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                "duration_sec": round(e - s, 6),
            }
            for s, e in intervals
        ]
        total = round(sum(e - s for s, e in intervals), 6)

        return {
            "zone_id": zrow["zone_id"],
            "crossed": bool(intervals),
            "total_duration_sec": total,
            "segments": segments,
        }

    # ------------------------------------------------------------------
    # 单车一次作业危险区风险预算复核
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_positive_finite(raw: Any, field: str) -> float:
        """校验正且有限的数值（权重/预算）；非法值明确拒绝（400）。"""
        if isinstance(raw, bool):
            raise ValidationError(f"{field}必须是正且有限的数值，收到: {raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"{field}必须是正且有限的数值，收到: {raw!r}")
        if not math.isfinite(value) or value <= 0:
            raise ValidationError(f"{field}必须是正且有限的数值，收到: {raw!r}")
        return value

    def _validate_zone_weights(self, raw: Any) -> Dict[str, float]:
        """校验危险区 ID -> 正且有限风险权重的非空映射（非法值 400）。"""
        if not isinstance(raw, dict) or not raw:
            raise ValidationError(
                "zone_weights 必须是非空 {危险区 ID: 正且有限风险权重} 映射"
            )
        weights: Dict[str, float] = {}
        for zone_id, w_raw in raw.items():
            if not isinstance(zone_id, str) or not zone_id.strip():
                raise ValidationError(f"zone_weights 的危险区 ID 必须是非空字符串，收到: {zone_id!r}")
            zid = zone_id.strip()
            weights[zid] = self._validate_positive_finite(
                w_raw, f"zone_weights[{zid!r}] 风险权重"
            )
        return weights

    def query_risk_budget(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """复核单车一次作业在给定危险区集合上的加权风险预算。

        - 输入：车次、带时区 UTC 半开查询时段、非空危险区 ID→正且有限权重映射、
          正且有限加权秒数预算，可选区域修订号与轨迹版本；
        - 各区占用沿用既有轨迹插值、区域生效时段与带洞边界规则；
          重叠区域同时占用时只按最高权重计入，轨迹未覆盖或不在指定区内的时间不计；
        - 区域（含指定修订号）与车次点集（含指定/最新版本）从**同一已提交读视图**读取；
        - 返回按时间排列的恒权重正时长区间（相接且同权重合并）、各段及总计加权秒数、
          累计值首次达到预算的时刻；未达到预算时刻为 null，无暴露总计 0 且区间为空。
        """
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        try:
            lo = parse_utc(payload.get("since"), field="since").timestamp()
            hi = parse_utc(payload.get("until"), field="until").timestamp()
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        zone_weights = self._validate_zone_weights(payload.get("zone_weights"))
        budget = self._validate_positive_finite(
            payload.get("budget_weighted_sec"), "budget_weighted_sec 加权秒数预算"
        )

        pinned_rev = payload.get("revision") is not None
        revision = (
            _validate_revision_param(payload.get("revision")) if pinned_rev else None
        )

        pinned_track = payload.get("track_version") is not None
        track_version = (
            self._validate_track_version_value(payload.get("track_version"))
            if pinned_track
            else None
        )

        # ---- 同一已提交读视图：区域配置 + 车次点集一次快照读取 ----
        used_version = track_version  # 指定轨迹版本时原样回填
        with self.db.read_view() as conn:
            if pinned_track:
                baseline, head = self.db._trip_version_info(conn, trip_id)
                self._check_track_version_range(trip_id, track_version, baseline, head)
                points = self.db._list_trip_point_version_rows(conn, trip_id, track_version)
            else:
                vrow = self.db._trip_version_row(conn, trip_id)
                points = self.db._list_trip_points(conn, trip_id)
                if vrow is None and not points:
                    raise NotFoundError(f"车次不存在（无轨迹数据）: {trip_id}")
                used_version = 0 if vrow is None else int(vrow["version"])

            if pinned_rev:
                head_rev = self.db._current_revision(conn)
                if revision > head_rev:
                    raise NotFoundError(
                        f"修订号 {revision} 超出当前范围：当前最新修订号为 {head_rev}"
                        + ("（尚无任何区域登记）" if head_rev == 0 else "")
                    )

            # 逐区取截至修订号的最后配置（未指定取最新）；未知区/该版本尚未登记明确拒绝
            zone_rows: Dict[str, Any] = {}
            for zid in zone_weights:
                if pinned_rev:
                    zrow = self.db._zone_row_at(conn, zid, revision)
                    if zrow is None:
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zid}"
                        )
                else:
                    zrow = self.db._latest_zone_row(conn, zid)
                    if zrow is None:
                        raise NotFoundError(f"危险区不存在: {zid}")
                zone_rows[zid] = zrow

            # 各区占用区间（同一快照数据，纯计算；沿用插值/窗口/带洞边界/半开裁剪规则）
            coverage: Dict[str, List[Tuple[float, float]]] = {
                zid: self._coverage_intervals(zrow, points, lo, hi)
                for zid, zrow in zone_rows.items()
            }

        # ---- 扫描线：逐时刻取同时占用区中的最高权重；权重变化即分段 ----
        event_times = {t for ivs in coverage.values() for s, e in ivs for t in (s, e)}
        events = sorted(event_times)
        pieces: List[List[float]] = []  # [start, end, weight]
        for a, b in zip(events, events[1:]):
            if b - a <= self.min_duration:
                continue  # 零时长分段不计
            mid = (a + b) / 2.0
            weight = 0.0
            for zid, ivs in coverage.items():
                if any(s <= mid < e for s, e in ivs):
                    weight = max(weight, zone_weights[zid])
            if weight <= 0.0:
                continue
            # 相接（1e-6 秒容差）且权重相同的区间合并
            if pieces and pieces[-1][2] == weight and a - pieces[-1][1] <= 1e-6:
                pieces[-1][1] = b
            else:
                pieces.append([a, b, weight])

        segments: List[Dict[str, Any]] = []
        total_weighted = 0.0
        reached_epoch: Optional[float] = None
        for s, e, weight in pieces:
            duration = e - s
            weighted = duration * weight
            segment_base = total_weighted
            total_weighted += weighted
            if reached_epoch is None and total_weighted + 1e-9 >= budget:
                # 累计在本段 [s, e) 内首次达到预算：按权重匀速反推时刻
                need = min(max(budget - segment_base, 0.0), weighted)
                reached_epoch = s if weight <= 0.0 else s + (need / weight)
            segments.append(
                {
                    "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                    "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                    "duration_sec": round(duration, 6),
                    "weight": weight,
                    "weighted_sec": round(weighted, 6),
                }
            )

        result: Dict[str, Any] = {
            "trip_id": trip_id,
            "since": _iso_z(datetime.fromtimestamp(lo, timezone.utc)),
            "until": _iso_z(datetime.fromtimestamp(hi, timezone.utc)),
            "zone_weights": dict(zone_weights),
            "budget_weighted_sec": budget,
            "point_count": len(points),
            "segments": segments,
            "total_weighted_sec": round(total_weighted, 6),
            "budget_reached_at": (
                None
                if reached_epoch is None
                else _iso_z(datetime.fromtimestamp(reached_epoch, timezone.utc))
            ),
        }
        if pinned_track:
            # 标明本次复现实际使用的轨迹版本；未指定时响应不含该字段（既有语义）
            result["track_version"] = used_version
        if pinned_rev:
            result["revision"] = revision
        return result

    # ------------------------------------------------------------------
    # 单车危险区内超速时段复核
    # ------------------------------------------------------------------

    def _validate_zone_speed_limits(self, raw: Any) -> Dict[str, float]:
        """校验危险区 ID -> 正且有限限速（米/秒）的非空映射（非法值 400）。"""
        if not isinstance(raw, dict) or not raw:
            raise ValidationError(
                "zone_speed_limits 必须是非空 {危险区 ID: 正且有限限速（米/秒）} 映射"
            )
        limits: Dict[str, float] = {}
        for zone_id, v_raw in raw.items():
            if not isinstance(zone_id, str) or not zone_id.strip():
                raise ValidationError(
                    f"zone_speed_limits 的危险区 ID 必须是非空字符串，收到: {zone_id!r}"
                )
            zid = zone_id.strip()
            limits[zid] = self._validate_positive_finite(
                v_raw, f"zone_speed_limits[{zid!r}] 限速（米/秒）"
            )
        return limits

    def query_speeding_review(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """复核单车在给定危险区内的超速时段。

        - 输入：车次、带时区 UTC 半开查询时段、非空危险区 ID→正且有限限速（米/秒）映射、
          正且有限最大采样间隔（秒），可选区域修订号与轨迹版本；
        - 区域（含指定修订号）与车次有效点（含指定/最新版本）从**同一已提交读视图**读取；
        - 相邻点按线性轨迹计算恒定速度（距离/间隔）；点间隔严格大于上限的整段视为无覆盖；
        - 仅当该时刻位于所选区几何内（外边界计入、洞及其边界不计）且区域生效时计入，
          重叠区域按最低限速；速度**严格大于**适用限速才违规，零时长接触不计；
        - 返回按时序切分的违规正时长区间（每段速度、适用限速、并列限速区 ID）、
          实际使用的区域修订号与轨迹版本，以及违规总时长与超速距离总量
          （Σ(速度-限速)×时长）；无违规返回空区间与零总量。
        """
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        try:
            lo = parse_utc(payload.get("since"), field="since").timestamp()
            hi = parse_utc(payload.get("until"), field="until").timestamp()
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        zone_limits = self._validate_zone_speed_limits(payload.get("zone_speed_limits"))
        max_gap = self._validate_positive_finite(
            payload.get("max_sample_interval_sec"), "max_sample_interval_sec 最大采样间隔（秒）"
        )

        pinned_rev = payload.get("revision") is not None
        revision = (
            _validate_revision_param(payload.get("revision")) if pinned_rev else None
        )

        pinned_track = payload.get("track_version") is not None
        track_version = (
            self._validate_track_version_value(payload.get("track_version"))
            if pinned_track
            else None
        )

        # ---- 同一已提交读视图：区域配置 + 车次点集一次快照读取 ----
        used_version = track_version  # 指定轨迹版本时原样回填
        with self.db.read_view() as conn:
            if pinned_track:
                baseline, head = self.db._trip_version_info(conn, trip_id)
                self._check_track_version_range(trip_id, track_version, baseline, head)
                points = self.db._list_trip_point_version_rows(conn, trip_id, track_version)
            else:
                vrow = self.db._trip_version_row(conn, trip_id)
                points = self.db._list_trip_points(conn, trip_id)
                if vrow is None and not points:
                    raise NotFoundError(f"车次不存在（无轨迹数据）: {trip_id}")
                used_version = 0 if vrow is None else int(vrow["version"])

            if pinned_rev:
                head_rev = self.db._current_revision(conn)
                if revision > head_rev:
                    raise NotFoundError(
                        f"修订号 {revision} 超出当前范围：当前最新修订号为 {head_rev}"
                        + ("（尚无任何区域登记）" if head_rev == 0 else "")
                    )
                used_revision = revision
            else:
                used_revision = self.db._current_revision(conn)

            # 逐区取截至修订号的最后配置（未指定取最新）；未知区/该版本尚未登记明确拒绝
            zone_data: Dict[str, Dict[str, Any]] = {}
            for zid in zone_limits:
                if pinned_rev:
                    zrow = self.db._zone_row_at(conn, zid, revision)
                    if zrow is None:
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zid}"
                        )
                else:
                    zrow = self.db._latest_zone_row(conn, zid)
                    if zrow is None:
                        raise NotFoundError(f"危险区不存在: {zid}")
                zone_data[zid] = self._prepare_zone(zrow)

        # ---- 逐相邻点段求“区内且生效”的覆盖子区间（点间隔超限整段无覆盖）----
        # cover_pieces: [start, end, speed, {zone_id, ...}]
        cover_pieces: List[List[Any]] = []
        for a, b in zip(points, points[1:]):
            t0, t1 = a["ts_epoch"], b["ts_epoch"]
            if t1 <= t0:
                # 同刻点不应出现（唯一约束），稳妥跳过
                continue
            dt = t1 - t0
            if dt > max_gap:
                # 点间隔超过最大采样间隔：整段视为无覆盖
                continue
            speed = math.hypot(b["x"] - a["x"], b["y"] - a["y"]) / dt
            for zid, zdata in zone_data.items():
                params = segment_inside_params(
                    zdata["polygon"], (a["x"], a["y"]), (b["x"], b["y"])
                )
                for pa, pb in params:
                    # 参数 0/1 直接取端点时刻，保证相邻段在公共采样点精确相接
                    s = t0 if pa == 0.0 else t0 + pa * dt
                    e = t1 if pb == 1.0 else t0 + pb * dt
                    # 区域 UTC 半开生效窗口 ∩ 查询半开范围
                    ivs = clip_intervals_halfopen([(s, e)], zdata["windows"])
                    bounds = [(lo, hi)]
                    ivs = clip_intervals_halfopen(ivs, bounds)
                    for cs, ce in ivs:
                        cover_pieces.append([cs, ce, speed, zid])

        # ---- 扫描线：每个正时长原子段内速度与适用限速（最低限速）恒定 ----
        cuts = {t for s, e, _v, _z in cover_pieces for t in (s, e)}
        events = sorted(cuts)
        # violations: [start, end, speed, limit, tied_zone_ids]
        violations: List[List[Any]] = []
        for a, b in zip(events, events[1:]):
            if b - a <= self.min_duration:
                continue  # 零时长接触不计
            mid = (a + b) / 2.0
            active = {
                zid for s, e, _v, zid in cover_pieces if s <= mid < e
            }
            if not active:
                continue
            # 同一相邻点段内速度恒定；取任一覆盖该区的片段速度即可
            speed = next(v for s, e, v, zid in cover_pieces if s <= mid < e)
            limit = min(zone_limits[zid] for zid in active)
            tied = sorted(zid for zid in active if zone_limits[zid] == limit)
            if speed <= limit:
                continue  # 必须严格大于限速才违规
            # 相接（1e-6 秒容差）且速度/限速/并列限速区集合一致的区间合并
            if (
                violations
                and violations[-1][2] == speed
                and violations[-1][3] == limit
                and violations[-1][4] == tied
                and a - violations[-1][1] <= 1e-6
            ):
                violations[-1][1] = b
            else:
                violations.append([a, b, speed, limit, tied])

        violations = [v for v in violations if v[1] - v[0] > self.min_duration]

        segments: List[Dict[str, Any]] = []
        total_duration = 0.0
        total_excess_distance = 0.0
        for s, e, speed, limit, tied in violations:
            duration = e - s
            excess_distance = (speed - limit) * duration
            total_duration += duration
            total_excess_distance += excess_distance
            segments.append(
                {
                    "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                    "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                    "duration_sec": round(duration, 6),
                    "speed_mps": speed,
                    "speed_limit_mps": limit,
                    "zone_ids": list(tied),
                    "excess_distance_m": round(excess_distance, 6),
                }
            )

        return {
            "trip_id": trip_id,
            "since": _iso_z(datetime.fromtimestamp(lo, timezone.utc)),
            "until": _iso_z(datetime.fromtimestamp(hi, timezone.utc)),
            "zone_speed_limits": dict(zone_limits),
            "max_sample_interval_sec": max_gap,
            "point_count": len(points),
            "segments": segments,
            "total_violation_duration_sec": round(total_duration, 6),
            "total_excess_distance_m": round(total_excess_distance, 6),
            "revision": used_revision,
            "track_version": used_version,
        }

    @staticmethod
    def _prepare_zone(zrow: Any) -> Dict[str, Any]:
        """解析一行区域修订为超速/占用计算所需的多边形与合并后生效窗口。"""
        windows_raw = json.loads(zrow["windows"])
        return {
            "polygon": Polygon(
                json.loads(zrow["exterior"]),
                json.loads(zrow["holes"]),
            ),
            "windows": merge_windows(
                [(w["start_epoch"], w["end_epoch"]) for w in windows_raw]
            ),
        }

    # ------------------------------------------------------------------
    # 多车同时占用审计
    # ------------------------------------------------------------------

    def query_co_occupancy(self, zone_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """查明指定危险区在 [since, until) 内被多车同时占用的时段。

        - 至少两辆互异车次；占用阈值介于 2 与车次数之间；
        - 可选区域修订号与逐车轨迹版本，未指定版本的车取查询时最新已提交状态；
        - 区域与全部车次点集从**同一已提交读视图**读取，并发更新不会拼出不存在的组合；
        - 返回达到阈值的半开区间，区间内车次集合与数量恒定；集合变化即分段，
          相邻同集合区间合并；合法但未达阈值返回空列表。
        """
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        # ---- 请求校验（时间范围 / 车次互异 / 阈值 / 逐车版本格式）----
        trip_ids_raw = payload.get("trip_ids")
        if not isinstance(trip_ids_raw, list) or len(trip_ids_raw) < 2:
            raise ValidationError("trip_ids 必须包含至少两辆车次")
        trip_ids: List[str] = []
        for idx, tid in enumerate(trip_ids_raw):
            if not isinstance(tid, str) or not tid.strip():
                raise ValidationError(f"trip_ids[{idx}] 必须是非空字符串")
            tid = tid.strip()
            if tid in trip_ids:
                raise ValidationError(f"trip_ids 中车次重复: {tid!r}（各车次必须互异）")
            trip_ids.append(tid)

        try:
            lo = parse_utc(payload.get("since"), field="since").timestamp()
            hi = parse_utc(payload.get("until"), field="until").timestamp()
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        threshold = payload.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            raise ValidationError(f"threshold 必须是整数，收到: {threshold!r}")
        if threshold < 2 or threshold > len(trip_ids):
            raise ValidationError(
                f"threshold 必须介于 2 与车次数 {len(trip_ids)} 之间，收到: {threshold}"
            )

        pinned_rev = payload.get("revision") is not None
        revision = (
            _validate_revision_param(payload.get("revision")) if pinned_rev else None
        )

        track_versions_raw = payload.get("track_versions") or {}
        if not isinstance(track_versions_raw, dict):
            raise ValidationError("track_versions 必须是 {车次: 版本号} 对象")
        pinned_versions: Dict[str, int] = {}
        for tid, raw_v in track_versions_raw.items():
            if tid not in trip_ids:
                raise ValidationError(
                    f"track_versions 包含未在 trip_ids 中的车次: {tid!r}"
                )
            pinned_versions[tid] = self._validate_track_version_value(raw_v)

        # ---- 同一已提交读视图：区域 + 全部车次点集一次快照读取 ----
        with self.db.read_view() as conn:
            if pinned_rev:
                head_rev = self.db._current_revision(conn)
                if revision > head_rev:
                    raise NotFoundError(
                        f"修订号 {revision} 超出当前范围：当前最新修订号为 {head_rev}"
                        + ("（尚无任何区域登记）" if head_rev == 0 else "")
                    )
                zrow = self.db._zone_row_at(conn, zone_id, revision)
                if zrow is None:
                    raise NotFoundError(
                        f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
                    )
            else:
                zrow = self.db._latest_zone_row(conn, zone_id)
                if zrow is None:
                    raise NotFoundError(f"危险区不存在: {zone_id}")

            per_trip: List[Tuple[str, int, List[Any]]] = []
            for tid in trip_ids:
                if tid in pinned_versions:
                    version = pinned_versions[tid]
                    baseline, head = self.db._trip_version_info(conn, tid)
                    self._check_track_version_range(tid, version, baseline, head)
                    points = self.db._list_trip_point_version_rows(conn, tid, version)
                else:
                    vrow = self.db._trip_version_row(conn, tid)
                    points = self.db._list_trip_points(conn, tid)
                    if vrow is None and not points:
                        raise NotFoundError(f"车次不存在（无轨迹数据）: {tid}")
                    version = 0 if vrow is None else int(vrow["version"])
                per_trip.append((tid, version, points))

            # 各车占用区间（同一快照数据，纯计算）
            occupancy: Dict[str, List[Tuple[float, float]]] = {
                tid: self._coverage_intervals(zrow, points, lo, hi)
                for tid, _version, points in per_trip
            }

        # ---- 扫描线分段：车次集合变化即分段，达到阈值才输出，相邻同集合合并 ----
        events = sorted(
            {s for ivs in occupancy.values() for s, _e in ivs}
            | {e for ivs in occupancy.values() for _s, e in ivs}
        )
        segments: List[List[Any]] = []  # [start, end, occupants]
        for a, b in zip(events, events[1:]):
            if b - a <= self.min_duration:
                continue  # 零时长分段不计
            mid = (a + b) / 2.0
            occupants = sorted(
                tid
                for tid, ivs in occupancy.items()
                if any(s <= mid < e for s, e in ivs)
            )
            if len(occupants) < threshold:
                continue
            if segments and segments[-1][2] == occupants and a - segments[-1][1] <= 1e-6:
                segments[-1][1] = b  # 相邻同集合：合并
            else:
                segments.append([a, b, occupants])

        result: Dict[str, Any] = {
            "zone_id": zone_id,
            "since": _iso_z(datetime.fromtimestamp(lo, timezone.utc)),
            "until": _iso_z(datetime.fromtimestamp(hi, timezone.utc)),
            "threshold": threshold,
            "trips": [
                {
                    "trip_id": tid,
                    "track_version": version,
                    "point_count": len(points),
                }
                for tid, version, points in per_trip
            ],
            "intervals": [
                {
                    "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                    "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                    "duration_sec": round(e - s, 6),
                    "trip_ids": list(occupants),
                    "count": len(occupants),
                }
                for s, e, occupants in segments
            ],
        }
        if pinned_rev:
            result["revision"] = revision
        return result

    # ------------------------------------------------------------------
    # 危险区内近距离相遇复核
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_max_distance(raw: Any) -> float:
        """校验米制距离阈值：必须是正且有限的数值（非法值 400）。"""
        if isinstance(raw, bool):
            raise ValidationError(f"max_distance 必须是正且有限的数值（米），收到: {raw!r}")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValidationError(f"max_distance 必须是正且有限的数值（米），收到: {raw!r}")
        if not math.isfinite(value) or value <= 0:
            raise ValidationError(f"max_distance 必须是正且有限的数值（米），收到: {raw!r}")
        return value

    def query_proximity(self, zone_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """复核指定危险区内两车是否曾在 [since, until) 内近距离相遇。

        - 至少两辆互异车次；距离阈值正且有限（米），严格小于才计入；
        - 可选区域修订号与逐车轨迹版本，未指定版本的车取查询时最新已提交状态；
        - 区域与全部车次点集从**同一已提交读视图**读取，并发更新不会拼出不存在的组合；
        - 只在两车均有轨迹覆盖且同时位于区内时比较同一时刻的平面距离
          （沿用相邻点线性插值、区域生效窗口与带洞边界规则）；
        - 按无序车次对返回距离严格小于阈值的正时长区间（起止、持续秒数、区间最小距离），
          相接且连续满足条件的区间合并，瞬时触及忽略；合法但无相遇返回空列表。
        """
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        # ---- 请求校验（时间范围 / 车次互异 / 距离阈值 / 逐车版本格式）----
        trip_ids_raw = payload.get("trip_ids")
        if not isinstance(trip_ids_raw, list) or len(trip_ids_raw) < 2:
            raise ValidationError("trip_ids 必须包含至少两辆车次")
        trip_ids: List[str] = []
        for idx, tid in enumerate(trip_ids_raw):
            if not isinstance(tid, str) or not tid.strip():
                raise ValidationError(f"trip_ids[{idx}] 必须是非空字符串")
            tid = tid.strip()
            if tid in trip_ids:
                raise ValidationError(f"trip_ids 中车次重复: {tid!r}（各车次必须互异）")
            trip_ids.append(tid)

        try:
            lo = parse_utc(payload.get("since"), field="since").timestamp()
            hi = parse_utc(payload.get("until"), field="until").timestamp()
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        max_distance = self._validate_max_distance(payload.get("max_distance"))

        pinned_rev = payload.get("revision") is not None
        revision = (
            _validate_revision_param(payload.get("revision")) if pinned_rev else None
        )

        track_versions_raw = payload.get("track_versions") or {}
        if not isinstance(track_versions_raw, dict):
            raise ValidationError("track_versions 必须是 {车次: 版本号} 对象")
        pinned_versions: Dict[str, int] = {}
        for tid, raw_v in track_versions_raw.items():
            if tid not in trip_ids:
                raise ValidationError(
                    f"track_versions 包含未在 trip_ids 中的车次: {tid!r}"
                )
            pinned_versions[tid] = self._validate_track_version_value(raw_v)

        # ---- 同一已提交读视图：区域 + 全部车次点集一次快照读取 ----
        with self.db.read_view() as conn:
            if pinned_rev:
                head_rev = self.db._current_revision(conn)
                if revision > head_rev:
                    raise NotFoundError(
                        f"修订号 {revision} 超出当前范围：当前最新修订号为 {head_rev}"
                        + ("（尚无任何区域登记）" if head_rev == 0 else "")
                    )
                zrow = self.db._zone_row_at(conn, zone_id, revision)
                if zrow is None:
                    raise NotFoundError(
                        f"修订号 {revision} 的配置中危险区尚未登记: {zone_id}"
                    )
            else:
                zrow = self.db._latest_zone_row(conn, zone_id)
                if zrow is None:
                    raise NotFoundError(f"危险区不存在: {zone_id}")

            per_trip: List[Tuple[str, int, List[Any]]] = []
            for tid in trip_ids:
                if tid in pinned_versions:
                    version = pinned_versions[tid]
                    baseline, head = self.db._trip_version_info(conn, tid)
                    self._check_track_version_range(tid, version, baseline, head)
                    points = self.db._list_trip_point_version_rows(conn, tid, version)
                else:
                    vrow = self.db._trip_version_row(conn, tid)
                    points = self.db._list_trip_points(conn, tid)
                    if vrow is None and not points:
                        raise NotFoundError(f"车次不存在（无轨迹数据）: {tid}")
                    version = 0 if vrow is None else int(vrow["version"])
                per_trip.append((tid, version, points))

            # 各车“轨迹覆盖且位于区内”的时间区间（同一快照数据，纯计算）
            coverage: Dict[str, List[Tuple[float, float]]] = {
                tid: self._coverage_intervals(zrow, points, lo, hi)
                for tid, _version, points in per_trip
            }

        # ---- 无序车次对：仅在两车同时位于区内时比较同一时刻平面距离 ----
        points_by_trip = {tid: points for tid, _version, points in per_trip}
        pairs: List[Dict[str, Any]] = []
        for i in range(len(trip_ids)):
            for j in range(i + 1, len(trip_ids)):
                tid_a, tid_b = trip_ids[i], trip_ids[j]
                # 两车覆盖区间的半开交集：双方均有轨迹覆盖且同时位于区内的时间
                common = clip_intervals_halfopen(coverage[tid_a], coverage[tid_b])
                intervals = self._encounter_intervals(
                    points_by_trip[tid_a], points_by_trip[tid_b], common, max_distance
                )
                if not intervals:
                    continue
                pairs.append(
                    {
                        "trip_ids": [tid_a, tid_b],
                        "intervals": [
                            {
                                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                                "duration_sec": round(e - s, 6),
                                "min_distance": round(min_d, 6),
                            }
                            for s, e, min_d in intervals
                        ],
                    }
                )

        result: Dict[str, Any] = {
            "zone_id": zone_id,
            "since": _iso_z(datetime.fromtimestamp(lo, timezone.utc)),
            "until": _iso_z(datetime.fromtimestamp(hi, timezone.utc)),
            "max_distance": max_distance,
            "trips": [
                {
                    "trip_id": tid,
                    "track_version": version,
                    "point_count": len(points),
                }
                for tid, version, points in per_trip
            ],
            "pairs": pairs,
        }
        if pinned_rev:
            result["revision"] = revision
        return result

    def _encounter_intervals(
        self,
        points_a: List[Any],
        points_b: List[Any],
        common: List[Tuple[float, float]],
        max_distance: float,
    ) -> List[Tuple[float, float, float]]:
        """两车公共在区时间内、平面距离严格小于阈值的正时长区间（含区间最小距离）。

        把公共区间按两车轨迹点时刻切成原子段：每段内两车各自线性运动，
        相对位置随时间线性变化，距离平方为开口向上抛物线，解析求严格小于
        阈值的子区间；相接（端点重合）且连续满足条件的区间合并，最小距离
        取各段最小；零时长触及不产生区间。
        """
        ts_a = [r["ts_epoch"] for r in points_a]
        ts_b = [r["ts_epoch"] for r in points_b]
        candidates: List[Tuple[float, float, float]] = []
        for s, e in common:
            cuts = {s, e}
            for t in ts_a:
                if s < t < e:
                    cuts.add(t)
            for t in ts_b:
                if s < t < e:
                    cuts.add(t)
            times = sorted(cuts)
            for a, b in zip(times, times[1:]):
                piece = self._close_interval_on_piece(
                    points_a, ts_a, points_b, ts_b, a, b, max_distance
                )
                if piece is not None:
                    candidates.append(piece)
        # 时间域合并留 1e-6 秒容差，吸收不同时刻解析路径的浮点误差
        merged: List[List[float]] = []
        for s, e, min_d in candidates:
            if merged and s - merged[-1][1] <= 1e-6:
                merged[-1][1] = max(merged[-1][1], e)
                merged[-1][2] = min(merged[-1][2], min_d)
            else:
                merged.append([s, e, min_d])
        return [(s, e, md) for s, e, md in merged if e - s > self.min_duration]

    def _close_interval_on_piece(
        self,
        points_a: List[Any],
        ts_a: List[float],
        points_b: List[Any],
        ts_b: List[float],
        a: float,
        b: float,
        max_distance: float,
    ) -> Optional[Tuple[float, float, float]]:
        """原子段 [a, b] 内距离严格小于阈值的子区间 (s, e, 最小距离)；无则 None。"""
        ax0, ay0 = self._position_at(points_a, ts_a, a)
        ax1, ay1 = self._position_at(points_a, ts_a, b)
        bx0, by0 = self._position_at(points_b, ts_b, a)
        bx1, by1 = self._position_at(points_b, ts_b, b)
        # 相对位置 r(u) = r0 + dv·u，u ∈ [0,1]；|r(u)|² < d² 为开口向上抛物线不等式
        rx0, ry0 = ax0 - bx0, ay0 - by0
        dvx, dvy = (ax1 - bx1) - rx0, (ay1 - by1) - ry0
        qa = dvx * dvx + dvy * dvy
        qc = rx0 * rx0 + ry0 * ry0 - max_distance * max_distance
        if qa == 0.0:
            # 相对静止：整段要么全满足要么全不满足
            if qc < 0.0:
                return (a, b, math.sqrt(rx0 * rx0 + ry0 * ry0))
            return None
        qb = 2.0 * (rx0 * dvx + ry0 * dvy)
        disc = qb * qb - 4.0 * qa * qc
        if disc <= 0.0:
            return None  # 始终不满足，或仅瞬时相切（零时长触及忽略）
        root = math.sqrt(disc)
        u1 = (-qb - root) / (2.0 * qa)
        u2 = (-qb + root) / (2.0 * qa)
        lo_u, hi_u = max(u1, 0.0), min(u2, 1.0)
        if hi_u <= lo_u:
            return None
        s = a + lo_u * (b - a)
        e = a + hi_u * (b - a)
        if e - s <= self.min_duration:
            return None

        def _dist(u: float) -> float:
            rx = rx0 + dvx * u
            ry = ry0 + dvy * u
            return math.sqrt(rx * rx + ry * ry)

        u_star = -qb / (2.0 * qa)  # 抛物线顶点：距离最小处
        if lo_u <= u_star <= hi_u:
            min_d = _dist(u_star)
        else:
            min_d = min(_dist(lo_u), _dist(hi_u))
        return (s, e, min_d)

    @staticmethod
    def _position_at(
        points: List[Any], ts: List[float], t: float
    ) -> Tuple[float, float]:
        """按相邻点线性插值取 t 时刻平面位置；t 须落在轨迹覆盖范围内。"""
        idx = bisect.bisect_right(ts, t) - 1
        idx = min(max(idx, 0), len(points) - 2)
        pa, pb = points[idx], points[idx + 1]
        t0, t1 = pa["ts_epoch"], pb["ts_epoch"]
        ratio = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return (
            pa["x"] + (pb["x"] - pa["x"]) * ratio,
            pa["y"] + (pb["y"] - pa["y"]) * ratio,
        )

    # ------------------------------------------------------------------
    # 危险区通行许可（登记 / 撤销 / 许可复核）
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_nonneg_int(raw: Any, field: str) -> int:
        """校验 >=0 的整数（许可修订号/撤销所见修订号）；非法值 400。"""
        if isinstance(raw, bool):
            raise ValidationError(f"{field}必须是 >=0 的整数，收到: {raw!r}")
        if isinstance(raw, int):
            value = raw
        else:
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                raise ValidationError(f"{field}必须是 >=0 的整数，收到: {raw!r}")
        if value < 0:
            raise ValidationError(f"{field}必须是 >=0 的整数，收到: {value}")
        return value

    @staticmethod
    def _permit_payload(row: Any) -> Dict[str, Any]:
        """许可当前状态行 -> 响应体（不含全局修订号，由调用方补充）。"""
        return {
            "permit_id": row["permit_id"],
            "trip_id": row["trip_id"],
            "zone_id": row["zone_id"],
            "start": row["start_iso"],
            "end": row["end_iso"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "revoked_revision": (
                None if row["revoked_revision"] is None else int(row["revoked_revision"])
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def register_permit(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """登记作业车进入危险区的通行许可。

        - 许可按许可 ID、车次、危险区及带时区 UTC 半开时段登记；登记时危险区须存在，
          车次可尚无轨迹（不校验车次）；
        - 同 ID 同内容重传幂等（不递增、不写历史，返回 200）；同 ID 异内容冲突（409）；
          已撤销 ID 再次登记同样冲突（撤销后 ID 永久占用）；
        - 真实登记在单事务内追加不可变修订流水并原子分配全局递增许可修订号。
        返回 (HTTP 状态码, 响应体)：新登记 201，幂等重传 200。
        """
        permit_id = payload.get("permit_id")
        if not isinstance(permit_id, str) or not permit_id.strip():
            raise ValidationError("permit_id 必须是非空字符串")
        permit_id = permit_id.strip()
        trip_id = payload.get("trip_id")
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()
        zone_id = payload.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise ValidationError("zone_id 必须是非空字符串")
        zone_id = zone_id.strip()

        try:
            start = parse_utc(payload.get("start"), field="start")
            end = parse_utc(payload.get("end"), field="end")
            validate_window(start, end)
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        start_epoch, end_epoch = start.timestamp(), end.timestamp()
        start_iso, end_iso = _iso_z(start), _iso_z(end)

        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            # 登记时危险区须存在（按当前最新配置）；车次可尚无轨迹，不做校验
            if self.db._latest_zone_row(conn, zone_id) is None:
                raise NotFoundError(f"危险区不存在: {zone_id}")

            existing = self.db._permit_row(conn, permit_id)
            if existing is not None:
                if existing["status"] == "revoked":
                    raise ConflictError(
                        f"permit_id={permit_id!r} 已撤销，许可 ID 不得重用；"
                        "如需再次通行请使用新的 permit_id 登记"
                    )
                same = (
                    existing["trip_id"] == trip_id
                    and existing["zone_id"] == zone_id
                    and existing["start_epoch"] == start_epoch
                    and existing["end_epoch"] == end_epoch
                )
                if not same:
                    raise ConflictError(
                        f"permit_id={permit_id!r} 已存在且内容不同："
                        f"已存 (trip_id={existing['trip_id']!r}, zone_id={existing['zone_id']!r}, "
                        f"[{existing['start_iso']}, {existing['end_iso']}))，"
                        f"新传 (trip_id={trip_id!r}, zone_id={zone_id!r}, [{start_iso}, {end_iso}))"
                    )
                # 同 ID 同内容：幂等重传，不递增、不写历史
                head = self.db._current_permit_revision(conn)
                body = self._permit_payload(existing)
                body["current_permit_revision"] = head
                body["idempotent"] = True
                return 200, body

            revision = self.db._insert_permit_revision(
                conn, permit_id, trip_id, zone_id, "register",
                start_epoch, end_epoch, start_iso, end_iso, now_iso,
            )
            self.db._insert_permit(
                conn, permit_id, trip_id, zone_id,
                start_epoch, end_epoch, start_iso, end_iso, revision, now_iso,
            )

        return 201, {
            "permit_id": permit_id,
            "trip_id": trip_id,
            "zone_id": zone_id,
            "start": start_iso,
            "end": end_iso,
            "status": "active",
            "revision": revision,
            "revoked_revision": None,
            "created_at": now_iso,
            "updated_at": now_iso,
            "current_permit_revision": revision,
            "idempotent": False,
        }

    def revoke_permit(self, permit_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """撤销通行许可。

        - 须提交所见全局许可修订号（乐观并发）：与当前全局号不一致即过期，拒绝（409）；
        - 许可不存在（ID 从未登记）404；已撤销再撤销为重复撤销，拒绝（409）；
        - 真实撤销在单事务内原子递增全局许可修订号并保留不可改历史；
          撤销后 permit_id 永久占用，不得再登记或再撤销。
        """
        if not isinstance(permit_id, str) or not permit_id.strip():
            raise ValidationError("permit_id 必须是非空字符串")
        permit_id = permit_id.strip()
        base_revision = self._validate_nonneg_int(payload.get("revision"), "revision 所见许可修订号")

        now_iso = _iso_z(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            row = self.db._permit_row(conn, permit_id)
            if row is None:
                raise NotFoundError(f"通行许可不存在（未登记或 ID 有误）: {permit_id}")
            if row["status"] == "revoked":
                # 重复撤销一律拒绝（无论提交的修订号是什么）
                raise ConflictError(
                    f"permit_id={permit_id!r} 已在许可修订号 "
                    f"{int(row['revoked_revision'])} 撤销，不得重复撤销"
                )
            head = self.db._current_permit_revision(conn)
            if base_revision != head:
                raise ConflictError(
                    f"许可修订号过期: 提交基于修订号 {base_revision}，"
                    f"当前全局许可修订号为 {head}；请重新读取许可状态后再撤销"
                )
            revision = self.db._insert_permit_revision(
                conn, permit_id, row["trip_id"], row["zone_id"], "revoke",
                None, None, None, None, now_iso,
            )
            self.db._revoke_permit(conn, permit_id, revision, now_iso)

        return {
            "permit_id": permit_id,
            "status": "revoked",
            "revision": revision,
            "current_permit_revision": revision,
        }

    def get_permit(self, permit_id: str) -> Optional[Dict[str, Any]]:
        """许可当前状态（含已撤销）；ID 从未登记返回 None。"""
        with self.db.read_view() as conn:
            row = self.db._permit_row(conn, permit_id)
            if row is None:
                return None
            body = self._permit_payload(row)
            body["current_permit_revision"] = self.db._current_permit_revision(conn)
            return body

    def list_permit_revisions(self, limit: int = 500) -> Dict[str, Any]:
        """全局通行许可修订流水（按修订号倒序），含当前全局许可修订号。"""
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 10000:
            raise ValidationError("limit 必须是 1..10000 的整数")
        with self.db.read_view() as conn:
            head = self.db._current_permit_revision(conn)
            rows = self.db._list_permit_revision_rows(conn, limit=limit)
            revisions = [
                {
                    "revision": int(r["revision"]),
                    "permit_id": r["permit_id"],
                    "trip_id": r["trip_id"],
                    "zone_id": r["zone_id"],
                    "action": r["action"],
                    "start": r["start_iso"],
                    "end": r["end_iso"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        return {"current_permit_revision": head, "revisions": revisions}

    def query_permit_review(self, trip_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """复核单车次在一组危险区内的实际占用是否被通行许可覆盖。

        - 输入：车次、非空互异危险区 ID 集合、带时区 UTC 半开查询时段，可选区域修订号、
          轨迹版本与全局许可修订号（含 0）；未指定版本取**同一已提交读视图**中的最新状态，
          无任何许可时许可修订号为 0；
        - 实际占用沿用既有轨迹插值、带洞边界与区域生效规则；仅核对**该车次该危险区**、
          在指定许可修订视图下仍有效（active 且 revision<=视图号）的许可；重叠许可时段取并集；
        - 按区返回无许可的正时长区间、累计秒数，以及实际使用的区域修订号、轨迹版本与
          许可修订号；无占用返回空区间和零秒数；
        - 指定旧许可修订号可复现补办或撤销前的结果（许可状态不可变）。
        """
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise ValidationError("trip_id 必须是非空字符串")
        trip_id = trip_id.strip()

        try:
            lo = parse_utc(payload.get("since"), field="since").timestamp()
            hi = parse_utc(payload.get("until"), field="until").timestamp()
        except TimeError as exc:
            raise ValidationError(str(exc)) from exc
        if lo >= hi:
            raise ValidationError("since 必须早于 until（查询范围为半开 [since, until)）")

        zone_ids_raw = payload.get("zone_ids")
        if not isinstance(zone_ids_raw, list) or not zone_ids_raw:
            raise ValidationError("zone_ids 必须是非空危险区 ID 列表")
        zone_ids: List[str] = []
        for idx, zid in enumerate(zone_ids_raw):
            if not isinstance(zid, str) or not zid.strip():
                raise ValidationError(f"zone_ids[{idx}] 必须是非空字符串")
            zid = zid.strip()
            if zid in zone_ids:
                raise ValidationError(f"zone_ids 中危险区重复: {zid!r}（各区必须互异）")
            zone_ids.append(zid)

        pinned_rev = payload.get("revision") is not None
        revision = _validate_revision_param(payload.get("revision")) if pinned_rev else None

        pinned_track = payload.get("track_version") is not None
        track_version = (
            self._validate_track_version_value(payload.get("track_version"))
            if pinned_track else None
        )

        pinned_permit = payload.get("permit_revision") is not None
        permit_revision = (
            self._validate_nonneg_int(
                payload.get("permit_revision"), "permit_revision 许可修订号"
            )
            if pinned_permit else None
        )

        # ---- 同一已提交读视图：区域配置 + 车次点集 + 许可状态一次快照读取 ----
        with self.db.read_view() as conn:
            if pinned_track:
                baseline, head_v = self.db._trip_version_info(conn, trip_id)
                self._check_track_version_range(trip_id, track_version, baseline, head_v)
                points = self.db._list_trip_point_version_rows(conn, trip_id, track_version)
            else:
                vrow = self.db._trip_version_row(conn, trip_id)
                points = self.db._list_trip_points(conn, trip_id)
                if vrow is None and not points:
                    raise NotFoundError(f"车次不存在（无轨迹数据）: {trip_id}")
                track_version = 0 if vrow is None else int(vrow["version"])

            if pinned_rev:
                head_rev = self.db._current_revision(conn)
                if revision > head_rev:
                    raise NotFoundError(
                        f"修订号 {revision} 超出当前范围：当前最新修订号为 {head_rev}"
                        + ("（尚无任何区域登记）" if head_rev == 0 else "")
                    )
                used_revision = revision
            else:
                used_revision = self.db._current_revision(conn)

            permit_head = self.db._current_permit_revision(conn)
            if pinned_permit:
                if permit_revision > permit_head:
                    raise NotFoundError(
                        f"许可修订号 {permit_revision} 超出当前范围："
                        f"当前最新全局许可修订号为 {permit_head}"
                    )
                used_permit_revision = permit_revision
            else:
                used_permit_revision = permit_head

            # 逐区取截至修订号的最后配置（未指定取最新）；未知区/该版本尚未登记明确拒绝
            zone_rows: Dict[str, Any] = {}
            for zid in zone_ids:
                if pinned_rev:
                    zrow = self.db._zone_row_at(conn, zid, revision)
                    if zrow is None:
                        raise NotFoundError(
                            f"修订号 {revision} 的配置中危险区尚未登记: {zid}"
                        )
                else:
                    zrow = self.db._latest_zone_row(conn, zid)
                    if zrow is None:
                        raise NotFoundError(f"危险区不存在: {zid}")
                zone_rows[zid] = zrow

            zone_results: List[Dict[str, Any]] = []
            for zid in zone_ids:
                # 实际占用：沿用既有插值/带洞边界/区域生效/半开裁剪规则
                occupancy = self._coverage_intervals(zone_rows[zid], points, lo, hi)
                # 仅核对该车次该区、在许可修订视图下仍有效的许可；重叠时段取并集。
                # 从不可变流水重放，撤销后的许可在撤销前旧视图中仍可见。
                permit_windows = self.db._active_permit_windows_at(
                    conn, trip_id, zid, used_permit_revision
                )
                permit_union = merge_windows(permit_windows)
                licensed = clip_intervals_halfopen(occupancy, permit_union)
                unlicensed = subtract_intervals_halfopen(occupancy, permit_union)
                unlicensed = [(s, e) for s, e in unlicensed if e - s > self.min_duration]

                occupied_sec = sum(e - s for s, e in occupancy)
                licensed_sec = sum(e - s for s, e in licensed)
                zone_results.append(
                    {
                        "zone_id": zid,
                        "occupied": bool(occupancy),
                        "occupied_duration_sec": round(occupied_sec, 6),
                        "licensed_duration_sec": round(licensed_sec, 6),
                        "unlicensed_duration_sec": round(
                            sum(e - s for s, e in unlicensed), 6
                        ),
                        "unlicensed_intervals": [
                            {
                                "start": _iso_z(datetime.fromtimestamp(s, timezone.utc)),
                                "end": _iso_z(datetime.fromtimestamp(e, timezone.utc)),
                                "duration_sec": round(e - s, 6),
                            }
                            for s, e in unlicensed
                        ],
                    }
                )

        return {
            "trip_id": trip_id,
            "since": _iso_z(datetime.fromtimestamp(lo, timezone.utc)),
            "until": _iso_z(datetime.fromtimestamp(hi, timezone.utc)),
            "point_count": len(points),
            "revision": used_revision,
            "track_version": track_version,
            "permit_revision": used_permit_revision,
            "zones": zone_results,
        }

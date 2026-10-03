"""作业车危险区通行许可测试。

- 登记：许可 ID + 车次 + 危险区 + UTC 半开时段；登记时危险区须存在，车次可尚无轨迹；
  同 ID 同内容幂等（200，不递增），异内容冲突（409），撤销后 ID 永久占用；
- 撤销：须提交所见全局许可修订号，过期/重复撤销拒绝（409），未登记 404；
  每次真实登记/撤销原子递增全局许可修订号并保留不可改历史；
- 复核：区域、轨迹、许可状态从同一已提交读视图读取；沿用插值/带洞/窗口规则，
  仅核对该车次该区许可，重叠时段取并集；按区返回无许可正时长区间、累计秒数与
  实际使用的区域修订号、轨迹版本、许可修订号（无许可为 0）；
  指定旧许可修订号复现补办/撤销前结果；非法输入与不存在资源明确报错。
"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _upload(client, trip, points):
    body = {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}
    r = client.post(f"/api/v1/trips/{trip}/points", json=body)
    assert r.status_code == 200, r.text
    return r


def _permit(client, permit_id, trip, zone, start, end):
    return client.post(
        "/api/v1/permits",
        json={
            "permit_id": permit_id,
            "trip_id": trip,
            "zone_id": zone,
            "start": start,
            "end": end,
        },
    )


# 10:00:00 自 x=-50 出发，10:01:20 到 x=150：x=0 在 10:00:20，x=100 在 10:01:00，
# 方块 [0,100]x[0,100] 内共 40 秒 [10:00:20, 10:01:00)
CROSS = [
    ("a", "2026-09-30T10:00:00Z", -50, 50),
    ("b", "2026-09-30T10:01:20Z", 150, 50),
]

P_START = "2026-09-30T10:00:20Z"
P_END = "2026-09-30T10:01:00Z"


def _review(client, trip, zones, **overrides):
    body = {
        "zone_ids": zones,
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T11:00:00Z",
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/permit-review", json=body)


# ---------------------------------------------------------------------
# 登记
# ---------------------------------------------------------------------

def test_register_permit_basic(client):
    register_square(client, zone_id="z1")
    r = _permit(client, "P1", "T-no-track", "z1", P_START, P_END)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["permit_id"] == "P1"
    assert body["trip_id"] == "T-no-track"
    assert body["zone_id"] == "z1"
    assert body["start"] == P_START
    assert body["end"] == P_END
    assert body["status"] == "active"
    assert body["revision"] == 1
    assert body["current_permit_revision"] == 1
    assert body["revoked_revision"] is None
    assert body["idempotent"] is False

    # 许可可读
    got = client.get("/api/v1/permits/P1").json()
    assert got["status"] == "active"
    assert got["current_permit_revision"] == 1


def test_register_requires_zone_exist(client):
    r = _permit(client, "P1", "T1", "ghost", P_START, P_END)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_register_trip_without_trajectory_allowed(client):
    """登记时车次可尚无轨迹（仅危险区须存在）。"""
    register_square(client, zone_id="z1")
    r = _permit(client, "P1", "future-trip", "z1", P_START, P_END)
    assert r.status_code == 201


def test_register_idempotent_same_content(client):
    register_square(client, zone_id="z1")
    r1 = _permit(client, "P1", "T1", "z1", P_START, P_END)
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1

    # 再登记一个不同许可，把全局号推进到 2
    r2 = _permit(client, "P2", "T1", "z1",
                 "2026-09-30T11:00:00Z", "2026-09-30T11:10:00Z")
    assert r2.status_code == 201 and r2.json()["revision"] == 2

    # 同 ID 同内容重传：200，不递增
    r3 = _permit(client, "P1", "T1", "z1", P_START, P_END)
    assert r3.status_code == 200
    body = r3.json()
    assert body["idempotent"] is True
    assert body["revision"] == 1
    assert body["current_permit_revision"] == 2


def test_register_conflict_different_content(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    # 时段不同
    r = _permit(client, "P1", "T1", "z1",
                "2026-09-30T10:00:21Z", P_END)
    assert r.status_code == 409
    assert r.json()["error"] == "conflict"
    # 车次不同
    r = _permit(client, "P1", "T2", "z1", P_START, P_END)
    assert r.status_code == 409
    # 危险区不同
    register_square(client, zone_id="z2",
                    exterior=[[200, 200], [300, 200], [300, 300], [200, 300]])
    r = _permit(client, "P1", "T1", "z2", P_START, P_END)
    assert r.status_code == 409


def test_register_invalid_window(client):
    register_square(client, zone_id="z1")
    r = _permit(client, "P1", "T1", "z1", P_END, P_START)
    assert r.status_code == 400
    # 朴素时间（无时区）拒绝
    r = _permit(client, "P2", "T1", "z1",
                "2026-09-30T10:00:00", P_END)
    assert r.status_code == 400
    # 非法 ISO
    r = _permit(client, "P3", "T1", "z1", "not-a-time", P_END)
    assert r.status_code == 400


def test_register_extra_field_rejected(client):
    register_square(client, zone_id="z1")
    r = client.post(
        "/api/v1/permits",
        json={
            "permit_id": "P1", "trip_id": "T1", "zone_id": "z1",
            "start": P_START, "end": P_END, "bogus": 1,
        },
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 撤销
# ---------------------------------------------------------------------

def test_revoke_permit(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    r = client.post("/api/v1/permits/P1/revoke", json={"revision": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["permit_id"] == "P1"
    assert body["status"] == "revoked"
    assert body["revision"] == 2
    assert body["current_permit_revision"] == 2

    got = client.get("/api/v1/permits/P1").json()
    assert got["status"] == "revoked"
    assert got["revision"] == 2
    assert got["revoked_revision"] == 2


def test_revoke_stale_revision(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    _permit(client, "P2", "T1", "z1",
            "2026-09-30T11:00:00Z", "2026-09-30T11:10:00Z")
    # 全局号已前进到 2，用所见号 1 撤销 P2 过期
    r = client.post("/api/v1/permits/P2/revoke", json={"revision": 1})
    assert r.status_code == 409
    assert "过期" in r.json()["detail"]
    # 许可仍有效
    assert client.get("/api/v1/permits/P2").json()["status"] == "active"


def test_revoke_duplicate_rejected(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    assert client.post("/api/v1/permits/P1/revoke", json={"revision": 1}).status_code == 200
    # 即使带上正确的新全局号，重复撤销也拒绝
    r = client.post("/api/v1/permits/P1/revoke", json={"revision": 2})
    assert r.status_code == 409
    assert "重复撤销" in r.json()["detail"]


def test_revoke_unknown_permit_404(client):
    r = client.post("/api/v1/permits/nope/revoke", json={"revision": 0})
    assert r.status_code == 404


def test_revoke_revision_validation(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    r = client.post("/api/v1/permits/P1/revoke", json={"revision": -1})
    assert r.status_code == 422
    r = client.post("/api/v1/permits/P1/revoke", json={"revision": "x"})
    assert r.status_code == 422


def test_revoked_id_cannot_be_reused(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    client.post("/api/v1/permits/P1/revoke", json={"revision": 1})
    # 同内容也不能再登记
    r = _permit(client, "P1", "T1", "z1", P_START, P_END)
    assert r.status_code == 409
    assert "不得重用" in r.json()["detail"]


def test_permit_revision_ledger(client):
    register_square(client, zone_id="z1")
    _permit(client, "P1", "T1", "z1", P_START, P_END)      # rev 1 register
    _permit(client, "P2", "T1", "z1",
            "2026-09-30T11:00:00Z", "2026-09-30T11:10:00Z")  # rev 2 register
    client.post("/api/v1/permits/P1/revoke", json={"revision": 2})  # rev 3 revoke
    # 幂等重传不留痕
    _permit(client, "P2", "T1", "z1",
            "2026-09-30T11:00:00Z", "2026-09-30T11:10:00Z")

    r = client.get("/api/v1/permit-revisions")
    body = r.json()
    assert body["current_permit_revision"] == 3
    revs = body["revisions"]
    # 倒序返回
    assert [x["revision"] for x in revs] == [3, 2, 1]
    assert revs[0]["action"] == "revoke" and revs[0]["permit_id"] == "P1"
    assert revs[1]["action"] == "register" and revs[1]["permit_id"] == "P2"
    assert revs[2]["action"] == "register" and revs[2]["permit_id"] == "P1"
    # revoke 行不带时段
    assert revs[0]["start"] is None and revs[0]["end"] is None


def test_permit_revision_independent_of_zone_revision(client):
    """许可修订号与区域修订号各自独立计数。"""
    register_square(client, zone_id="z1")  # 区域 rev 1
    r = _permit(client, "P1", "T1", "z1", P_START, P_END)  # 许可 rev 1
    assert r.json()["revision"] == 1
    register_square(client, zone_id="z1")  # 区域 rev 2（覆盖）
    # 许可全局号不受区域登记影响
    assert client.get("/api/v1/permit-revisions").json()["current_permit_revision"] == 1


# ---------------------------------------------------------------------
# 复核：基本覆盖/无许可/部分许可/并集
# ---------------------------------------------------------------------

def test_review_no_permit_when_none_registered(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    r = _review(client, "T1", ["z1"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["permit_revision"] == 0
    assert body["revision"] == 1
    assert body["track_version"] == 1
    z = body["zones"][0]
    assert z["zone_id"] == "z1"
    assert z["occupied"] is True
    assert z["occupied_duration_sec"] == 40.0
    assert z["licensed_duration_sec"] == 0
    assert z["unlicensed_duration_sec"] == 40.0
    assert len(z["unlicensed_intervals"]) == 1
    iv = z["unlicensed_intervals"][0]
    assert iv["start"] == "2026-09-30T10:00:20Z"
    assert iv["end"] == "2026-09-30T10:01:00Z"
    assert iv["duration_sec"] == 40.0


def test_review_fully_licensed(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    body = _review(client, "T1", ["z1"]).json()
    z = body["zones"][0]
    assert z["occupied_duration_sec"] == 40.0
    assert z["licensed_duration_sec"] == 40.0
    assert z["unlicensed_duration_sec"] == 0
    assert z["unlicensed_intervals"] == []
    assert body["permit_revision"] == 1


def test_review_partial_permit_splits_intervals(client):
    """许可只覆盖占用的中间一段：返回前后两段无许可区间。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    # 覆盖 [10:00:30, 10:00:50)
    _permit(client, "P1", "T1", "z1",
            "2026-09-30T10:00:30Z", "2026-09-30T10:00:50Z")
    z = _review(client, "T1", ["z1"]).json()["zones"][0]
    assert z["licensed_duration_sec"] == 20.0
    assert z["unlicensed_duration_sec"] == 20.0
    ivs = z["unlicensed_intervals"]
    assert [(i["start"], i["end"]) for i in ivs] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:30Z"),
        ("2026-09-30T10:00:50Z", "2026-09-30T10:01:00Z"),
    ]


def test_review_overlapping_permits_take_union(client):
    """多张重叠许可的覆盖时段取并集。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1",
            "2026-09-30T10:00:20Z", "2026-09-30T10:00:45Z")
    _permit(client, "P2", "T1", "z1",
            "2026-09-30T10:00:40Z", "2026-09-30T10:01:00Z")
    z = _review(client, "T1", ["z1"]).json()["zones"][0]
    assert z["licensed_duration_sec"] == 40.0
    assert z["unlicensed_duration_sec"] == 0
    assert z["unlicensed_intervals"] == []


def test_review_permits_for_other_trip_or_zone_ignored(client):
    """只核对该车次该危险区的许可：别的车次/别的区许可不抵用。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    # 别的车次
    _permit(client, "P1", "OTHER", "z1", P_START, P_END)
    # 别的危险区
    register_square(client, zone_id="z2",
                    exterior=[[200, 200], [300, 200], [300, 300], [200, 300]])
    _permit(client, "P2", "T1", "z2", P_START, P_END)
    body = _review(client, "T1", ["z1", "z2"]).json()
    by_zone = {z["zone_id"]: z for z in body["zones"]}
    assert by_zone["z1"]["unlicensed_duration_sec"] == 40.0
    # z2 无占用：空区间、零秒数
    assert by_zone["z2"]["occupied"] is False
    assert by_zone["z2"]["occupied_duration_sec"] == 0
    assert by_zone["z2"]["unlicensed_duration_sec"] == 0
    assert by_zone["z2"]["unlicensed_intervals"] == []


def test_review_permit_outside_query_window(client):
    """许可时段超出查询窗口时按半开求交裁剪。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1",
            "2026-09-30T09:00:00Z", "2026-09-30T10:00:40Z")
    z = _review(
        client, "T1", ["z1"],
        since="2026-09-30T10:00:30Z", until="2026-09-30T10:00:50Z",
    ).json()["zones"][0]
    # 查询窗口内占用 [30,50)，许可到 40：10 秒无许可
    assert z["occupied_duration_sec"] == 20.0
    assert z["licensed_duration_sec"] == 10.0
    assert z["unlicensed_duration_sec"] == 10.0
    assert z["unlicensed_intervals"][0]["start"] == "2026-09-30T10:00:40Z"
    assert z["unlicensed_intervals"][0]["end"] == "2026-09-30T10:00:50Z"


def test_review_no_occupancy_empty_intervals(client):
    """车次有轨迹但全程在区外：空区间、零秒数。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", [
        ("a", "2026-09-30T10:00:00Z", 200, 200),
        ("b", "2026-09-30T10:01:00Z", 300, 200),
    ])
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    z = _review(client, "T1", ["z1"]).json()["zones"][0]
    assert z["occupied"] is False
    assert z["occupied_duration_sec"] == 0
    assert z["licensed_duration_sec"] == 0
    assert z["unlicensed_duration_sec"] == 0
    assert z["unlicensed_intervals"] == []


def test_review_zone_window_rule_applies(client):
    """区域生效窗口规则沿用：许可不能覆盖区域未生效时的占用（此处根本不占用）。"""
    register_square(
        client, zone_id="z1",
        windows=[{"start": "2026-09-30T10:00:30Z", "end": "2026-09-30T10:00:50Z"}],
    )
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1", P_START, P_END)
    z = _review(client, "T1", ["z1"]).json()["zones"][0]
    # 实际占用只有区域生效的 [30,50)，被许可完全覆盖
    assert z["occupied_duration_sec"] == 20.0
    assert z["unlicensed_duration_sec"] == 0


# ---------------------------------------------------------------------
# 复核：历史复现（许可修订号 / 区域修订号 / 轨迹版本）
# ---------------------------------------------------------------------

def test_review_pin_permit_revision_zero_before_backfill(client):
    """指定旧许可修订号复现补办前结果：rev 0 时即使现在有许可也按无许可算。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1", P_START, P_END)  # 补办许可，许可 rev 1
    # 不指定：已覆盖
    assert _review(client, "T1", ["z1"]).json()["zones"][0]["unlicensed_duration_sec"] == 0
    # 指定 rev=0：复现补办前，全部无许可
    body = _review(client, "T1", ["z1"], permit_revision=0).json()
    assert body["permit_revision"] == 0
    assert body["zones"][0]["unlicensed_duration_sec"] == 40.0


def test_review_pin_old_permit_revision_after_new_registration(client):
    """旧许可修订号视图看不到后来登记的许可。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1",
            P_START, "2026-09-30T10:00:40Z")  # rev 1 覆盖前 20 秒
    _permit(client, "P2", "T1", "z1",
            "2026-09-30T10:00:40Z", P_END)    # rev 2 覆盖后 20 秒
    # 最新：全覆盖
    assert _review(client, "T1", ["z1"]).json()["zones"][0]["unlicensed_duration_sec"] == 0
    # rev 1：只有前 20 秒被覆盖
    z = _review(client, "T1", ["z1"], permit_revision=1).json()["zones"][0]
    assert z["licensed_duration_sec"] == 20.0
    assert z["unlicensed_duration_sec"] == 20.0
    ivs = z["unlicensed_intervals"]
    assert len(ivs) == 1
    assert ivs[0]["start"] == "2026-09-30T10:00:40Z"
    assert ivs[0]["end"] == P_END


def test_review_revoked_permit_excluded_after_revoke_but_visible_before(client):
    """撤销后许可不再覆盖；指定撤销前修订号可复现撤销前结果。"""
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    _permit(client, "P1", "T1", "z1", P_START, P_END)  # rev 1
    client.post("/api/v1/permits/P1/revoke", json={"revision": 1})  # rev 2
    # 最新视图：许可已撤销，全部无许可
    body = _review(client, "T1", ["z1"]).json()
    assert body["permit_revision"] == 2
    assert body["zones"][0]["unlicensed_duration_sec"] == 40.0
    # 指定 rev 1（撤销前）：许可仍有效，全覆盖
    body = _review(client, "T1", ["z1"], permit_revision=1).json()
    assert body["permit_revision"] == 1
    assert body["zones"][0]["licensed_duration_sec"] == 40.0
    assert body["zones"][0]["unlicensed_duration_sec"] == 0


def test_review_pin_all_three_versions(client):
    """同时指定区域修订号、轨迹版本、许可修订号。"""
    register_square(client, zone_id="z1")  # 区域 rev 1：全天方块
    _upload(client, "T1", CROSS)            # 轨迹 v1
    _permit(client, "P1", "T1", "z1",
            P_START, "2026-09-30T10:00:40Z")  # 许可 rev 1 覆盖 20 秒
    # 缩小区域（rev 2）：右半 [50,100]x[0,100]，占用变 20 秒 [40,60)
    register_square(
        client, zone_id="z1",
        exterior=[[50, 0], [100, 0], [100, 100], [50, 100]],
    )
    body = _review(
        client, "T1", ["z1"],
        revision=1, track_version=1, permit_revision=1,
    ).json()
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["permit_revision"] == 1
    z = body["zones"][0]
    # 旧区域占用 40 秒 [20,60)，旧许可覆盖 [20,40)
    assert z["occupied_duration_sec"] == 40.0
    assert z["licensed_duration_sec"] == 20.0
    assert z["unlicensed_duration_sec"] == 20.0


# ---------------------------------------------------------------------
# 复核：错误
# ---------------------------------------------------------------------

def test_review_unknown_trip_404(client):
    register_square(client, zone_id="z1")
    r = _review(client, "ghost", ["z1"])
    assert r.status_code == 404


def test_review_unknown_zone_404(client):
    _upload(client, "T1", CROSS)
    r = _review(client, "T1", ["ghost"])
    assert r.status_code == 404


def test_review_zone_not_registered_at_zone_revision_404(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    # rev 1 时只有 z1；z2 尚未登记
    register_square(client, zone_id="z2",
                    exterior=[[200, 200], [300, 200], [300, 300], [200, 300]])
    r = _review(client, "T1", ["z2"], revision=1)
    assert r.status_code == 404


def test_review_zone_revision_out_of_range_404(client):
    _upload(client, "T1", CROSS)
    r = _review(client, "T1", ["z1"], revision=99)
    assert r.status_code == 404


def test_review_track_version_out_of_range_404(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    r = _review(client, "T1", ["z1"], track_version=99)
    assert r.status_code == 404
    # 新上传车次最早版本为 1，0 不存在
    r = _review(client, "T1", ["z1"], track_version=0)
    assert r.status_code == 404


def test_review_permit_revision_out_of_range_404(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    r = _review(client, "T1", ["z1"], permit_revision=1)
    assert r.status_code == 404
    assert "许可修订号 1 超出当前范围" in r.json()["detail"]
    r = _review(client, "T1", ["z1"], permit_revision=-1)
    assert r.status_code == 422


def test_review_invalid_window_400(client):
    register_square(client, zone_id="z1")
    _upload(client, "T1", CROSS)
    r = _review(
        client, "T1", ["z1"],
        since="2026-09-30T11:00:00Z", until="2026-09-30T10:00:00Z",
    )
    assert r.status_code == 400
    r = _review(client, "T1", ["z1"], since="2026-09-30T11:00:00", until="2026-09-30T12:00:00Z")
    assert r.status_code == 400


def test_review_empty_or_duplicate_zones(client):
    _upload(client, "T1", CROSS)
    r = client.post(
        "/api/v1/trips/T1/permit-review",
        json={"zone_ids": [], "since": "2026-09-30T09:00:00Z",
              "until": "2026-09-30T11:00:00Z"},
    )
    assert r.status_code == 422  # pydantic min_length
    r = _review(client, "T1", ["z1", "z1"])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]


# ---------------------------------------------------------------------
# 持久化与并发
# ---------------------------------------------------------------------

def test_permits_persist_across_reopen(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.service import AuditService

    db_path = tmp_path / "p.db"
    settings = Settings()
    settings.db_path = db_path
    db = Database(settings)
    svc = AuditService(db)
    svc.register_zone({
        "zone_id": "z1", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS,
    })
    _code, body = svc.register_permit({
        "permit_id": "P1", "trip_id": "T1", "zone_id": "z1",
        "start": P_START, "end": P_END,
    })
    assert body["revision"] == 1
    db.close()

    db2 = Database(settings)
    svc2 = AuditService(db2)
    try:
        assert svc2.current_permit_revision() == 1
        got = svc2.get_permit("P1")
        assert got["status"] == "active"
        # 撤销后再开仍为 revoked，ID 仍不可重用
        svc2.revoke_permit("P1", {"revision": 1})
    finally:
        db2.close()

    db3 = Database(settings)
    svc3 = AuditService(db3)
    try:
        assert svc3.current_permit_revision() == 2
        assert svc3.get_permit("P1")["status"] == "revoked"
        from app.service import ConflictError
        try:
            svc3.register_permit({
                "permit_id": "P1", "trip_id": "T1", "zone_id": "z1",
                "start": P_START, "end": P_END,
            })
            assert False, "应冲突"
        except ConflictError:
            pass
    finally:
        db3.close()


def test_concurrent_permit_registrations_unique_monotonic_revisions():
    import threading
    from app.main import service

    # 先建一个区供许可引用
    service.register_zone({
        "zone_id": "zcon", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS,
    })
    errors = []

    def register(prefix: str, start: int) -> None:
        try:
            for i in range(start, start + 20):
                service.register_permit({
                    "permit_id": f"{prefix}-{i}",
                    "trip_id": "T1",
                    "zone_id": "zcon",
                    "start": "2026-09-30T10:00:00Z",
                    "end": "2026-09-30T10:00:30Z",
                })
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=register, args=("a", 0)),
        threading.Thread(target=register, args=("b", 100)),
        threading.Thread(target=register, args=("c", 200)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert service.current_permit_revision() == 60

"""事故后危险区内车次间接触传播追踪测试。

POST /api/v1/zones/{zone_id}/contact-tracing：以源车在查询起点已到达为前提，
沿用近距离相遇的插值、区域窗口与带洞边界规则，仅正时长且距离严格小于阈值的
相遇区间可传递；上游到达时刻严格早于相遇段终点时，下游最早可在该段起点与
上游到达时刻的较晚者到达，按不倒退的时间接续可多跳；返回每车最早可达时刻与
一条车次链（同刻优先跳数少，再按车次 ID 序列排序），未可达明确标记。
区域与全部车次点集从同一已提交读视图读取。
"""

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))
    assert r.status_code == 200, r.text
    return r


def _ask(client, zone_id, **overrides):
    body = {
        "source_trip_id": "S",
        "trip_ids": ["S", "A", "B", "C"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "max_distance": 20,
    }
    body.update(overrides)
    return client.post(f"/api/v1/zones/{zone_id}/contact-tracing", json=body)


# S 沿 y=30 以 1m/s 横穿：S(t)=(t,30)，t∈[0,100]（10:00:00 起）
TRACK_S = [
    ("a", "2026-09-30T10:00:00Z", 0, 30),
    ("b", "2026-09-30T10:01:40Z", 100, 30),
]
# A 沿 y=46 平行横穿：与 S 恒定相距 16m（<20），整段 [0,100) 相遇
TRACK_A = [
    ("a", "2026-09-30T10:00:00Z", 0, 46),
    ("b", "2026-09-30T10:01:40Z", 100, 46),
]
# B 静止在 (50,62)：A(t)=(t,46) 与其距离 sqrt((t-50)^2+256)<20 → t∈(38,62)
TRACK_B = [
    ("a", "2026-09-30T10:00:00Z", 50, 62),
    ("b", "2026-09-30T10:05:00Z", 50, 62),
]
# C 沿 y=95 横穿：与各车最近距离 ≥33m，阈值 20 下不与任何车相遇
TRACK_C = [
    ("a", "2026-09-30T10:00:00Z", 0, 95),
    ("b", "2026-09-30T10:01:40Z", 100, 95),
]


def _results_map(body):
    return {r["trip_id"]: r for r in body["results"]}


# ---------------------------------------------------------------------
# 基本传播语义
# ---------------------------------------------------------------------

def test_basic_multihop_tracing(client):
    """S 在起点已到达 → A 起点可达（1 跳）→ B 在 10:00:38 经两跳可达；C 不可达。"""
    register_square(client, zone_id="ct1")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    _upload(client, "B", TRACK_B)
    _upload(client, "C", TRACK_C)

    r = _ask(client, "ct1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone_id"] == "ct1"
    assert body["source_trip_id"] == "S"
    assert body["since"] == "2026-09-30T10:00:00Z"
    assert body["until"] == "2026-09-30T10:05:00Z"
    assert body["max_distance"] == 20.0
    assert "revision" not in body  # 未指定修订号：不出现

    # trips 回填实际使用的逐车版本与点数（请求顺序）
    trips = {t["trip_id"]: t for t in body["trips"]}
    assert [t["trip_id"] for t in body["trips"]] == ["S", "A", "B", "C"]
    assert all(trips[t]["track_version"] == 1 and trips[t]["point_count"] == 2
               for t in ("S", "A", "B", "C"))

    results = _results_map(body)
    # 结果按请求车次顺序排列
    assert [r2["trip_id"] for r2 in body["results"]] == ["S", "A", "B", "C"]

    s = results["S"]
    assert s["reachable"] is True
    assert s["earliest_arrival_at"] == "2026-09-30T10:00:00Z"
    assert s["hops"] == 0
    assert s["chain"] == ["S"]

    a = results["A"]
    assert a["reachable"] is True
    assert a["earliest_arrival_at"] == "2026-09-30T10:00:00Z"
    assert a["hops"] == 1
    assert a["chain"] == ["S", "A"]

    b = results["B"]
    assert b["reachable"] is True
    # S→A 起点可达（早于 B 相遇段终点 62s），B 最早 max(相遇起点 38s, 0)=38s
    assert b["earliest_arrival_at"] == "2026-09-30T10:00:38Z"
    assert b["hops"] == 2
    assert b["chain"] == ["S", "A", "B"]

    c = results["C"]
    assert c["reachable"] is False
    assert c["earliest_arrival_at"] is None
    assert c["hops"] is None
    assert c["chain"] is None


def test_source_is_reachable_even_without_any_encounter(client):
    """即使源车与任何车都不相遇，源车自身仍在起点可达，其余车不可达。"""
    register_square(client, zone_id="ct2")
    _upload(client, "S", TRACK_S)
    _upload(client, "C", TRACK_C)
    r = _ask(client, "ct2", trip_ids=["S", "C"])
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    assert results["S"] == {
        "trip_id": "S", "reachable": True,
        "earliest_arrival_at": "2026-09-30T10:00:00Z",
        "hops": 0, "chain": ["S"],
    }
    assert results["C"]["reachable"] is False


def test_arrival_never_goes_backwards(client):
    """上游在相遇段开始之后才到达：下游到达时刻取上游到达时刻（时间不倒退）。"""
    register_square(client, zone_id="ct3")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    # B 静止在 (80,62)：与 A(t)=(t,46) 相遇 t∈(68,92)，段起点 68s
    _upload(client, "B", [
        ("a", "2026-09-30T10:00:00Z", 80, 62),
        ("b", "2026-09-30T10:05:00Z", 80, 62),
    ])
    # 查询起点 10:01:10（t=70s）：S 此时才到达，A 同刻可达，
    # B 相遇段 [68,92)，下游最早 max(68,70)=70s
    r = _ask(client, "ct3", trip_ids=["S", "A", "B"],
             since="2026-09-30T10:01:10Z")
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    assert results["A"]["earliest_arrival_at"] == "2026-09-30T10:01:10Z"
    assert results["B"]["earliest_arrival_at"] == "2026-09-30T10:01:10Z"
    assert results["B"]["chain"] == ["S", "A", "B"]


def test_arrival_exactly_at_encounter_end_not_transitive(client):
    """上游到达时刻恰等于相遇段终点：严格早于不成立，不能向该下游传递。"""
    register_square(client, zone_id="ct4")
    # S 静止 (70,30)：与 A(t)=(t,46) 相遇 |t-70|<12 → t∈(58,82)，A 最早 58s
    _upload(client, "S", [
        ("a", "2026-09-30T10:00:00Z", 70, 30),
        ("b", "2026-09-30T10:05:00Z", 70, 30),
    ])
    _upload(client, "A", TRACK_A)
    # D 静止 (46,62)：与 A 相遇 |t-46|<12 → t∈(34,58)，终点恰为 58s；
    # S-D 恒定相距 sqrt(24^2+32^2)=40m，无直接相遇
    _upload(client, "D", [
        ("a", "2026-09-30T10:00:00Z", 46, 62),
        ("b", "2026-09-30T10:05:00Z", 46, 62),
    ])
    r = _ask(client, "ct4", trip_ids=["S", "A", "D"])
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    assert results["A"]["reachable"] is True
    assert results["A"]["earliest_arrival_at"] == "2026-09-30T10:00:58Z"
    assert results["D"]["reachable"] is False
    assert results["D"]["earliest_arrival_at"] is None


def test_same_time_prefers_fewer_hops(client):
    """同刻可达：优先跳数少的直达链，而非经中间车同刻到达的两跳链。"""
    register_square(client, zone_id="ct5")
    _upload(client, "S", TRACK_S)
    # X 与 Z 同位置静止在 (30,30)：S(t)=(t,30) 与二者均在 t∈(10,50) 相遇，
    # X、Z 之间全程贴近。Z 既被 S 直达（1 跳，10s），也可经 X 两跳同刻（10s）到达。
    static = [
        ("a", "2026-09-30T10:00:00Z", 30, 30),
        ("b", "2026-09-30T10:05:00Z", 30, 30),
    ]
    _upload(client, "X", [("X-" + p, ts, x, y) for p, ts, x, y in static])
    _upload(client, "Z", [("Z-" + p, ts, x, y) for p, ts, x, y in static])
    r = _ask(client, "ct5", trip_ids=["S", "X", "Z"])
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    x = results["X"]
    assert x["reachable"] is True
    assert x["earliest_arrival_at"] == "2026-09-30T10:00:10Z"
    z = results["Z"]
    assert z["reachable"] is True
    assert z["earliest_arrival_at"] == "2026-09-30T10:00:10Z"
    assert z["hops"] == 1
    assert z["chain"] == ["S", "Z"]


def test_same_time_and_hops_prefers_lexicographic_chain(client):
    """同刻同跳数存在多条链：按车次 ID 序列字典序取最优，与请求顺序无关。"""
    register_square(client, zone_id="ct6")
    # 全部静止：S(50,30)、m2(52,46)、m1(50,46)、Z(50,62)
    # S-m1=16m、S-m2≈16.12m、m1-Z=16m、m2-Z≈16.12m 均 <20；S-Z=32m 不相遇
    static = [
        ("a", "2026-09-30T10:00:00Z"),
        ("b", "2026-09-30T10:05:00Z"),
    ]
    _upload(client, "S", [(p, ts, 50, 30) for p, ts in static])
    _upload(client, "m2", [(p, ts, 52, 46) for p, ts in static])
    _upload(client, "m1", [(p, ts, 50, 46) for p, ts in static])
    _upload(client, "Z", [(p, ts, 50, 62) for p, ts in static])
    # 请求顺序故意把 m2 放在 m1 前
    r = _ask(client, "ct6", trip_ids=["S", "m2", "m1", "Z"])
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    z = results["Z"]
    assert z["reachable"] is True
    assert z["earliest_arrival_at"] == "2026-09-30T10:00:00Z"
    assert z["hops"] == 2
    assert z["chain"] == ["S", "m1", "Z"]  # m1 < m2，字典序优先


def test_reachable_only_via_multihop_chain(client):
    """没有直达相遇时，只能经中间车两跳到达。"""
    register_square(client, zone_id="ct7")
    # S 静止 (10,30)
    _upload(client, "S", [
        ("a", "2026-09-30T10:00:00Z", 10, 30),
        ("b", "2026-09-30T10:05:00Z", 10, 30),
    ])
    # i 静止 (10,46)：与 S 相距 16m，起点即相遇
    _upload(client, "i", [
        ("a", "2026-09-30T10:00:00Z", 10, 46),
        ("b", "2026-09-30T10:05:00Z", 10, 46),
    ])
    # d 静止 (20,62)：i-d=sqrt(10^2+16^2)≈18.87m（起点相遇）；
    # S-d=sqrt(10^2+32^2)≈33.5m，全程不相遇 → 只能经 i 两跳
    _upload(client, "d", [
        ("a", "2026-09-30T10:00:00Z", 20, 62),
        ("b", "2026-09-30T10:05:00Z", 20, 62),
    ])
    r = _ask(client, "ct7", trip_ids=["S", "i", "d"])
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    assert results["d"] == {
        "trip_id": "d", "reachable": True,
        "earliest_arrival_at": "2026-09-30T10:00:00Z",
        "hops": 2, "chain": ["S", "i", "d"],
    }


def test_threshold_boundary_strictly_less(client):
    """恒定距离恰等于阈值不产生相遇：下游不可达；阈值略大即可达。"""
    register_square(client, zone_id="ct8")
    _upload(client, "S", TRACK_S)
    # A 沿 y=50：与 S 恒定相距 20m
    _upload(client, "A", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),
    ])
    r = _ask(client, "ct8", trip_ids=["S", "A"], max_distance=20)
    results = _results_map(r.json())
    assert results["A"]["reachable"] is False

    r = _ask(client, "ct8", trip_ids=["S", "A"], max_distance=20.000001)
    results = _results_map(r.json())
    assert results["A"]["reachable"] is True
    assert results["A"]["chain"] == ["S", "A"]


def test_hole_and_zone_window_rules_reused(client):
    """带洞边界与区域窗口规则沿用：洞内相遇不可传递，洞外相遇段仍可传递。"""
    hole = [[[40, 40], [60, 40], [60, 80], [40, 80]]]  # x∈[40,60]、y∈[40,80] 的洞
    register_square(client, zone_id="ct9", holes=hole)
    _upload(client, "S", TRACK_S)   # y=30，全程在区
    # A 沿 y=50 横穿：t∈[40,60] 穿洞，与 S 相距 20m；阈值 25 下仅洞外两段相遇
    _upload(client, "A", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),
    ])
    r = _ask(client, "ct9", trip_ids=["S", "A"], max_distance=25)
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    # A 在第一段相遇（t∈[0,40)）起点即可达
    assert results["A"]["reachable"] is True
    assert results["A"]["earliest_arrival_at"] == "2026-09-30T10:00:00Z"

    # 查询窗口只覆盖洞内时段：相遇被洞完全剔除，A 不可达
    r = _ask(client, "ct9", trip_ids=["S", "A"], max_distance=25,
             since="2026-09-30T10:00:40Z", until="2026-09-30T10:01:00Z")
    results = _results_map(r.json())
    assert results["S"]["reachable"] is True
    assert results["A"]["reachable"] is False


# ---------------------------------------------------------------------
# 区域修订号与逐车轨迹版本（同一读视图）
# ---------------------------------------------------------------------

def test_pinned_track_version_reproduces(client):
    """逐车轨迹版本：按指定版本点集核算传播链；未指定的车取最新状态。"""
    register_square(client, zone_id="ct10")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    _upload(client, "B", TRACK_B)
    # 更正 A：整体平移到 y=90 → 与 S、B 都不再近距离相遇
    r = client.post(
        "/api/v1/trips/A/corrections",
        json={
            "base_version": 1,
            "reason": "A 轨迹录错",
            "items": [
                {"action": "update", "point_id": "a",
                 "ts": "2026-09-30T10:00:00Z", "x": 0, "y": 90},
                {"action": "update", "point_id": "b",
                 "ts": "2026-09-30T10:01:40Z", "x": 100, "y": 90},
            ],
        },
    )
    assert r.status_code == 200

    r = _ask(client, "ct10", trip_ids=["S", "A", "B"])
    results = _results_map(r.json())
    assert results["A"]["reachable"] is False
    assert results["B"]["reachable"] is False

    # 指定 A@1：旧点集下 A 起点可达、B 经两跳在 38s 可达
    r = _ask(client, "ct10", trip_ids=["S", "A", "B"], track_versions={"A": 1})
    assert r.status_code == 200, r.text
    results = _results_map(r.json())
    assert results["A"]["earliest_arrival_at"] == "2026-09-30T10:00:00Z"
    assert results["B"]["earliest_arrival_at"] == "2026-09-30T10:00:38Z"
    assert results["B"]["chain"] == ["S", "A", "B"]
    trips = {t["trip_id"]: t for t in r.json()["trips"]}
    assert trips["A"]["track_version"] == 1
    assert trips["S"]["track_version"] == 1


def test_pinned_revision_reproduces_zone_config(client):
    """区域修订号：按截至该号的区域配置核算并回填 revision。"""
    register_square(client, zone_id="ct11")  # rev1 大方块
    register_square(client, zone_id="ct11",
                    exterior=[[0, 0], [30, 0], [30, 30], [0, 30]])  # rev2 小区：车都在外
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)

    r = _ask(client, "ct11", trip_ids=["S", "A"])
    assert "revision" not in r.json()
    assert _results_map(r.json())["A"]["reachable"] is False

    r = _ask(client, "ct11", trip_ids=["S", "A"], revision=1)
    body = r.json()
    assert body["revision"] == 1
    results = _results_map(body)
    assert results["A"]["reachable"] is True
    assert results["A"]["chain"] == ["S", "A"]


# ---------------------------------------------------------------------
# 错误：区域 / 车次 / 版本 / 源车
# ---------------------------------------------------------------------

def test_zone_not_found(client):
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    r = _ask(client, "ghost-zone", trip_ids=["S", "A"])
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_zone_not_registered_at_revision(client):
    register_square(client, zone_id="ct-z")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    r = _ask(client, "ct-z", trip_ids=["S", "A"], revision=999)
    assert r.status_code == 404
    assert "超出当前范围" in r.json()["detail"]
    register_square(client, zone_id="other")  # rev2
    r = _ask(client, "other", trip_ids=["S", "A"], revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_trip_not_found(client):
    register_square(client, zone_id="ct-t")
    _upload(client, "S", TRACK_S)
    r = _ask(client, "ct-t", trip_ids=["S", "ghost"])
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]
    r = _ask(client, "ct-t", trip_ids=["S", "ghost"], track_versions={"ghost": 1})
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_track_version_out_of_range(client):
    register_square(client, zone_id="ct-v")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    r = _ask(client, "ct-v", trip_ids=["S", "A"], track_versions={"S": 0})
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]
    r = _ask(client, "ct-v", trip_ids=["S", "A"], track_versions={"A": 9})
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]


def test_source_must_be_among_trips(client):
    register_square(client, zone_id="ct-src")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    _upload(client, "B", TRACK_B)
    r = _ask(client, "ct-src", source_trip_id="S", trip_ids=["A", "B"])
    assert r.status_code == 400
    assert "source_trip_id" in r.json()["detail"]


# ---------------------------------------------------------------------
# 错误：非法请求
# ---------------------------------------------------------------------

def test_duplicate_trip_ids_rejected(client):
    register_square(client, zone_id="ct-dup")
    _upload(client, "S", TRACK_S)
    r = _ask(client, "ct-dup", trip_ids=["S", "S"])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]


def test_invalid_max_distance_rejected(client):
    register_square(client, zone_id="ct-d")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    for bad in (0, -5, "NaN", "Infinity", "-Infinity"):
        r = _ask(client, "ct-d", trip_ids=["S", "A"], max_distance=bad)
        assert r.status_code in (400, 422), bad
    r = client.post("/api/v1/zones/ct-d/contact-tracing", json={
        "source_trip_id": "S",
        "trip_ids": ["S", "A"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
    })
    assert r.status_code == 422  # 缺 max_distance


def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="ct-r")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    r = _ask(client, "ct-r", trip_ids=["S", "A"],
             since="2026-09-30T10:02:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    r = _ask(client, "ct-r", trip_ids=["S", "A"],
             since="2026-09-30T10:00:00", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]


def test_invalid_request_shape_rejected(client):
    register_square(client, zone_id="ct-s")
    # 车次不足两辆
    r = _ask(client, "ct-s", trip_ids=["S"])
    assert r.status_code in (400, 422)
    # 缺字段
    r = client.post("/api/v1/zones/ct-s/contact-tracing",
                    json={"trip_ids": ["S", "A"]})
    assert r.status_code == 422
    # 多余字段
    r = _ask(client, "ct-s", trip_ids=["S", "A"], bogus=1)
    assert r.status_code == 422
    # 空源车
    r = _ask(client, "ct-s", trip_ids=["S", "A"], source_trip_id="")
    assert r.status_code == 422


def test_invalid_track_versions_rejected(client):
    register_square(client, zone_id="ct-tv")
    _upload(client, "S", TRACK_S)
    _upload(client, "A", TRACK_A)
    r = _ask(client, "ct-tv", trip_ids=["S", "A"], track_versions={"S": -1})
    assert r.status_code in (400, 422)
    r = _ask(client, "ct-tv", trip_ids=["S", "A"], track_versions={"S": "abc"})
    assert r.status_code in (400, 422)
    r = _ask(client, "ct-tv", trip_ids=["S", "A"], track_versions={"T9": 1})
    assert r.status_code == 400
    assert "T9" in r.json()["detail"]

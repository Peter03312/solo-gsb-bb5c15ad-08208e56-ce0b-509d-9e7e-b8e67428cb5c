"""事故后危险区内车次间接触传播追踪测试。

POST /api/v1/zones/{zone_id}/contact-tracing：区域与全部车次点集从同一已提交读视图
读取；仅正时长且距离严格小于阈值的相遇区间（沿用近距离相遇的插值、区域窗口与
带洞边界规则）可传递；源车在查询起点已到达，上游到达时刻早于相遇段终点时
下游最早可在该段起点与上游到达时刻的较晚者到达，按不倒退的时间多跳接续；
返回每车最早可达时刻与一条车次链，同刻优先跳数少、再按车次 ID 序列排序，
未可达明确标记。
"""
import threading

from tests.conftest import DAY_WINDOWS, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))
    assert r.status_code == 200, r.text
    return r


def _ask(client, zone_id, **overrides):
    body = {
        "source_trip_id": "S",
        "trip_ids": ["S", "A", "B"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "max_distance": 25,
    }
    body.update(overrides)
    return client.post(f"/api/v1/zones/{zone_id}/contact-tracing", json=body)


def _result_map(body):
    return {r["trip_id"]: r for r in body["results"]}


# 基础轨迹（t 为 10:00:00 起秒数，方形区 [0,100]² 全天，阈值 25）：
#   S：(t,50) 以 1m/s 横穿，t∈[0,100]
#   A：(t,65) 同时横穿，与 S 恒定相距 15m；S-A 相遇 [0,100)
S_TRACK = [
    ("s1", "2026-09-30T10:00:00Z", 0, 50),
    ("s2", "2026-09-30T10:01:40Z", 100, 50),
]
A_TRACK = [
    ("a1", "2026-09-30T10:00:00Z", 0, 65),
    ("a2", "2026-09-30T10:01:40Z", 100, 65),
]
# B 静止 (85,80)：与 S 纵向相距 30m 不相遇；A 经过时 dy=15，
# sqrt((t-85)^2+225)<25 → t∈(65,100) 相遇 → B 只能经 S→A→B 在 t=65 到达
B_PARKED_FAR = [
    ("b1", "2026-09-30T09:55:00Z", 85, 80),
    ("b2", "2026-09-30T10:10:00Z", 85, 80),
]


# ---------------------------------------------------------------------
# 基本语义：单跳与多跳时间接续
# ---------------------------------------------------------------------

def test_basic_multi_hop_arrival(client):
    register_square(client, zone_id="ct1")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    _upload(client, "B", B_PARKED_FAR)

    r = _ask(client, "ct1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone_id"] == "ct1"
    assert body["source_trip_id"] == "S"
    assert body["since"] == "2026-09-30T10:00:00Z"
    assert body["until"] == "2026-09-30T10:05:00Z"
    assert body["max_distance"] == 25.0
    assert "revision" not in body
    assert [t["trip_id"] for t in body["trips"]] == ["S", "A", "B"]
    assert all(t["track_version"] == 1 and t["point_count"] == 2 for t in body["trips"])

    results = _result_map(body)
    # 结果按请求车次顺序
    assert [row["trip_id"] for row in body["results"]] == ["S", "A", "B"]
    # 源车：查询起点即到达，0 跳，链只含自己
    assert results["S"] == {
        "trip_id": "S", "reachable": True,
        "arrived_at": "2026-09-30T10:00:00Z", "hops": 0, "chain": ["S"],
    }
    # A：S-A 段 [0,100) 起点即接续 → 10:00:00，1 跳
    assert results["A"]["reachable"] is True
    assert results["A"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["A"]["hops"] == 1
    assert results["A"]["chain"] == ["S", "A"]
    # B：与 S 不相遇，只能多跳，S→A（0 到达）→B 段起点 65 → 10:01:05，2 跳
    assert results["B"]["reachable"] is True
    assert results["B"]["arrived_at"] == "2026-09-30T10:01:05Z"
    assert results["B"]["hops"] == 2
    assert results["B"]["chain"] == ["S", "A", "B"]


def test_unreachable_marked(client):
    """无任何可达路径的车次明确标记 reachable=false、时刻与链为 null。"""
    register_square(client, zone_id="ct2")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    # C 静止 (85,99)：与 A 最近 34m、与 S 更远，谁都不相遇
    _upload(client, "C", [
        ("c1", "2026-09-30T09:55:00Z", 85, 99),
        ("c2", "2026-09-30T10:10:00Z", 85, 99),
    ])
    r = _ask(client, "ct2", trip_ids=["S", "A", "C"])
    assert r.status_code == 200, r.text
    assert _result_map(r.json())["C"] == {
        "trip_id": "C", "reachable": False, "arrived_at": None, "hops": None, "chain": None,
    }


def test_direct_edge_beats_multihop(client):
    """B 与 S 直接相遇且更早：1 跳直达优于经由 A 的 2 跳链。"""
    register_square(client, zone_id="ct3")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    # B 静止 (85,50)：S-B 同 y，相遇 |t-85|<25 → t∈(60,100)（直达 1 跳，60 起）；
    # A-B 距 sqrt((t-85)^2+225)<25 → t∈(65,100)（经 A 两跳，65 起，更晚）
    _upload(client, "B", [
        ("b1", "2026-09-30T09:55:00Z", 85, 50),
        ("b2", "2026-09-30T10:10:00Z", 85, 50),
    ])
    r = _ask(client, "ct3")
    results = _result_map(r.json())
    assert results["B"]["arrived_at"] == "2026-09-30T10:01:00Z"  # 直达段起点 60
    assert results["B"]["hops"] == 1
    assert results["B"]["chain"] == ["S", "B"]


def test_non_regressing_time(client):
    """上游到达时刻晚于相遇段起点：下游到达时刻 = max(段起点, 上游到达)，时间不倒退。"""
    register_square(client, zone_id="ct4")
    # A 静止 (10,65)：S 距 A <25 当 (t-10)^2<400（y 差 15 已含）→ t∈[0,30)，
    # S 在 t=0 即到达 A（段起点）
    _upload(client, "S", S_TRACK)
    _upload(client, "A", [
        ("a1", "2026-09-30T09:55:00Z", 10, 65),
        ("a2", "2026-09-30T10:10:00Z", 10, 65),
    ])
    # B 静止 (10,80)：与 A 相距 15m 全程相遇（段起点 0）；S-B 距离
    # sqrt(t^2 近似 (t-10)^2+900)：最近 t=10 时 sqrt(900)=30>25，不相遇。
    # 窗口起点 S→A→B：max(0,0)=0 → B 在 10:00:00 到达。
    # 非倒退由“查询起点推迟到 10:00:10”验证：S 10 到达、A 10 到达（S-A 裁剪段起点 10），
    # A-B 段起点 0，max(0,10)=10 → B 也在 10:00:10 到达而非倒退到 0。
    _upload(client, "B", [
        ("b1", "2026-09-30T09:55:00Z", 10, 80),
        ("b2", "2026-09-30T10:10:00Z", 10, 80),
    ])
    r = _ask(client, "ct4")
    assert r.status_code == 200, r.text
    results = _result_map(r.json())
    assert results["A"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["B"]["reachable"] is True
    assert results["B"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["B"]["chain"] == ["S", "A", "B"]

    r2 = _ask(
        client, "ct4",
        since="2026-09-30T10:00:10Z", until="2026-09-30T10:05:00Z",
    )
    results2 = _result_map(r2.json())
    assert results2["A"]["arrived_at"] == "2026-09-30T10:00:10Z"
    assert results2["B"]["arrived_at"] == "2026-09-30T10:00:10Z"


def test_skip_earlier_ended_interval_and_strict_end(client):
    """上游到达晚于首个相遇段终点时跳过该段、续接后段；恰在某段终点（含被窗口裁掉）
    到达则该段不可传递；段起点早于上游到达时时间不倒退（取二者较晚者）。"""
    register_square(client, zone_id="ct5")
    # A 静止 (50,65)：S-A 距离 sqrt((t-50)^2+225)<25 → t∈(30,70)，S 在 t=30 到达 A
    _upload(client, "S", S_TRACK)
    _upload(client, "A", [
        ("a1", "2026-09-30T09:55:00Z", 50, 65),
        ("a2", "2026-09-30T10:10:00Z", 50, 65),
    ])
    # B 始终在 x=50 上运动，与 S 纵向距离恒 >=30m，故 S-B 从不直接相遇：
    #   [0,10] 停 (50,80)：与 A 相距 15m，相遇段 [0,15)（含驶离的过渡）；
    #   [10,20] 以 2m/s 北上 (50,80)→(50,100)：t=15 时距 A 恰 25m，之后超出；
    #   [20,40] 停 (50,100)：相距 35m 的空档；
    #   [40,50] 以 2m/s 南返 (50,100)→(50,80)：t=45 起相距重新 <25；
    #   [50,300] 停 (50,80)：第二段 (45,300)。
    _upload(client, "B", [
        ("b1", "2026-09-30T09:55:00Z", 50, 80),
        ("b2", "2026-09-30T10:00:10Z", 50, 80),
        ("b3", "2026-09-30T10:00:20Z", 50, 100),
        ("b4", "2026-09-30T10:00:40Z", 50, 100),
        ("b5", "2026-09-30T10:00:50Z", 50, 80),
        ("b6", "2026-09-30T10:05:00Z", 50, 80),
    ])
    # 默认窗口（源 t=0 到达）：A 在 t=30 到达；首段 [0,15) 终点 15 不晚于 30 被跳过，
    # 续接第二段起点 45 → B 在 10:00:45 到达
    r = _ask(client, "ct5")
    assert r.status_code == 200, r.text
    results = _result_map(r.json())
    assert results["A"]["arrived_at"] == "2026-09-30T10:00:30Z"
    assert results["B"]["reachable"] is True
    assert results["B"]["arrived_at"] == "2026-09-30T10:00:45Z"
    assert results["B"]["chain"] == ["S", "A", "B"]

    # 窗口起点 10：S-A 裁剪为 (30,70)，A 仍 30 到达；A-B 首段裁剪为 (10,15)，
    # 终点 15 早于 30 仍被跳过 → B 仍为 45
    r2 = _ask(
        client, "ct5",
        since="2026-09-30T10:00:10Z", until="2026-09-30T10:05:00Z",
    )
    results2 = _result_map(r2.json())
    assert results2["A"]["arrived_at"] == "2026-09-30T10:00:30Z"
    assert results2["B"]["arrived_at"] == "2026-09-30T10:00:45Z"

    # 窗口起点 50：源车 50 到达，S-A 裁剪段起点 50 → A 50 到达；第二段起点 45 早于
    # 50，max(45,50)=50（时间不倒退）→ B 50 到达
    r3 = _ask(
        client, "ct5",
        since="2026-09-30T10:00:50Z", until="2026-09-30T10:05:00Z",
    )
    results3 = _result_map(r3.json())
    assert results3["A"]["arrived_at"] == "2026-09-30T10:00:50Z"
    assert results3["B"]["arrived_at"] == "2026-09-30T10:00:50Z"

    # 窗口起点 70：S-A 段 (30,70) 被半开窗口裁尽（恰在终点 70 到达不满足严格早于）
    # → A、B 均不可达
    r4 = _ask(
        client, "ct5",
        since="2026-09-30T10:01:10Z", until="2026-09-30T10:05:00Z",
    )
    results4 = _result_map(r4.json())
    assert results4["A"]["reachable"] is False
    assert results4["B"]["reachable"] is False


def test_intervals_clipped_to_query_window(client):
    """相遇段被查询半开范围裁剪：窗口内仍可在窗口起点接续；窗口晚于全部相遇段则不可达。"""
    register_square(client, zone_id="ct6")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    _upload(client, "B", B_PARKED_FAR)  # A-B 相遇 (65,100)
    # 窗口只保留 t∈[80,90)：S-A、A-B 裁剪后段起点均为 80
    r = _ask(
        client, "ct6",
        since="2026-09-30T10:01:20Z", until="2026-09-30T10:01:30Z",
    )
    results = _result_map(r.json())
    assert results["A"]["arrived_at"] == "2026-09-30T10:01:20Z"
    assert results["B"]["arrived_at"] == "2026-09-30T10:01:20Z"
    # 窗口晚于全部相遇段：无人可达
    r = _ask(
        client, "ct6",
        since="2026-09-30T10:02:00Z", until="2026-09-30T10:05:00Z",
    )
    results = _result_map(r.json())
    assert results["A"]["reachable"] is False
    assert results["B"]["reachable"] is False


# ---------------------------------------------------------------------
# 同刻决胜：跳数少，再按车次 ID 序列排序
# ---------------------------------------------------------------------

def test_tie_prefer_fewer_hops(client):
    """同一到达时刻存在 1 跳与 2 跳链：取跳数少的。"""
    register_square(client, zone_id="ct7")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    # C 静止 (5,65)：与 S 距 <25 当 (t-5)^2<400 → t∈[0,25)（1 跳，起点到达）；
    # 与 A 相距 20m 全程相遇，S→A→C 同为 2 跳起点到达 —— 1 跳链取胜。
    _upload(client, "C", [
        ("c1", "2026-09-30T09:55:00Z", 5, 65),
        ("c2", "2026-09-30T10:10:00Z", 5, 65),
    ])
    r = _ask(client, "ct7", trip_ids=["S", "A", "C"])
    results = _result_map(r.json())
    assert results["C"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["C"]["hops"] == 1
    assert results["C"]["chain"] == ["S", "C"]


def test_tie_same_hops_lexicographic_chain(client):
    """同一到达时刻、同样跳数：按车次 ID 序列字典序取较小链。"""
    register_square(client, zone_id="ct8")
    # S：(t,50)；A：(t,80)（距 S 30m，阈值 35 时相遇 [0,100)）
    _upload(client, "S", S_TRACK)
    _upload(client, "A", [
        ("a1", "2026-09-30T10:00:00Z", 0, 80),
        ("a2", "2026-09-30T10:01:40Z", 100, 80),
    ])
    # B：(t,55)（距 S 5m、距 A 25m，阈值 35 均相遇）
    _upload(client, "B", [
        ("b1", "2026-09-30T10:00:00Z", 0, 55),
        ("b2", "2026-09-30T10:01:40Z", 100, 55),
    ])
    # C 静止 (5,90)：与 S 最近 sqrt(25+1600)>40 不相遇（阈值 35）；
    # 与 A 相距 10m、与 B 相距 sqrt(0+35^2)=35m（严格小于 35 不成立）→ 只能经 A，
    # 为构造同刻两链，改用 C 静止 (5,88)：与 A 距 8m、与 B 距 sqrt(9+33^2)≈33.14<35，
    # 与 S 距 sqrt(9+38^2)≈38.1 不相遇；S→A、S→B 均在 0 到达 → C 两条 2 跳链同刻，
    # 字典序 [S,A,C] < [S,B,C]。
    _upload(client, "C", [
        ("c1", "2026-09-30T09:55:00Z", 5, 88),
        ("c2", "2026-09-30T10:10:00Z", 5, 88),
    ])
    r = _ask(client, "ct8", trip_ids=["S", "A", "B", "C"], max_distance=35)
    assert r.status_code == 200, r.text
    results = _result_map(r.json())
    assert results["C"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["C"]["hops"] == 2
    assert results["C"]["chain"] == ["S", "A", "C"]


def test_request_trip_order_does_not_change_result(client):
    """车次在请求中的排列不同，最优链一致（链序按 ID 决胜，不按请求顺序）。"""
    register_square(client, zone_id="ct9")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    _upload(client, "B", B_PARKED_FAR)
    r1 = _ask(client, "ct9")
    r2 = _ask(client, "ct9", trip_ids=["B", "S", "A"])
    m1 = _result_map(r1.json())
    m2 = _result_map(r2.json())
    assert m1["B"] == m2["B"]
    assert m2["B"]["chain"] == ["S", "A", "B"]
    # 结果列表仍按各自请求顺序
    assert [row["trip_id"] for row in r2.json()["results"]] == ["B", "S", "A"]


# ---------------------------------------------------------------------
# 规则沿用：严格小于阈值、洞、窗口、覆盖
# ---------------------------------------------------------------------

def test_strict_threshold_not_transmissible(client):
    """距离恰等于阈值不相遇（严格小于），边不可传递。"""
    register_square(client, zone_id="ct10")
    _upload(client, "S", S_TRACK)
    # A 平行 y=75：与 S 恒定相距 25m —— 阈值 25 时严格小于不成立
    _upload(client, "A", [
        ("a1", "2026-09-30T10:00:00Z", 0, 75),
        ("a2", "2026-09-30T10:01:40Z", 100, 75),
    ])
    r = _ask(client, "ct10", trip_ids=["S", "A"])
    assert _result_map(r.json())["A"]["reachable"] is False
    # 阈值放宽即整段可传递，起点到达
    r = _ask(client, "ct10", trip_ids=["S", "A"], max_distance=25.000001)
    results = _result_map(r.json())
    assert results["A"]["reachable"] is True
    assert results["A"]["arrived_at"] == "2026-09-30T10:00:00Z"


def test_hole_rule_reused(client):
    """带洞边界沿用相遇复核：穿洞期间不构成共同在区，相遇被洞切成两段但仍可在起点传递。"""
    hole = [[[40, 40], [60, 40], [60, 60], [40, 60]]]
    register_square(client, zone_id="ct11", holes=hole)
    # S、A 沿 y=50/51 横穿：洞外相距 1m 相遇两段 [0,40) 与 [60,100)
    _upload(client, "S", [
        ("s1", "2026-09-30T10:00:00Z", 0, 50),
        ("s2", "2026-09-30T10:01:40Z", 100, 50),
    ])
    _upload(client, "A", [
        ("a1", "2026-09-30T10:00:00Z", 0, 51),
        ("a2", "2026-09-30T10:01:40Z", 100, 51),
    ])
    r = _ask(client, "ct11", trip_ids=["S", "A"], max_distance=2)
    results = _result_map(r.json())
    assert results["A"]["reachable"] is True
    assert results["A"]["arrived_at"] == "2026-09-30T10:00:00Z"


def test_zone_window_rule_reused(client):
    """区域只在 S、A 都穿洞（不在区）期间生效：无共同在区时间，不可传递。"""
    hole = [[[40, 40], [60, 40], [60, 60], [40, 60]]]
    register_square(
        client, zone_id="ct12", holes=hole,
        windows=[{"start": "2026-09-30T10:00:40Z", "end": "2026-09-30T10:01:00Z"}],
    )
    _upload(client, "S", [
        ("s1", "2026-09-30T10:00:00Z", 0, 50),
        ("s2", "2026-09-30T10:01:40Z", 100, 50),
    ])
    _upload(client, "A", [
        ("a1", "2026-09-30T10:00:00Z", 0, 51),
        ("a2", "2026-09-30T10:01:40Z", 100, 51),
    ])
    r = _ask(client, "ct12", trip_ids=["S", "A"], max_distance=2)
    assert _result_map(r.json())["A"]["reachable"] is False


def test_source_arrives_without_coverage_within_window(client):
    """源车在查询起点已到达：源车点全部落在查询窗口之前（窗口内无覆盖），
    源车仍标记为起点到达；但没有相遇边则下游不可达。"""
    register_square(client, zone_id="ct13")
    _upload(client, "S", [
        ("s1", "2026-09-30T09:00:00Z", 0, 50),
        ("s2", "2026-09-30T09:01:40Z", 100, 50),
    ])
    _upload(client, "A", A_TRACK)
    r = _ask(client, "ct13", trip_ids=["S", "A"])
    assert r.status_code == 200, r.text
    body = r.json()
    results = _result_map(body)
    assert results["S"]["reachable"] is True
    assert results["S"]["arrived_at"] == "2026-09-30T10:00:00Z"
    assert results["S"]["chain"] == ["S"]
    assert results["A"]["reachable"] is False
    src = next(t for t in body["trips"] if t["trip_id"] == "S")
    assert src["point_count"] == 2 and src["track_version"] == 1


def test_single_point_intermediate_has_no_edges(client):
    """中间车点数不足 2：无覆盖、无相遇边，不能传播也不能到达。"""
    register_square(client, zone_id="ct14")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", [("a0", "2026-09-30T10:00:30Z", 30, 60)])
    _upload(client, "B", B_PARKED_FAR)  # 与 S 不相遇，只可能经 A
    r = _ask(client, "ct14")
    results = _result_map(r.json())
    assert results["A"]["reachable"] is False
    assert results["B"]["reachable"] is False


# ---------------------------------------------------------------------
# 逐车轨迹版本与区域修订号（同一已提交读视图复现）
# ---------------------------------------------------------------------

def test_pinned_versions_and_revision_reproduce(client):
    register_square(client, zone_id="ct15")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    _upload(client, "B", B_PARKED_FAR)
    # 更正 A：移出区内（y=-50），当前视图 S-A 不相遇；
    # B(85,90) 与 S 纵向相距 40m 也不相遇 → 当前视图 B 不可达
    r = client.post(
        "/api/v1/trips/A/corrections",
        json={
            "base_version": 1,
            "reason": "A 轨迹录错",
            "items": [
                {"action": "update", "point_id": "a1",
                 "ts": "2026-09-30T10:00:00Z", "x": 0, "y": -50},
                {"action": "update", "point_id": "a2",
                 "ts": "2026-09-30T10:01:40Z", "x": 100, "y": -50},
            ],
        },
    )
    assert r.status_code == 200, r.text

    r = _ask(client, "ct15")
    assert _result_map(r.json())["B"]["reachable"] is False

    # 指定 A@1：按当时点集复现 → B 经 S→A→B 在 10:01:05 到达
    r = _ask(client, "ct15", track_versions={"A": 1})
    body = r.json()
    results = _result_map(body)
    assert results["B"]["reachable"] is True
    assert results["B"]["arrived_at"] == "2026-09-30T10:01:05Z"
    trips = {t["trip_id"]: t for t in body["trips"]}
    assert trips["A"]["track_version"] == 1
    assert trips["S"]["track_version"] == 1

    # 区域修订号：把区缩小到角落（rev2），指定 rev1 仍可复现
    register_square(
        client, zone_id="ct15",
        exterior=[[0, 0], [10, 0], [10, 10], [0, 10]],
    )
    r = _ask(client, "ct15", track_versions={"A": 1})
    assert _result_map(r.json())["B"]["reachable"] is False
    r = _ask(client, "ct15", revision=1, track_versions={"A": 1})
    body = r.json()
    assert body["revision"] == 1
    assert _result_map(body)["B"]["arrived_at"] == "2026-09-30T10:01:05Z"


def test_concurrent_writes_keep_single_read_view(client):
    """writer 持续登记区域/上传点，reader 反复追踪：结构完整，固定版本组合结果确定。"""
    register_square(client, zone_id="ct-live")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    _upload(client, "B", B_PARKED_FAR)

    stop = threading.Event()
    failures = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            client.post(
                "/api/v1/zones",
                json={
                    "zone_id": "ct-live",
                    "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
                    "holes": [],
                    "windows": DAY_WINDOWS,
                },
            )
            client.post(
                "/api/v1/trips/A/points",
                json={"points": [
                    {"point_id": f"extra-{i}", "ts": f"2026-09-30T12:{i % 60:02d}:00Z",
                     "x": 10 + i, "y": 65},
                ]},
            )
            i += 1

    def reader() -> None:
        pinned = dict(revision=1, track_versions={"S": 1, "A": 1, "B": 1})
        while not stop.is_set():
            r = _ask(client, "ct-live")
            if r.status_code != 200:
                failures.append(("status", r.status_code, r.text))
                return
            for row in r.json()["results"]:
                if row["reachable"]:
                    if not row["chain"] or row["chain"][0] != "S" or row["hops"] is None:
                        failures.append(("shape", row))
                        return
            r1 = _ask(client, "ct-live", **pinned)
            r2 = _ask(client, "ct-live", **pinned)
            if r1.status_code != 200 or r1.json() != r2.json():
                failures.append(("pinned-nondeterministic", r1.text))
                return
            if _result_map(r1.json())["B"]["arrived_at"] != "2026-09-30T10:01:05Z":
                failures.append(("pinned-wrong", r1.json()))
                return

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not failures


# ---------------------------------------------------------------------
# 明确报错
# ---------------------------------------------------------------------

def test_source_must_be_in_trip_ids(client):
    register_square(client, zone_id="ct-e1")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    r = _ask(client, "ct-e1", source_trip_id="X", trip_ids=["S", "A"])
    assert r.status_code == 400
    assert "source_trip_id" in r.json()["detail"]


def test_duplicate_trip_ids_rejected(client):
    register_square(client, zone_id="ct-e2")
    _upload(client, "S", S_TRACK)
    r = _ask(client, "ct-e2", trip_ids=["S", "S"])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]


def test_source_trip_must_exist(client):
    """源车也是参与车次：源车不存在同样明确拒绝（404）。"""
    register_square(client, zone_id="ct-e0")
    _upload(client, "A", A_TRACK)
    r = _ask(client, "ct-e0", trip_ids=["S", "A"])
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_zone_and_trip_not_found(client):
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    r = _ask(client, "ghost")
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]

    register_square(client, zone_id="ct-e3")
    r = _ask(client, "ct-e3", trip_ids=["S", "ghost"])
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]
    r = _ask(client, "ct-e3", trip_ids=["S", "ghost"], track_versions={"ghost": 1})
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_versions_out_of_range(client):
    register_square(client, zone_id="ct-e4")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    r = _ask(client, "ct-e4", track_versions={"A": 0})
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]
    r = _ask(client, "ct-e4", track_versions={"A": 9})
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]
    r = _ask(client, "ct-e4", revision=999)
    assert r.status_code == 404
    assert "超出当前范围" in r.json()["detail"]


def test_invalid_arguments_rejected(client):
    register_square(client, zone_id="ct-e5")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    # 距离阈值非正/非有限
    for bad in (0, -1, "NaN", "Infinity"):
        r = _ask(client, "ct-e5", max_distance=bad)
        assert r.status_code in (400, 422), bad
    # 时间非法
    r = _ask(client, "ct-e5", since="2026-09-30T10:02:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    r = _ask(client, "ct-e5", since="2026-09-30T10:00:00", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    # track_versions 含未知车次
    r = _ask(client, "ct-e5", track_versions={"Z": 1})
    assert r.status_code == 400
    assert "Z" in r.json()["detail"]
    # 车次不足两辆 / 缺字段 / 多余字段 / 版本号非法
    r = _ask(client, "ct-e5", trip_ids=["S"])
    assert r.status_code in (400, 422)
    r = client.post("/api/v1/zones/ct-e5/contact-tracing", json={
        "trip_ids": ["S", "A"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "max_distance": 25,
    })
    assert r.status_code == 422
    r = _ask(client, "ct-e5", bogus=1)
    assert r.status_code == 422
    r = _ask(client, "ct-e5", track_versions={"A": -1})
    assert r.status_code in (400, 422)


def test_existing_endpoints_unaffected(client):
    """接触传播追踪上线后，既有相遇/穿越查询语义不变。"""
    register_square(client, zone_id="ct-keep")
    _upload(client, "S", S_TRACK)
    _upload(client, "A", A_TRACK)
    r = client.post("/api/v1/zones/ct-keep/proximity", json={
        "trip_ids": ["S", "A"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:05:00Z",
        "max_distance": 25,
    })
    assert r.status_code == 200
    segs = r.json()["pairs"][0]["intervals"]
    assert len(segs) == 1 and segs[0]["duration_sec"] == 100.0

    r = client.get("/api/v1/trips/S/crossings?zone_id=ct-keep")
    assert r.status_code == 200
    assert r.json()["zones"][0]["total_duration_sec"] == 100.0

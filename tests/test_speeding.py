"""单车危险区内超速时段复核测试。

POST /api/v1/trips/{trip_id}/speeding-review：
区域与车次有效点集从同一已提交读视图读取；相邻点按线性轨迹计算恒定速度，
点间隔严格超过最大采样间隔的整段视为无覆盖；仅在所选区几何内（外边界计入、
洞及其边界不计）且生效时计入；重叠区域按最低限速；速度严格大于限速才违规，
零时长接触不计。返回按时序切分的违规正时长区间（每段速度、适用限速与并列
限速区 ID）、实际使用的区域修订号与轨迹版本、违规总时长与超速距离总量
（逐段 (速度-限速)×时长）；无违规返回空区间与零总量。
"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))
    assert r.status_code == 200, r.text
    return r


def _ask(client, trip, **overrides):
    body = {
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T11:00:00Z",
        "zone_speed_limits": {"sp": 2.0},
        "max_sample_interval_sec": 300.0,
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/speeding-review", json=body)


# 沿 y=50：10:00:00 自 x=-50 出发，10:01:40 到 x=150：200m/100s=2m/s；
# x=0 在 10:00:25，x=100 在 10:01:15，方块 [0,100]x[0,100] 内共 50 秒
CROSS = [
    ("a", "2026-09-30T10:00:00Z", -50, 50),
    ("b", "2026-09-30T10:01:40Z", 150, 50),
]


# ---------------------------------------------------------------------
# 基本超速判定
# ---------------------------------------------------------------------

def test_basic_speeding_segment(client):
    register_square(client, zone_id="sp")
    _upload(client, "T1", CROSS)

    r = _ask(client, "T1", zone_speed_limits={"sp": 1.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["point_count"] == 2
    assert body["zone_speed_limits"] == {"sp": 1.5}
    assert body["max_sample_interval_sec"] == 300.0
    # 新端点总是回填实际使用的区域修订号与轨迹版本
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:25Z"
    assert seg["end"] == "2026-09-30T10:01:15Z"
    assert seg["duration_sec"] == 50.0
    assert seg["speed_mps"] == 2.0
    assert seg["speed_limit_mps"] == 1.5
    assert seg["zone_ids"] == ["sp"]
    assert seg["excess_distance_m"] == 25.0
    assert body["total_violation_duration_sec"] == 50.0
    assert body["total_excess_distance_m"] == 25.0


def test_integer_limits_and_interval_accepted(client):
    """限速与采样间隔以整数给出时按正有限数值接受（不要求字面浮点）。"""
    register_square(client, zone_id="sp")
    _upload(client, "T1", CROSS)
    r = _ask(client, "T1", zone_speed_limits={"sp": 1}, max_sample_interval_sec=300)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone_speed_limits"] == {"sp": 1.0}
    assert body["max_sample_interval_sec"] == 300.0
    assert body["segments"][0]["speed_limit_mps"] == 1.0


def test_speed_equal_to_limit_not_violation(client):
    """速度严格大于限速才违规：恰好等于限速时无违规、零总量。"""
    register_square(client, zone_id="eq")
    _upload(client, "T2", CROSS)
    r = _ask(client, "T2", zone_speed_limits={"eq": 2.0})
    assert r.status_code == 200
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


def test_speed_below_limit_not_violation(client):
    register_square(client, zone_id="slow")
    _upload(client, "T3", CROSS)
    r = _ask(client, "T3", zone_speed_limits={"slow": 10.0})
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


def test_stationary_inside_zone_not_violation(client):
    """静止相邻点（恒定速度 0）不超速。"""
    register_square(client, zone_id="stat")
    _upload(client, "TS", [
        ("a", "2026-09-30T10:00:00Z", 50, 50),
        ("b", "2026-09-30T10:01:00Z", 50, 50),
    ])
    r = _ask(client, "TS", zone_speed_limits={"stat": 0.001})
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


def test_no_zone_geometry_hit_empty(client):
    """全程在区外：空区间、零总量。"""
    register_square(client, zone_id="miss")
    _upload(client, "TM", [
        ("a", "2026-09-30T10:00:00Z", 200, 200),
        ("b", "2026-09-30T10:01:00Z", 300, 200),
    ])
    r = _ask(client, "TM", zone_speed_limits={"miss": 0.1})
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


# ---------------------------------------------------------------------
# 多采样点、变速切分与相邻同条件合并
# ---------------------------------------------------------------------

def test_variable_speed_segments(client):
    """不同相邻点段速度不同：在区内只输出超速段，段按时序排列。

    0→10m 用时 10s（1m/s），10→90m 用时 20s（4m/s），90→100m 用时 10s（1m/s）。
    """
    register_square(client, zone_id="vs")
    _upload(client, "TV", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:10Z", 10, 50),
        ("c", "2026-09-30T10:00:30Z", 90, 50),
        ("d", "2026-09-30T10:00:40Z", 100, 50),
    ])
    r = _ask(client, "TV", zone_speed_limits={"vs": 2.0})
    body = r.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert (seg["start"], seg["end"]) == (
        "2026-09-30T10:00:10Z", "2026-09-30T10:00:30Z")
    assert seg["duration_sec"] == 20.0
    assert seg["speed_mps"] == 4.0
    assert seg["speed_limit_mps"] == 2.0
    assert body["total_violation_duration_sec"] == 20.0
    assert body["total_excess_distance_m"] == 40.0


def test_multiple_violating_pieces_timeliness_and_totals(client):
    """两段超速（中间夹一段不超速）：各自成段，总量为两段之和。"""
    register_square(client, zone_id="mv")
    # 区内全程 0→100：1m/s(20s) → 4m/s(20s, 80m?) 这里安排：
    # 0→10(10s,1m/s)；10→90(20s,4m/s，超速)；90→95(5s,1m/s)；
    # 95→100(5s) 仍低速，不违规 —— 改为末尾再安排一段高速：
    _upload(client, "TMV", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:10Z", 10, 50),    # 1m/s
        ("c", "2026-09-30T10:00:30Z", 90, 50),    # 4m/s 超速
        ("d", "2026-09-30T10:00:40Z", 95, 50),    # 0.5m/s
        ("e", "2026-09-30T10:00:41Z", 100, 50),   # 5m/s 超速（5m/1s）
    ])
    r = _ask(client, "TMV", zone_speed_limits={"mv": 2.0})
    segs = r.json()["segments"]
    assert [(s["start"], s["end"], s["speed_mps"]) for s in segs] == [
        ("2026-09-30T10:00:10Z", "2026-09-30T10:00:30Z", 4.0),
        ("2026-09-30T10:00:40Z", "2026-09-30T10:00:41Z", 5.0),
    ]
    assert r.json()["total_violation_duration_sec"] == 21.0
    # (4-2)*20 + (5-2)*1 = 43
    assert r.json()["total_excess_distance_m"] == 43.0


def test_touching_same_condition_segments_merge(client):
    """两段在采样点相接、速度与限速相同：合并为一段。"""
    register_square(client, zone_id="mc")
    _upload(client, "TMC", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:25Z", 50, 50),   # 2m/s
        ("c", "2026-09-30T10:00:50Z", 100, 50),  # 2m/s
    ])
    r = _ask(client, "TMC", zone_speed_limits={"mc": 1.0})
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert (segs[0]["start"], segs[0]["end"]) == (
        "2026-09-30T10:00:00Z", "2026-09-30T10:00:50Z")
    assert segs[0]["speed_mps"] == 2.0
    assert r.json()["total_excess_distance_m"] == 50.0


# ---------------------------------------------------------------------
# 最大采样间隔：超过上限的整段无覆盖
# ---------------------------------------------------------------------

def test_gap_pair_entirely_uncovered(client):
    """唯一相邻点段间隔超过上限：整段无覆盖，无违规。"""
    register_square(client, zone_id="gap")
    _upload(client, "TG", CROSS)  # 100s 间隔
    r = _ask(client, "TG", zone_speed_limits={"gap": 0.1}, max_sample_interval_sec=50.0)
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


def test_gap_equal_to_limit_is_covered(client):
    """间隔恰好等于上限不算超过（严格大于才无覆盖）。"""
    register_square(client, zone_id="geq")
    _upload(client, "TGE", CROSS)  # 100s
    r = _ask(client, "TGE", zone_speed_limits={"geq": 1.0}, max_sample_interval_sec=100.0)
    body = r.json()
    assert body["total_violation_duration_sec"] == 50.0


def test_middle_oversized_gap_splits_coverage(client):
    """中间点段间隔超限：该整段无覆盖；前后段仍各自核算。"""
    register_square(client, zone_id="mg")
    _upload(client, "TMG", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:10Z", 10, 50),   # 10s, 1m/s
        ("c", "2026-09-30T10:00:40Z", 90, 50),   # 30s 间隔 > 15s 上限：整段无覆盖
        ("d", "2026-09-30T10:00:50Z", 100, 50),  # 10s, 1m/s
    ])
    r = _ask(client, "TMG", zone_speed_limits={"mg": 0.5},
             max_sample_interval_sec=15.0)
    segs = r.json()["segments"]
    # 仅前后两段各 10s 在区内且超速（1 > 0.5），中间 30s 整段无覆盖
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:10Z"),
        ("2026-09-30T10:00:40Z", "2026-09-30T10:00:50Z"),
    ]
    assert r.json()["total_violation_duration_sec"] == 20.0


# ---------------------------------------------------------------------
# 重叠区域：最低限速与并列限速区
# ---------------------------------------------------------------------

def test_overlap_uses_minimum_limit(client):
    """重叠区域按最低限速：低限速区 [0,100] 限速 3，右半 [50,100] 限速 1。"""
    register_square(client, zone_id="lo")
    assert client.post("/api/v1/zones", json={
        "zone_id": "hi",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TO", CROSS)  # 2m/s
    r = _ask(client, "TO", zone_speed_limits={"lo": 3.0, "hi": 1.0})
    segs = r.json()["segments"]
    # x∈[0,50)：限速 3，2m/s 不违规；x∈[50,100)：限速 1，超速 25s
    assert len(segs) == 1
    seg = segs[0]
    assert (seg["start"], seg["end"]) == (
        "2026-09-30T10:00:50Z", "2026-09-30T10:01:15Z")
    assert seg["speed_mps"] == 2.0
    assert seg["speed_limit_mps"] == 1.0
    assert seg["zone_ids"] == ["hi"]
    assert r.json()["total_excess_distance_m"] == 25.0


def test_tied_minimum_zones_listed_and_split_on_change(client):
    """多区并列最低限速时 zone_ids 列出全部并列区（排序）；并列集合变化即分段。"""
    register_square(client, zone_id="za")
    assert client.post("/api/v1/zones", json={
        "zone_id": "zb",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TT", CROSS)  # 2m/s，两区同为限速 1
    r = _ask(client, "TT", zone_speed_limits={"za": 1.0, "zb": 1.0})
    segs = r.json()["segments"]
    # [25,50)s 仅 za；[50,75)s za/zb 并列：集合变化分成两段
    assert [(s["start"], s["end"], s["zone_ids"]) for s in segs] == [
        ("2026-09-30T10:00:25Z", "2026-09-30T10:00:50Z", ["za"]),
        ("2026-09-30T10:00:50Z", "2026-09-30T10:01:15Z", ["za", "zb"]),
    ]
    # 两段速度限速相同但并列集合不同，不得合并
    assert r.json()["total_violation_duration_sec"] == 50.0


def test_non_minimum_overlap_zone_not_driving(client):
    """高限速重叠区不抬高也不降低最低限速：以低限速区为准，zone_ids 不含高限速区。"""
    register_square(client, zone_id="strict")
    assert client.post("/api/v1/zones", json={
        "zone_id": "lenient",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TN", CROSS)  # 2m/s
    r = _ask(client, "TN", zone_speed_limits={"strict": 1.0, "lenient": 10.0})
    seg = r.json()["segments"][0]
    assert seg["speed_limit_mps"] == 1.0
    assert seg["zone_ids"] == ["strict"]


# ---------------------------------------------------------------------
# 带洞边界、生效窗口、半开查询裁剪、零时长接触
# ---------------------------------------------------------------------

def test_hole_excluded_from_violation(client):
    """洞及其边界内时间不计入：洞内高速穿过不违规。"""
    register_square(
        client, zone_id="hz",
        holes=[[[30, 30], [70, 30], [70, 70], [30, 70]]],
    )
    # 100m/50s=2m/s 沿 y=50：区内 [0,30) 与 (70,100] 各 15s，洞内 20s 剔除
    _upload(client, "TH", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:00:50Z", 100, 50),
    ])
    r = _ask(client, "TH", zone_speed_limits={"hz": 1.0})
    segs = r.json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:15Z"),
        ("2026-09-30T10:00:35Z", "2026-09-30T10:00:50Z"),
    ]
    # (2-1)*30 = 30
    assert r.json()["total_violation_duration_sec"] == 30.0
    assert r.json()["total_excess_distance_m"] == 30.0


def test_zone_window_not_effective_empty(client):
    """区域在查询时间内不生效：无违规。"""
    register_square(
        client, zone_id="wf",
        windows=[{"start": "2026-09-30T12:00:00Z", "end": "2026-09-30T13:00:00Z"}],
    )
    _upload(client, "TW", CROSS)
    r = _ask(client, "TW", zone_speed_limits={"wf": 0.5})
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0


def test_window_gap_separates_violations(client):
    """生效窗口在中途断开：两段违规不合并，窗口外时间不计。"""
    register_square(
        client, zone_id="wg",
        windows=[
            {"start": "2026-09-30T10:00:25Z", "end": "2026-09-30T10:00:40Z"},
            {"start": "2026-09-30T10:01:00Z", "end": "2026-09-30T10:01:15Z"},
        ],
    )
    _upload(client, "TWD", CROSS)  # 2m/s
    r = _ask(client, "TWD", zone_speed_limits={"wg": 1.0})
    segs = r.json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-09-30T10:00:25Z", "2026-09-30T10:00:40Z"),
        ("2026-09-30T10:01:00Z", "2026-09-30T10:01:15Z"),
    ]
    assert r.json()["total_violation_duration_sec"] == 30.0
    assert r.json()["total_excess_distance_m"] == 30.0


def test_query_window_halfopen_clip(client):
    """查询时段按半开 [since, until) 裁剪违规区间。"""
    register_square(client, zone_id="qc")
    _upload(client, "TQ", CROSS)  # 区内 [25s,75s]
    r = _ask(
        client, "TQ",
        since="2026-09-30T10:00:30Z", until="2026-09-30T10:00:50Z",
        zone_speed_limits={"qc": 1.0},
    )
    seg = r.json()["segments"][0]
    assert (seg["start"], seg["end"]) == (
        "2026-09-30T10:00:30Z", "2026-09-30T10:00:50Z")
    assert seg["duration_sec"] == 20.0
    # (2-1)*20 = 20
    assert r.json()["total_excess_distance_m"] == 20.0


def test_query_window_end_exclusive(client):
    """until 端点不含：裁剪到 until 的部分为左闭右开，零剩余不计。"""
    register_square(client, zone_id="qe")
    _upload(client, "TQE", CROSS)
    r = _ask(
        client, "TQE",
        since="2026-09-30T10:00:00Z", until="2026-09-30T10:00:25Z",
        zone_speed_limits={"qe": 1.0},
    )
    # until 恰为进入区域时刻：区内无正时长
    assert r.json()["segments"] == []
    assert r.json()["total_violation_duration_sec"] == 0


def test_zero_duration_contact_ignored(client):
    """仅瞬时擦过角点：零时长接触不计。"""
    register_square(client, zone_id="zc")
    _upload(client, "TZ", [
        ("a", "2026-09-30T10:00:00Z", -10, 10),
        ("b", "2026-09-30T10:00:10Z", 10, -10),
    ])
    r = _ask(client, "TZ", zone_speed_limits={"zc": 0.001})
    body = r.json()
    assert body["segments"] == []
    assert body["total_violation_duration_sec"] == 0
    assert body["total_excess_distance_m"] == 0


# ---------------------------------------------------------------------
# 区域修订号与轨迹版本复现
# ---------------------------------------------------------------------

def test_pinned_revision_reproduces(client):
    register_square(client, zone_id="rv")  # rev1: 100x100
    assert client.post("/api/v1/zones", json={
        "zone_id": "rv",
        "exterior": [[0, 0], [200, 0], [200, 200], [0, 200]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201  # rev2: 200x200
    _upload(client, "TRV", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:02:30Z", 250, 50),  # 300m/150s=2m/s
    ])
    # rev1 下区内 x∈[0,100] 共 50s 超速；rev2 下 x∈[0,200] 共 100s 超速
    r1 = _ask(client, "TRV", zone_speed_limits={"rv": 1.0}, revision=1)
    b1 = r1.json()
    assert b1["revision"] == 1
    assert b1["total_violation_duration_sec"] == 50.0
    assert b1["total_excess_distance_m"] == 50.0

    r2 = _ask(client, "TRV", zone_speed_limits={"rv": 1.0})
    b2 = r2.json()
    # 不指定 revision 时回填查询时最新修订号
    assert b2["revision"] == 2
    assert b2["total_violation_duration_sec"] == 100.0


def test_revision_out_of_range_rejected(client):
    register_square(client, zone_id="rr")
    _upload(client, "TRR", CROSS)
    r = _ask(client, "TRR", zone_speed_limits={"rr": 1.0}, revision=999)
    assert r.status_code == 404
    assert "修订号" in r.json()["detail"]


def test_zone_not_registered_at_revision_rejected(client):
    register_square(client, zone_id="early")
    assert client.post("/api/v1/zones", json={
        "zone_id": "late", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TL", CROSS)
    r = _ask(client, "TL", zone_speed_limits={"late": 1.0}, revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_pinned_track_version_reproduces(client):
    register_square(client, zone_id="tv")
    _upload(client, "TTV", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),  # v1: 1m/s，限速 0.5 时全区超速 100s
    ])
    # 更正：末点提前到 10:00:30 => 当前版本速度 100m/30s≈3.33m/s，仅前 30s 在区内
    r = client.post("/api/v1/trips/TTV/corrections", json={
        "base_version": 1,
        "reason": "现场复核：末点时刻录错",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T10:00:30Z", "x": 100, "y": 50}],
    })
    assert r.status_code == 200, r.text

    r_v1 = _ask(client, "TTV", zone_speed_limits={"tv": 0.5}, track_version=1)
    b1 = r_v1.json()
    assert b1["track_version"] == 1
    assert b1["point_count"] == 2
    assert b1["total_violation_duration_sec"] == 100.0
    assert b1["segments"][0]["speed_mps"] == 1.0

    r_latest = _ask(client, "TTV", zone_speed_limits={"tv": 0.5})
    bl = r_latest.json()
    assert bl["track_version"] == 2
    assert bl["point_count"] == 2
    assert bl["total_violation_duration_sec"] == 30.0


def test_track_version_out_of_range_rejected(client):
    register_square(client, zone_id="tvx")
    _upload(client, "TTX", CROSS)
    r = _ask(client, "TTX", zone_speed_limits={"tvx": 1.0}, track_version=9)
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]
    r = _ask(client, "TTX", zone_speed_limits={"tvx": 1.0}, track_version=0)
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]


def test_revision_and_track_version_together(client):
    register_square(client, zone_id="both")
    _upload(client, "TB", CROSS)
    r = _ask(client, "TB", zone_speed_limits={"both": 1.0},
             revision=1, track_version=1)
    body = r.json()
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["total_violation_duration_sec"] == 50.0


# ---------------------------------------------------------------------
# 明确拒绝：未知资源 / 非法输入
# ---------------------------------------------------------------------

def test_unknown_trip_rejected(client):
    register_square(client, zone_id="unk")
    r = _ask(client, "ghost", zone_speed_limits={"unk": 1.0})
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_unknown_zone_rejected(client):
    _upload(client, "TU", CROSS)
    r = _ask(client, "TU", zone_speed_limits={"ghost-zone": 1.0})
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_some_unknown_zone_rejected(client):
    register_square(client, zone_id="known")
    _upload(client, "TU2", CROSS)
    r = _ask(client, "TU2", zone_speed_limits={"known": 1.0, "nope": 2.0})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="bad-t")
    _upload(client, "TBT", CROSS)
    r = _ask(
        client, "TBT",
        since="2026-09-30T10:05:00Z", until="2026-09-30T10:00:00Z",
        zone_speed_limits={"bad-t": 1.0},
    )
    assert r.status_code == 400
    assert "since" in r.json()["detail"]


def test_naive_time_rejected(client):
    register_square(client, zone_id="naive")
    _upload(client, "TN", CROSS)
    r = client.post("/api/v1/trips/TN/speeding-review", json={
        "since": "2026-09-30T10:00:00",
        "until": "2026-09-30T11:00:00Z",
        "zone_speed_limits": {"naive": 1.0},
        "max_sample_interval_sec": 300.0,
    })
    assert r.status_code in (400, 422)


def test_empty_zone_limits_rejected(client):
    _upload(client, "TE", CROSS)
    r = client.post("/api/v1/trips/TE/speeding-review", json={
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T11:00:00Z",
        "zone_speed_limits": {},
        "max_sample_interval_sec": 300.0,
    })
    assert r.status_code in (400, 422)


def test_non_positive_speed_limits_rejected(client):
    _upload(client, "TF", CROSS)
    for bad in (0, -1, "NaN", "Infinity", "-Infinity", True):
        r = client.post("/api/v1/trips/TF/speeding-review", json={
            "since": "2026-09-30T09:00:00Z",
            "until": "2026-09-30T11:00:00Z",
            "zone_speed_limits": {"sp": bad},
            "max_sample_interval_sec": 300.0,
        })
        assert r.status_code in (400, 422), bad


def test_non_positive_sample_interval_rejected(client):
    _upload(client, "TG2", CROSS)
    for bad in (0, -5, "NaN", "Infinity", True, "x"):
        r = client.post("/api/v1/trips/TG2/speeding-review", json={
            "since": "2026-09-30T09:00:00Z",
            "until": "2026-09-30T11:00:00Z",
            "zone_speed_limits": {"sp": 1.0},
            "max_sample_interval_sec": bad,
        })
        assert r.status_code in (400, 422), bad


def test_extra_field_rejected(client):
    _upload(client, "TX", CROSS)
    r = client.post("/api/v1/trips/TX/speeding-review", json={
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T11:00:00Z",
        "zone_speed_limits": {"sp": 1.0},
        "max_sample_interval_sec": 300.0,
        "unexpected": 1,
    })
    assert r.status_code == 422


def test_missing_field_rejected(client):
    r = client.post("/api/v1/trips/TX/speeding-review", json={
        "since": "2026-09-30T09:00:00Z",
        "until": "2026-09-30T11:00:00Z",
        "zone_speed_limits": {"sp": 1.0},
    })
    assert r.status_code == 422


def test_legacy_trip_track_version_zero(client):
    """旧库基线车次（版本 0）可按 track_version=0 复核。"""
    register_square(client, zone_id="leg")
    import sqlite3
    from app.main import db as _db
    with _db.transaction() as conn:
        conn.execute(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            ("LEG", "p1", 0.0, 0.0, 50.0),
        )
        conn.execute(
            "INSERT INTO points(trip_id, point_id, ts_epoch, x, y) VALUES (?,?,?,?,?)",
            ("LEG", "p2", 100.0, 100.0, 50.0),
        )
    now = "2026-10-01T00:00:00Z"
    with _db.transaction() as conn:
        _db.ensure_legacy_baseline(conn, "LEG", now)
        _db.upsert_trip_version(conn, "LEG", 0, now)
    # 直接指定一个当天生效窗口以便不依赖具体日期：用足够宽的窗口重建区域
    assert client.post("/api/v1/zones", json={
        "zone_id": "leg",
        "exterior": [[0, 0], [100, 0], [100, 100], [0, 100]],
        "holes": [],
        "windows": [{"start": "1970-01-01T00:00:00Z", "end": "1970-01-02T00:00:00Z"}],
    }).status_code == 201
    r = client.post("/api/v1/trips/LEG/speeding-review", json={
        "since": "1970-01-01T00:00:00Z",
        "until": "1970-01-02T00:00:00Z",
        "zone_speed_limits": {"leg": 0.5},
        "max_sample_interval_sec": 300.0,
        "track_version": 0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["track_version"] == 0
    # 100m/100s = 1m/s，限速 0.5，全程 100s 在区内
    assert body["total_violation_duration_sec"] == 100.0


# ---------------------------------------------------------------------
# 同一已提交读视图（并发下结果确定一致）
# ---------------------------------------------------------------------

def test_concurrent_updates_read_view_consistent(client):
    assert client.post("/api/v1/zones", json={
        "zone_id": "live", "exterior": SQUARE, "holes": [],
        "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-02T00:00:00Z"}],
    }).status_code == 201
    client.post("/api/v1/trips/live-trip/points", json=_batch([
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:40Z", 150, 50),
    ]))

    stop = threading.Event()
    failures = []

    def writer():
        i = 0
        while not stop.is_set():
            size = 100 + (i % 4) * 40
            client.post("/api/v1/zones", json={
                "zone_id": "live",
                "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                "holes": [],
                "windows": [{"start": "2026-09-30T00:00:00Z",
                             "end": "2026-10-02T00:00:00Z"}],
            })
            i += 1

    def reader():
        while not stop.is_set():
            body = {
                "since": "2026-09-30T09:00:00Z",
                "until": "2026-09-30T11:00:00Z",
                "zone_speed_limits": {"live": 1.0},
                "max_sample_interval_sec": 300.0,
            }
            r1 = client.post("/api/v1/trips/live-trip/speeding-review", json=body)
            r2 = client.post("/api/v1/trips/live-trip/speeding-review", json=body)
            if r1.status_code != 200 or r2.status_code != 200:
                failures.append(("status", r1.status_code, r2.status_code))
                return
            b1, b2 = r1.json(), r2.json()
            for b in (b1, b2):
                # 段超速距离之和必须等于总量（同快照自洽）
                assert abs(
                    sum(s["excess_distance_m"] for s in b["segments"])
                    - b["total_excess_distance_m"]
                ) < 1e-4
                assert b["revision"] >= 1
                # 2m/s、限速 1：超速距离 == 区内时长（宽度 100/140/.../220）
                assert abs(
                    b["total_excess_distance_m"] - b["total_violation_duration_sec"]
                ) < 1e-4

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not failures

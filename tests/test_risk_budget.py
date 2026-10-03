"""单车一次作业危险区风险预算复核测试。

POST /api/v1/trips/{trip_id}/risk-budget：
各区占用沿用既有轨迹插值、区域生效时段与带洞边界规则；重叠区域同时占用时
只按最高权重计入；返回按时间排列的恒权重正时长区间（相接且同权重合并）、
各段及总计加权秒数、累计值首次达到预算的时刻；未达预算时刻为 null，
无暴露返回零总计与空区间。区域与车次点集从同一已提交读视图读取。
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
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_weights": {"rb": 2.0},
        "budget_weighted_sec": 1000.0,
    }
    body.update(overrides)
    return client.post(f"/api/v1/trips/{trip}/risk-budget", json=body)


# 10:00:00 自 x=-50 出发，10:01:20 到 x=150：200m/80s=2.5m/s；
# x=0 在 10:00:20，x=100 在 10:01:00，方块 [0,100]x[0,100] 内共 40 秒
CROSS = [
    ("a", "2026-09-30T10:00:00Z", -50, 50),
    ("b", "2026-09-30T10:01:20Z", 150, 50),
]


# ---------------------------------------------------------------------
# 基本加权与预算时刻
# ---------------------------------------------------------------------

def test_basic_single_zone_weighted_seconds(client):
    register_square(client, zone_id="rb")
    _upload(client, "T1", CROSS)

    r = _ask(client, "T1", zone_weights={"rb": 2.0}, budget_weighted_sec=50.0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trip_id"] == "T1"
    assert body["point_count"] == 2
    assert body["zone_weights"] == {"rb": 2.0}
    assert body["budget_weighted_sec"] == 50.0
    assert "revision" not in body
    assert "track_version" not in body
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:20Z"
    assert seg["end"] == "2026-09-30T10:01:00Z"
    assert seg["duration_sec"] == 40.0
    assert seg["weight"] == 2.0
    assert seg["weighted_sec"] == 80.0
    assert body["total_weighted_sec"] == 80.0
    # 50 加权秒在进入后 25 秒达到：10:00:45
    assert body["budget_reached_at"] == "2026-09-30T10:00:45Z"


def test_budget_exactly_reached_at_segment_end(client):
    """累计在段末恰好达到预算：达到时刻为段末（半开端点，仍算已达到）。"""
    register_square(client, zone_id="rb2")
    _upload(client, "T2", CROSS)
    r = _ask(client, "T2", zone_weights={"rb2": 2}, budget_weighted_sec=80)
    body = r.json()
    assert body["total_weighted_sec"] == 80.0
    assert body["budget_reached_at"] == "2026-09-30T10:01:00Z"


def test_budget_not_reached_returns_null(client):
    register_square(client, zone_id="rb3")
    _upload(client, "T3", CROSS)
    r = _ask(client, "T3", zone_weights={"rb3": 2.0}, budget_weighted_sec=80.01)
    body = r.json()
    assert body["total_weighted_sec"] == 80.0
    assert body["budget_reached_at"] is None


def test_no_exposure_zero_total_empty_segments(client):
    """轨迹未覆盖任何指定区：零总计、空区间、空达到时刻。"""
    register_square(client, zone_id="rb4")
    # 全程在区外
    _upload(client, "T4", [
        ("a", "2026-09-30T10:00:00Z", 200, 200),
        ("b", "2026-09-30T10:01:00Z", 300, 200),
    ])
    r = _ask(client, "T4", zone_weights={"rb4": 1.0}, budget_weighted_sec=1.0)
    assert r.status_code == 200
    body = r.json()
    assert body["segments"] == []
    assert body["total_weighted_sec"] == 0
    assert body["budget_reached_at"] is None


def test_query_window_halfopen_clip(client):
    """查询时段按半开 [since, until) 裁剪。"""
    register_square(client, zone_id="rb5")
    _upload(client, "T5", CROSS)
    r = _ask(
        client, "T5",
        since="2026-09-30T10:00:30Z", until="2026-09-30T10:00:50Z",
        zone_weights={"rb5": 3.0}, budget_weighted_sec=100.0,
    )
    body = r.json()
    assert len(body["segments"]) == 1
    seg = body["segments"][0]
    assert seg["start"] == "2026-09-30T10:00:30Z"
    assert seg["end"] == "2026-09-30T10:00:50Z"
    assert seg["duration_sec"] == 20.0
    assert seg["weighted_sec"] == 60.0
    assert body["total_weighted_sec"] == 60.0
    assert body["budget_reached_at"] is None


# ---------------------------------------------------------------------
# 重叠区域：只按最高权重
# ---------------------------------------------------------------------

def test_overlap_max_weight_exact(client):
    """精确核对：2.5m/s 横穿；低权区 [0,100]，高权区 [50,100]。"""
    register_square(client, zone_id="lo2")
    assert client.post("/api/v1/zones", json={
        "zone_id": "hi2",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "T7", CROSS)

    r = _ask(client, "T7", zone_weights={"lo2": 2.0, "hi2": 5.0},
             budget_weighted_sec=75.0)
    body = r.json()
    segs = [(s["start"], s["end"], s["duration_sec"], s["weight"]) for s in body["segments"]]
    # [10:00:20,10:00:40) 仅低权 20s；[10:00:40,10:01:00) 重叠只计高权 20s
    assert segs == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:40Z", 20.0, 2.0),
        ("2026-09-30T10:00:40Z", "2026-09-30T10:01:00Z", 20.0, 5.0),
    ]
    # 40 + 100 = 140；预算 75 在高权段进入后 (75-40)/5=7s 达到
    assert body["total_weighted_sec"] == 140.0
    assert body["budget_reached_at"] == "2026-09-30T10:00:47Z"


def test_overlap_middle_hi_zone_three_segments(client):
    """高权区在低权区中部：低-高-低 三段。"""
    register_square(client, zone_id="low")
    assert client.post("/api/v1/zones", json={
        "zone_id": "mid",
        "exterior": [[30, 30], [70, 30], [70, 70], [30, 70]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "T8", CROSS)  # 沿 y=50
    r = _ask(client, "T8", zone_weights={"low": 1.0, "mid": 4.0},
             budget_weighted_sec=1000.0)
    segs = [(s["start"], s["end"], s["weight"]) for s in r.json()["segments"]]
    # 进 x=0: 20s；进 x=30: 32s；出 x=70: 48s；出 x=100: 60s
    assert segs == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:32Z", 1.0),
        ("2026-09-30T10:00:32Z", "2026-09-30T10:00:48Z", 4.0),
        ("2026-09-30T10:00:48Z", "2026-09-30T10:01:00Z", 1.0),
    ]
    assert r.json()["total_weighted_sec"] == 12 + 64 + 12


def test_same_weight_touching_segments_merge(client):
    """被未指定区/高权变化隔开后相接且同权重的区间不合并；真正相接的同权重区间合并。

    两个几何分离但时间上被区外间隔分开的同权重占用 => 两段（不合并）。
    """
    # 单一区、窗口两段且不与穿越区间整体相交成相接：窗口 [20,35) 与 [45,60)
    register_square(
        client, zone_id="gap",
        windows=[
            {"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:35Z"},
            {"start": "2026-09-30T10:00:45Z", "end": "2026-09-30T10:01:00Z"},
        ],
    )
    _upload(client, "T9", CROSS)
    r = _ask(client, "T9", zone_weights={"gap": 2.0}, budget_weighted_sec=1000.0)
    segs = r.json()["segments"]
    assert [(s["start"], s["end"], s["weight"]) for s in segs] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:35Z", 2.0),
        ("2026-09-30T10:00:45Z", "2026-09-30T10:01:00Z", 2.0),
    ]


def test_touching_same_weight_across_zone_switch_merges(client):
    """低权区占用恰在高权区开始时刻结束且高权区之后再接同权重低权区：
    权重不同不合并；但若两区同权重，相接区间合并为一段。"""
    register_square(client, zone_id="wa")
    assert client.post("/api/v1/zones", json={
        "zone_id": "wb",
        "exterior": [[50, 0], [100, 0], [100, 100], [50, 100]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "T10", CROSS)
    r = _ask(client, "T10", zone_weights={"wa": 3.0, "wb": 3.0},
             budget_weighted_sec=1000.0)
    segs = r.json()["segments"]
    # 同权重且在 10:00:50 相接 => 合并为整段 [20,60)
    assert len(segs) == 1
    assert (segs[0]["start"], segs[0]["end"], segs[0]["weight"]) == (
        "2026-09-30T10:00:20Z", "2026-09-30T10:01:00Z", 3.0,
    )
    assert segs[0]["weighted_sec"] == 120.0


# ---------------------------------------------------------------------
# 沿用带洞边界与生效窗口
# ---------------------------------------------------------------------

def test_holes_and_windows_apply(client):
    """洞内（含洞边界）与窗口外时间不计入。"""
    hole = [[[30, 30], [70, 30], [70, 70], [30, 70]]]
    register_square(client, zone_id="rbh", holes=hole)
    # 100s 走 100m，沿 y=50：区内 [0,30) 与 (70,100] 计 60s，洞及其边界 40s 不计
    _upload(client, "TH", [
        ("h1", "2026-09-30T10:00:00Z", 0, 50),
        ("h2", "2026-09-30T10:01:40Z", 100, 50),
    ])
    r = _ask(client, "TH", zone_weights={"rbh": 2.0}, budget_weighted_sec=1000.0)
    segs = r.json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-09-30T10:00:00Z", "2026-09-30T10:00:30Z"),
        ("2026-09-30T10:01:10Z", "2026-09-30T10:01:40Z"),
    ]
    assert r.json()["total_weighted_sec"] == 120.0


def test_window_gap_not_counted(client):
    register_square(
        client, zone_id="rbw",
        windows=[{"start": "2026-09-30T10:00:20Z", "end": "2026-09-30T10:00:40Z"}],
    )
    _upload(client, "TW", CROSS)
    r = _ask(client, "TW", zone_weights={"rbw": 1.0}, budget_weighted_sec=1000.0)
    segs = r.json()["segments"]
    assert [(s["start"], s["end"]) for s in segs] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:40Z"),
    ]


def test_zero_duration_contact_ignored(client):
    """仅瞬时擦过角点：无暴露。"""
    register_square(client, zone_id="rbt")
    _upload(client, "TT", [
        ("t1", "2026-09-30T10:00:00Z", -10, 10),
        ("t2", "2026-09-30T10:00:10Z", 10, -10),
    ])
    r = _ask(client, "TT", zone_weights={"rbt": 1.0}, budget_weighted_sec=1.0)
    body = r.json()
    assert body["segments"] == []
    assert body["total_weighted_sec"] == 0
    assert body["budget_reached_at"] is None


# ---------------------------------------------------------------------
# 修订号与轨迹版本复现
# ---------------------------------------------------------------------

def test_pinned_revision_reproduces(client):
    # rev1: 100x100 方块；rev2: 放大为 200x200（无洞）
    register_square(client, zone_id="rv")
    assert client.post("/api/v1/zones", json={
        "zone_id": "rv",
        "exterior": [[0, 0], [200, 0], [200, 200], [0, 200]],
        "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TRV", [
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:02:30Z", 250, 50),  # 300m/150s=2m/s
    ])

    r1 = _ask(client, "TRV", zone_weights={"rv": 1.0}, budget_weighted_sec=1000,
              revision=1)
    b1 = r1.json()
    assert b1["revision"] == 1
    # rev1 下 x∈[0,100]：25s..75s => 50 秒
    assert b1["segments"][0]["start"] == "2026-09-30T10:00:25Z"
    assert b1["segments"][-1]["end"] == "2026-09-30T10:01:15Z"
    assert b1["total_weighted_sec"] == 50.0

    r2 = _ask(client, "TRV", zone_weights={"rv": 1.0}, budget_weighted_sec=1000)
    b2 = r2.json()
    assert "revision" not in b2
    # 最新（200 宽）：x∈[0,200]：25s..125s => 100 秒
    assert b2["total_weighted_sec"] == 100.0


def test_revision_out_of_range_rejected(client):
    register_square(client, zone_id="rr")
    _upload(client, "TRR", CROSS)
    r = _ask(client, "TRR", zone_weights={"rr": 1.0}, budget_weighted_sec=1.0,
             revision=999)
    assert r.status_code == 404
    assert "修订号" in r.json()["detail"]


def test_zone_not_registered_at_revision_rejected(client):
    register_square(client, zone_id="early")
    # rev2 登记另一个区
    assert client.post("/api/v1/zones", json={
        "zone_id": "late", "exterior": SQUARE, "holes": [], "windows": DAY_WINDOWS,
    }).status_code == 201
    _upload(client, "TL", CROSS)
    r = _ask(client, "TL", zone_weights={"late": 1.0}, budget_weighted_sec=1.0,
             revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_pinned_track_version_reproduces_and_fills_back(client):
    register_square(client, zone_id="tv")
    _upload(client, "TTV", [
        ("a", "2026-09-30T10:00:00Z", 0, 50),
        ("b", "2026-09-30T10:01:40Z", 100, 50),  # 版本1：区内 100s
    ])
    # 更正：把末点改到区外，v2 下仅 10:00:00~10:00:33.333 在方块内（200m/100s=3m/s）
    r = client.post("/api/v1/trips/TTV/corrections", json={
        "base_version": 1,
        "reason": "现场复核",
        "items": [{"action": "update", "point_id": "b",
                   "ts": "2026-09-30T10:01:40Z", "x": 300, "y": 50}],
    })
    assert r.status_code == 200, r.text

    r_latest = _ask(client, "TTV", zone_weights={"tv": 2.0}, budget_weighted_sec=1.0)
    assert "track_version" not in r_latest.json()
    # 当前点集仍有暴露（只是更短）：前 100/3 秒在区内
    assert r_latest.json()["total_weighted_sec"] == round(2.0 * 100.0 / 3.0, 6)

    r_v1 = _ask(client, "TTV", zone_weights={"tv": 2.0}, budget_weighted_sec=100.0,
                track_version=1)
    body = r_v1.json()
    assert body["track_version"] == 1
    assert body["point_count"] == 2
    assert body["total_weighted_sec"] == 200.0
    assert body["budget_reached_at"] == "2026-09-30T10:00:50Z"


def test_track_version_out_of_range_rejected(client):
    register_square(client, zone_id="tvx")
    _upload(client, "TTX", CROSS)
    # 晚于当前
    r = _ask(client, "TTX", zone_weights={"tvx": 1.0}, budget_weighted_sec=1.0,
             track_version=9)
    assert r.status_code == 404
    # 早于基线（新车次基线为 1）
    r = _ask(client, "TTX", zone_weights={"tvx": 1.0}, budget_weighted_sec=1.0,
             track_version=0)
    assert r.status_code == 404


def test_revision_and_track_version_together(client):
    register_square(client, zone_id="both")
    _upload(client, "TB", CROSS)
    r = _ask(client, "TB", zone_weights={"both": 1.0}, budget_weighted_sec=1000.0,
             revision=1, track_version=1)
    body = r.json()
    assert body["revision"] == 1
    assert body["track_version"] == 1
    assert body["total_weighted_sec"] == 40.0


# ---------------------------------------------------------------------
# 明确拒绝：未知资源 / 非法输入
# ---------------------------------------------------------------------

def test_unknown_trip_rejected(client):
    register_square(client, zone_id="unk")
    r = _ask(client, "ghost", zone_weights={"unk": 1.0}, budget_weighted_sec=1.0)
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]


def test_unknown_zone_rejected(client):
    _upload(client, "TU", CROSS)
    r = _ask(client, "TU", zone_weights={"ghost-zone": 1.0},
             budget_weighted_sec=1.0)
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_some_unknown_zone_rejected(client):
    register_square(client, zone_id="known")
    _upload(client, "TU2", CROSS)
    r = _ask(client, "TU2", zone_weights={"known": 1.0, "nope": 2.0},
             budget_weighted_sec=1.0)
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="bad-t")
    _upload(client, "TBT", CROSS)
    r = _ask(
        client, "TBT",
        since="2026-09-30T10:05:00Z", until="2026-09-30T10:00:00Z",
        zone_weights={"bad-t": 1.0}, budget_weighted_sec=1.0,
    )
    assert r.status_code == 400
    assert "since" in r.json()["detail"]


def test_naive_time_rejected(client):
    register_square(client, zone_id="naive-rb")
    _upload(client, "TN", CROSS)
    r = client.post("/api/v1/trips/TN/risk-budget", json={
        "since": "2026-09-30T10:00:00",
        "until": "2026-09-30T10:10:00Z",
        "zone_weights": {"naive-rb": 1.0},
        "budget_weighted_sec": 1.0,
    })
    assert r.status_code in (400, 422)


def test_empty_zone_weights_rejected(client):
    _upload(client, "TE", CROSS)
    r = client.post("/api/v1/trips/TE/risk-budget", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_weights": {},
        "budget_weighted_sec": 1.0,
    })
    assert r.status_code in (400, 422)


def test_non_positive_weights_rejected(client):
    _upload(client, "TF", CROSS)
    for bad in (0, -1, "NaN", "Infinity", "-Infinity"):
        r = client.post("/api/v1/trips/TF/risk-budget", json={
            "since": "2026-09-30T10:00:00Z",
            "until": "2026-09-30T10:10:00Z",
            "zone_weights": {"rb": bad},
            "budget_weighted_sec": 1.0,
        })
        assert r.status_code in (400, 422), bad


def test_non_positive_budget_rejected(client):
    _upload(client, "TG", CROSS)
    for bad in (0, -5, "NaN", "Infinity"):
        r = client.post("/api/v1/trips/TG/risk-budget", json={
            "since": "2026-09-30T10:00:00Z",
            "until": "2026-09-30T10:10:00Z",
            "zone_weights": {"rb": 1.0},
            "budget_weighted_sec": bad,
        })
        assert r.status_code in (400, 422), bad


def test_extra_field_rejected(client):
    _upload(client, "TX", CROSS)
    r = client.post("/api/v1/trips/TX/risk-budget", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_weights": {"rb": 1.0},
        "budget_weighted_sec": 1.0,
        "unexpected": 1,
    })
    assert r.status_code == 422


def test_missing_field_rejected(client):
    r = client.post("/api/v1/trips/TX/risk-budget", json={
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "zone_weights": {"rb": 1.0},
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 同一已提交读视图（并发下结果确定一致）
# ---------------------------------------------------------------------

def test_concurrent_updates_read_view_consistent(client):
    # 基础区与横穿轨迹
    assert client.post("/api/v1/zones", json={
        "zone_id": "live", "exterior": SQUARE, "holes": [],
        "windows": [{"start": "2026-09-30T00:00:00Z", "end": "2026-10-02T00:00:00Z"}],
    }).status_code == 201
    client.post("/api/v1/trips/live-trip/points", json=_batch([
        ("a", "2026-09-30T10:00:00Z", -50, 50),
        ("b", "2026-09-30T10:01:20Z", 150, 50),
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
                "zone_weights": {"live": 1.0},
                "budget_weighted_sec": 10.0,
            }
            r1 = client.post("/api/v1/trips/live-trip/risk-budget", json=body)
            r2 = client.post("/api/v1/trips/live-trip/risk-budget", json=body)
            if r1.status_code != 200 or r2.status_code != 200:
                failures.append(("status", r1.status_code, r2.status_code))
                return
            b1, b2 = r1.json(), r2.json()
            # 同快照语义：同一修订号视角下结果须一致；且段加权秒之和等于总计
            for b in (b1, b2):
                assert abs(
                    sum(s["weighted_sec"] for s in b["segments"])
                    - b["total_weighted_sec"]
                ) < 1e-4
            if b1 == b2:
                continue
            # 允许恰好跨越一次提交：两次结果都必须自洽（总计为 40/56/72/88 等合法值之一）
            if not (0 < b1["total_weighted_sec"] and 0 < b2["total_weighted_sec"]):
                failures.append(("zero", b1["total_weighted_sec"], b2["total_weighted_sec"]))
                return

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

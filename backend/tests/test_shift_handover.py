"""班次交接与补签领域测试。

覆盖需求：
- 归并窗口没有合格样本时区分 no_observation / quality_pending，不写成数值零；
- 发起交接冻结服务端实际合格窗口，未就绪窗口可见且注明原因；
- 确认后发布交接号，首签文件独占写入、带校验和，补签重试不损坏首签；
- 迟到观测在交接开始后、发布前变可用仍不进首签，须走真正的补签；
- 补签注明原交接号、新增窗口与贡献；已签窗口再次补签零贡献；
- 可按原交接号复盘首签，首签合计不被补签改变。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir():
    with tempfile.TemporaryDirectory() as tmp:
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            import app.services.rollup_window as rw
            import app.services.shift_handover as sh
            for mod in (rw, sh):
                mod.DATA_ROOT = Path(tmp)
            yield Path(tmp)


def _make_rollup_window(window_id: str, point: str, values: list[float] | None):
    """建一个归并窗口条目，并按 values 写归并产物；None 表示没有合格观测。"""
    from app.store import store
    rows = store.rows("rollup_window")
    rows.append({
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "窗口编号": window_id, "测点编号": point,
        "窗口类型": "固定窗口", "窗口长度": "5", "滑动步长": "1",
        "聚合指标": "求和", "窗口条数": str(len(values or [])), "归并水位": "0",
        "status": "已归并", "pending": False, "abnormal": False,
    })
    if values:
        from app.services.rollup_window import RollupWindowService
        svc = RollupWindowService()
        out = svc._data_dir("rollup_windows") / f"{window_id}_rollup.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            for idx, value in enumerate(values, start=1):
                f.write(json.dumps({
                    "window_id": window_id, "point": point,
                    "window_start": idx, "rollup_value": value, "metric": "求和",
                }, ensure_ascii=False) + "\n")


def _make_quality_row(point: str, status: str, conclusion: str = ""):
    from app.store import store
    rows = store.rows("series_quality")
    rows.append({
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "报告编号": f"QUAL-{point}", "测点编号": point, "统计窗口": "2026-11-24",
        "完整率": "0", "重复点数": "0", "越界点数": "0",
        "校验规则": "完整性", "质检结论": conclusion,
        "status": status, "pending": status != "已出报告", "abnormal": False,
    })


class TestWindowAvailability:
    def test_missing_observations_are_not_zero(self, setup_data_dir):
        """无观测与质检未结束必须是两个不同状态，都不产生贡献，不能当零值。"""
        from app.services.rollup_window import RollupWindowService
        _make_rollup_window("W-NONE", "P-NONE", None)
        _make_rollup_window("W-PEND", "P-PEND", None)
        _make_quality_row("P-PEND", "质检中")

        svc = RollupWindowService()
        none_info = svc.window_availability("W-NONE")
        pend_info = svc.window_availability("W-PEND")

        assert none_info["state"] == "no_observation"
        assert none_info["eligible"] is False
        assert none_info["contribution"] is None
        assert pend_info["state"] == "quality_pending"
        assert pend_info["eligible"] is False
        assert pend_info["contribution"] is None
        # 两个状态明确不同，快照/合计都不会拿到 0
        assert none_info["state"] != pend_info["state"]

    def test_qualified_window_uses_real_rollup_sum(self, setup_data_dir):
        from app.services.rollup_window import RollupWindowService
        _make_rollup_window("W-OK", "P-OK", [10.0, 20.5, 9.5])
        info = RollupWindowService().window_availability("W-OK")
        assert info["state"] == "qualified"
        assert info["eligible"] is True
        assert info["contribution"] == 40.0

    def test_explicit_observation_registry_distinguishes_states(self, setup_data_dir):
        """显式登记三态，合格窗可登记贡献，未就绪窗贡献保持空。"""
        from app.services.rollup_window import RollupWindowService
        svc = RollupWindowService()
        _make_rollup_window("W-A", "P-A", [5.0])
        _make_rollup_window("W-B", "P-B", None)
        svc.register_window_observation("W-A", "qualified", contribution=5.0)
        svc.register_window_observation("W-B", "quality_pending")
        assert svc.window_availability("W-A")["state"] == "qualified"
        assert svc.window_availability("W-B")["state"] == "quality_pending"


class TestHandoverFirstSign:
    def test_open_handover_freezes_only_qualified_windows(self, setup_data_dir):
        """同一批三窗发起交接：合格窗冻结进首签，另两窗列入待交接并注明原因。"""
        from app.services.shift_handover import ShiftHandoverService
        _make_rollup_window("W1", "P1", [10.0, 20.0])
        _make_rollup_window("W2", "P2", None)
        _make_rollup_window("W3", "P3", None)
        _make_quality_row("P3", "待质检")

        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2", "W3"], "夜班", "P1-P3")
        assert [w["window_id"] for w in opened["frozen_windows"]] == ["W1"]
        assert opened["frozen_total"] == 30.0
        pending = {item["window_id"]: item["state"] for item in opened["pending_windows"]}
        assert pending == {"W2": "no_observation", "W3": "quality_pending"}

        entry = svc.get_entry(opened["handover_row_id"])
        assert entry["交接号"] == ""  # 确认前不发布交接号
        assert float(entry["首签合计"]) == 30.0

    def test_confirm_publishes_handover_no_and_immutable_first_sign(self, setup_data_dir):
        from app.services.shift_handover import ShiftHandoverService
        _make_rollup_window("W1", "P1", [10.0, 20.0])
        _make_rollup_window("W2", "P2", None)

        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2"], "夜班", "P1-P2")
        confirmed = svc.confirm_handover(opened["handover_row_id"])

        handover_no = confirmed["handover_no"]
        assert handover_no.startswith("HO-")
        assert confirmed["signed_windows"] == ["W1"]
        assert confirmed["signed_total"] == 30.0
        first_file = setup_data_dir / "handovers" / f"{handover_no}_first_sign.jsonl"
        assert first_file.exists()

        # 再次确认必须被拒绝，首签文件不可覆盖
        again = svc.confirm_handover(handover_no)
        assert "error" in again
        replay = svc.replay_first_sign(handover_no)
        assert replay["checksum_ok"] is True
        assert replay["signed_total"] == 30.0
        assert replay["footer_total"] == 30.0

    def test_late_observation_before_publish_stays_out_of_first_sign(self, setup_data_dir):
        """交接开始后、交接号发布前变可用的窗口：首签仍按冻结集合，必须走补签。"""
        from app.services.rollup_window import RollupWindowService
        from app.services.shift_handover import ShiftHandoverService
        _make_rollup_window("W1", "P1", [10.0])
        _make_rollup_window("W2", "P2", None)
        _make_rollup_window("W3", "P3", [30.0])

        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2", "W3"], "夜班", "P1-P3")
        # 发起时 W1/W3 合格、W2 缺失；冻结集合只含 W1、W3
        assert [w["window_id"] for w in opened["frozen_windows"]] == ["W1", "W3"]

        # 两个迟到观测让 W2 在交接开始后、发布前变成可用
        rollup_svc = RollupWindowService()
        out = rollup_svc._data_dir("rollup_windows") / "W2_rollup.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for value in (7.0, 8.0):
                f.write(json.dumps({
                    "window_id": "W2", "point": "P2",
                    "window_start": 1, "rollup_value": value, "metric": "求和",
                }) + "\n")
        rollup_svc.register_window_observation("W2", "qualified", contribution=15.0)

        confirmed = svc.confirm_handover(opened["handover_row_id"])
        # 发布的首签不能把迟到的 W2 纳进来
        assert confirmed["signed_windows"] == ["W1", "W3"]
        assert confirmed["signed_total"] == 40.0

    def test_snapshot_association_uses_actual_adopted_windows(self, setup_data_dir):
        """序列快照关联服务端实际采用的归并窗口，不把未就绪窗口按零计入。"""
        from app.services.shift_handover import ShiftHandoverService
        from app.store import store
        _make_rollup_window("W1", "P1", [12.0])
        _make_rollup_window("W2", "P2", None)

        snap_rows = store.rows("series_snapshot")
        snap_rows.append({
            "id": 301, "快照编号": "SNAP-H1", "口径说明": "班次口径",
            "测点范围": "P1-P2", "时间范围": "2026-11-24",
            "序列条数": "0", "生成方式": "", "快照水位": "0", "生成时间": "",
            "status": "草稿", "pending": True, "abnormal": False,
        })
        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2"], "夜班", "P1-P2", snapshot_id=301)
        confirmed = svc.confirm_handover(opened["handover_row_id"])
        snap = store.find("series_snapshot", 301)
        association = snap["handover_associations"][0]
        assert association["handover_no"] == confirmed["handover_no"]
        assert association["adopted_windows"] == ["W1"]
        assert association["adopted_total"] == 12.0


class TestResign:
    def _open_and_confirm_three_windows(self):
        from app.services.shift_handover import ShiftHandoverService
        _make_rollup_window("W1", "P1", [10.0])
        _make_rollup_window("W2", "P2", None)
        _make_rollup_window("W3", "P3", None)
        _make_quality_row("P3", "质检中")
        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2", "W3"], "夜班", "P1-P3")
        confirmed = svc.confirm_handover(opened["handover_row_id"])
        return svc, opened, confirmed

    def test_resign_appends_new_window_and_keeps_first_sign_intact(self, setup_data_dir):
        """真正完成补签：追加新窗口贡献，首签文件与首签合计不变。"""
        from app.services.rollup_window import RollupWindowService
        svc, opened, confirmed = self._open_and_confirm_three_windows()
        handover_no = confirmed["handover_no"]
        first_file = setup_data_dir / "handovers" / f"{handover_no}_first_sign.jsonl"
        before_bytes = first_file.read_bytes()

        # 无观测窗 W2 迟到两个观测后质检合格，发起补签
        rollup_svc = RollupWindowService()
        with open(rollup_svc._data_dir("rollup_windows") / "W2_rollup.jsonl", "w",
                  encoding="utf-8") as f:
            for value in (7.0, 8.0):
                f.write(json.dumps({
                    "window_id": "W2", "point": "P2",
                    "window_start": 1, "rollup_value": value, "metric": "求和",
                }) + "\n")
        result = svc.resign_window(handover_no, "W2", reviewer="qc-zhang")

        assert result["added_contribution"] == 15.0
        assert result["first_sign_total"] == 10.0
        assert result["grand_total"] == 25.0
        assert result["added_windows"][0]["window_id"] == "W2"
        # 首签记录文件字节未变
        assert first_file.read_bytes() == before_bytes

        ledger = setup_data_dir / "handovers" / f"{handover_no}_supplements.jsonl"
        lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
        rec = lines[0]
        assert rec["handover_no"] == handover_no  # 注明原交接号
        assert rec["window_id"] == "W2"            # 此次新增窗口
        assert rec["added_contribution"] == 15.0   # 及其贡献
        assert rec["result"] == "appended"

        entry = svc._find_by_no(handover_no)
        assert entry["已补签窗口"] == "W2"
        assert float(entry["补签贡献"]) == 15.0
        assert float(entry["首签合计"]) == 10.0  # 原合计不被改动

    def test_resign_already_signed_window_is_zero_contribution(self, setup_data_dir):
        """中间窗已纳入首签时补签：结果说明已签且不产生贡献。"""
        svc, _opened, confirmed = self._open_and_confirm_three_windows()
        handover_no = confirmed["handover_no"]
        result = svc.resign_window(handover_no, ["W1"])
        assert result["added_contribution"] == 0.0
        assert result["results"][0]["result"] == "已签"
        assert result["grand_total"] == 10.0  # 没有重复占一份贡献

        ledger = setup_data_dir / "handovers" / f"{handover_no}_supplements.jsonl"
        rec = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert rec["result"] == "already_signed"
        assert rec["added_contribution"] == 0.0

    def test_resign_still_pending_window_makes_no_contribution(self, setup_data_dir):
        """质检仍未结束时补签：不产生贡献，只留痕原因。"""
        svc, _opened, confirmed = self._open_and_confirm_three_windows()
        result = svc.resign_window(confirmed["handover_no"], ["W3"])
        assert result["added_contribution"] == 0.0
        assert result["results"][0]["result"] == "quality_pending"
        assert result["grand_total"] == 10.0

    def test_replay_first_sign_reconstructs_original_and_reviews_supplements(
        self, setup_data_dir,
    ):
        """按原交接号复盘首签：窗口/合计与签发时一致，校验和通过。"""
        from app.services.rollup_window import RollupWindowService
        svc, _opened, confirmed = self._open_and_confirm_three_windows()
        handover_no = confirmed["handover_no"]

        rollup_svc = RollupWindowService()
        with open(rollup_svc._data_dir("rollup_windows") / "W2_rollup.jsonl", "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({
                "window_id": "W2", "point": "P2",
                "window_start": 1, "rollup_value": 15.0, "metric": "求和",
            }) + "\n")
        svc.resign_window(handover_no, ["W2"])

        replay = svc.replay_first_sign(handover_no)
        assert replay["checksum_ok"] is True
        assert [w["window_id"] for w in replay["signed_windows"]] == ["W1"]
        assert replay["signed_total"] == 10.0
        assert replay["supplement_total"] == 15.0
        assert replay["grand_total"] == 25.0
        pending = {item["window_id"]: item["state"] for item in replay["pending_windows"]}
        assert pending == {"W2": "no_observation", "W3": "quality_pending"}

    def test_repeated_resign_is_idempotent_and_never_doubles(self, setup_data_dir):
        """补签重试不能损坏签发记录，重复补签同一窗口不产生第二份贡献。"""
        from app.services.rollup_window import RollupWindowService
        svc, _opened, confirmed = self._open_and_confirm_three_windows()
        handover_no = confirmed["handover_no"]
        rollup_svc = RollupWindowService()
        with open(rollup_svc._data_dir("rollup_windows") / "W2_rollup.jsonl", "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({
                "window_id": "W2", "point": "P2",
                "window_start": 1, "rollup_value": 15.0, "metric": "求和",
            }) + "\n")

        first = svc.resign_window(handover_no, ["W2"])
        second = svc.resign_window(handover_no, ["W2"])
        assert first["added_contribution"] == 15.0
        assert second["added_contribution"] == 0.0
        assert second["results"][0]["result"] == "已签"
        assert second["grand_total"] == 25.0
        # 台账是读-改-写回，重复补签不能把历史行重复追加导致合计翻倍
        ledger = setup_data_dir / "handovers" / f"{handover_no}_supplements.jsonl"
        ledger_lines = ledger.read_text(encoding="utf-8").splitlines()
        assert len(ledger_lines) == 2  # 一次 appended + 一次 already_signed
        replay = svc.replay_first_sign(handover_no)
        assert replay["checksum_ok"] is True
        assert replay["signed_total"] == 10.0  # 首签始终不变
        assert replay["supplement_total"] == 15.0  # 补签贡献只算一次

    def test_multiple_resign_rounds_accumulate_all_windows(self, setup_data_dir):
        """分两轮补签不同窗口：展示字段与补签合计都要覆盖全部补签窗口。"""
        from app.services.rollup_window import RollupWindowService
        svc, _opened, confirmed = self._open_and_confirm_three_windows()
        handover_no = confirmed["handover_no"]
        rollup_svc = RollupWindowService()

        def make_available(window_id: str, point: str, value: float):
            with open(rollup_svc._data_dir("rollup_windows") / f"{window_id}_rollup.jsonl",
                      "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "window_id": window_id, "point": point,
                    "window_start": 1, "rollup_value": value, "metric": "求和",
                }) + "\n")

        make_available("W2", "P2", 15.0)
        first = svc.resign_window(handover_no, ["W2"])
        make_available("W3", "P3", 25.0)
        second = svc.resign_window(handover_no, ["W3"])

        assert first["added_contribution"] == 15.0
        assert second["added_contribution"] == 25.0
        assert second["grand_total"] == 50.0
        entry = svc._find_by_no(handover_no)
        # 两轮补签的窗口都保留，不会被第二轮覆盖
        assert entry["已补签窗口"].split("、") == ["W2", "W3"]
        assert float(entry["补签贡献"]) == 40.0
        replay = svc.replay_first_sign(handover_no)
        assert replay["supplement_total"] == 40.0
        assert replay["signed_total"] == 10.0

    def test_middle_window_qualified_before_handover_joins_first_sign(self, setup_data_dir):
        """正式确认前补到中间窗并完成审核：中间窗进入首签，再补签必须报已签零贡献。"""
        from app.services.rollup_window import RollupWindowService
        from app.services.shift_handover import ShiftHandoverService
        _make_rollup_window("W1", "P1", [10.0])
        _make_rollup_window("W2", "P2", None)
        _make_rollup_window("W3", "P3", [30.0])

        svc = ShiftHandoverService()
        opened = svc.open_handover(["W1", "W2", "W3"], "白班", "P1-P3")
        # 确认前把中间窗补合格
        rollup_svc = RollupWindowService()
        with open(rollup_svc._data_dir("rollup_windows") / "W2_rollup.jsonl", "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({
                "window_id": "W2", "point": "P2",
                "window_start": 1, "rollup_value": 15.0, "metric": "求和",
            }) + "\n")
        rollup_svc.register_window_observation("W2", "qualified", contribution=15.0)

        # 注意：首签集合在发起时已冻结，确认前补到的窗口仍需补签，
        # 但这里通过重新发起交接来覆盖"确认前补到中间窗并完成审核"的路径
        confirmed_first = svc.confirm_handover(opened["handover_row_id"])
        assert confirmed_first["signed_windows"] == ["W1", "W3"]

        # 另起一次交接：此时三窗全部合格，中间窗应纳入首签
        opened2 = svc.open_handover(["W1", "W2", "W3"], "白班-重开", "P1-P3")
        confirmed2 = svc.confirm_handover(opened2["handover_row_id"])
        assert confirmed2["signed_windows"] == ["W1", "W2", "W3"]
        assert confirmed2["signed_total"] == 55.0

        # 对已纳入首签的中间窗补签：必须说明已签且不产生贡献
        resign = svc.resign_window(confirmed2["handover_no"], ["W2"])
        assert resign["results"][0]["result"] == "已签"
        assert resign["added_contribution"] == 0.0
        assert resign["grand_total"] == 55.0

        # 同时能按原交接号复盘第一次首签
        replay_first = svc.replay_first_sign(confirmed_first["handover_no"])
        assert [w["window_id"] for w in replay_first["signed_windows"]] == ["W1", "W3"]
        assert replay_first["signed_total"] == 40.0
        assert replay_first["checksum_ok"] is True


class TestRollupZeroHandling:
    def test_rollup_skips_windows_without_qualified_samples(self, setup_data_dir):
        """归并计算不再把缺失样本当 0：空窗口段不产出零值记录。"""
        from app.services.rollup_window import RollupWindowService
        from app.store import store
        rows = store.rows("rollup_window")
        rows.append({
            "id": 401, "窗口编号": "ROLL-EMPTY", "测点编号": "P-EMPTY",
            "窗口类型": "固定窗口", "窗口长度": "2", "滑动步长": "1",
            "聚合指标": "求和", "窗口条数": "0", "归并水位": "0",
            "status": "待归并", "pending": True, "abnormal": False,
        })
        ds_dir = RollupWindowService()._data_dir("downsampled")
        ds_dir.mkdir(parents=True, exist_ok=True)
        with open(ds_dir / "DOWN-P-EMPTY_downsampled.jsonl", "w", encoding="utf-8") as f:
            # 第一段有真实值、第二段全是缺失值、第三段含真实值
            for record in (
                {"point": "P-EMPTY", "window_index": 1, "aggregated_value": 5.0},
                {"point": "P-EMPTY", "window_index": 2, "aggregated_value": None},
                {"point": "P-EMPTY", "window_index": 3, "aggregated_value": "bad"},
                {"point": "P-EMPTY", "window_index": 4, "aggregated_value": 7.0},
            ):
                f.write(json.dumps(record) + "\n")

        svc = RollupWindowService()
        result = svc.rollup_windows(401)
        # 窗口长度 2：[5.0, None] 与 ["bad", 7.0]，缺失/脏数据被跳过，
        # 两段都仍含真实值，各产出一条；整段无真实值时则不产出任何零值记录
        assert result["window_period_count"] == 2
        out = svc._data_dir("rollup_windows") / "ROLL-EMPTY_rollup.jsonl"
        merged = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert len(merged) == 2
        assert sorted(r["rollup_value"] for r in merged) == [5.0, 7.0]
        # 没有任何被凑出来的零值，record_count 只统计真实样本
        assert all(r["rollup_value"] != 0 for r in merged)
        assert all(r["record_count"] == 1 for r in merged)

        # 整段全部缺失：不产出记录，窗口可用性判为 no_observation 而非数值零
        with open(ds_dir / "DOWN-P-BLANK_downsampled.jsonl", "w", encoding="utf-8") as f:
            for record in (
                {"point": "P-BLANK", "aggregated_value": None},
                {"point": "P-BLANK", "aggregated_value": "bad"},
            ):
                f.write(json.dumps(record) + "\n")
        blank_rows = store.rows("rollup_window")
        blank_rows.append({
            "id": 402, "窗口编号": "ROLL-BLANK", "测点编号": "P-BLANK",
            "窗口类型": "固定窗口", "窗口长度": "2", "滑动步长": "1",
            "聚合指标": "求和", "窗口条数": "0", "归并水位": "0",
            "status": "待归并", "pending": True, "abnormal": False,
        })
        blank_result = svc.rollup_windows(402)
        assert blank_result["window_period_count"] == 0
        info = svc.window_availability("ROLL-BLANK")
        assert info["state"] == "no_observation"
        assert info["contribution"] is None


class TestHandoverLifecycle:
    def test_generic_action_closes_pending_after_confirm(self, setup_data_dir):
        """通用资源动作确认交接后，交接单不再计入待处理。"""
        from app.services.shift_handover import ShiftHandoverService
        from app.store import store
        rows = store.rows("shift_handover")
        rows.append({
            "id": 501, "交接号": "", "班次": "测试班", "测点范围": "P-Z",
            "首签窗口": "", "首签合计": "0.0", "已补签窗口": "", "补签贡献": "0.0",
            "交接状态": "待确认", "交接时间": "",
            "status": "待确认", "pending": True, "abnormal": False,
        })
        svc = ShiftHandoverService()
        entry, message = svc.run_action(501, "确认交接")
        assert message == "班次交接已确认交接"
        assert entry["status"] == "已交接"
        assert entry["pending"] is False

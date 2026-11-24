"""窗口归并领域服务：维护固定、滑动与会话窗口的时序归并聚合指标与窗口条数。
对降采样后的序列数据按窗口类型进行窗口归并计算，生成归并结果数据集。

窗口观测三态（交接/快照据此判断窗口能否计入合计）：
- qualified       有合格观测且质检结束，可以交接，贡献为归并值合计；
- no_observation  窗口确实没有观测到合格样本，不能当成数值 0 写入合计；
- quality_pending 观测已到但质检还没结束，同样不能计入合计，待质检完成后补签。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 观测登记文件中每个窗口只保留一条最新状态记录
OBSERVATION_REGISTRY = "window_observations.jsonl"

# 尚未具备交接条件的观测状态：无观测与质检未结束都不是数值零
INELIGIBLE_STATES = {"no_observation", "quality_pending", "quality_failed", "unknown"}

STATE_REASONS = {
    "qualified": "",
    "no_observation": "窗口没有合格观测样本，暂不能交接",
    "quality_pending": "质检尚未结束，窗口暂不能交接",
    "quality_failed": "质检结论不合格，窗口不能交接",
    "unknown": "窗口不存在或尚未完成归并",
}


def _is_number(value: Any) -> bool:
    """区分真正的数值与缺失/脏数据，缺失值不得按 0 参与聚合。"""
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


class RollupWindowService(BaseModuleService):
    MODULE = "rollup_window"
    ENTITY = "归并窗口"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["窗口编号", "测点编号", "窗口类型", "窗口长度", "滑动步长", "聚合指标", "窗口条数", "归并水位"]
    REQUIRED_FIELDS = ["窗口编号", "测点编号", "窗口类型"]
    STATUS_ORDER = ["草稿", "待归并", "已归并", "已作废"]
    ACTION_RULES = {"启动归并": "待归并", "确认归并": "已归并", "标记作废": "已作废"}
    # 数据归并操作：按窗口归并规则执行聚合计算生成归并结果数据集
    NEGATIVE_ACTIONS = []

    # -- 窗口观测登记：合格 / 无观测 / 质检未结束 --

    def _observation_file(self):
        return self._data_dir("rollup_windows") / OBSERVATION_REGISTRY

    def register_window_observation(
        self,
        window_id: str,
        observation_state: str,
        *,
        contribution: float | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记窗口观测状态。质检未结束与确实无观测必须显式区分，不能合并成数值零。"""
        if observation_state not in STATE_REASONS:
            return {"error": f"未知观测状态：{observation_state}"}
        records = self._read_jsonl(self._observation_file())
        records = [r for r in records if r.get("window_id") != window_id]
        record = {
            "window_id": window_id,
            "observation_state": observation_state,
            "contribution": contribution,
            "note": note,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        records.append(record)
        # 登记表是读-改-写回，必须覆盖写，避免同一窗口状态被重复追加
        self._overwrite_jsonl(self._observation_file(), records)
        return record

    def get_window_observation(self, window_id: str) -> dict[str, Any] | None:
        for record in self._read_jsonl(self._observation_file()):
            if record.get("window_id") == window_id:
                return record
        return None

    def _find_window_entry(self, window_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if str(row.get("窗口编号")) == str(window_id):
                return row
        return None

    def window_rollup_values(self, window_id: str) -> list[float]:
        """读取窗口归并产物中的真实数值；缺失/脏数据一律跳过，不补零。"""
        rollup_file = self._data_dir("rollup_windows") / f"{window_id}_rollup.jsonl"
        values: list[float] = []
        for record in self._read_jsonl(rollup_file):
            value = record.get("rollup_value")
            if _is_number(value):
                values.append(float(value))
        return values

    def window_availability(self, window_id: str) -> dict[str, Any]:
        """判定窗口当前能否交接及其贡献。

        显式观测登记优先；没有登记时从归并产物和质检状态推导：
        没有任何数值样本即 no_observation，质检行处于待质检/质检中即 quality_pending。
        """
        registered = self.get_window_observation(window_id)
        values = self.window_rollup_values(window_id)
        entry = self._find_window_entry(window_id)
        point_key = str(entry.get("测点编号", "")) if entry else ""

        if registered is not None:
            state = registered["observation_state"]
            contribution = registered.get("contribution")
            if contribution is None and state == "qualified":
                contribution = round(sum(values), 4)
        elif values:
            # 归并产物里存在真实数值即视为已具备合格观测；
            # 只有在没有数值时，才需要区分"确实无观测"与"质检还没结束"。
            quality_rows = [
                row for row in store.rows("series_quality")
                if str(row.get("测点编号", "")) == point_key
            ]
            if any(row.get("质检结论") == "不合格" for row in quality_rows):
                state = "quality_failed"
                contribution = None
            else:
                state = "qualified"
                contribution = round(sum(values), 4)
        else:
            # 没有任何数值样本：可能确实无观测，也可能质检还没结束，
            # 若该测点存在未完结质检行则判为质检未结束，否则判为无观测。
            quality_rows = [
                row for row in store.rows("series_quality")
                if str(row.get("测点编号", "")) == point_key
            ]
            if any(row.get("status") in ("待质检", "质检中") for row in quality_rows):
                state = "quality_pending"
            else:
                state = "no_observation"
            contribution = None

        return {
            "window_id": str(window_id),
            "point": point_key,
            "state": state,
            "eligible": state == "qualified",
            "contribution": contribution,
            "reason": STATE_REASONS.get(state, state),
        }

    def rollup_windows(self, rollup_id: int) -> dict[str, Any]:
        """按窗口归并规则对降采样后的时序数据执行聚合计算，生成归并结果数据集。"""
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        self.run_action(rollup_id, "启动归并")
        point_key = entry["测点编号"]
        window_type = entry.get("窗口类型", "固定窗口")
        window_len = int(entry.get("窗口长度", "5"))
        agg_metric = entry.get("聚合指标", "均值")

        # 读取降采样后的数据
        ds_files = list(self._data_dir("downsampled").glob(f"*{point_key}*_downsampled.jsonl")) if self._data_dir("downsampled").exists() else []
        records = []
        for f in ds_files[:3]:
            records.extend(self._read_jsonl(f))
        if not records:
            filtered_file = self._data_dir("filtered_series") / f"{point_key}_filtered.jsonl"
            records = self._read_jsonl(filtered_file)

        # 按窗口类型执行归并聚合计算
        merged = []
        for i in range(0, len(records), window_len):
            window = records[i:i + window_len]
            values = []
            # 数据产物输出：写入归并聚合结果到窗口数据JSONL文件
            for r in window:
                v = r.get("aggregated_value", r.get("calibrated_value_V"))
                # 缺失/脏数据不是数值零：跳过，避免把"没有合格样本"写成 0
                if _is_number(v):
                    values.append(float(v))
            if not values:
                # 该窗口段没有合格样本，不产出零值归并记录
                continue
            if agg_metric in ("均值", "avg"):
                agg_val = round(sum(values) / len(values), 4)
            elif agg_metric in ("求和", "sum"):
                agg_val = round(sum(values), 4)
            elif agg_metric in ("最大值", "max"):
                agg_val = round(max(values), 4)
            else:
                agg_val = round(sum(values) / len(values), 4)
            merged.append({
                "window_id": entry["窗口编号"],
                "point": point_key,
                "window_type": window_type,
                "window_start": i + 1,
                "window_end": i + len(window),
                "record_count": len(values),
                "rollup_value": agg_val,
                "metric": agg_metric,
            })
        # 将窗口归并结果写入归并窗口数据文件
        output_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        written = self._write_jsonl(output_file, merged)
        entry["窗口条数"] = str(written)
        entry["归并水位"] = str(written)
        self.run_action(rollup_id, "确认归并")
        return {"rollup_id": entry["窗口编号"], "point": point_key,
                "window_type": window_type, "window_period_count": written,
                "metric": agg_metric}

    def compute_cumulative_rollup(self, rollup_id: int) -> dict[str, Any]:
        """对归并窗口数据计算累计聚合指标写入累计快照数据文件。"""
        entry = store.find(self.MODULE, rollup_id)
        if entry is None:
            return {"error": f"归并窗口 {rollup_id} 不存在"}
        # 先执行归并
        rollup_result = self.rollup_windows(rollup_id)
        if "error" in rollup_result:
            return rollup_result
        # 读取归并结果计算累计
        rollup_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_rollup.jsonl"
        records = self._read_jsonl(rollup_file)
        cumulative = []
        running_sum = 0.0
        for i, r in enumerate(records):
            # 累计只认真实数值，缺失记录不按 0 推进水位
            if not _is_number(r.get("rollup_value")):
                continue
            val = float(r["rollup_value"])
            running_sum += val
            cumulative.append({
                "window_index": i + 1,
                "point": r["point"],
                "window_value": val,
                "cumulative_value": round(running_sum, 4),
                "window_id": r["window_id"],
            })
        # 写入累计聚合数据文件
        cum_file = self._data_dir("rollup_windows") / f"{entry['窗口编号']}_cumulative.jsonl"
        self._write_jsonl(cum_file, cumulative)
        return {"rollup_id": entry["窗口编号"], "cumulative_periods": len(cumulative),
                "final_cumulative_value": round(running_sum, 4)}

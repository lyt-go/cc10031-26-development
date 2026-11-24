"""序列快照领域服务：维护降采样与归并结果的口径说明版本快照与快照水位。
生成指定口径的序列数据版本快照并写入归档快照数据文件。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.rollup_window import _is_number
from app.store import store


class SeriesSnapshotService(BaseModuleService):
    MODULE = "series_snapshot"
    ENTITY = "版本快照"
    OPERATION_TERM = "归并"
    LIST_FIELDS = ["快照编号", "口径说明", "测点范围", "时间范围", "序列条数", "生成方式", "快照水位", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "口径说明", "测点范围"]
    STATUS_ORDER = ["草稿", "待确认", "已发布", "已归档"]
    ACTION_RULES = {"提交确认": "待确认", "发布快照": "已发布", "标记归档": "已归档"}
    # 数据归并操作：生成指定口径降采样与归并结果版本快照写入快照归档数据
    NEGATIVE_ACTIONS = []

    def create_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """生成指定口径的降采样与归并结果版本快照，写入快照归档数据文件。"""
        snap = store.find(self.MODULE, snapshot_id)
        if snap is None:
            return {"error": f"版本快照 {snapshot_id} 不存在"}
        self.run_action(snapshot_id, "提交确认")
        caliber = snap.get("口径说明", "标准口径")
        point_range = snap.get("测点范围", "")
        time_range = snap.get("时间范围", "")

        # 扫描归并结果作为快照数据源；没有合格观测的窗口不会产出零值记录，
        # 快照不得把"无观测/质检未结束"当成数值零写入合计。
        snapshot_data = []
        rollup_dir = self._data_dir("rollup_windows")
        if rollup_dir.exists():
            for f in rollup_dir.glob("*_rollup.jsonl"):
                records = self._read_jsonl(f)
                for r in records:
                    if not _is_number(r.get("rollup_value")):
                        continue
                    snapshot_data.append({
                        "snapshot_id": snap["快照编号"],
                        "caliber": caliber,
                        "point": r.get("point", ""),
                        "window_index": r.get("window_start", 0),
                        "rollup_value": r.get("rollup_value"),
                        "metric": r.get("metric", ""),
                    # 数据归档输出：将已发布快照归档到冷区存储写入快照归档日志文件
                    })

        ds_dir = self._data_dir("downsampled")
        if ds_dir.exists():
            for f in ds_dir.glob("*_downsampled.jsonl"):
                records = self._read_jsonl(f)
                for r in records:
                    if not _is_number(r.get("aggregated_value")):
                        continue
                    snapshot_data.append({
                        "snapshot_id": snap["快照编号"],
                        "caliber": caliber,
                        "point": r.get("point", ""),
                        "window_index": r.get("window_index", 0),
                        "aggregated_value": r.get("aggregated_value"),
                        "method": r.get("method", ""),
                    })
        # 也包含质检通过的数据；质检未完成时不存在结论记录，不会以零值占位
        quality_dir = self._data_dir("quality_reports")
        if quality_dir.exists():
            for f in quality_dir.glob("*_quality.jsonl"):
                records = self._read_jsonl(f)
                for r in records:
                    if not _is_number(r.get("completeness")):
                        continue
                    snapshot_data.append({
                        "snapshot_id": snap["快照编号"],
                        "caliber": caliber,
                        "quality_check_point": r.get("point", ""),
                        "conclusion": r.get("conclusion", ""),
                        "completeness": r.get("completeness"),
                    })

        # 将快照数据写入序列版本快照归档文件
        output_file = self._data_dir("snapshots") / f"{snap['快照编号']}_snapshot.jsonl"
        written = self._write_jsonl(output_file, snapshot_data)
        snap["序列条数"] = str(written)
        snap["生成方式"] = "归并+降采样+质量聚合"
        snap["快照水位"] = str(written)
        snap["生成时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        self.run_action(snapshot_id, "发布快照")
        return {"snapshot_id": snap["快照编号"], "caliber": caliber,
                "records_in_snapshot": written}

    def archive_snapshot(self, snapshot_id: int) -> dict[str, Any]:
        """将已发布快照归档到冷区存储并写入快照归档日志文件。"""
        snap = store.find(self.MODULE, snapshot_id)
        if snap is None:
            return {"error": f"版本快照 {snapshot_id} 不存在"}
        self.run_action(snapshot_id, "标记归档")
        snap_file = self._data_dir("snapshots") / f"{snap['快照编号']}_snapshot.jsonl"
        records = self._read_jsonl(snap_file)
        # 归档记录到冷区
        self._write_jsonl(
            self._data_dir("snapshots") / f"{snap['快照编号']}_archived.jsonl",
            [{"snapshot_id": snap["快照编号"], "archived_at": time.time(),
              "record_count": len(records), "cold_path": f"/cold/snapshots/{snap['快照编号']}"}]
        )
        return {"snapshot_id": snap["快照编号"], "archived_records": len(records),
                "status": "已归档"}
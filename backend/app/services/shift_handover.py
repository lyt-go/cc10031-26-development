"""班次交接领域服务：以归并窗口为单位组织班次交接、交接号发布与迟到观测补签。

关键规则：
1. 发起交接时按服务端实际可用性冻结窗口集合——只有 qualified 窗口纳入首签；
   no_observation（确实无观测）与 quality_pending（质检未结束）都不能当成数值零写入合计，
   而是列入未交接窗口并注明原因，值班人员可以看到。
2. 确认交接后才发布交接号，首签记录写入只读文件（x 独占创建 + 校验和），
   任何补签重试都不会改动首签文件与首签合计。
3. 补签只向追加式台账写入，必须注明原交接号、本次新增窗口及其贡献；
   已经签过的窗口再次补签返回"已签"且贡献为 0，不会重复占一份贡献。
4. 可按原交接号复盘首签，复盘结果与首签文件校验和一致。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.rollup_window import RollupWindowService
from app.store import store


class ShiftHandoverService(BaseModuleService):
    MODULE = "shift_handover"
    ENTITY = "班次交接"
    OPERATION_TERM = "交接"
    LIST_FIELDS = [
        "交接号", "班次", "测点范围", "首签窗口", "首签合计",
        "已补签窗口", "补签贡献", "交接状态", "交接时间",
    ]
    REQUIRED_FIELDS = ["班次", "测点范围"]
    STATUS_ORDER = ["待确认", "已交接", "已补签"]
    ACTION_RULES = {"确认交接": "已交接", "完成补签": "已补签"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.rollup_service = RollupWindowService()

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """已交接/已补签都是闭环状态，进入后不再计入待处理。"""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and entry.get("status") in ("已交接", "已补签"):
            entry["pending"] = False
        return entry, message

    # -- 文件布局 --

    def _handover_dir(self):
        return self._data_dir("handovers")

    def _first_sign_file(self, handover_no: str):
        return self._handover_dir() / f"{handover_no}_first_sign.jsonl"

    def _supplement_file(self, handover_no: str):
        return self._handover_dir() / f"{handover_no}_supplements.jsonl"

    # -- 仓库辅助 --

    def _find_by_no(self, handover_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if str(row.get("交接号")) == str(handover_no):
                return row
        return None

    def _resolve_handover(self, ref: int | str) -> dict[str, Any] | None:
        """支持按行 id 或交接号定位交接单。"""
        if isinstance(ref, int) or str(ref).isdigit():
            found = store.find(self.MODULE, int(ref))
            if found is not None:
                return found
        return self._find_by_no(str(ref))

    @staticmethod
    def _round(value: float) -> float:
        return round(value + 0.0, 4)

    # -- 窗口盘点 --

    def inspect_windows(self, window_ids: list[str]) -> dict[str, Any]:
        """盘点一批窗口的实际可用性，供交接与页面展示使用。"""
        windows, pending, total = [], [], 0.0
        for window_id in window_ids:
            info = self.rollup_service.window_availability(window_id)
            windows.append(info)
            if info["eligible"]:
                total += float(info["contribution"] or 0.0)
            else:
                pending.append({
                    "window_id": info["window_id"],
                    "state": info["state"],
                    "reason": info["reason"],
                })
        return {
            "windows": windows,
            "eligible_windows": [w["window_id"] for w in windows if w["eligible"]],
            "pending_windows": pending,
            "eligible_total": self._round(total),
        }

    def list_pending_windows(self, window_ids: list[str]) -> list[dict[str, Any]]:
        """值班人员需要看到的、当前还交不出去的窗口及原因。"""
        return self.inspect_windows(window_ids)["pending_windows"]

    # -- 发起交接（冻结当时合格窗口，交接号在确认时才发布） --

    def open_handover(
        self,
        window_ids: list[str],
        shift: str,
        point_range: str,
        snapshot_id: int | None = None,
    ) -> dict[str, Any]:
        """用同一批窗口发起交接：冻结服务端实际合格的窗口，未就绪窗口单列原因。"""
        if not window_ids:
            return {"error": "交接至少需要一个归并窗口"}
        inspection = self.inspect_windows(list(window_ids))

        rows = store.rows(self.MODULE)
        entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        frozen_windows = [
            {
                "window_id": w["window_id"],
                "point": w["point"],
                "contribution": self._round(float(w["contribution"] or 0.0)),
            }
            for w in inspection["windows"] if w["eligible"]
        ]
        entry: dict[str, Any] = {
            "id": entry_id,
            # 交接号在确认发布前为空，避免窗口在交接开始后、发布前被重新纳入首签
            "交接号": "",
            "班次": shift,
            "测点范围": point_range,
            "首签窗口": "、".join(w["window_id"] for w in frozen_windows),
            "首签合计": str(self._round(inspection["eligible_total"])),
            "已补签窗口": "",
            "补签贡献": "0.0",
            "交接状态": "待确认",
            "交接时间": "",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
            # 冻结结果与请求范围不放进展示字段，单独承载
            "frozen_windows": frozen_windows,
            "requested_windows": [str(w) for w in window_ids],
            "pending_windows": inspection["pending_windows"],
            "snapshot_id": snapshot_id,
            "supplement_windows": [],
        }
        rows.append(entry)
        return {
            "handover_row_id": entry_id,
            "status": "待确认",
            "frozen_windows": frozen_windows,
            "frozen_total": self._round(inspection["eligible_total"]),
            "pending_windows": inspection["pending_windows"],
        }

    # -- 确认交接：发布交接号并写入不可变首签文件 --

    def confirm_handover(self, handover_ref: int | str) -> dict[str, Any]:
        entry = self._resolve_handover(handover_ref)
        if entry is None:
            return {"error": "交接单不存在"}
        if entry["status"] != "待确认":
            return {"error": f"交接单当前状态为{entry['status']}，不能重复确认"}

        handover_no = "HO-{date}-{seq:04d}".format(
            date=time.strftime("%Y%m%d", time.localtime()),
            seq=len(store.rows(self.MODULE)),
        )
        frozen_windows = entry["frozen_windows"]
        total = self._round(sum(float(w["contribution"]) for w in frozen_windows))

        header = {
            "record_type": "first_sign_header",
            "handover_no": handover_no,
            "shift": entry["班次"],
            "point_range": entry["测点范围"],
            "requested_windows": entry["requested_windows"],
            "pending_windows": entry["pending_windows"],
            "signed_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        lines = [json.dumps(header, ensure_ascii=False)]
        lines.extend(json.dumps({
            "record_type": "first_sign_window",
            "handover_no": handover_no,
            **window,
        }, ensure_ascii=False) for window in frozen_windows)
        checksum = self._compute_checksum("\n".join(lines))
        lines.append(json.dumps({
            "record_type": "first_sign_footer",
            "handover_no": handover_no,
            "signed_window_count": len(frozen_windows),
            "signed_total": total,
            "checksum": checksum,
        }, ensure_ascii=False))

        first_sign_file = self._first_sign_file(handover_no)
        try:
            # 独占创建：首签文件一旦发布即不可覆盖，补签重试无法损坏签发记录
            with open(first_sign_file, "x", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except FileExistsError:
            return {"error": f"交接号 {handover_no} 的首签记录已存在，禁止覆盖"}

        entry["交接号"] = handover_no
        entry["交接状态"] = "已交接"
        entry["status"] = "已交接"
        entry["pending"] = False
        entry["交接时间"] = header["signed_at"]
        entry["首签合计"] = str(total)
        entry["first_sign_checksum"] = checksum

        # 若由序列快照发起，把服务端实际采用的窗口关联回快照
        snapshot_id = entry.get("snapshot_id")
        if snapshot_id is not None:
            self._attach_to_snapshot(snapshot_id, handover_no, frozen_windows, total)

        return {
            "handover_no": handover_no,
            "signed_windows": [w["window_id"] for w in frozen_windows],
            "signed_total": total,
            "pending_windows": entry["pending_windows"],
            "checksum": checksum,
            "first_sign_file": str(first_sign_file),
        }

    def _attach_to_snapshot(
        self,
        snapshot_id: int,
        handover_no: str,
        frozen_windows: list[dict[str, Any]],
        total: float,
    ) -> dict[str, Any]:
        """序列快照关联服务端实际采用的归并窗口，而不是把请求窗口全部按零计入。"""
        snap = store.find("series_snapshot", snapshot_id)
        if snap is None:
            return {"error": f"版本快照 {snapshot_id} 不存在"}
        association = {
            "handover_no": handover_no,
            "adopted_windows": [w["window_id"] for w in frozen_windows],
            "adopted_total": total,
            "associated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        associations = snap.setdefault("handover_associations", [])
        associations.append(association)
        snap["快照水位"] = str(len(associations))
        return association

    # -- 补签：只追加，不碰首签 --

    def resign_window(
        self,
        handover_ref: int | str,
        window_ids: list[str] | str,
        *,
        reviewer: str = "",
    ) -> dict[str, Any]:
        """对已发布交接补签窗口。

        - 窗口已在首签或既往补签中：结果说明已签，本次贡献 0，不重复占份额；
        - 窗口现已 qualified：追加一次贡献，写入补签台账，可按原交接号复盘首签；
        - 窗口仍未就绪：记录原因，不产生贡献。
        """
        entry = self._resolve_handover(handover_ref)
        if entry is None:
            return {"error": "交接单不存在"}
        if not entry.get("交接号"):
            return {"error": "交接尚未确认发布，没有可补签的交接号"}
        if isinstance(window_ids, str):
            window_ids = [window_ids]

        handover_no = entry["交接号"]
        already_signed = {w["window_id"] for w in entry.get("frozen_windows", [])}
        already_signed.update(entry.get("supplement_windows", []))

        supplement_records = self._read_jsonl(self._supplement_file(handover_no))
        results = []
        added_windows: list[dict[str, Any]] = []
        added_contribution = 0.0

        for window_id in window_ids:
            info = self.rollup_service.window_availability(window_id)
            base = {
                "record_type": "supplement",
                "handover_no": handover_no,
                "window_id": info["window_id"],
                "reviewer": reviewer,
                "supplemented_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            }
            if info["window_id"] in already_signed:
                # 原来已经签完的窗不能再占一份贡献
                record = {**base, "result": "already_signed",
                          "state": "qualified", "added_contribution": 0.0,
                          "note": "窗口已纳入该交接（首签或既往补签），不重复计贡献"}
                results.append({"window_id": info["window_id"], "result": "已签",
                                "added_contribution": 0.0})
            elif info["eligible"]:
                contribution = self._round(float(info["contribution"] or 0.0))
                record = {**base, "result": "appended",
                          "state": "qualified", "added_contribution": contribution,
                          "point": info["point"],
                          "note": f"补签追加窗口 {info['window_id']} 贡献 {contribution}"}
                added_windows.append({"window_id": info["window_id"],
                                      "point": info["point"],
                                      "contribution": contribution})
                added_contribution += contribution
                already_signed.add(info["window_id"])
                results.append({"window_id": info["window_id"], "result": "已补签",
                                "added_contribution": contribution})
            else:
                # 仍然无观测或质检未结束：不产生贡献，仅留痕说明
                record = {**base, "result": "not_eligible",
                          "state": info["state"], "added_contribution": 0.0,
                          "note": info["reason"]}
                results.append({"window_id": info["window_id"], "result": info["state"],
                                "added_contribution": 0.0, "reason": info["reason"]})
            supplement_records.append(record)

        # 逻辑上历史台账行只增不改；物理写回用覆盖模式，避免基类追加 IO
        # 在多次补签时把存量行重复写入。首签文件完全不参与本次写入。
        self._overwrite_jsonl(self._supplement_file(handover_no), supplement_records)

        if added_windows:
            supplement_set = sorted(
                already_signed
                - {w["window_id"] for w in entry.get("frozen_windows", [])}
            )
            entry["supplement_windows"] = supplement_set
            entry["已补签窗口"] = "、".join(supplement_set)
            entry["补签贡献"] = str(self._round(
                float(entry.get("补签贡献", "0") or 0.0) + added_contribution))
            entry["交接状态"] = "已补签"
            entry["status"] = "已补签"
            entry["pending"] = False

        first_total = float(entry.get("首签合计", "0") or 0.0)
        return {
            "handover_no": handover_no,
            "results": results,
            "added_windows": added_windows,
            "added_contribution": self._round(added_contribution),
            "first_sign_total": self._round(first_total),
            "grand_total": self._round(
                first_total + float(entry.get("补签贡献", "0") or 0.0)),
            "supplement_ledger": str(self._supplement_file(handover_no)),
        }

    # -- 复盘首签：按原交接号还原签发时的窗口与合计 --

    def replay_first_sign(self, handover_ref: int | str) -> dict[str, Any]:
        entry = self._resolve_handover(handover_ref)
        if entry is None:
            return {"error": "交接单不存在"}
        handover_no = entry.get("交接号")
        if not handover_no:
            return {"error": "交接尚未确认发布，无法复盘首签"}

        first_sign_file = self._first_sign_file(handover_no)
        records = self._read_jsonl(first_sign_file)
        if not records:
            return {"error": f"交接号 {handover_no} 的首签记录缺失"}

        header = next((r for r in records if r.get("record_type") == "first_sign_header"), {})
        windows = [r for r in records if r.get("record_type") == "first_sign_window"]
        footer = next((r for r in records if r.get("record_type") == "first_sign_footer"), {})

        # 重新计算校验和，确认补签重试没有损坏签发记录
        body_lines = [
            json.dumps(r, ensure_ascii=False)
            for r in records if r.get("record_type") != "first_sign_footer"
        ]
        checksum_ok = self._compute_checksum("\n".join(body_lines)) == footer.get("checksum")
        signed_total = self._round(sum(float(w["contribution"]) for w in windows))
        supplements = self._read_jsonl(self._supplement_file(handover_no))
        supplement_total = self._round(sum(
            float(r.get("added_contribution", 0.0))
            for r in supplements if r.get("result") == "appended"
        ))

        return {
            "handover_no": handover_no,
            "signed_windows": [
                {"window_id": w["window_id"], "point": w.get("point", ""),
                 "contribution": float(w["contribution"])}
                for w in windows
            ],
            "signed_total": signed_total,
            "footer_total": float(footer.get("signed_total", 0.0)),
            "requested_windows": header.get("requested_windows", []),
            "pending_windows": header.get("pending_windows", []),
            "signed_at": header.get("signed_at", ""),
            "checksum": footer.get("checksum", ""),
            "checksum_ok": checksum_ok,
            "supplement_total": supplement_total,
            "grand_total": self._round(signed_total + supplement_total),
            "first_sign_file": str(first_sign_file),
        }

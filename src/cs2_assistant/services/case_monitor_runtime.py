from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cs2_assistant.clients import C5GameClient
from cs2_assistant.config import PROJECT_ROOT, Settings
from cs2_assistant.db import Database
from cs2_assistant.services.guadao_case_monitor import (
    DEFAULT_COLLECTION_INTERVAL_MINUTES,
    DEFAULT_RATIO_BUCKET_SIZE,
    build_case_ratio_report,
    build_steam_clients_for_monitor,
    collect_case_ratio_snapshots,
    enrich_case_ratio_report_with_steam_liquidity,
    ensure_case_report_frontend_payload,
    list_case_monitor_targets,
    save_case_ratio_snapshots,
    write_case_ratio_report_files,
)
from cs2_assistant.services.strategy import load_strategy_config
from cs2_assistant.utils import utc_now_iso


CASE_MONITOR_RUNTIME_KEY = "case_monitor"
CASE_MONITOR_INTERVALS = (5, 10, 15, 30)
CASE_MONITOR_DEFAULT_INTERVAL = int(DEFAULT_COLLECTION_INTERVAL_MINUTES)
CASE_MONITOR_COLLECT_WORKERS = 8
CASE_MONITOR_LIQUIDITY_WORKERS = 8
CASE_MONITOR_EXPORT_KEYS = {
    "json": ("json", "application/json; charset=utf-8"),
    "summary_csv": ("summaryCsv", "text/csv; charset=utf-8"),
    "buckets_csv": ("bucketCsv", "text/csv; charset=utf-8"),
    "markdown": ("markdown", "text/markdown; charset=utf-8"),
}


class CaseMonitorBusyError(RuntimeError):
    def __init__(self, job: dict[str, Any]) -> None:
        self.job = dict(job)
        super().__init__("箱子挂刀比监控已有任务在执行")


def _json_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso_after_minutes(minutes: float, *, now: datetime | None = None) -> str:
    return ((now or _utc_now()) + timedelta(minutes=float(minutes))).isoformat()


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _status_counts(rows: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(getattr(row, "status", None) or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def ensure_case_monitor_runtime_state(db: Database) -> Any:
    row = db.get_case_monitor_runtime_state()
    if row is not None:
        return row
    return db.upsert_case_monitor_runtime_state(
        enabled=False,
        interval_minutes=CASE_MONITOR_DEFAULT_INTERVAL,
        runtime_status="paused",
        payload={
            "message": "监控尚未启动",
            "restartRequiresManualResume": True,
        },
    )


def public_case_monitor_job(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    parameters = _json_dict(row["parameters_json"])
    result = _json_dict(row["result_json"])
    public_result = {
        key: value
        for key, value in result.items()
        if key not in {"exportPaths", "webDataPath"}
    }
    return {
        "jobId": str(row["job_id"]),
        "jobType": str(row["job_type"]),
        "triggerSource": str(row["trigger_source"]),
        "status": str(row["status"]),
        "progressCurrent": int(row["progress_current"] or 0),
        "progressTotal": int(row["progress_total"] or 0),
        "message": row["message"],
        "parameters": parameters,
        "result": public_result,
        "error": row["error"],
        "requestedAt": row["requested_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "updatedAt": row["updated_at"],
    }


def _public_case_monitor_interruption(row: Any) -> dict[str, Any] | None:
    job = public_case_monitor_job(row)
    if job is None or job["status"] != "interrupted":
        return None
    return {
        "jobId": job["jobId"],
        "jobType": job["jobType"],
        "progressCurrent": job["progressCurrent"],
        "progressTotal": job["progressTotal"],
        "savedCount": int(job["result"].get("savedCount") or 0),
        "interruptedAt": job["finishedAt"],
        "reason": job["error"] or job["message"],
    }


class CaseMonitorCliJob:
    """Cross-process single-flight guard shared by the CLI and web runtime."""

    def __init__(
        self,
        settings: Settings,
        *,
        job_type: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.job_type = str(job_type)
        self.parameters = dict(parameters or {})
        self.job_id: str | None = None
        self._result: dict[str, Any] = {}

    def __enter__(self) -> CaseMonitorCliJob:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            ensure_case_monitor_runtime_state(db)
            job, busy = db.create_case_monitor_job_if_idle(
                job_type=self.job_type,
                trigger_source="cli",
                parameters=self.parameters,
                start_immediately=True,
            )
            if job is None:
                raise CaseMonitorBusyError(public_case_monitor_job(busy) or {})
            self.job_id = str(job["job_id"])
        finally:
            db.close()
        return self

    def set_result(self, result: dict[str, Any] | None) -> None:
        self._result = dict(result or {})

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        if not self.job_id:
            return False
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            current = ensure_case_monitor_runtime_state(db)
            payload = _json_dict(current["payload_json"])
            now = utc_now_iso()
            if exc is None:
                db.finish_case_monitor_job(self.job_id, result=self._result)
                if self.job_type == "collect":
                    payload["lastCollectionResult"] = dict(self._result)
                else:
                    payload["lastReportResult"] = {
                        key: value
                        for key, value in self._result.items()
                        if key not in {"exportPaths", "webDataPath"}
                    }
            else:
                db.finish_case_monitor_job(self.job_id, error=str(exc))
            db.upsert_case_monitor_runtime_state(
                enabled=bool(current["enabled"]),
                interval_minutes=float(current["interval_minutes"]),
                runtime_status="idle" if bool(current["enabled"]) else "paused",
                current_job_id=None,
                next_run_at=current["next_run_at"],
                last_collection_at=(
                    now if exc is None and self.job_type == "collect" else current["last_collection_at"]
                ),
                last_report_at=(
                    now if exc is None and self.job_type == "report" else current["last_report_at"]
                ),
                last_error=str(exc) if exc is not None else None,
                payload=payload,
            )
        finally:
            db.close()
        return False


class CaseMonitorRuntimeController:
    """Read-only case ratio scheduler owned by the local web API process."""

    def __init__(
        self,
        settings: Settings,
        *,
        poll_seconds: float = 0.5,
    ) -> None:
        self.settings = settings
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._wake = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._last_error: str | None = None

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.alive:
                return
            self._shutdown.clear()
            self._wake.clear()
            self._initialize_after_restart()
            self._thread = threading.Thread(
                target=self._worker,
                name="case-ratio-monitor",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._shutdown.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout)))

    def _initialize_after_restart(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = ensure_case_monitor_runtime_state(db)
            interrupted_job_id = str(row["current_job_id"] or "").strip()
            interrupted = db.interrupt_case_monitor_jobs("后端已重启，未完成的监控任务已中断")
            payload = _json_dict(row["payload_json"])
            if interrupted_job_id:
                interrupted_job = db.get_case_monitor_job(interrupted_job_id)
                interruption = _public_case_monitor_interruption(interrupted_job)
                if interruption is not None:
                    payload["lastInterruption"] = interruption
            payload.update(
                {
                    "message": "后端已重启，监控已暂停，请手动恢复",
                    "restartRequiresManualResume": True,
                    "interruptedJobsOnRestart": interrupted,
                    "restartedAt": utc_now_iso(),
                }
            )
            db.upsert_case_monitor_runtime_state(
                enabled=False,
                interval_minutes=self._validated_interval(row["interval_minutes"]),
                runtime_status="paused",
                current_job_id=None,
                next_run_at=None,
                last_collection_at=row["last_collection_at"],
                last_report_at=row["last_report_at"],
                last_error=None,
                payload=payload,
            )
        finally:
            db.close()

    @staticmethod
    def _validated_interval(value: Any) -> int:
        try:
            interval = int(float(value))
        except (TypeError, ValueError):
            interval = CASE_MONITOR_DEFAULT_INTERVAL
        if interval not in CASE_MONITOR_INTERVALS:
            raise ValueError("采集间隔只允许 5、10、15、30 分钟")
        return interval

    def start_monitor(self, interval_minutes: Any) -> dict[str, Any]:
        interval = self._validated_interval(interval_minutes)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = ensure_case_monitor_runtime_state(db)
            payload = _json_dict(row["payload_json"])
            payload.update(
                {
                    "message": f"监控已启动，每 {interval} 分钟采集一次",
                    "startedAt": utc_now_iso(),
                    "restartRequiresManualResume": True,
                }
            )
            current_job = db.get_case_monitor_job(str(row["current_job_id"] or ""))
            busy = current_job is not None and str(current_job["status"]) in {"queued", "running"}
            db.upsert_case_monitor_runtime_state(
                enabled=True,
                interval_minutes=interval,
                runtime_status=(
                    str(row["runtime_status"])
                    if busy
                    else "idle"
                ),
                current_job_id=str(row["current_job_id"]) if busy else None,
                next_run_at=utc_now_iso(),
                last_collection_at=row["last_collection_at"],
                last_report_at=row["last_report_at"],
                last_error=None,
                payload=payload,
            )
        finally:
            db.close()
        self._wake.set()
        return self.status()

    def pause_monitor(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = ensure_case_monitor_runtime_state(db)
            payload = _json_dict(row["payload_json"])
            current_job = db.get_case_monitor_job(str(row["current_job_id"] or ""))
            busy = current_job is not None and str(current_job["status"]) in {"queued", "running"}
            payload["message"] = (
                "监控已暂停，当前任务完成后不再继续采集"
                if busy
                else "监控已暂停"
            )
            payload["pausedAt"] = utc_now_iso()
            db.upsert_case_monitor_runtime_state(
                enabled=False,
                interval_minutes=float(row["interval_minutes"]),
                runtime_status=str(row["runtime_status"]) if busy else "paused",
                current_job_id=str(row["current_job_id"]) if busy else None,
                next_run_at=None,
                last_collection_at=row["last_collection_at"],
                last_report_at=row["last_report_at"],
                last_error=row["last_error"],
                payload=payload,
            )
        finally:
            db.close()
        self._wake.set()
        return self.status()

    def request_collect(self, *, trigger_source: str = "manual") -> dict[str, Any]:
        return self._request_job(
            "collect",
            trigger_source=trigger_source,
            parameters={"allCrateTypes": True},
        )

    def request_report(self, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(parameters or {})
        date_from = str(raw.get("dateFrom") or "").strip() or None
        date_to = str(raw.get("dateTo") or "").strip() or None
        if bool(date_from) != bool(date_to):
            raise ValueError("自定义范围必须同时提供 dateFrom 和 dateTo")
        hours = float(raw.get("hours") or 24.0)
        if not date_from and not 0 < hours <= 24 * 366:
            raise ValueError("hours 必须大于 0 且不超过 366 天")
        normalized = {
            "hours": hours,
            "dateFrom": date_from,
            "dateTo": date_to,
            "refreshLiquidity": bool(raw.get("refreshLiquidity", True)),
            "allCrateTypes": True,
        }
        return self._request_job(
            "report",
            trigger_source="manual",
            parameters=normalized,
        )

    def _request_job(
        self,
        job_type: str,
        *,
        trigger_source: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            ensure_case_monitor_runtime_state(db)
            job, busy = db.create_case_monitor_job_if_idle(
                job_type=job_type,
                trigger_source=trigger_source,
                parameters=parameters,
            )
            if job is None:
                raise CaseMonitorBusyError(public_case_monitor_job(busy) or {})
            result = public_case_monitor_job(job) or {}
        finally:
            db.close()
        self._wake.set()
        return result

    def status(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = ensure_case_monitor_runtime_state(db)
            payload = _json_dict(row["payload_json"])
            current_job = db.get_case_monitor_job(str(row["current_job_id"] or ""))
            if current_job is None or str(current_job["status"]) not in {"queued", "running"}:
                current_job = None
            latest_job = db.latest_case_monitor_job()
            latest_interrupted = db.conn.execute(
                """
                SELECT *
                FROM case_monitor_jobs
                WHERE status = 'interrupted'
                ORDER BY finished_at DESC, job_id DESC
                LIMIT 1
                """
            ).fetchone()
            latest_report = db.conn.execute(
                """
                SELECT *
                FROM case_monitor_jobs
                WHERE job_type = 'report' AND status = 'completed'
                ORDER BY requested_at DESC, job_id DESC
                LIMIT 1
                """
            ).fetchone()
            started_at = _parse_iso(payload.get("startedAt"))
            running_seconds = (
                max(0, int((_utc_now() - started_at).total_seconds()))
                if bool(row["enabled"]) and started_at is not None
                else 0
            )
            last_result = dict(payload.get("lastCollectionResult") or {})
            last_interruption = payload.get("lastInterruption")
            if not isinstance(last_interruption, dict):
                candidate = _public_case_monitor_interruption(latest_interrupted)
                interrupted_at = _parse_iso((candidate or {}).get("interruptedAt"))
                last_collection_at = _parse_iso(row["last_collection_at"])
                if candidate is not None and (
                    last_collection_at is None
                    or (interrupted_at is not None and interrupted_at > last_collection_at)
                ):
                    last_interruption = candidate
                else:
                    last_interruption = None
            return {
                "ok": True,
                "backend": {
                    "online": True,
                    "workerAlive": self.alive,
                    "lastError": self._last_error,
                },
                "runtime": {
                    "enabled": bool(row["enabled"]),
                    "status": str(row["runtime_status"]),
                    "intervalMinutes": int(float(row["interval_minutes"])),
                    "busy": current_job is not None,
                    "nextRunAt": row["next_run_at"],
                    "lastCollectionAt": row["last_collection_at"],
                    "lastReportAt": row["last_report_at"],
                    "lastError": row["last_error"],
                    "message": payload.get("message"),
                    "startedAt": payload.get("startedAt"),
                    "runningSeconds": running_seconds,
                    "restartRequiresManualResume": True,
                    "lastCollectionResult": last_result,
                    "lastInterruption": last_interruption,
                },
                "currentJob": public_case_monitor_job(current_job),
                "latestJob": public_case_monitor_job(latest_job),
                "latestReport": {
                    "available": latest_report is not None
                    or (PROJECT_ROOT / "frontend" / "public" / "guadao_case_ratio_report.json").exists(),
                    "reportId": str(latest_report["job_id"]) if latest_report is not None else None,
                },
                "generatedAt": utc_now_iso(),
            }
        finally:
            db.close()

    def latest_report(self) -> dict[str, Any]:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = db.conn.execute(
                """
                SELECT *
                FROM case_monitor_jobs
                WHERE job_type = 'report' AND status = 'completed'
                ORDER BY requested_at DESC, job_id DESC
                LIMIT 1
                """
            ).fetchone()
            report_id = str(row["job_id"]) if row is not None else None
            result = _json_dict(row["result_json"]) if row is not None else {}
            path = Path(str((result.get("exportPaths") or {}).get("json") or ""))
            source = "generated"
            if not path.is_file():
                path = PROJECT_ROOT / "frontend" / "public" / "guadao_case_ratio_report.json"
                source = "static"
            if not path.is_file():
                raise FileNotFoundError("尚未生成箱子挂刀比报告")
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise RuntimeError("箱子挂刀比报告格式无效")
            return {
                "ok": True,
                "report": report,
                "reportId": report_id,
                "source": source,
                "exportFormats": list(CASE_MONITOR_EXPORT_KEYS),
            }
        finally:
            db.close()

    def export_file(
        self,
        export_format: str,
        *,
        report_id: str | None = None,
    ) -> tuple[Path, str]:
        normalized = str(export_format or "").strip().lower()
        if normalized not in CASE_MONITOR_EXPORT_KEYS:
            raise ValueError("format must be json, summary_csv, buckets_csv or markdown")
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            if report_id:
                row = db.get_case_monitor_job(report_id)
            else:
                row = db.conn.execute(
                    """
                    SELECT *
                    FROM case_monitor_jobs
                    WHERE job_type = 'report' AND status = 'completed'
                    ORDER BY requested_at DESC, job_id DESC
                    LIMIT 1
                    """
                ).fetchone()
            if row is None or str(row["job_type"]) != "report" or str(row["status"]) != "completed":
                raise FileNotFoundError("找不到可导出的报告")
            result = _json_dict(row["result_json"])
            result_key, content_type = CASE_MONITOR_EXPORT_KEYS[normalized]
            path = Path(str((result.get("exportPaths") or {}).get(result_key) or ""))
            if not path.is_file():
                raise FileNotFoundError("报告导出文件不存在")
            reports_root = (self.settings.db_path.parent / "reports" / "guadao_case_ratio").resolve()
            resolved = path.resolve()
            if reports_root not in resolved.parents:
                raise RuntimeError("报告导出路径越界")
            return resolved, content_type
        finally:
            db.close()

    def _worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._enqueue_scheduled_collect_if_due()
                db = Database(self.settings.db_path)
                try:
                    db.initialize()
                    job = db.claim_next_case_monitor_job()
                finally:
                    db.close()
                if job is not None:
                    self._execute_job(job)
                    continue
            except Exception as exc:
                self._last_error = str(exc)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _enqueue_scheduled_collect_if_due(self) -> None:
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            row = ensure_case_monitor_runtime_state(db)
            if not bool(row["enabled"]):
                return
            next_run = _parse_iso(row["next_run_at"])
            if next_run is not None and next_run > _utc_now():
                return
            db.create_case_monitor_job_if_idle(
                job_type="collect",
                trigger_source="scheduled",
                parameters={"allCrateTypes": True},
            )
        finally:
            db.close()

    def _execute_job(self, row: Any) -> None:
        job_id = str(row["job_id"])
        job_type = str(row["job_type"])
        parameters = _json_dict(row["parameters_json"])
        try:
            if job_type == "collect":
                result = self._run_collect_job(job_id)
            elif job_type == "report":
                result = self._run_report_job(job_id, parameters)
            else:
                raise RuntimeError(f"未知监控任务类型: {job_type}")
        except Exception as exc:
            self._last_error = str(exc)
            self._finish_job(job_id, job_type=job_type, error=str(exc))
        else:
            self._last_error = None
            self._finish_job(job_id, job_type=job_type, result=result)

    def _run_collect_job(self, job_id: str) -> dict[str, Any]:
        if not self.settings.c5_api_key:
            raise RuntimeError("缺少 C5GAME_API_KEY / C5_API_KEY")
        config = load_strategy_config(self.settings)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            targets = list_case_monitor_targets(db)
            db.update_case_monitor_job_progress(
                job_id,
                current=0,
                total=len(targets),
                message=f"准备采集 {len(targets)} 个广义箱子",
            )
        finally:
            db.close()
        if not targets:
            raise RuntimeError("没有找到可监控的广义箱子，请先同步 CSGO-API crates")
        steam_clients = build_steam_clients_for_monitor(self.settings)
        if not steam_clients:
            raise RuntimeError("没有可用的 Steam Cookie")
        c5_client = C5GameClient(self.settings.c5_api_key, self.settings.c5_base_url)
        progress_lock = threading.Lock()
        saved_count = 0

        def on_progress(current: int, total: int, snapshot: Any) -> None:
            nonlocal saved_count
            with progress_lock:
                progress_db = Database(self.settings.db_path)
                try:
                    progress_db.initialize()
                    saved_count += save_case_ratio_snapshots(progress_db, [snapshot])
                    progress_db.update_case_monitor_job_progress(
                        job_id,
                        current=current,
                        total=total,
                        message=f"正在采集 {current}/{total}，已保存 {saved_count}",
                        partial_result={"savedCount": saved_count},
                    )
                finally:
                    progress_db.close()

        snapshots = collect_case_ratio_snapshots(
            settings=self.settings,
            config=config,
            targets=targets,
            c5_client=c5_client,
            steam_clients=steam_clients,
            max_workers=CASE_MONITOR_COLLECT_WORKERS,
            progress_callback=on_progress,
        )
        counts = _status_counts(snapshots)
        return {
            "savedCount": saved_count,
            "targetCount": len(targets),
            "statusCounts": counts,
            "okCount": int(counts.get("ok", 0)),
            "missingC5Count": int(counts.get("missing_c5", 0)),
            "missingSteamCount": int(counts.get("missing_steam", 0)),
            "observedAt": snapshots[0].observed_at if snapshots else utc_now_iso(),
        }

    def _run_report_job(self, job_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        config = load_strategy_config(self.settings)
        start, end = self._report_boundaries(parameters)
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            report = build_case_ratio_report(
                db,
                start_utc=start.isoformat(),
                end_utc=end.isoformat(),
                recommendation_crate_type="all",
                bucket_size=DEFAULT_RATIO_BUCKET_SIZE,
                expected_interval_minutes=DEFAULT_COLLECTION_INTERVAL_MINUTES,
                top_n=50,
            )
            db.update_case_monitor_job_progress(
                job_id,
                current=0,
                total=int(report.get("itemCount") or 0),
                message="历史快照统计完成",
            )
        finally:
            db.close()

        refresh_liquidity = bool(parameters.get("refreshLiquidity", True))
        if refresh_liquidity and report.get("items"):
            steam_clients = build_steam_clients_for_monitor(self.settings)
            if steam_clients:
                progress_lock = threading.Lock()
                last_saved = 0

                def on_progress(current: int, total: int, market_hash_name: str) -> None:
                    nonlocal last_saved
                    with progress_lock:
                        if current != total and current - last_saved < 5:
                            return
                        last_saved = current
                    progress_db = Database(self.settings.db_path)
                    try:
                        progress_db.initialize()
                        progress_db.update_case_monitor_job_progress(
                            job_id,
                            current=current,
                            total=total,
                            message=f"正在刷新 Steam 成交量 {current}/{total}",
                        )
                    finally:
                        progress_db.close()

                report = enrich_case_ratio_report_with_steam_liquidity(
                    report,
                    settings=self.settings,
                    config=config,
                    steam_clients=steam_clients,
                    recommendation_crate_type="all",
                    top_n=50,
                    max_workers=CASE_MONITOR_LIQUIDITY_WORKERS,
                    progress_callback=on_progress,
                )
            else:
                report["steamLiquidityStatus"] = "skipped_no_steam_client"
        elif not refresh_liquidity:
            report["steamLiquidityStatus"] = "skipped_by_user"

        stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
        output_dir = (
            self.settings.db_path.parent
            / "reports"
            / "guadao_case_ratio"
            / f"{stamp}_{job_id[-8:]}"
        )
        paths = write_case_ratio_report_files(report, output_dir)
        web_path = ensure_case_report_frontend_payload(
            report,
            PROJECT_ROOT / "frontend" / "public" / "guadao_case_ratio_report.json",
        )
        return {
            "reportId": job_id,
            "generatedAt": report.get("generatedAt"),
            "rangeHours": report.get("rangeHours"),
            "snapshotCount": report.get("snapshotCount"),
            "itemCount": report.get("itemCount"),
            "statusCounts": report.get("statusCounts") or {},
            "crateTypeCounts": report.get("crateTypeCounts") or {},
            "refreshLiquidity": refresh_liquidity,
            "exportPaths": paths,
            "webDataPath": str(web_path),
        }

    @staticmethod
    def _report_boundaries(parameters: dict[str, Any]) -> tuple[datetime, datetime]:
        date_from = _parse_iso(parameters.get("dateFrom"))
        date_to = _parse_iso(parameters.get("dateTo"))
        if date_from is not None or date_to is not None:
            if date_from is None or date_to is None:
                raise ValueError("自定义范围必须同时提供 dateFrom 和 dateTo")
            if date_to <= date_from:
                raise ValueError("报告结束时间必须晚于开始时间")
            return date_from, date_to
        end = _utc_now()
        hours = float(parameters.get("hours") or 24.0)
        if not 0 < hours <= 24 * 366:
            raise ValueError("hours 必须大于 0 且不超过 366 天")
        return end - timedelta(hours=hours), end

    def _finish_job(
        self,
        job_id: str,
        *,
        job_type: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        db = Database(self.settings.db_path)
        try:
            db.initialize()
            db.finish_case_monitor_job(job_id, result=result, error=error)
            row = ensure_case_monitor_runtime_state(db)
            payload = _json_dict(row["payload_json"])
            if error:
                payload["message"] = f"{'采集' if job_type == 'collect' else '报告'}失败：{error}"
            elif job_type == "collect":
                payload.pop("lastInterruption", None)
                payload["lastCollectionResult"] = dict(result or {})
                payload["message"] = (
                    f"采集完成：成功 {int((result or {}).get('okCount') or 0)}，"
                    f"缺 C5 {int((result or {}).get('missingC5Count') or 0)}"
                )
            else:
                payload["lastReportResult"] = {
                    key: value
                    for key, value in dict(result or {}).items()
                    if key not in {"exportPaths", "webDataPath"}
                }
                payload["message"] = "报告生成完成"

            enabled = bool(row["enabled"])
            next_run = row["next_run_at"]
            if enabled:
                if job_type == "collect":
                    next_run = _iso_after_minutes(float(row["interval_minutes"]), now=now)
                else:
                    parsed_next = _parse_iso(next_run)
                    if parsed_next is None or parsed_next <= now:
                        next_run = now.isoformat()
            else:
                next_run = None
            db.upsert_case_monitor_runtime_state(
                enabled=enabled,
                interval_minutes=float(row["interval_minutes"]),
                runtime_status="idle" if enabled else "paused",
                current_job_id=None,
                next_run_at=next_run,
                last_collection_at=(
                    now.isoformat()
                    if not error and job_type == "collect"
                    else row["last_collection_at"]
                ),
                last_report_at=(
                    now.isoformat()
                    if not error and job_type == "report"
                    else row["last_report_at"]
                ),
                last_error=error,
                payload=payload,
            )
        finally:
            db.close()
        self._wake.set()

"""Performance anomaly monitor — background trigger.

Continuously reads CPU/memory via psutil (lightweight, no LLM).
When thresholds are exceeded, emits a PERF_ANOMALY event with a snapshot
to wake the main agent for diagnosis.

Per docs/04-features.md §4.8:
- Monitoring uses a lightweight script, NOT an LLM
- Over-threshold → pass snapshot to wake agent → agent explains + suggests
- Killing a process requires 二次确认 (handled at tool layer)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import psutil

from agent_assistant.daemon.events import EventType, TriggerEvent, event_bus

logger = logging.getLogger(__name__)


@dataclass
class PerfThresholds:
    """Configurable thresholds for anomaly detection.

    J18: Lowered from 90%/10s to 80%/5s — catches more real anomalies
    without excessive false positives.
    """

    cpu_percent: float = 80.0  # J18: was 90, too high to catch most spikes
    memory_percent: float = 80.0  # J18: was 90
    sustained_seconds: float = 5.0  # J18: was 10, transient spikes escape
    check_interval: float = 2.0  # How often to check (seconds)
    cooldown_seconds: float = 300.0  # Don't re-trigger within 5 minutes


class PerfMonitor:
    """Background thread that monitors system performance."""

    def __init__(self, thresholds: PerfThresholds | None = None) -> None:
        self._thresholds = thresholds or PerfThresholds()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_trigger_time: float = 0
        self._anomaly_start: float | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="perf-monitor")
        self._thread.start()
        logger.info("PerfMonitor started (interval=%.1fs)", self._thresholds.check_interval)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("PerfMonitor stopped")

    def _monitor_loop(self) -> None:
        while self._running:
            time.sleep(self._thresholds.check_interval)

            try:
                cpu = psutil.cpu_percent(interval=0.5)
                mem = psutil.virtual_memory()
            except Exception as e:
                logger.warning("Perf read failed: %s", e)
                continue

            is_anomaly = (
                cpu >= self._thresholds.cpu_percent
                or mem.percent >= self._thresholds.memory_percent
            )

            if is_anomaly:
                if self._anomaly_start is None:
                    self._anomaly_start = time.time()

                sustained = time.time() - self._anomaly_start
                if sustained >= self._thresholds.sustained_seconds:
                    self._maybe_trigger(cpu, mem.percent)
            else:
                self._anomaly_start = None

    def _maybe_trigger(self, cpu: float, mem_percent: float) -> None:
        """Emit event if cooldown has passed."""
        now = time.time()
        if now - self._last_trigger_time < self._thresholds.cooldown_seconds:
            return

        self._last_trigger_time = now
        self._anomaly_start = None

        # Build snapshot for the agent
        top_procs = []
        for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = p.info
                mem_mb = (info["memory_info"].rss / 1024 / 1024) if info["memory_info"] else 0
                top_procs.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "mem_mb": round(mem_mb, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        top_procs.sort(key=lambda x: x["mem_mb"], reverse=True)

        snapshot = {
            "cpu_percent": cpu,
            "memory_percent": mem_percent,
            "top_processes": top_procs[:10],
            "message": (
                f"System anomaly detected: CPU {cpu:.0f}%, Memory {mem_percent:.0f}%. "
                "Please diagnose and suggest actions."
            ),
        }

        logger.warning("PERF ANOMALY: CPU=%.1f%%, MEM=%.1f%%", cpu, mem_percent)
        event_bus.emit(TriggerEvent(
            type=EventType.PERF_ANOMALY,
            payload=snapshot,
            source="perf_monitor",
        ))

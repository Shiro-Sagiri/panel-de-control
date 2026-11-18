"""Battery diagnostics sampler for charge/discharge calibration.

Collects a bounded ring of battery readings over time so the user can watch a
full discharge on a handheld (e.g. a ROG Ally whose SOC jumps 15% -> 0%) and
export a CSV.  Also integrates current over time (coulomb counting) to estimate
the *actually delivered* energy of a discharge segment — the number a fuel
gauge should be calibrated against, independent of its reported SOC.

Design goals:
- Never raises out of the loop: one bad sysfs read can't kill sampling.
- Bounded memory: a deque with maxlen caps the CSV at a sane size.
- Blocking sysfs I/O runs off the event loop (asyncio.to_thread).
- No event loop (unit tests / import time) => start() is a safe no-op.
"""

import asyncio
import csv
import io
import time
from collections import deque

# Columns exported to CSV. Ordered, stable — the header is always written even
# when no samples were collected.
CSV_COLUMNS = (
    "elapsed_s",
    "time_iso",
    "capacity_pct",
    "status",
    "ac_online",
    "energy_now_mwh",
    "energy_full_mwh",
    "energy_full_design_mwh",
    "health_percent",
    "voltage_now_uv",
    "current_now_ua",
    "power_now_w",
    "segment_discharged_mwh",
    "total_discharged_mwh",
)


class BatterySampler:
    """Poll *reader* every *interval* seconds into a bounded ring buffer.

    *read_on_ac()* is injected separately (AC-online has a single project-wide
    source and is intentionally not read by BatteryReader).

    Coulomb counting: while ``status == "Discharging"`` we accumulate energy as
    ``|current_ua| * voltage_now_uv * dt / 3.6e12`` mWh, falling back to
    ``|power_now_w| * dt / 3600`` mWh when current/voltage are missing.  The
    accumulator resets whenever charging/full is seen (a new discharge segment
    begins).  Implausibly large dt (a suspended/stalled sampler) is skipped so
    a single long gap cannot fake a huge energy figure.
    """

    def __init__(self, reader, read_on_ac, interval: float = 2.0,
                 max_rows: int = 5000) -> None:
        self._reader = reader
        self._read_on_ac = read_on_ac
        self._interval = max(0.25, float(interval))
        self._max_rows = max(1, int(max_rows))
        self._rows: deque = deque(maxlen=self._max_rows)
        self._task: asyncio.Task | None = None
        self._active = False
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._error: str | None = None
        self._last_row: dict | None = None
        self._last_t: float | None = None
        self._segment_discharged_mwh = 0.0
        self._total_discharged_mwh = 0.0
        self._was_discharging = False

    # -- lifecycle ----------------------------------------------------------
    def start(self, interval: float | None = None) -> dict:
        """Begin sampling. Idempotent: a second start while active is a no-op."""
        if interval is not None:
            self._interval = max(0.25, float(interval))
        if self._active:
            return self.status()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (tests / import) — cannot run the daemon, report it.
            self._error = "no running event loop"
            return self.status()
        self._rows.clear()
        self._last_t = None
        self._segment_discharged_mwh = 0.0
        self._total_discharged_mwh = 0.0
        self._was_discharging = False
        self._error = None
        self._started_at = time.time()
        self._stopped_at = None
        self._last_row = None
        self._active = True
        self._task = asyncio.create_task(self._loop())
        return self.status()

    def stop(self) -> dict:
        """Stop sampling, keeping the collected rows for export."""
        self._active = False
        self._stopped_at = time.time()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        return {"ok": True, "active": False, "samples": len(self._rows),
                "stopped_at": self._stopped_at}

    def status(self) -> dict:
        started = self._started_at
        elapsed = None
        if started is not None:
            end = self._stopped_at if (not self._active and self._stopped_at) else time.time()
            elapsed = round(max(0.0, end - started), 1)
        return {
            "active": self._active,
            "interval_s": round(self._interval, 2),
            "samples": len(self._rows),
            "max_rows": self._max_rows,
            "started_at": started,
            "elapsed_s": elapsed,
            "segment_discharged_mwh": round(self._segment_discharged_mwh, 1),
            "total_discharged_mwh": round(self._total_discharged_mwh, 1),
            "last_sample": self._last_row,
            "error": self._error,
        }

    # -- sampling -----------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
                # Blocking sysfs reads — keep them off the event loop.
                sample = await asyncio.to_thread(self._take_sample)
                if sample is not None:
                    self._rows.append(sample)
                    self._last_row = sample
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — loop must never die
                self._error = f"{type(exc).__name__}: {exc}"

    def _take_sample(self) -> dict | None:
        """Read one sample and update the coulomb integral. Runs in a thread."""
        try:
            battery = dict(self._reader.read() or {})
        except Exception:  # noqa: BLE001 — a transient read failure is skipped
            return None
        if not battery.get("present"):
            return None
        try:
            ac = bool(self._read_on_ac())
        except Exception:  # noqa: BLE001
            ac = False

        now_t = time.time()
        status = battery.get("status")
        discharging = status == "Discharging"

        # Reset the segment accumulator when a charge/full boundary is crossed.
        if self._was_discharging and not discharging:
            self._segment_discharged_mwh = 0.0
        self._was_discharging = discharging

        dt = None
        if self._last_t is not None:
            dt = now_t - self._last_t
        self._last_t = now_t

        if discharging and dt is not None and 0 < dt <= self._interval * 5:
            delta = self._energy_delta(battery, dt)
            if delta is not None and delta > 0:
                self._segment_discharged_mwh += delta
                self._total_discharged_mwh += delta

        row = {
            "elapsed_s": round(now_t - (self._started_at or now_t), 1),
            "time_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_t)),
            "capacity_pct": battery.get("percent"),
            "status": status,
            "ac_online": ac,
            "energy_now_mwh": battery.get("energy_now_mwh"),
            "energy_full_mwh": battery.get("energy_full_mwh"),
            "energy_full_design_mwh": battery.get("energy_full_design_mwh"),
            "health_percent": battery.get("health_percent"),
            "voltage_now_uv": battery.get("voltage_now_uv"),
            "current_now_ua": battery.get("current_now_ua"),
            "power_now_w": battery.get("power_now_w"),
            "segment_discharged_mwh": round(self._segment_discharged_mwh, 1),
            "total_discharged_mwh": round(self._total_discharged_mwh, 1),
        }
        return row

    @staticmethod
    def _energy_delta(battery: dict, dt_s: float) -> float | None:
        """Energy delivered over dt_s in mWh (positive)."""
        current = battery.get("current_now_ua")
        voltage = battery.get("voltage_now_uv")
        power = battery.get("power_now_w")
        if current is not None and voltage:
            # |µA| * µV = 1e-12 W -> W; * dt / 3600 -> Wh; * 1000 -> mWh
            return abs(current) * voltage * dt_s / 3.6e12
        if power is not None:
            return abs(power) * dt_s / 3600 * 1000
        return None

    # -- export -------------------------------------------------------------
    def export_csv(self) -> dict:
        """Serialize collected rows to a CSV string (JSON-safe for RPC)."""
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in self._rows:
            writer.writerow({col: ("" if row.get(col) is None else row.get(col))
                             for col in CSV_COLUMNS})
        return {"ok": True, "csv": buf.getvalue(), "rows": len(self._rows)}

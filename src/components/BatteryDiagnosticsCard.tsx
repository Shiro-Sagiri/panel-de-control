import { FC, useCallback, useEffect, useRef, useState } from "react";
import { ButtonItem, Focusable, PanelSectionRow } from "@decky/ui";
import { LuActivity, LuDownload, LuPlay, LuSquare } from "react-icons/lu";

import {
  BatteryCsvExport,
  BatterySamplingStatus,
  exportBatteryCsv,
  getBatterySamplingStatus,
  startBatterySampling,
  stopBatterySampling,
} from "../api";
import { useI18n } from "../i18n";
import { theme } from "../theme";

const POLL_MS = 2000;

const Stat: FC<{ label: string; value: string; accent?: boolean }> = ({ label, value, accent }) => (
  <div style={{ ...theme.tile, display: "flex", flexDirection: "column", gap: 2, flex: "1 1 0" }}>
    <span style={{ fontSize: theme.font.caption, color: theme.color.textMuted }}>{label}</span>
    <span style={{
      fontSize: theme.font.body, fontWeight: 700,
      color: accent ? theme.color.accent : theme.color.textPrimary,
      fontVariantNumeric: "tabular-nums",
    }}>{value}</span>
  </div>
);

const fmtMwh = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : `${v.toFixed(1)} mWh`;

/**
 * Battery charge/discharge calibration: sample sysfs battery readings into a
 * backend ring buffer while the user runs a full discharge, then export CSV.
 */
export const BatteryDiagnosticsCard: FC = () => {
  const { t } = useI18n();
  const [status, setStatus] = useState<BatterySamplingStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [exported, setExported] = useState(false);
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const s = await getBatterySamplingStatus();
      if (mounted.current) setStatus(s);
    } catch {
      // keep last known status
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, [refresh]);

  const onStart = async () => {
    setBusy(true);
    setExported(false);
    try {
      const s = await startBatterySampling(2);
      if (mounted.current) setStatus(s);
    } finally { if (mounted.current) setBusy(false); }
  };

  const onStop = async () => {
    setBusy(true);
    try {
      await stopBatterySampling();
      await refresh();
    } finally { if (mounted.current) setBusy(false); }
  };

  const onExport = async () => {
    setBusy(true);
    try {
      const result: BatteryCsvExport = await exportBatteryCsv();
      if (!result.ok || !result.csv || result.rows === 0) return;
      const blob = new Blob([result.csv], { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      a.href = url;
      a.download = `battery-diagnostics-${stamp}.csv`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      if (mounted.current) setExported(true);
    } finally { if (mounted.current) setBusy(false); }
  };

  const active = !!status?.active;
  const elapsed = status?.elapsed_s ?? null;
  const last = status?.last_sample;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: theme.space.sm }}>
      <div style={{ fontSize: theme.font.caption, color: theme.color.textMuted, lineHeight: 1.4 }}>
        {t("system.batteryDiag.hint")}
      </div>

      <div style={{ display: "flex", gap: theme.space.sm }}>
        <Stat label={t("system.batteryDiag.interval")} value={`${status?.interval_s ?? 2}s`} />
        <Stat label={t("system.batteryDiag.samples")} value={String(status?.samples ?? 0)} />
        <Stat label={t("system.batteryDiag.elapsed")} value={elapsed === null ? "—" : `${Math.round(elapsed)}s`} />
      </div>

      <div style={{ display: "flex", gap: theme.space.sm }}>
        <Stat label={t("system.batteryDiag.segment")} value={fmtMwh(status?.segment_discharged_mwh)} accent />
        <Stat label={t("system.batteryDiag.total")} value={fmtMwh(status?.total_discharged_mwh)} accent />
      </div>

      {last && (
        <Focusable style={{ ...theme.tile, display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontSize: theme.font.caption, color: theme.color.textMuted }}>
            {last.time_iso} · {last.status ?? "—"} · {last.capacity_pct ?? "—"}%
          </span>
          <span style={{ fontSize: theme.font.caption, color: theme.color.textMuted, fontVariantNumeric: "tabular-nums" }}>
            {last.voltage_now_uv ? `${(last.voltage_now_uv / 1e6).toFixed(2)}V` : "—"}
            {" · "}{last.current_now_ua ? `${(last.current_now_ua / 1e6).toFixed(2)}A` : "—"}
            {" · "}{fmtMwh(last.energy_now_mwh)}
          </span>
        </Focusable>
      )}

      {!status || status.samples === 0 ? (
        <div style={{ fontSize: theme.font.caption, color: theme.color.textMuted }}>
          {status?.error ? `${t("system.batteryDiag.error")}: ${status.error}` : t("system.batteryDiag.noData")}
        </div>
      ) : null}

      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || active} onClick={() => void onStart()}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <LuPlay size={16} /> {t("system.batteryDiag.start")}
          </span>
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || !active} onClick={() => void onStop()}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <LuSquare size={16} /> {t("system.batteryDiag.stop")}
          </span>
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || !status || status.samples === 0} onClick={() => void onExport()}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <LuDownload size={16} /> {t("system.batteryDiag.export")}{exported ? ` · ${t("system.batteryDiag.exported")}` : ""}
          </span>
        </ButtonItem>
      </PanelSectionRow>

      <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: theme.font.caption, color: active ? theme.color.accent : theme.color.textMuted }}>
        <LuActivity size={14} />
        {active ? t("system.batteryDiag.active") : t("system.batteryDiag.idle")}
      </div>
    </div>
  );
};

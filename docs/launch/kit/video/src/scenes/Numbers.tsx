import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { STATS } from "../data";
import { Background, Caption, COLORS, MONO } from "../ui";

export const Numbers: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Background note="Measured: docs/transfer-audit.md and real Claude Code transcripts">
      <div style={{ position: "absolute", left: 150, right: 150, top: 110, display: "grid",
        gridTemplateColumns: "1fr 1fr", gap: 36 }}>
        {STATS.map((s, i) => (
          <Interactive.Div
            key={s.label}
            name={`Stat ${i + 1}`}
            style={{
              backgroundColor: COLORS.window, border: `2px solid ${COLORS.border}`, borderRadius: 18,
              padding: "30px 38px", display: "flex", flexDirection: "column", gap: 10, height: 290,
              opacity: interpolate(frame, [8 + i * 12, 20 + i * 12], [0, 1], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp",
              }),
              translate: interpolate(frame, [8 + i * 12, 26 + i * 12], ["0px 40px", "0px 0px"], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.16, 1, 0.3, 1),
              }),
            }}
          >
            <div style={{ display: "flex", alignItems: "baseline", gap: 18 }}>
              <span style={{ fontSize: 96, fontWeight: 800, color: COLORS.accent }}>{s.value}</span>
              <span style={{ fontSize: 36, color: COLORS.muted }}>{s.unit}</span>
            </div>
            <div style={{ fontSize: 36, color: COLORS.text, fontWeight: 600 }}>{s.label}</div>
            <div style={{ fontFamily: MONO, fontSize: 24, color: COLORS.muted }}>{s.note}</div>
          </Interactive.Div>
        ))}
      </div>
      <Caption text="Measured, not claimed" from={0} to={180} />
    </Background>
  );
};

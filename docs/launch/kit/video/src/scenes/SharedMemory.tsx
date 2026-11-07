import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { Background, Caption, COLORS, MONO } from "../ui";

const SESSIONS = [
  { agent: "codex", color: COLORS.codex, text: "Updated format_eur to produce German formatting · 3 tests pass" },
  { agent: "claude.ai", color: COLORS.web, text: "Resumed BILL-311 through the CogniCore connector" },
  { agent: "claude-code", color: COLORS.claude, text: "Ticket BILL-311: customers in Germany see invoice totals that are off by one cent" },
];

export const SharedMemory: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Background note="Recreated from real runs · scripts/bench_realworld.py and a live sync test">
      <Interactive.Div
        name="Viewer"
        style={{
          position: "absolute", left: 150, right: 150, top: 100, height: 690, borderRadius: 18,
          backgroundColor: "#fbfaf8", color: "#1f1e1c", padding: "44px 56px", display: "flex",
          flexDirection: "column", gap: 20,
          opacity: interpolate(frame, [0, 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
        }}
      >
        <div style={{ fontSize: 54, fontWeight: 800 }}>CogniCore memory</div>
        <div style={{ fontFamily: MONO, fontSize: 28, color: "#6b6862" }}>billing-app · local, synced to your account with cognicore login</div>
        {SESSIONS.map((s, i) => (
          <div
            key={s.agent}
            style={{
              border: "2px solid #e4e1db", borderRadius: 14, padding: "18px 30px", backgroundColor: "#ffffff",
              display: "flex", flexDirection: "column", gap: 10,
              translate: interpolate(frame, [18 + i * 14, 34 + i * 14], ["0px 40px", "0px 0px"], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.16, 1, 0.3, 1),
              }),
              opacity: interpolate(frame, [18 + i * 14, 30 + i * 14], [0, 1], {
                extrapolateLeft: "clamp", extrapolateRight: "clamp",
              }),
            }}
          >
            <div style={{ fontSize: 34, fontWeight: 600 }}>
              billing-app <span style={{ color: s.color }}>· by {s.agent}</span>
            </div>
            <div style={{ fontSize: 30, color: "#3a3833" }}>{s.text}</div>
          </div>
        ))}
      </Interactive.Div>
      <Caption text="One memory for all three" />
    </Background>
  );
};

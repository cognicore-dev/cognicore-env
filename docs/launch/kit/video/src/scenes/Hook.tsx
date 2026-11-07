import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { Background, COLORS, MONO } from "../ui";

const APPS = [
  { name: "Claude Code", color: COLORS.claude },
  { name: "Codex", color: COLORS.codex },
  { name: "claude.ai", color: COLORS.web },
];

export const Hook: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Background note="CogniCore · open-source memory for AI agents">
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", gap: 56, paddingBottom: 80 }}>
        <Interactive.Div
          name="Line"
          style={{
            fontSize: 84, fontWeight: 800, color: COLORS.text, textAlign: "center", maxWidth: 1500,
            opacity: interpolate(frame, [0, 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
            translate: interpolate(frame, [0, 16], ["0px 30px", "0px 0px"], {
              extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.16, 1, 0.3, 1),
            }),
          }}
        >
          Every new session starts from zero.
        </Interactive.Div>
        <div style={{ display: "flex", gap: 40 }}>
          {APPS.map((app, i) => (
            <Interactive.Div
              key={app.name}
              name={app.name}
              style={{
                fontFamily: MONO, fontSize: 44, color: app.color, border: `3px solid ${app.color}`,
                borderRadius: 16, padding: "18px 36px",
                opacity: interpolate(frame, [22 + i * 8, 32 + i * 8], [0, 1], {
                  extrapolateLeft: "clamp", extrapolateRight: "clamp",
                }),
                scale: interpolate(frame, [22 + i * 8, 34 + i * 8], [0.8, 1], {
                  extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.spring({ damping: 200 }),
                  output: "perceptual-scale",
                }),
              }}
            >
              {app.name}
            </Interactive.Div>
          ))}
        </div>
        <Interactive.Div
          name="Sub"
          style={{
            fontSize: 48, color: COLORS.muted,
            opacity: interpolate(frame, [50, 62], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          }}
        >
          You re-explain the ticket, the decisions, what's left. Every time.
        </Interactive.Div>
      </div>
    </Background>
  );
};

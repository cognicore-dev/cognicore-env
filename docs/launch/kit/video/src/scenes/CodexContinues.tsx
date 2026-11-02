import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { RECALLED, SESSION2_PROMPT, SESSION2_REPLY } from "../data";
import { Background, Caption, COLORS, TerminalWindow, typed } from "../ui";

export const CodexContinues: React.FC = () => {
  const frame = useCurrentFrame();
  return (
    <Background>
      <TerminalWindow title="Codex — billing-app" accent={COLORS.codex} fontSize={40}>
        <div style={{ color: COLORS.user }}>{"> "}{typed(SESSION2_PROMPT, frame, 5, 45)}</div>
        <div>
          <span style={{ color: COLORS.codex, opacity: frame > 60 ? 1 : 0 }}>● </span>
          {typed(SESSION2_REPLY, frame, 65, 150)}
        </div>
        <Interactive.Div
          name="Recalled"
          style={{
            display: "flex", flexWrap: "wrap", gap: 16, alignItems: "center", marginTop: 12,
            opacity: interpolate(frame, [165, 175], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          }}
        >
          <span style={{ color: COLORS.muted, fontSize: 34 }}>Recalled from memory:</span>
          {RECALLED.map((value, i) => (
            <span
              key={value}
              style={{
                border: `2px solid ${COLORS.codex}`, color: COLORS.codex, borderRadius: 999, padding: "6px 20px",
                fontSize: 34,
                scale: interpolate(frame, [172 + i * 8, 182 + i * 8], [0.6, 1], {
                  extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.spring({ damping: 200 }),
                  output: "perceptual-scale",
                }),
                opacity: interpolate(frame, [172 + i * 8, 180 + i * 8], [0, 1], {
                  extrapolateLeft: "clamp", extrapolateRight: "clamp",
                }),
              }}
            >
              {value}
            </span>
          ))}
        </Interactive.Div>
      </TerminalWindow>
      <Caption text="It picks up the follow-up, unprompted" />
    </Background>
  );
};

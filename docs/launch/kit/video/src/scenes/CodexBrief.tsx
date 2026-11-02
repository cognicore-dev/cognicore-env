import React from "react";
import { Interactive, interpolate, useCurrentFrame } from "remotion";
import { BRIEF, BRIEF_TOKENS } from "../data";
import { Background, Caption, COLORS, TerminalWindow } from "../ui";

export const CodexBrief: React.FC = () => {
  const frame = useCurrentFrame();
  const show = (at: number) => interpolate(frame, [at, at + 8], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <Background>
      <TerminalWindow title="Codex — billing-app" accent={COLORS.codex} fontSize={38}>
        <Interactive.Div name="Status" style={{ color: COLORS.accent, opacity: show(8) }}>
          ● Loading CogniCore memory
        </Interactive.Div>
        <Interactive.Div
          name="Brief"
          style={{ opacity: show(30), border: `2px solid ${COLORS.border}`, borderRadius: 12, padding: "18px 24px",
            display: "flex", flexDirection: "column", gap: 12, fontSize: 34 }}
        >
          <div style={{ color: COLORS.muted }}>CogniCore brief · {BRIEF_TOKENS} tokens</div>
          {BRIEF.map((item, i) => (
            <div key={item.label} style={{ opacity: show(40 + i * 12) }}>
              <span style={{ color: COLORS.accent }}>{item.label}: </span>{item.text}
            </div>
          ))}
        </Interactive.Div>
      </TerminalWindow>
      <Caption text="Open a different agent" />
    </Background>
  );
};

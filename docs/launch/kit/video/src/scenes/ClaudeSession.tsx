import React from "react";
import { Interactive, interpolate, useCurrentFrame } from "remotion";
import { SESSION1_PROMPT, SESSION1_REPLY } from "../data";
import { Background, Caption, COLORS, TerminalWindow, typed } from "../ui";

export const ClaudeSession: React.FC = () => {
  const frame = useCurrentFrame();
  const show = (at: number) => interpolate(frame, [at, at + 10], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <Background>
      <TerminalWindow title="Claude Code — billing-app" accent={COLORS.claude}>
        <div style={{ color: COLORS.user, fontSize: 29 }}>
          {"> "}{typed(SESSION1_PROMPT, frame, 15, 135)}
        </div>
        {SESSION1_REPLY.map((line, i) => (
          <Interactive.Div key={line} name={`Reply ${i + 1}`} style={{ opacity: show(150 + i * 22), fontSize: 29 }}>
            <span style={{ color: COLORS.claude }}>● </span>{line}
          </Interactive.Div>
        ))}
        <Interactive.Div name="Saved" style={{ opacity: show(240), color: COLORS.codex, fontSize: 28 }}>
          ✓ CogniCore: session saved from the transcript (0 model calls)
        </Interactive.Div>
      </TerminalWindow>
      <Caption text="Tell Claude Code what's going on" from={0} to={150} />
      <Caption text="Work done, session closed" from={150} to={300} />
    </Background>
  );
};

import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { WEB_ANSWER, WEB_PROMPT, WEB_TOOL } from "../data";
import { Background, Caption, COLORS, typed } from "../ui";

// A plain chat window (not a copy of any product UI): the question, the tool
// call, and an answer built only from the CogniCore brief.
export const WebRecall: React.FC = () => {
  const frame = useCurrentFrame();
  const show = (at: number) => interpolate(frame, [at, at + 10], [0, 1], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  });
  return (
    <Background note="Reconstructed · in a real test claude.ai called CogniCore's resume on its own">
      <Interactive.Div
        name="Chat"
        style={{
          position: "absolute", left: 150, right: 150, top: 100, height: 690, borderRadius: 18,
          backgroundColor: "#1d1b26", border: `2px solid ${COLORS.border}`, overflow: "hidden",
          display: "flex", flexDirection: "column",
          opacity: interpolate(frame, [0, 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
          translate: interpolate(frame, [0, 14], ["0px 30px", "0px 0px"], {
            extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
        }}
      >
        <div style={{ padding: "18px 30px", borderBottom: `2px solid ${COLORS.border}`, display: "flex",
          alignItems: "center", gap: 16 }}>
          <div style={{ width: 16, height: 16, borderRadius: 8, backgroundColor: COLORS.web }} />
          <div style={{ color: COLORS.web, fontSize: 30, fontWeight: 600 }}>claude.ai · CogniCore connector on</div>
        </div>
        <div style={{ padding: "30px 44px", display: "flex", flexDirection: "column", gap: 22, flex: 1 }}>
          <div style={{ alignSelf: "flex-end", backgroundColor: "#2e2b3b", color: COLORS.text, fontSize: 34,
            borderRadius: 18, padding: "16px 26px", maxWidth: 1100 }}>
            {typed(WEB_PROMPT, frame, 10, 50)}
          </div>
          <Interactive.Div name="Tool call" style={{ opacity: show(62), color: COLORS.muted, fontSize: 28 }}>
            <span style={{ color: COLORS.web }}>⟳ </span>{WEB_TOOL}
          </Interactive.Div>
          {WEB_ANSWER.map((line, i) => (
            <Interactive.Div key={line} name={`Answer ${i + 1}`}
              style={{ opacity: show(84 + i * 22), color: COLORS.text, fontSize: 32, lineHeight: 1.4 }}>
              {line}
            </Interactive.Div>
          ))}
        </div>
      </Interactive.Div>
      <Caption text="Ask claude.ai. It already knows." from={0} to={240} />
    </Background>
  );
};

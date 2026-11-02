import React from "react";
import { Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { INSTALL, REPO } from "../data";
import { Background, COLORS, MONO } from "../ui";

export const EndCard: React.FC = () => {
  const frame = useCurrentFrame();
  const rise = (at: number) => ({
    opacity: interpolate(frame, [at, at + 10], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp" }),
    translate: interpolate(frame, [at, at + 14], ["0px 24px", "0px 0px"], {
      extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: Easing.bezier(0.16, 1, 0.3, 1),
    }),
  });
  return (
    <Background>
      <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column",
        alignItems: "center", justifyContent: "center", gap: 34, color: COLORS.text, paddingBottom: 60 }}>
        <Interactive.Div name="Name" style={{ fontSize: 150, fontWeight: 800, ...rise(0) }}>CogniCore</Interactive.Div>
        <Interactive.Div name="Tagline" style={{ fontSize: 64, color: COLORS.muted, ...rise(8) }}>
          Memory that moves between your agents
        </Interactive.Div>
        <Interactive.Div
          name="Install"
          style={{ fontFamily: MONO, fontSize: 40, color: COLORS.codex, backgroundColor: COLORS.window,
            border: `2px solid ${COLORS.border}`, borderRadius: 14, padding: "20px 34px", ...rise(16) }}
        >
          {INSTALL}
        </Interactive.Div>
        <Interactive.Div name="Repo" style={{ fontSize: 40, ...rise(24) }}>
          {REPO} · open source · local · ~450-token briefs
        </Interactive.Div>
      </div>
    </Background>
  );
};

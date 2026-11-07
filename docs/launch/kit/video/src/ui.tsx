import React from "react";
import { AbsoluteFill, Easing, Interactive, interpolate, useCurrentFrame } from "remotion";
import { loadFont as loadInter } from "@remotion/google-fonts/Inter";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";

export const { fontFamily: SANS } = loadInter("normal", { weights: ["400", "600", "800"], subsets: ["latin"] });
export const { fontFamily: MONO } = loadMono("normal", { weights: ["400", "700"], subsets: ["latin"] });

export const COLORS = {
  bg: "#0e1116",
  window: "#161b22",
  border: "#30363d",
  text: "#e6edf3",
  muted: "#8b949e",
  user: "#79c0ff",
  claude: "#d97757",
  codex: "#7ee787",
  accent: "#f2cc60",
  web: "#c4a7ff",
};

const EASE = Easing.bezier(0.16, 1, 0.3, 1);

/** Characters of `text` revealed between frames `from` and `to`. */
export const typed = (text: string, frame: number, from: number, to: number) =>
  text.slice(0, Math.round(interpolate(frame, [from, to], [0, text.length], {
    extrapolateLeft: "clamp", extrapolateRight: "clamp",
  })));

export const Background: React.FC<{ children: React.ReactNode; note?: string }> = ({ children, note }) => (
  <AbsoluteFill name="Background" style={{ backgroundColor: COLORS.bg, fontFamily: SANS }}>
    {children}
    <Footer text={note} />
  </AbsoluteFill>
);

/** A terminal window filling the safe area above the caption. */
export const TerminalWindow: React.FC<{ title: string; accent: string; fontSize?: number; children: React.ReactNode }> = ({
  title, accent, fontSize = 32, children,
}) => {
  const frame = useCurrentFrame();
  return (
    <Interactive.Div
      name="Window"
      style={{
        position: "absolute", left: 150, right: 150, top: 100, height: 690,
        backgroundColor: COLORS.window, border: `2px solid ${COLORS.border}`, borderRadius: 18,
        overflow: "hidden", display: "flex", flexDirection: "column",
        opacity: interpolate(frame, [0, 12], [0, 1], { extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: EASE }),
        translate: interpolate(frame, [0, 12], ["0px 30px", "0px 0px"], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp", easing: EASE,
        }),
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "18px 26px",
        borderBottom: `2px solid ${COLORS.border}` }}>
        {["#ff5f57", "#febc2e", "#28c840"].map((c) => (
          <div key={c} style={{ width: 18, height: 18, borderRadius: 9, backgroundColor: c }} />
        ))}
        <div style={{ marginLeft: 18, color: accent, fontSize: 30, fontWeight: 600 }}>{title}</div>
      </div>
      <div style={{ padding: "28px 38px", fontFamily: MONO, fontSize, lineHeight: 1.45, color: COLORS.text,
        display: "flex", flexDirection: "column", gap: 18 }}>
        {children}
      </div>
    </Interactive.Div>
  );
};

/** One caption per shot, in the band below the window. */
export const Caption: React.FC<{ text: string; from?: number; to?: number }> = ({ text, from = 0, to = 10000 }) => {
  const frame = useCurrentFrame();
  return (
    <Interactive.Div
      name="Caption"
      style={{
        position: "absolute", left: 150, right: 150, top: 830, textAlign: "center",
        fontSize: 72, fontWeight: 800, color: COLORS.text,
        opacity: interpolate(frame, [from, from + 10, to - 8, to], [0, 1, 1, 0], {
          extrapolateLeft: "clamp", extrapolateRight: "clamp",
        }),
      }}
    >
      {text}
    </Interactive.Div>
  );
};

const Footer: React.FC<{ text?: string }> = ({ text = "Recreated from a real benchmark run · scripts/bench_realworld.py" }) => (
  <div style={{ position: "absolute", bottom: 34, width: "100%", textAlign: "center", color: COLORS.muted,
    fontSize: 24 }}>
    {text}
  </div>
);

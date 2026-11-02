import React from "react";
import { Series } from "remotion";
import { ClaudeSession } from "./scenes/ClaudeSession";
import { CodexBrief } from "./scenes/CodexBrief";
import { CodexContinues } from "./scenes/CodexContinues";
import { EndCard } from "./scenes/EndCard";
import { SharedMemory } from "./scenes/SharedMemory";

export const SCENES = [
  { id: "ClaudeSession", component: ClaudeSession, durationInFrames: 300 },
  { id: "CodexBrief", component: CodexBrief, durationInFrames: 120 },
  { id: "CodexContinues", component: CodexContinues, durationInFrames: 240 },
  { id: "SharedMemory", component: SharedMemory, durationInFrames: 150 },
  { id: "EndCard", component: EndCard, durationInFrames: 90 },
];

export const DemoVideo: React.FC = () => (
  <Series>
    <Series.Sequence name="Claude session" durationInFrames={300}><ClaudeSession /></Series.Sequence>
    <Series.Sequence name="Codex gets the brief" durationInFrames={120}><CodexBrief /></Series.Sequence>
    <Series.Sequence name="Codex continues" durationInFrames={240}><CodexContinues /></Series.Sequence>
    <Series.Sequence name="Shared memory" durationInFrames={150}><SharedMemory /></Series.Sequence>
    <Series.Sequence name="End card" durationInFrames={90}><EndCard /></Series.Sequence>
  </Series>
);

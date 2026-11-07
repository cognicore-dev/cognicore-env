import React from "react";
import { Series } from "remotion";
import { ClaudeSession } from "./scenes/ClaudeSession";
import { CodexBrief } from "./scenes/CodexBrief";
import { CodexContinues } from "./scenes/CodexContinues";
import { EndCard } from "./scenes/EndCard";
import { Hook } from "./scenes/Hook";
import { Numbers } from "./scenes/Numbers";
import { SharedMemory } from "./scenes/SharedMemory";
import { WebRecall } from "./scenes/WebRecall";

// 48 s at 30 fps. Each scene is also its own composition in Root.tsx.
export const SCENES = [
  { id: "Hook", name: "Starts from zero", component: Hook, durationInFrames: 90 },
  { id: "ClaudeSession", name: "Claude session", component: ClaudeSession, durationInFrames: 300 },
  { id: "WebRecall", name: "claude.ai picks it up", component: WebRecall, durationInFrames: 240 },
  { id: "CodexBrief", name: "Codex gets the brief", component: CodexBrief, durationInFrames: 120 },
  { id: "CodexContinues", name: "Codex continues", component: CodexContinues, durationInFrames: 240 },
  { id: "Numbers", name: "The numbers", component: Numbers, durationInFrames: 180 },
  { id: "SharedMemory", name: "Shared memory", component: SharedMemory, durationInFrames: 150 },
  { id: "EndCard", name: "End card", component: EndCard, durationInFrames: 120 },
];

export const DemoVideo: React.FC = () => (
  <Series>
    {SCENES.map(({ id, name, component: Scene, durationInFrames }) => (
      <Series.Sequence key={id} name={name} durationInFrames={durationInFrames}>
        <Scene />
      </Series.Sequence>
    ))}
  </Series>
);

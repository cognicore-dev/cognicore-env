import React from "react";
import { Composition, Folder } from "remotion";
import { DemoVideo, SCENES } from "./DemoVideo";

export const RemotionRoot: React.FC = () => (
  <>
    <Folder name="Demo-Scenes">
      {SCENES.map((scene) => (
        <Composition key={scene.id} id={scene.id} component={scene.component} width={1920} height={1080} fps={30}
          durationInFrames={scene.durationInFrames} />
      ))}
    </Folder>
    <Composition id="CogniCoreDemo" component={DemoVideo} width={1920} height={1080} fps={30}
      durationInFrames={SCENES.reduce((sum, s) => sum + s.durationInFrames, 0)} />
  </>
);

import React from 'react';
import { store } from './store.js';
import { CloudShotPanel, useCloud } from './CloudStudio.jsx';
import H3Inputs from './H3Inputs.jsx';

export default function ShotMediaWorkspace({ project, shot, onInspect, compact = false }) {
  const capabilities = useCloud().capabilities,
    local = store.getState().workspace.mode !== 'cloud';
  return (
    <div className={'guided-shot-workspace ' + (compact ? 'compact' : '')}>
      <H3Inputs
        project={project}
        shot={shot}
        recipes={capabilities?.recipes || []}
        local={local}
        onInspect={onInspect}
      />
      <div>
        <CloudShotPanel
          project={project}
          shot={shot}
          compact={compact}
          showRecipeSelector={false}
          draftOnly={local}
        />
      </div>
    </div>
  );
}

import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { recipeExecutionStatus } from './recipe-execution-model.js';

export default function RecipeExecutionNotice({ recipe, project, shot }) {
  const { t: at, locale } = useI18n();
  const status = recipeExecutionStatus(recipe, { project, shot, locale });
  return (
    <section
      className="cloud-warning"
      aria-label={at('adjunct.recipeExecutionNotice.copy168')}
      aria-live="polite"
    >
      <b>{status.title}</b>
      <p>{status.detail}</p>
      {status.issues.length > 0 && (
        <ul>
          {status.issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      )}
      {status.summary.length > 0 && (
        <details>
          <summary>{at('adjunct.recipeExecutionNotice.copy169')}</summary>
          <ul>
            {status.summary.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

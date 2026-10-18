import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { store } from './store.js';
export default function StoryCoach({ project: p, onQuickShot }) {
  const { t: lt } = useI18n();
  const brief = p.journey?.brief || {};
  const [expanded, setExpanded] = React.useState(!p.logline);
  const set = (key, value) =>
    store.editProject(
      (q) => {
        q.journey = { ...q.journey, brief: { ...q.journey?.brief, [key]: value } };
      },
      { coalesceKey: 'brief:' + key },
    );
  const summary =
    brief.protagonist && brief.desire
      ? lt('library.coachSummary', {
          protagonist: brief.protagonist,
          desire: brief.desire,
          obstacle: brief.obstacle
            ? lt('library.coachObstaclePart', { value: brief.obstacle })
            : '',
          tone: brief.tone ? lt('library.coachTonePart', { value: brief.tone }) : '',
        })
      : '';
  return (
    <div className="story-coach">
      <button className="journey-minor-button" onClick={onQuickShot}>
        {lt('legacy.route98')}
      </button>
      {brief.entry !== 'script' && (
        <details open={expanded} onToggle={(e) => setExpanded(e.currentTarget.open)}>
          <summary>{lt('legacy.route99')}</summary>
          <p>{lt('legacy.route100')}</p>
          <div className="v6-requirements">
            {[
              ['protagonist', 'Protagonist'],
              ['desire', 'Desire'],
              ['obstacle', 'Obstacle'],
              ['tone', 'Tone'],
            ].map(([key, label]) => (
              <label key={key}>
                {lt('library.coach' + label)}
                <input
                  value={brief[key] || ''}
                  maxLength={600}
                  placeholder={lt('library.coach' + label + 'Hint')}
                  onChange={(e) => set(key, e.target.value)}
                />
              </label>
            ))}
          </div>
          {summary && (
            <div className="v6-brief-summary">
              <strong>{lt('legacy.route101')}</strong>
              <p>{summary}</p>
              <button
                disabled={summary === p.logline && brief.adoptedSummary === summary}
                onClick={() =>
                  store.editProject((q) => {
                    q.journey.brief = {
                      ...q.journey.brief,
                      previousLogline:
                        q.journey.brief.adoptedSummary === q.logline
                          ? q.journey.brief.previousLogline
                          : q.logline,
                      adoptedSummary: summary,
                    };
                    q.logline = summary;
                  })
                }
              >
                {lt('legacy.route102')}
              </button>
              {brief.adoptedSummary === p.logline && (
                <button
                  onClick={() =>
                    store.editProject((q) => {
                      q.logline = q.journey.brief.previousLogline || '';
                      delete q.journey.brief.adoptedSummary;
                    })
                  }
                >
                  {lt('legacy.route103')}
                </button>
              )}
            </div>
          )}
        </details>
      )}
    </div>
  );
}

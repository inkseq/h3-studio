import { useI18n } from './i18n/I18nProvider.jsx';
import { Disclosure } from './ui/Disclosure.jsx';
import React from 'react';
import { failureDetails } from './cloud-model.js';

export default function GenerationFailure({ job }) {
  const { t: at, locale } = useI18n();
  const details = failureDetails(job, locale);
  if (!details) return null;
  return (
    <Disclosure
      className="qc-generation-failure"
      summary={
        <>
          {at('adjunct.generationFailure.copy104')}
          {details.stage} · {details.reason}
        </>
      }
    >
      <dl>
        {details.rows.map(([label, value]) => (
          <React.Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </React.Fragment>
        ))}
      </dl>
      <p>{at('adjunct.generationFailure.copy105')}</p>
    </Disclosure>
  );
}

import { useI18n } from './i18n/I18nProvider.jsx';
import React from 'react';
import { CONTROL_LABELS, newExcerptControl } from './quick-chat-model.js';

export default function GenerationControl({
  field,
  schema,
  value,
  onChange,
  className = 'qc-field',
}) {
  const { t: at } = useI18n();
  const options = {
    video_conditioning: {
      reference: at('adjunct.option0'),
      denoise: at('adjunct.option1'),
      in_context: at('adjunct.option2'),
    },
    audio_conditioning: {
      reference: at('adjunct.option3'),
      soundtrack: at('adjunct.option4'),
      from_video: at('adjunct.option5'),
    },
    audio_refinement: { none: at('adjunct.option6'), enabled: at('adjunct.option7') },
    mask_mode: { grouped_rows: at('adjunct.option8'), shared_timestep: at('adjunct.option9') },
  };
  const type = Array.isArray(schema.type) ? schema.type.find((t) => t !== 'null') : schema.type,
    nullable = Array.isArray(schema.type) && schema.type.includes('null'),
    disabled = schema.available === false,
    label = Object.hasOwn(CONTROL_LABELS, field)
      ? at('adjunct.control.' + field)
      : schema.title || field;
  if (type === 'array' && ['video_excerpts', 'audio_excerpts'].includes(field)) {
    const entries = Array.isArray(value) ? value : [],
      properties = schema.items?.properties || {},
      maximum = schema.maxItems;
    const patch = (index, key, next) =>
      onChange(entries.map((entry, i) => (i === index ? { ...entry, [key]: next } : entry)));
    return (
      <fieldset className={`${className} qc-excerpts`} disabled={disabled}>
        <legend>{label}</legend>
        <small>
          {at('adjunct.generationControl.copy85')}
          {field === 'video_excerpts' ? at('adjunct.generationControl.copy86') : ''}
        </small>
        {entries.map((entry, index) => (
          <div className="qc-excerpt-row" key={index}>
            {[
              ['time_seconds', at('adjunct.generationControl.copy87')],
              ['duration_seconds', at('adjunct.generationControl.copy88')],
            ].map(([key, title]) => (
              <label key={key}>
                {title}
                {at('adjunct.generationControl.copy89')}
                <input
                  aria-label={at('adjunct.generationControl.copy90', {
                    v0: label,
                    v1: index + 1,
                    v2: title,
                  })}
                  type="number"
                  required={schema.items?.required?.includes(key)}
                  placeholder={at('adjunct.generationControl.copy91')}
                  min={properties[key]?.minimum}
                  max={properties[key]?.maximum}
                  step="any"
                  value={entry[key] ?? ''}
                  onChange={(event) =>
                    patch(index, key, event.target.value === '' ? null : Number(event.target.value))
                  }
                />
              </label>
            ))}
            <button
              type="button"
              aria-label={at('adjunct.generationControl.copy92', { v0: label, v1: index + 1 })}
              onClick={() => onChange(entries.filter((_, i) => i !== index))}
            >
              {at('adjunct.generationControl.copy93')}
            </button>
          </div>
        ))}
        <button
          type="button"
          disabled={disabled || (Number.isInteger(maximum) && entries.length >= maximum)}
          onClick={() => onChange([...entries, newExcerptControl(field, schema)])}
        >
          {at('adjunct.generationControl.copy94')}
          {Number.isInteger(maximum) ? `（${entries.length}/${maximum}）` : ''}
        </button>
        {schema.reason && <small>{schema.reason}</small>}
      </fieldset>
    );
  }
  if (['array', 'object'].includes(type)) return null; // Guides and video soundtracks are edited on materials.
  const selected = value ?? schema.default,
    unset = selected === undefined;
  return (
    <label className={`${className}${disabled ? ' qc-disabled' : ''}`}>
      <span>
        {label}
        {schema.experimental ? at('adjunct.generationControl.copy95') : ''}
      </span>
      {schema.enum ? (
        <select
          aria-label={label}
          value={unset ? '' : String(selected)}
          disabled={disabled}
          onChange={(event) =>
            onChange(
              event.target.value === ''
                ? undefined
                : schema.enum.find((option) => String(option) === event.target.value),
            )
          }
        >
          {!Object.hasOwn(schema, 'default') && (
            <option value="">{at('adjunct.generationControl.copy96')}</option>
          )}
          {schema.enum.map((option) => (
            <option key={String(option)} value={String(option)}>
              {options[field]?.[option] || String(option)}
            </option>
          ))}
        </select>
      ) : type === 'boolean' ? (
        <input
          aria-label={label}
          type="checkbox"
          checked={value ?? schema.default ?? false}
          disabled={disabled}
          onChange={(event) => onChange(event.target.checked)}
        />
      ) : (
        <input
          aria-label={label}
          type={field === 'seed' || type === 'string' ? 'text' : 'number'}
          inputMode={field === 'seed' ? 'numeric' : undefined}
          disabled={disabled}
          value={value ?? ''}
          min={schema.minimum}
          max={schema.maximum}
          step={schema.multipleOf || (type === 'integer' ? 1 : 'any')}
          placeholder={nullable || field === 'seed' ? at('adjunct.generationControl.copy97') : ''}
          onChange={(event) =>
            onChange(
              event.target.value === ''
                ? nullable
                  ? null
                  : undefined
                : type === 'string' || field === 'seed'
                  ? event.target.value
                  : Number(event.target.value),
            )
          }
        />
      )}
      <small>
        {schema.reason ||
          (field === 'generate_audio'
            ? at('adjunct.generationControl.copy98')
            : field === 'denoise'
              ? at('adjunct.generationControl.copy99')
              : field === 'video_conditioning'
                ? at('adjunct.generationControl.copy100')
                : field === 'audio_conditioning'
                  ? at('adjunct.generationControl.copy101')
                  : schema.description) ||
          [
            schema.minimum === undefined
              ? ''
              : at('adjunct.generationControl.copy102', { v0: schema.minimum }),
            schema.maximum === undefined
              ? ''
              : at('adjunct.generationControl.copy103', { v0: schema.maximum }),
          ]
            .filter(Boolean)
            .join(' · ')}
      </small>
    </label>
  );
}

import { useI18n } from './i18n/I18nProvider.jsx';
import { chapterTiming } from './media-timing.js';
import React from 'react';
import { deferredPanel } from './deferred-panel.js';
import {
  ArrowRight,
  ArrowLeft,
  ArrowUp,
  ArrowDown,
  Plus,
  Check,
  Circle,
  Clapperboard,
  FileText,
  Users,
  Image,
  Headphones,
  Download,
  RotateCcw,
  Sparkles,
  Film,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react';
import { store, typeLabels, children } from './store.js';
import './creation-journey.css';
import CandidateReview from './CandidateReview.jsx';
const DeliveryStudio = deferredPanel(() => import('./DeliveryStudio.jsx'), '交付与粗剪');
import { deliveryIssues } from './delivery-model.js';
import StoryCoach from './StoryCoach.jsx';
const CharacterStudio = deferredPanel(() => import('./CharacterStudio.jsx'), '角色造型');
const ScriptBreakdown = deferredPanel(() => import('./ScriptBreakdown.jsx'), '剧本拆分');
import ReferenceEditor from './ReferenceEditor.jsx';
import ShotMediaWorkspace from './ShotMediaWorkspace.jsx';
import LocationStudio, { SceneLocationPicker } from './LocationStudio.jsx';
import AssetPreview from './AssetPreview.jsx';
import { entityCover } from './EntityMediaUpload.jsx';
const SoundStudio = deferredPanel(() => import('./SoundStudio.jsx'), '声音编辑器');
const SubtitleStudio = deferredPanel(() => import('./SubtitleStudio.jsx'), '字幕编辑器');

const journeyStages = (lt) => [
  {
    title: lt('legacy.stage1'),
    short: lt('legacy.stage2'),
    icon: FileText,
    decision: lt('legacy.stage3'),
    next: lt('legacy.stage4'),
  },
  {
    title: lt('legacy.stage5'),
    short: lt('legacy.stage6'),
    icon: Clapperboard,
    decision: lt('legacy.stage7'),
    next: lt('legacy.stage8'),
  },
  {
    title: lt('legacy.stage9'),
    short: lt('legacy.stage10'),
    icon: Users,
    decision: lt('legacy.stage11'),
    next: lt('legacy.stage12'),
  },
  {
    title: lt('legacy.stage13'),
    short: lt('legacy.stage14'),
    icon: Image,
    decision: lt('legacy.stage15'),
    next: lt('legacy.stage16'),
  },
  {
    title: lt('legacy.stage17'),
    short: lt('legacy.stage18'),
    icon: CheckCircle2,
    decision: lt('legacy.stage19'),
    next: lt('legacy.stage20'),
  },
  {
    title: lt('legacy.stage21'),
    short: lt('legacy.stage22'),
    icon: Headphones,
    decision: lt('legacy.stage23'),
    next: lt('legacy.stage24'),
  },
  {
    title: lt('legacy.stage25'),
    short: lt('legacy.stage26'),
    icon: Download,
    decision: lt('legacy.stage27'),
    next: null,
  },
];
const order = (a, b) => a.order - b.order;
const orderedShots = (p, chapter) =>
  chapter
    ? children(p, chapter.id)
        .filter((x) => x.type === 'scene')
        .flatMap((scene) => children(p, scene.id).filter((x) => x.type === 'shot'))
    : [];
const Field = ({ label, hint, children: content }) => (
  <label className="journey-field">
    <span>{label}</span>
    {React.isValidElement(content)
      ? React.cloneElement(content, { 'aria-label': content.props['aria-label'] || label })
      : content}
    {hint && <small>{hint}</small>}
  </label>
);
const Empty = ({ title, body, action, label }) => (
  <div className="journey-empty">
    <Circle size={24} />
    <h3>{title}</h3>
    <p>{body}</p>
    {action && (
      <button onClick={action}>
        <Plus size={15} />
        {label}
      </button>
    )}
  </div>
);
const reviewNames = {
  selected: '已有选择',
  revision: '待返修',
  failed: '演示：失败',
  retry: '演示：等待重试',
  cancelled: '演示：已取消',
};
function NumberInput({ value, min = 1 / 24, max = 3600, positiveOnly = false, onValue }) {
  const { t: lt, locale } = useI18n();
  const format = (number) => String(Number(Number(number).toFixed(6)));
  const [draft, setDraft] = React.useState(format(value));
  React.useEffect(() => setDraft(format(value)), [value]);
  const valid = (text) =>
    text !== '' &&
    Number.isFinite(Number(text)) &&
    (positiveOnly ? Number(text) > 0 : Number(text) >= min) &&
    (max === null || Number(text) <= max);
  return (
    <input
      type="number"
      step="any"
      min={positiveOnly ? undefined : min}
      max={max ?? undefined}
      value={draft}
      onChange={(e) => {
        const text = e.target.value;
        setDraft(text);
        if (valid(text)) onValue(Number(text));
      }}
      onBlur={() => {
        if (!valid(draft)) {
          setDraft(String(value));
          store.notify(
            lt(positiveOnly ? 'library.positiveDuration' : 'journey.durationInvalid', { min, max }),
          );
        }
      }}
    />
  );
}

export default function CreationJourney({ state, onAdd, onInspect, onPreview }) {
  const { t: at } = useI18n();
  const { t: lt, locale } = useI18n();
  const stages = journeyStages(lt);
  const typeLabels = { character: lt('journey.character'), location: lt('journey.location') };
  const p = state.project,
    j = p.journey || {},
    brief = j.brief || {},
    sound = j.sound || {};
  const stage = Number.isInteger(j.stage) && j.stage >= 1 && j.stage <= 7 ? j.stage : 1;
  const chapters = p.entities.filter((x) => x.type === 'chapter').sort(order);
  const chapter = chapters.find((x) => x.id === state.scopeId) || chapters[0] || null;
  const scenes = chapter ? children(p, chapter.id).filter((x) => x.type === 'scene') : [];
  const shots = orderedShots(p, chapter),
    allShots = chapters.flatMap((c) => orderedShots(p, c));
  const characters = p.entities.filter((x) => x.type === 'character'),
    locations = p.entities.filter((x) => x.type === 'location');
  const currentShot = shots.find((x) => x.id === j.reviewShotId) || shots[0] || null;
  const currentAsset =
    currentShot && p.entities.find((x) => x.id === currentShot.data.selectedAssetId);
  const demoChoice =
    currentShot && !currentShot.data.selectedAssetId ? currentShot.data.uxReview?.chosen : null;
  const usableCandidate = (shot) =>
    shot.data.selectedAssetId
      ? p.entities.some(
          (x) => x.id === shot.data.selectedAssetId && x.data.fileId && !x.data.missingFile,
        )
      : !!shot.data.uxReview?.chosen;
  const totalSeconds = Number((chapterTiming(shots).at(-1)?.end || 0).toFixed(3));
  const setJourney = (patch, key) => {
    const current = store.getState().project;
    return store.updateProject(
      { journey: { ...(current.journey || {}), ...patch } },
      { coalesceKey: key ? `journey:${key}` : null },
    );
  };
  const setBrief = (key, value) =>
    setJourney(
      { brief: { ...(store.getState().project.journey?.brief || {}), [key]: value } },
      `brief:${key}`,
    );
  const setSound = (key, value) =>
    setJourney(
      { sound: { ...(store.getState().project.journey?.sound || {}), [key]: value } },
      `sound:${key}`,
    );
  const go = (next) => {
    setJourney({ stage: next });
    store.select(null);
  };
  const add = (kind, parent) => {
    if (parent?.type === 'chapter') store.setScope(parent.id);
    if (parent) store.select(parent.id);
    onAdd(kind);
  };
  const patchShot = (id, key, value) =>
    store.updateEntity(
      id,
      key === 'description' ? { description: value } : { data: { [key]: value } },
      { coalesceKey: `journey:${id}:${key}` },
    );
  const setReview = (patch) => {
    if (!currentShot) return;
    const latest = store.getState().project.entities.find((x) => x.id === currentShot.id);
    store.updateEntity(
      currentShot.id,
      {
        ...(patch.status === 'selected' ? { status: 'draft' } : {}),
        data: {
          ...(patch.chosen ? { selectedAssetId: '' } : {}),
          uxReview: { ...(latest?.data.uxReview || {}), ...patch },
        },
      },
      { coalesceKey: patch.note !== undefined ? `review:${currentShot.id}:note` : null },
    );
  };
  const addExample = () => {
    const n = store.getState().project.entities.filter((e) => e.type === 'chapter').length + 1;
    const chapterId = store.addEntity('chapter', null, {
      title: `示例拆解 ${n} · 错过的车票`,
      description: '示例：她发现一张写着明天日期的旧车票。可改写，也可删除。',
    });
    if (!chapterId) return;
    const sceneId = store.addEntity('scene', chapterId, {
      title: '车站 · 雨夜',
      description: '让观众看到车票上的异常，留下悬念。',
      data: {
        script:
          '林岚走进空荡的车站，捡起一张旧车票。\n她低声说：“这不是……明天吗？”\n广播突然响起，她抬头。',
        time: '雨夜',
      },
    });
    if (!sceneId) return;
    store.addEntity('shot', sceneId, {
      title: '旧车票的日期',
      description: '手指拂去车票上的雨水，镜头看清票面日期。',
      data: { demoType: 'ticket', seconds: 4, goal: '给出异常线索', shotSize: '特写' },
    });
    store.addEntity('shot', sceneId, {
      title: '她抬头看向广播',
      description: '林岚握着车票抬头，空车站里只有她一人。',
      data: { demoType: 'reaction', seconds: 6, goal: '人物反应，留下悬念', shotSize: '中景' },
    });
    store.setScope(chapterId);
    store.select(null);
    store.notify(lt('journey.more1'));
  };
  const useScript = () => {
    const project = store.getState().project;
    const text = String(project.journey?.brief?.sourceScript || '').trim();
    if (!text) {
      store.notify(lt('journey.more2'));
      return;
    }
    const previous = project.journey?.scriptImport;
    if (
      previous?.source === text &&
      project.entities.some((e) => e.id === previous.chapterId && e.type === 'chapter')
    ) {
      store.setScope(previous.chapterId);
      go(2);
      store.notify(lt('journey.more3'));
      return;
    }
    const id = store.addEntity('chapter', null, {
      title: '导入剧本 · 待分章',
      description: '保留原文，接下来可手动拆成多个章节与场戏。',
    });
    if (!id) return;
    store.addEntity('scene', id, { title: '原始剧本 · 待拆场', data: { script: text } });
    setJourney({ scriptImport: { source: text, chapterId: id } });
    store.setScope(id);
    store.select(null);
    go(2);
    store.notify(lt('journey.more4'));
  };
  const checks = [
    {
      stage: 1,
      title: at('adjunct.creationJourney.copy274'),
      ok: !!p.logline.trim(),
      detail: p.logline.trim()
        ? at('adjunct.creationJourney.copy275')
        : at('adjunct.creationJourney.copy276'),
    },
    {
      stage: 2,
      title: at('adjunct.creationJourney.copy277'),
      ok: !!chapter && scenes.length > 0 && scenes.every((s) => String(s.data.script || '').trim()),
      detail: !chapter
        ? at('adjunct.creationJourney.copy278')
        : !scenes.length
          ? at('adjunct.creationJourney.copy279')
          : at('adjunct.creationJourney.copy280', {
              v0: scenes.filter((s) => String(s.data.script || '').trim()).length,
              v1: scenes.length,
            }),
    },
    {
      stage: 3,
      title: at('adjunct.creationJourney.copy281'),
      ok: characters.length > 0 && locations.length > 0,
      optional: true,
      detail: at('adjunct.creationJourney.copy282', {
        v0: characters.length,
        v1: locations.length,
      }),
    },
    {
      stage: 4,
      title: at('adjunct.creationJourney.copy283'),
      ok:
        shots.length > 0 &&
        shots.every(
          (s) => String(s.data.prompt || s.description || '').trim() && s.data.seconds > 0,
        ),
      detail: shots.length
        ? at('adjunct.creationJourney.copy284', {
            v0: shots.filter(
              (s) => String(s.data.prompt || s.description || '').trim() && s.data.seconds > 0,
            ).length,
            v1: shots.length,
          })
        : at('adjunct.creationJourney.copy285'),
    },
    {
      stage: 5,
      title: at('adjunct.creationJourney.copy286'),
      ok:
        shots.length > 0 &&
        shots.every(
          (s) =>
            usableCandidate(s) &&
            s.status !== 'review' &&
            !['revision', 'failed', 'retry', 'cancelled'].includes(s.data.uxReview?.status),
        ),
      detail: at('adjunct.creationJourney.copy287', {
        v0: shots.filter(usableCandidate).length,
        v1: shots.length,
        v2: shots.some((s) => s.status === 'review') ? at('adjunct.creationJourney.copy288') : '',
      }),
    },
    {
      stage: 6,
      title: at('adjunct.creationJourney.copy289'),
      ok: !!sound.mode,
      detail:
        sound.mode === 'silent'
          ? at('adjunct.creationJourney.copy290')
          : sound.mode
            ? at('adjunct.creationJourney.copy291')
            : at('adjunct.creationJourney.copy292'),
    },
  ];
  const incomplete = deliveryIssues(p, chapter?.id, locale).filter((c) => !c.accepted);
  const stageStatus = [
    p.logline.trim() ? lt('journey.directionSaved') : lt('journey.awaitIdea'),
    chapters.length
      ? lt('journey.chapterScene', {
          chapters: chapters.length,
          scenes: p.entities.filter((e) => e.type === 'scene').length,
        })
      : lt('journey.firstChapter'),
    lt('journey.charactersLocations', {
      characters: characters.length,
      locations: locations.length,
    }),
    lt('journey.shotCount', { count: allShots.length }),
    lt('journey.chosenCount', {
      count: allShots.filter((s) => s.data.uxReview?.chosen || s.data.selectedAssetId).length,
    }),
    sound.mode === 'silent'
      ? lt('legacy.journey59')
      : sound.mode
        ? lt('journey.audioSaved')
        : lt('journey.silentFirst'),
    incomplete.length
      ? lt('journey.remaining', { count: incomplete.length })
      : lt('journey.decisionsSaved'),
  ];
  const quickShot = () => {
    const ids = ['chapter', 'scene', 'shot'].map((kind) => kind + '-' + crypto.randomUUID());
    const ok = store.editProject((q) => {
      const base = { description: '', order: 0, version: 1, status: 'draft', data: {} };
      q.entities.push(
        {
          ...base,
          id: ids[0],
          type: 'chapter',
          title: '单镜练习',
          parentId: null,
          order: q.entities.filter((e) => e.type === 'chapter').length,
        },
        {
          ...base,
          id: ids[1],
          type: 'scene',
          title: '第一场',
          parentId: ids[0],
          data: { script: q.logline },
        },
        {
          ...base,
          id: ids[2],
          type: 'shot',
          title: '我的第一个镜头',
          parentId: ids[1],
          description: q.logline,
          data: { seconds: 6 },
        },
      );
      q.journey = { ...q.journey, stage: 4, reviewShotId: ids[2] };
    });
    if (ok) {
      store.setScope(ids[0]);
      store.select(null);
    }
  };
  const fixIssue = (issue) => {
    const entity = p.entities.find((e) => e.id === issue.entityId);
    let e = entity;
    while (e && e.type !== 'chapter') e = p.entities.find((x) => x.id === e.parentId);
    if (e) store.setScope(e.id);
    setJourney({
      stage: issue.stage,
      focusCharacterId: entity?.type === 'character' ? entity.id : j.focusCharacterId || null,
      focusLookId: issue.lookId || null,
      reviewShotId: entity?.type === 'shot' ? entity.id : j.reviewShotId || null,
      returnToDelivery: {
        issueId: issue.id,
        entityId: issue.entityId || null,
        field: issue.field || '',
        title: issue.title || '回看制作决定',
      },
      lastDeliveryIssue: issue.id,
    });
    store.select(null);
  };
  React.useEffect(() => {
    if (stage === 7 || !j.returnToDelivery?.entityId) return;
    let observer;
    const focus = () => {
      const element =
        stage === 3
          ? document.querySelector('.character-studio')
          : document.getElementById('journey-entity-' + j.returnToDelivery.entityId);
      if (!element) return false;
      element.scrollIntoView({ block: 'center' });
      element.classList.add('v6-target');
      if (j.returnToDelivery.field === 'referenceRanges') {
        const details = element.querySelector('details');
        if (details) details.open = true;
      }
      element.querySelector('textarea,input')?.focus({ preventScroll: true });
      observer?.disconnect();
      return true;
    };
    if (!focus()) {
      const panel = document.querySelector('.journey-stage-body');
      if (panel) {
        observer = new MutationObserver(focus);
        observer.observe(panel, { childList: true, subtree: true });
      }
    }
    return () => observer?.disconnect();
  }, [stage, j.returnToDelivery?.entityId, j.returnToDelivery?.field]);
  const StageIcon = stages[stage - 1].icon;

  return (
    <div className="creation-journey" data-stage={stage}>
      {stage !== 7 && j.returnToDelivery && (
        <div className="v6-return" role="status">
          <span>{lt('journey.returnIssue', { title: j.returnToDelivery.title })}</span>
          <button
            onClick={() => {
              setJourney({ stage: 7, returnToDelivery: null });
              store.select(null);
            }}
          >
            {lt('legacy.journey1')}
          </button>
        </div>
      )}
      <header className="journey-heading">
        <div>
          <span className="eyebrow">YOUR STORY, ONE DECISION AT A TIME</span>
          <h1>{lt('legacy.journey2')}</h1>
          <p>{lt('legacy.journey3')}</p>
        </div>
        <div className="journey-project-chip">
          <Film size={16} />
          <span>{p.title}</span>
          <small>{lt('legacy.journey4')}</small>
        </div>
      </header>
      <nav className="journey-map" aria-label={lt('legacy.journey71')}>
        {stages.map((s, index) => (
          <button
            key={s.title}
            data-testid={`journey-stage-${index + 1}`}
            aria-current={stage === index + 1 ? 'step' : undefined}
            onClick={() => go(index + 1)}
          >
            <span className="journey-step-number">{String(index + 1).padStart(2, '0')}</span>
            <span>
              <strong>{s.title}</strong>
              <small>{s.short}</small>
            </span>
          </button>
        ))}
      </nav>
      <section
        className="journey-panel"
        data-testid="journey-panel"
        aria-label={stages[stage - 1].title}
      >
        <header className="journey-panel-heading">
          <div className="journey-step-icon">
            <StageIcon size={24} />
          </div>
          <div>
            <span className="eyebrow">{lt('journey.step', { step: stage })}</span>
            <h2>{stages[stage - 1].title}</h2>
            <p>
              <b>{lt('legacy.journey5')}</b> · {stages[stage - 1].decision}
            </p>
          </div>
          <span className="journey-status">{stageStatus[stage - 1]}</span>
        </header>

        {stage === 1 && (
          <div className="journey-stage-body">
            <div className="journey-entry-options" role="group" aria-label={lt('legacy.journey72')}>
              <button
                aria-pressed={(brief.entry || 'idea') === 'idea'}
                onClick={() => setBrief('entry', 'idea')}
              >
                <Sparkles size={20} />
                <span>
                  <strong>{lt('legacy.journey6')}</strong>
                  <small>{lt('legacy.journey7')}</small>
                </span>
                {(brief.entry || 'idea') === 'idea' && <Check size={16} />}
              </button>
              <button
                aria-pressed={brief.entry === 'script'}
                onClick={() => setBrief('entry', 'script')}
              >
                <FileText size={20} />
                <span>
                  <strong>{lt('legacy.journey8')}</strong>
                  <small>{lt('legacy.journey9')}</small>
                </span>
                {brief.entry === 'script' && <Check size={16} />}
              </button>
            </div>
            <StoryCoach project={p} onQuickShot={quickShot} />
            <Field label={lt('legacy.stage54')} hint={lt('legacy.stage45')}>
              <textarea
                rows={3}
                maxLength={24000}
                value={p.logline}
                onChange={(e) =>
                  store.updateProject(
                    { logline: e.target.value },
                    { coalesceKey: 'journey:logline' },
                  )
                }
                placeholder={lt('legacy.stage29')}
              />
            </Field>
            <div className="journey-field-grid">
              <Field label={lt('legacy.stage47')}>
                <input
                  value={brief.audience || ''}
                  maxLength={300}
                  onChange={(e) => setBrief('audience', e.target.value)}
                  placeholder={lt('legacy.stage32')}
                />
              </Field>
              <Field label={lt('legacy.stage46')}>
                <select
                  value={brief.aspect || '9:16'}
                  onChange={(e) => setBrief('aspect', e.target.value)}
                >
                  <option value="9:16">{lt('legacy.journey10')}</option>
                  <option value="16:9">{lt('legacy.journey11')}</option>
                  <option value="1:1">{lt('legacy.journey12')}</option>
                </select>
              </Field>
              <Field label={lt('legacy.stage37')} hint={lt('legacy.stage35')}>
                <NumberInput
                  positiveOnly
                  max={null}
                  value={brief.seconds ?? 60}
                  onValue={(value) => setBrief('seconds', value)}
                />
              </Field>
            </div>
            {brief.entry === 'script' && (
              <div className="journey-soft-panel">
                <Field label={lt('legacy.stage48')} hint={lt('legacy.stage38')}>
                  <textarea
                    rows={7}
                    maxLength={120000}
                    value={brief.sourceScript || ''}
                    onChange={(e) => setBrief('sourceScript', e.target.value)}
                    placeholder={lt('legacy.stage49')}
                  />
                </Field>
                <button
                  className="primary"
                  disabled={!String(brief.sourceScript || '').trim()}
                  onClick={() => go(2)}
                >
                  <FileText size={16} />
                  {lt('legacy.journey13')}
                </button>
                <details>
                  <summary>{lt('legacy.journey14')}</summary>
                  <p>{lt('legacy.journey15')}</p>
                  <button disabled={!String(brief.sourceScript || '').trim()} onClick={useScript}>
                    {lt('legacy.journey16')}
                  </button>
                </details>
              </div>
            )}
            <aside className="journey-tip">
              <span>{lt('legacy.journey17')}</span>
              {lt('legacy.journey18')}
            </aside>
          </div>
        )}

        {stage === 2 && (
          <div className="journey-stage-body">
            <div className="journey-section-top">
              <div>
                <h3>{lt('legacy.journey19')}</h3>
                <p>{lt('legacy.journey20')}</p>
              </div>
              <button className="primary" onClick={() => add('chapter')}>
                <Plus size={16} />
                {lt('legacy.journey21')}
              </button>
            </div>
            {brief.sourceScript && (
              <ScriptBreakdown locale={locale} project={p} onInspect={onInspect} />
            )}
            {!chapters.length && (
              <Empty title={lt('legacy.journey73')} body={lt('legacy.stage33')} />
            )}
            <div className="journey-chapters">
              {chapters.map((c, index) => (
                <article className="journey-chapter" id={'journey-entity-' + c.id} key={c.id}>
                  <div className="journey-chapter-head">
                    <span className="journey-chapter-number">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <div>
                      <button className="journey-title-button" onClick={() => onInspect(c.id)}>
                        {c.title}
                      </button>
                      <p>{c.description || lt('journey.more5')}</p>
                    </div>
                    <button onClick={() => add('scene', c)}>
                      <Plus size={14} />
                      {lt('legacy.journey22')}
                    </button>
                  </div>
                  {children(p, c.id)
                    .filter((s) => s.type === 'scene')
                    .map((s) => (
                      <div className="journey-scene" id={'journey-entity-' + s.id} key={s.id}>
                        <div>
                          <button className="journey-title-button" onClick={() => onInspect(s.id)}>
                            {s.title}
                            <ArrowRight size={13} />
                          </button>
                          <small>
                            {lt('journey.shotCount', {
                              count: children(p, s.id).filter((q) => q.type === 'shot').length,
                            })}
                          </small>
                        </div>
                        <Field label={lt('journey.field1', { title: s.title })}>
                          <textarea
                            value={s.data.script || ''}
                            maxLength={120000}
                            rows={3}
                            onChange={(e) =>
                              store.updateEntity(
                                s.id,
                                { data: { script: e.target.value } },
                                { coalesceKey: `journey:script:${s.id}` },
                              )
                            }
                            placeholder={lt('legacy.stage50')}
                          />
                        </Field>
                        <SceneLocationPicker project={p} scene={s} />
                        <button
                          className="journey-minor-button"
                          onClick={() => {
                            store.setScope(c.id);
                            add('shot', s);
                          }}
                        >
                          <Plus size={14} />
                          {lt('legacy.journey23')}
                        </button>
                      </div>
                    ))}
                </article>
              ))}
            </div>
            <div className="journey-example">
              <div>
                <Sparkles size={19} />
                <div>
                  <strong>{lt('legacy.journey24')}</strong>
                  <p>{lt('legacy.journey25')}</p>
                </div>
              </div>
              <button data-testid="journey-add-example" onClick={addExample}>
                {lt('legacy.journey26')}
                <ArrowRight size={14} />
              </button>
            </div>
          </div>
        )}

        {stage === 3 && (
          <div className="journey-stage-body">
            <div className="journey-library-grid">
              {[
                ['character', lt('journey.more6'), characters, lt('journey.more7')],
                ['location', lt('journey.more8'), locations, lt('journey.more9')],
              ].map(([kind, title, list, description]) => (
                <section key={kind} className="journey-library">
                  <div className="journey-section-top">
                    <div>
                      <h3>{title}</h3>
                      <p>{description}</p>
                    </div>
                    <button
                      onClick={() => add(kind)}
                      aria-label={lt('journey.addType', { type: typeLabels[kind] })}
                    >
                      <Plus size={15} />
                    </button>
                  </div>
                  {list.length ? (
                    list.map((e) => (
                      <button
                        className="journey-library-item"
                        key={e.id}
                        onClick={() => onInspect(e.id)}
                      >
                        <span className={'journey-avatar ' + kind}>
                          {entityCover(p, e) ? (
                            <AssetPreview entity={entityCover(p, e)} />
                          ) : kind === 'character' ? (
                            <Users size={19} />
                          ) : (
                            <Image size={19} />
                          )}
                        </span>
                        <span>
                          <strong>{e.title}</strong>
                          <small>{e.description || lt('journey.more10')}</small>
                        </span>
                        <ArrowRight size={14} />
                      </button>
                    ))
                  ) : (
                    <Empty
                      title={kind === 'character' ? lt('journey.more11') : lt('journey.more12')}
                      body={lt('legacy.stage39')}
                      label={lt('journey.addType', { type: typeLabels[kind] })}
                      action={() => add(kind)}
                    />
                  )}
                </section>
              ))}
            </div>
            <CharacterStudio
              locale={locale}
              project={p}
              chapterId={chapter?.id}
              onAdd={onAdd}
              onInspect={onInspect}
            />
            <LocationStudio project={p} onAdd={onAdd} onInspect={onInspect} />
            <aside className="journey-tip">{lt('legacy.journey27')}</aside>
          </div>
        )}

        {stage >= 4 && (
          <div className="journey-chapter-filter">
            <Field label={lt('legacy.stage44')}>
              <select
                value={chapter?.id || ''}
                onChange={(e) => {
                  store.setScope(e.target.value || null);
                  store.select(null);
                }}
              >
                {!chapters.length && <option value="">{lt('legacy.journey28')}</option>}
                {chapters.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
              </select>
            </Field>
            <span>{lt('journey.planned', { count: shots.length, seconds: totalSeconds })}</span>
          </div>
        )}

        {stage === 4 && (
          <div className="journey-stage-body">
            <div className="journey-section-top">
              <div>
                <h3>{lt('legacy.journey29')}</h3>
                <p>{lt('legacy.journey30')}</p>
              </div>
              <button
                onClick={() =>
                  scenes.length ? add('shot', scenes[0]) : chapter ? add('scene', chapter) : go(2)
                }
              >
                <Plus size={15} />
                {scenes.length
                  ? lt('journey.more13')
                  : chapter
                    ? lt('journey.more14')
                    : lt('journey.more15')}
              </button>
            </div>
            {!shots.length ? (
              <Empty
                title={lt('legacy.journey74')}
                body={lt('legacy.stage34')}
                action={() => go(2)}
                label={lt('legacy.stage40')}
              />
            ) : (
              <>
                <div className="shot-visual-nav" role="group" aria-label={lt('legacy.journey75')}>
                  {shots.map((shot, index) => {
                    const linked =
                        p.links.find(
                          (link) => link.target === shot.id && link.role === 'firstFrame',
                        ) ||
                        p.links.find(
                          (link) =>
                            link.target === shot.id &&
                            p.entities.find((entity) => entity.id === link.source)?.type ===
                              'image',
                        ),
                      asset = p.entities.find(
                        (entity) =>
                          entity.id === (shot.data.selectedAssetId || linked?.source) &&
                          ['image', 'video'].includes(entity.type),
                      );
                    return (
                      <button
                        key={shot.id}
                        type="button"
                        aria-pressed={shot.id === currentShot?.id}
                        onClick={() => setJourney({ reviewShotId: shot.id })}
                      >
                        {asset ? (
                          <AssetPreview entity={asset} compact />
                        ) : (
                          <div className="shot-nav-placeholder">
                            <Film size={28} />
                          </div>
                        )}
                        <strong>
                          {String(index + 1).padStart(2, '0')} · {shot.title}
                        </strong>
                        <small>
                          {lt('journey.shotRefs', {
                            seconds: shot.data.seconds || 5,
                            count: p.links.filter((link) => link.target === shot.id).length,
                          })}
                        </small>
                      </button>
                    );
                  })}
                </div>
                <div className="journey-shot-cards">
                  {[currentShot].filter(Boolean).map((s) => (
                    <article className="journey-shot-card" id={'journey-entity-' + s.id} key={s.id}>
                      <header>
                        <span className="journey-shot-number">
                          {String(shots.findIndex((shot) => shot.id === s.id) + 1).padStart(2, '0')}
                        </span>
                        <h3>{s.title}</h3>
                        <small>{p.entities.find((e) => e.id === s.parentId)?.title}</small>
                      </header>
                      <details className="shot-storyboard-details">
                        <summary>{lt('legacy.journey31')}</summary>
                        <div className="journey-shot-fields">
                          <Field label={lt('journey.field2', { title: s.title })}>
                            <input
                              value={s.data.goal || ''}
                              maxLength={1000}
                              onChange={(e) => patchShot(s.id, 'goal', e.target.value)}
                              placeholder={lt('legacy.stage30')}
                            />
                          </Field>
                          <Field label={lt('journey.field3', { title: s.title })}>
                            <NumberInput
                              value={s.data.seconds || 6}
                              onValue={(value) => patchShot(s.id, 'seconds', value)}
                            />
                          </Field>
                        </div>
                        <Field label={lt('journey.field4', { title: s.title })}>
                          <textarea
                            rows={3}
                            value={s.description}
                            maxLength={24000}
                            onChange={(e) => patchShot(s.id, 'description', e.target.value)}
                            placeholder={lt('legacy.stage28')}
                          />
                        </Field>
                        <label className="journey-field">
                          <span>{lt('journey.field5', { title: s.title })}</span>
                          <select
                            aria-label={lt('journey.field5', { title: s.title })}
                            value={s.data.shotSize || ''}
                            onChange={(e) => patchShot(s.id, 'shotSize', e.target.value)}
                          >
                            <option value="">{lt('legacy.journey32')}</option>
                            <option value="特写">{lt('legacy.journey33')}</option>
                            <option value="中景">{lt('legacy.journey34')}</option>
                            <option value="全景">{lt('legacy.journey35')}</option>
                          </select>
                        </label>
                        <details className="v6-camera-options">
                          <summary>{lt('legacy.journey36')}</summary>
                          <div className="v6-requirements">
                            <label>
                              {lt('legacy.journey37')}
                              <select
                                aria-label={lt('journey.field6', { title: s.title })}
                                value={s.data.cameraMove || ''}
                                onChange={(e) => patchShot(s.id, 'cameraMove', e.target.value)}
                              >
                                <option value="">{lt('legacy.journey38')}</option>
                                <option value="固定机位 · 让动作自己发生">
                                  {lt('legacy.journey39')}
                                </option>
                                <option value="缓慢推进 · 靠近细节或情绪">
                                  {lt('legacy.journey40')}
                                </option>
                                <option value="缓慢拉远 · 揭示周围环境">
                                  {lt('legacy.journey41')}
                                </option>
                                <option value="跟随人物 · 和主角一起移动">
                                  {lt('legacy.journey42')}
                                </option>
                                <option value="横向摇摄 · 逐步展示信息">
                                  {lt('legacy.journey43')}
                                </option>
                              </select>
                            </label>
                            <label>
                              {lt('legacy.journey44')}
                              <select
                                aria-label={lt('journey.field7', { title: s.title })}
                                value={s.data.cameraHeight || ''}
                                onChange={(e) => patchShot(s.id, 'cameraHeight', e.target.value)}
                              >
                                <option value="">{lt('legacy.journey38')}</option>
                                <option value="平视 · 像面对面观察">
                                  {lt('legacy.journey45')}
                                </option>
                                <option value="稍低 · 增强人物气势">
                                  {lt('legacy.journey46')}
                                </option>
                                <option value="稍高 · 交代人物处境">
                                  {lt('legacy.journey47')}
                                </option>
                                <option value="俯视 · 看清布局与关系">
                                  {lt('legacy.journey48')}
                                </option>
                              </select>
                            </label>
                          </div>
                        </details>
                      </details>
                      <ShotMediaWorkspace project={p} shot={s} onInspect={onInspect} />
                      <div className="journey-shot-reference">
                        <div>
                          <span>{lt('legacy.journey49')}</span>
                          <small>
                            {lt('journey.referenceCount', {
                              count: p.links.filter((l) => l.target === s.id).length,
                            })}
                          </small>
                        </div>
                        <button onClick={() => onInspect(s.id)}>
                          {lt('legacy.journey50')}
                          <ArrowRight size={14} />
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              </>
            )}
            <div className="journey-tip">
              <span>{lt('legacy.journey51')}</span>
              {lt('legacy.journey52')}
            </div>
            <button className="journey-skip" onClick={() => go(5)}>
              {lt('legacy.journey53')}
              <ArrowRight size={14} />
            </button>
          </div>
        )}

        {stage === 5 && (
          <div className="journey-stage-body">
            <CandidateReview
              project={p}
              shots={shots}
              currentShot={currentShot}
              onChooseShot={(id) => setJourney({ reviewShotId: id })}
              onInspect={onInspect}
              onReferences={() => go(4)}
            />
          </div>
        )}

        {stage === 6 && (
          <div className="journey-stage-body">
            <div className="journey-runtime">
              <div>
                <span>{lt('legacy.journey54')}</span>
                <strong>
                  {totalSeconds}
                  <small>{lt('legacy.journey55')}</small>
                </strong>
                <p>
                  {lt('journey.target', { seconds: brief.seconds ?? 60 })}
                  {totalSeconds
                    ? lt(totalSeconds > (brief.seconds ?? 60) ? 'journey.over' : 'journey.under', {
                        seconds: Number(Math.abs(totalSeconds - (brief.seconds ?? 60)).toFixed(3)),
                      })
                    : ''}
                </p>
              </div>
              <button
                disabled={!shots.length}
                onClick={() => {
                  if (chapter) store.setScope(chapter.id);
                  onPreview();
                }}
              >
                <Film size={17} />
                {lt('legacy.journey56')}
              </button>
            </div>
            <p className="journey-small">{lt('legacy.journey57')}</p>
            <div className="journey-timeline">
              {shots.map((s, index) => {
                const siblings = children(p, s.parentId).filter((e) => e.type === 'shot'),
                  at = siblings.findIndex((e) => e.id === s.id);
                return (
                  <div key={s.id}>
                    <span>{String(index + 1).padStart(2, '0')}</span>
                    <button className="journey-timeline-name" onClick={() => onInspect(s.id)}>
                      <strong>{s.title}</strong>
                      <small>{p.entities.find((e) => e.id === s.parentId)?.title}</small>
                    </button>
                    <label>
                      <span className="visually-hidden">
                        {lt('journey.field8', { title: s.title })}
                      </span>
                      <NumberInput
                        value={s.data.seconds || 6}
                        onValue={(value) => patchShot(s.id, 'seconds', value)}
                      />
                      {lt('legacy.journey55')}
                    </label>
                    <div>
                      <button
                        aria-label={lt('journey.moveUp', { title: s.title })}
                        disabled={at === 0}
                        onClick={() => store.reorderEntity(s.id, -1)}
                      >
                        <ArrowUp size={14} />
                      </button>
                      <button
                        aria-label={lt('journey.moveDown', { title: s.title })}
                        disabled={at === siblings.length - 1}
                        onClick={() => store.reorderEntity(s.id, 1)}
                      >
                        <ArrowDown size={14} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
            {!shots.length && (
              <Empty
                title={lt('legacy.journey76')}
                body={lt('legacy.stage36')}
                label={lt('legacy.stage52')}
                action={() => go(4)}
              />
            )}
            <div className="journey-sound">
              <Field label={lt('legacy.stage41')}>
                <select value={sound.mode || ''} onChange={(e) => setSound('mode', e.target.value)}>
                  <option value="">{lt('legacy.journey58')}</option>
                  <option value="silent">{lt('legacy.journey59')}</option>
                  <option value="dialogue">{lt('legacy.journey60')}</option>
                  <option value="music">{lt('legacy.journey61')}</option>
                  <option value="mixed">{lt('legacy.journey62')}</option>
                </select>
              </Field>
              {sound.mode && sound.mode !== 'silent' && (
                <div className="journey-field-grid two">
                  <Field label={lt('legacy.stage43')}>
                    <textarea
                      rows={3}
                      value={sound.dialogue || ''}
                      maxLength={12000}
                      onChange={(e) => setSound('dialogue', e.target.value)}
                      placeholder={lt('legacy.stage51')}
                    />
                  </Field>
                  <Field label={lt('legacy.stage53')}>
                    <textarea
                      rows={3}
                      value={sound.music || ''}
                      maxLength={12000}
                      onChange={(e) => setSound('music', e.target.value)}
                      placeholder={lt('legacy.stage31')}
                    />
                  </Field>
                </div>
              )}
              <Field label={lt('legacy.stage42')}>
                <select
                  value={sound.subtitles || 'none'}
                  onChange={(e) => setSound('subtitles', e.target.value)}
                >
                  <option value="none">{lt('legacy.journey63')}</option>
                  <option value="dialogue">{lt('legacy.journey64')}</option>
                  <option value="bilingual">{lt('legacy.journey65')}</option>
                  <option value="accessibility">{lt('legacy.journey66')}</option>
                </select>
              </Field>
              {sound.mode === 'silent' && <p className="journey-tip">{lt('legacy.journey67')}</p>}
            </div>
            <SoundStudio locale={locale} project={p} chapterId={chapter?.id} shots={shots} />
            <SubtitleStudio
              locale={locale}
              key={p.id + ':' + chapter?.id}
              project={p}
              chapterId={chapter?.id}
            />
          </div>
        )}

        {stage === 7 && (
          <div className="journey-stage-body">
            <DeliveryStudio
              locale={locale}
              project={p}
              chapterId={chapter?.id}
              onFix={fixIssue}
              focusIssue={j.lastDeliveryIssue}
            />
          </div>
        )}

        <footer className="journey-panel-footer">
          <button disabled={stage === 1} onClick={() => go(stage - 1)}>
            <ArrowLeft size={15} />
            {lt('legacy.journey68')}
          </button>
          <span>{lt('legacy.journey69')}</span>
          {stage < 7 ? (
            <button className="primary" onClick={() => go(stage + 1)}>
              {stages[stage - 1].next}
              <ArrowRight size={15} />
            </button>
          ) : (
            <button onClick={() => go(1)}>
              {lt('legacy.journey70')}
              <ArrowRight size={15} />
            </button>
          )}
        </footer>
      </section>
    </div>
  );
}

import {useI18n} from './i18n/I18nProvider.jsx';
import React, { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ReactFlow, Background, Controls, MiniMap, Handle, Position, MarkerType, applyNodeChanges } from '@xyflow/react';
import { Plus, Search, LocateFixed, Layers3, Link2, ChevronRight, Film, Clapperboard, UserRound, MapPin, Image, Music2, Video, StickyNote, Sparkles, BookOpen, X, SlidersHorizontal, Trash2, HelpCircle, ArrowLeft, ArrowRight, ArrowUp, ArrowDown } from 'lucide-react';
import { store, typeLabels, roleLabels } from './store';
import { resolveCanvasPositions } from './canvas-layout.js';
import { castCanvasLinks } from './canvas-references.js';
import '@xyflow/react/dist/style.css';
import './canvas.css';

const icons = { chapter: BookOpen, scene: Clapperboard, shot: Film, character: UserRound, location: MapPin, image: Image, audio: Music2, video: Video, note: StickyNote, generation: Sparkles };
const colors = { chapter: '#8ca689', scene: '#97bbae', shot: '#b6c5c0', character: '#d8b29b', location: '#c0b7db', image: '#accce1', audio: '#dfc482', video: '#a2c5c7', note: '#dbc88f', generation: '#b4cc71' };
const statusLabels = lt => ({ draft: lt('canvas.ui1'), review: lt('canvas.ui2'), ready: lt('canvas.ui3') });
const cannotConnect = new Set(['chapter', 'scene']);
const isNarrative = entity => ['chapter', 'scene', 'shot'].includes(entity.type);
const sortEntities = (a, b) => (a.order ?? 0) - (b.order ?? 0) || a.id.localeCompare(b.id);
const finitePosition = pos => pos && Number.isFinite(pos.x) && Number.isFinite(pos.y);

function chapterFor(entities, id) {
  const map = new Map(entities.map(entity => [entity.id, entity]));
  let entity = map.get(id), visited = new Set();
  while (entity && !visited.has(entity.id)) {
    if (entity.type === 'chapter') return entity.id;
    visited.add(entity.id);
    entity = map.get(entity.parentId);
  }
  return null;
}

function tidyPositions(entities) {
  const result = {};
  const shared = entities.filter(entity => !isNarrative(entity)).sort(sortEntities);
  shared.forEach((entity, index) => { result[entity.id] = { x: 40, y: 40 + index * 260 }; });
  let y = 40;
  for (const chapter of entities.filter(entity => entity.type === 'chapter').sort(sortEntities)) {
    result[chapter.id] = { x: 390, y };
    const scenes = entities.filter(entity => entity.type === 'scene' && entity.parentId === chapter.id).sort(sortEntities);
    for (const scene of scenes) {
      result[scene.id] = { x: 740, y };
      const shots = entities.filter(entity => entity.type === 'shot' && entity.parentId === scene.id).sort(sortEntities);
      for (const shot of shots) { result[shot.id] = { x: 1090, y }; y += 260; }
      if (!shots.length) y += 260;
      y += 25;
    }
    if (!scenes.length) y += 260;
    y += 40;
  }
  return result;
}

const StoryNode = memo(function StoryNode({ id, data, selected }) {
 const {t:lt}=useI18n();const labels=statusLabels(lt),typeLabels=Object.fromEntries(["chapter", "scene", "shot", "character", "location", "prop", "image", "audio", "video", "note", "generation"].map(key=>[key,lt('canvas.type.'+key)])),roleLabels=Object.fromEntries(["identity", "location", "firstFrame", "lastFrame", "motion", "audio", "reference", "dependency"].map(key=>[key,lt('canvas.role.'+key)]));
  const { entity, childCount, onInspect, onBeginConnect } = data;
  const Icon = icons[entity.type] || StickyNote;
  const canReceive = ['shot', 'generation'].includes(entity.type);
  const canSend = !cannotConnect.has(entity.type);
  const text = entity.description || entity.data?.prompt || entity.data?.script || entity.data?.goal || entity.data?.fileName || (entity.type === 'generation' ? lt('canvas.ui4') : lt('canvas.ui5'));
  return <article className={`studio-node studio-node-${entity.type}${selected ? ' is-selected' : ''}`} data-entity-id={id}>
    <Handle type="target" position={Position.Left} id="hierarchy-in" isConnectable={false} className="hierarchy-handle" />
    <Handle type="source" position={Position.Right} id="hierarchy-out" isConnectable={false} className="hierarchy-handle" />
    {canReceive && <Handle type="target" position={Position.Left} id="input" aria-label={lt('canvas.input',{title:entity.title})} />}
    {canSend && <Handle type="source" position={Position.Right} id="output" aria-label={lt('canvas.output',{title:entity.title})} />}
    <div className="studio-node-top"><span className="studio-node-kind"><Icon size={14} aria-hidden="true" />{typeLabels[entity.type] || entity.type}</span><span className={`studio-node-status status-${entity.status}`}>{labels[entity.status] || lt('canvas.ui1')}</span></div>
    <h3 title={entity.title}>{entity.title || lt('canvas.ui24')}</h3>
    <p className="studio-node-description">{text}</p>
    <div className="studio-node-meta"><span>{childCount ? lt('canvas.children',{count:childCount}) : entity.data?.seconds ? lt('canvas.seconds',{seconds:entity.data.seconds}) : entity.data?.fileName ? (entity.data?.cloudAssetId||entity.data?.cloudArtifactId?lt('canvas.ui30'):lt('canvas.ui20')) : lt('canvas.version',{version:entity.version||1})}</span>{entity.type === 'generation' && <span>{lt('legacy.route1')}</span>}</div>
    <div className="studio-node-actions nodrag nopan"><button type="button" onClick={event => { event.stopPropagation(); onInspect(id); }} aria-label={lt('canvas.edit',{title:entity.title})}>{lt('legacy.route2')}<ChevronRight size={13} aria-hidden="true" /></button>{canSend && <button type="button" onClick={event => { event.stopPropagation(); onBeginConnect(id); }} aria-label={lt('canvas.connect',{title:entity.title})}><Link2 size={13} aria-hidden="true" />{lt('legacy.route3')}</button>}</div>
  </article>;
});
const nodeTypes = { story: StoryNode };
const canvasAriaLabels = lt => ({
  'node.a11yDescription.default': lt('canvas.ui6'),
  'node.a11yDescription.keyboardDisabled': lt('canvas.ui7'),
  'node.a11yDescription.ariaLiveMessage': ({ x, y }) => lt('canvas.position',{x:Math.round(x),y:Math.round(y)}),
  'edge.a11yDescription.default': lt('canvas.ui8'),
  'controls.ariaLabel': lt('canvas.ui9'),
  'controls.zoomIn.ariaLabel': lt('canvas.ui10'),
  'controls.zoomOut.ariaLabel': lt('canvas.ui11'),
  'controls.fitView.ariaLabel': lt('canvas.ui12'),
  'controls.interactive.ariaLabel': lt('canvas.ui13'),
  'minimap.ariaLabel': lt('canvas.ui14'),
  'handle.ariaLabel': lt('canvas.ui15'),
});

export default function Canvas({ state, onAdd, onInspect, onDelete, onConnect }) {
 const {t:lt}=useI18n();const labels=statusLabels(lt),typeLabels=Object.fromEntries(["chapter", "scene", "shot", "character", "location", "prop", "image", "audio", "video", "note", "generation"].map(key=>[key,lt('canvas.type.'+key)])),roleLabels=Object.fromEntries(["identity", "location", "firstFrame", "lastFrame", "motion", "audio", "reference", "dependency"].map(key=>[key,lt('canvas.role.'+key)]));
  const { project, selectedId, scopeId } = state;
  const entities = project.entities || [];
  const [instance, setInstance] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [showDetails, setShowDetails] = useState(false);
  const [showAssets, setShowAssets] = useState(true);
  const [showHelp, setShowHelp] = useState(false);
  const [query, setQuery] = useState('');
  const [connectionSource, setConnectionSource] = useState(null);
  const [connectionTarget, setConnectionTarget] = useState('');
  const locallySelected = useRef(null);
  const pendingPositionSave = useRef({});
  const saveTimer = useRef(null);
  const initialFocus = useRef(false);
  const surfaceRef = useRef(null);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const entitiesRef = useRef(entities);
  entitiesRef.current = entities;
  const chapters = useMemo(() => entities.filter(entity => entity.type === 'chapter').sort(sortEntities), [entities]);
  const activeScope = chapters.some(entity => entity.id === scopeId) ? scopeId : null;
  const selected = entities.find(entity => entity.id === selectedId);
  const selectedChapter = chapterFor(entities, selectedId);
  const activeChapter = chapters.find(entity => entity.id === activeScope);

  useEffect(() => { setQuery(''); setConnectionSource(null); setConnectionTarget(''); }, [project.id]);
  const initializeFlow = useCallback(flow => { initialFocus.current = false; setInstance(flow); }, []);

  useEffect(() => {
    if (selectedChapter && selectedChapter !== activeScope && selected?.type !== 'chapter') store.setScope(selectedChapter);
  }, [selectedId, selectedChapter]);

  const beginConnect = useCallback(id => {
    setConnectionSource(id); setConnectionTarget('');
    locallySelected.current = id; store.select(id);
  }, []);
  const visibleLinks = useMemo(() => [...(project.links || []), ...castCanvasLinks(project)], [project.entities, project.links]);
  const visibleEntities = useMemo(() => {
    const main = entities.filter(entity => {
      if (!isNarrative(entity)) return false;
      if (activeScope) return chapterFor(entities, entity.id) === activeScope;
      return showDetails || entity.type === 'chapter';
    });
    const visibleIds = new Set(main.map(entity => entity.id));
    const referenced = new Set(visibleLinks.filter(link => visibleIds.has(link.target)).map(link => link.source));
    return [...main, ...entities.filter(entity => !isNarrative(entity) && (showAssets || referenced.has(entity.id) || entity.id === selectedId))];
  }, [entities, visibleLinks, activeScope, showDetails, showAssets, selectedId]);
  const positions = useMemo(() => resolveCanvasPositions(entities, project.layout?.positions), [entities, project.layout?.positions]);
  const childCounts = useMemo(() => entities.reduce((result, entity) => { if (entity.parentId) result[entity.parentId] = (result[entity.parentId] || 0) + 1; return result; }, {}), [entities]);
  const projectedNodes = useMemo(() => visibleEntities.map(entity => ({
    id: entity.id,
    type: 'story',
    position: positions[entity.id],
    data: { entity, childCount: childCounts[entity.id] || 0, onInspect, onBeginConnect: beginConnect },
    selected: entity.id === selectedId,
    deletable: false,
    ariaLabel: lt('canvas.node',{type:typeLabels[entity.type]||entity.type,title:entity.title,status:labels[entity.status]||lt('canvas.ui1')}),
    style: { width: 258 },
  })), [visibleEntities, project.layout?.positions, positions, childCounts, selectedId, onInspect, beginConnect]);

  useEffect(() => {
    setNodes(previous => projectedNodes.map(node => {
      const old = previous.find(item => item.id === node.id);
      return { ...old, ...node, position: old?.dragging ? old.position : node.position };
    }));
  }, [projectedNodes]);

  useEffect(() => {
    if (!instance || !visibleEntities.length) return;
    if (!initialFocus.current) {
      initialFocus.current = true;
      if (selectedId && visibleEntities.some(entity => entity.id === selectedId)) {
        requestAnimationFrame(() => instance.fitView({ nodes: [{ id: selectedId }], padding: 1, maxZoom: 1, duration: 0 }));
      } else if (!Object.keys(project.layout?.positions || {}).length || (project.layout?.viewport?.x === 0 && project.layout?.viewport?.y === 0 && project.layout?.viewport?.zoom === 1)) {
        requestAnimationFrame(() => instance.fitView({ padding: .2, minZoom: .25, maxZoom: 1, duration: 0 }));
      }
      return;
    }
    if (selectedId && selectedId !== locallySelected.current && visibleEntities.some(entity => entity.id === selectedId)) {
      requestAnimationFrame(() => instance.fitView({ nodes: [{ id: selectedId }], padding: 1, maxZoom: 1, duration: 220 }));
    }
    locallySelected.current = null;
  }, [instance, selectedId, activeScope]);

  const fitAll = useCallback(() => instance?.fitView({ padding: .2, minZoom: .2, maxZoom: 1, duration: 220 }), [instance]);
  useEffect(() => {
    if (!instance || !surfaceRef.current || typeof ResizeObserver === 'undefined') return;
    let last = null, timer;
    const observer = new ResizeObserver(entries => {
      const size = entries[0].contentRect;
      if (last && (Math.abs(size.width - last.width) > 20 || Math.abs(size.height - last.height) > 20)) {
        clearTimeout(timer);
        timer = setTimeout(() => {
          const id = selectedRef.current;
          const target = id && instance.getNode(id);
          instance.fitView({ ...(target ? { nodes: [{ id }], padding: 1 } : { padding: .2 }), minZoom: .15, maxZoom: 1, duration: 0 });
        }, 120);
      }
      last = { width: size.width, height: size.height };
    });
    observer.observe(surfaceRef.current);
    return () => { observer.disconnect(); clearTimeout(timer); };
  }, [instance]);
  const moveSelected = (dx,dy) => {const node=instance?.getNode(selectedId);if(!node)return;store.setPositions({[selectedId]:{x:node.position.x+dx,y:node.position.y+dy}});store.notify(lt('canvas.ui21'));};
  const tidyLayout = () => {
    store.setPositions(tidyPositions(visibleEntities));
    store.notify(lt('canvas.ui19'));
    requestAnimationFrame(() => requestAnimationFrame(fitAll));
  };
  useEffect(() => {
    if (!instance || !initialFocus.current) return;
    const timer = setTimeout(() => {
      if (!selectedId || !visibleEntities.some(entity => entity.id === selectedId)) fitAll();
    }, 80);
    return () => clearTimeout(timer);
  }, [activeScope, showDetails, showAssets]);

  const edges = useMemo(() => {
    const ids = new Set(visibleEntities.map(entity => entity.id));
    const hierarchy = visibleEntities.filter(entity => entity.parentId && ids.has(entity.parentId)).map(entity => ({
      id: `hierarchy:${entity.id}`, source: entity.parentId, target: entity.id,
      sourceHandle: 'hierarchy-out', targetHandle: 'hierarchy-in',
      type: 'smoothstep', label: entity.type === 'scene' ? lt('canvas.ui17') : entity.type === 'shot' ? lt('canvas.ui18') : lt('canvas.ui22'),
      style: { stroke: '#a8b1a7', strokeWidth: 1.3, strokeDasharray: '5 5' },
      labelStyle: { fill: '#66746b', fontSize: 10 }, labelBgStyle: { fill: '#f5f7f0', fillOpacity: .98 },
      deletable: false, ariaLabel: lt('canvas.hierarchy',{title:entity.title}), data: { hierarchy: true },
    }));
    const links = visibleLinks.filter(link => ids.has(link.source) && ids.has(link.target)).map(link => ({
      id: link.id, source: link.source, target: link.target, sourceHandle: 'output', targetHandle: 'input',
      type: 'default', label: link.label || roleLabels[link.role] || link.role,
      markerEnd: { type: MarkerType.ArrowClosed, color: '#54735b', width: 16, height: 16 },
      style: { stroke: '#54735b', strokeWidth: 1.8 },
      labelStyle: { fill: '#35563b', fontSize: 10 }, labelBgStyle: { fill: '#f3f6ed', fillOpacity: .98 },
      deletable: false, ariaLabel: lt('canvas.edge',{label:link.label||roleLabels[link.role]||link.role,hint:link.role==='candidate'?lt('canvas.ui31'):link.derived?lt('canvas.ui25'):lt('canvas.ui23')}),
    }));
    return [...hierarchy, ...links];
  }, [visibleEntities, visibleLinks]);

  const flushPositions = useCallback(() => {
    clearTimeout(saveTimer.current);
    if (Object.keys(pendingPositionSave.current).length) {
      const map = pendingPositionSave.current; pendingPositionSave.current = {};
      const alive = new Set(entitiesRef.current.map(entity => entity.id));
      const valid = Object.fromEntries(Object.entries(map).filter(([id]) => alive.has(id)));
      if (Object.keys(valid).length) store.setPositions(valid);
    }
  }, []);
  useEffect(() => () => flushPositions(), [flushPositions]);
  const handleNodeChanges = useCallback(changes => {
    setNodes(current => applyNodeChanges(changes.filter(change => change.type !== 'remove'), current));
    for (const change of changes) {
      if (change.type === 'select' && change.selected) { locallySelected.current = change.id; store.select(change.id); }
      if (change.type === 'position' && finitePosition(change.position)) {
        pendingPositionSave.current[change.id] = change.position;
        clearTimeout(saveTimer.current);
        if (change.dragging !== true) saveTimer.current = setTimeout(flushPositions, 180);
      }
    }
  }, [flushPositions]);
  const hits = query.trim() ? entities.filter(entity => `${entity.title} ${entity.description || ''} ${typeLabels[entity.type] || ''}`.toLowerCase().includes(query.toLowerCase().trim())).slice(0, 8) : [];
  const focusEntity = id => {
    const chapter = chapterFor(entities, id);
    if (chapter) store.setScope(chapter);
    locallySelected.current = null;
    store.select(id); setQuery('');
    if (visibleEntities.some(entity => entity.id === id)) instance?.fitView({ nodes: [{ id }], padding: 1, maxZoom: 1, duration: 220 });
  };
  const targetChoices = entities.filter(entity => ['shot', 'generation'].includes(entity.type) && entity.id !== connectionSource);
  const sourceEntity = entities.find(entity => entity.id === connectionSource);
  const hiddenCount = entities.length - visibleEntities.length;

  return <section className="studio-canvas" aria-label={lt('legacy.route25')}>
    <div className="canvas-toolbar">
      <div className="canvas-scope-control"><Layers3 size={16} aria-hidden="true" /><select aria-label={lt('legacy.route26')} value={activeScope || ''} onChange={event => { locallySelected.current = null; store.select(null); store.setScope(event.target.value || null); }}><option value="">{lt('legacy.route4')}</option>{chapters.map((entity, index) => <option key={entity.id} value={entity.id}>{String(index + 1).padStart(2, '0')} · {entity.title}</option>)}</select></div>
      <div className="canvas-search"><Search size={15} aria-hidden="true" /><input value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') setQuery(''); if (event.key === 'Enter' && hits[0]) { event.preventDefault(); focusEntity(hits[0].id); } }} placeholder={lt('legacy.route27')} aria-label={lt('legacy.route28')} aria-controls={query.trim() ? 'canvas-search-results' : undefined} />{query && <button type="button" onClick={() => setQuery('')} aria-label={lt('legacy.route29')}><X size={14} /></button>}
        {query.trim() && <div className="canvas-search-results" id="canvas-search-results" aria-label={lt('legacy.route30')}>{hits.length ? hits.map(entity => <button type="button" key={entity.id} onClick={() => focusEntity(entity.id)}><span>{typeLabels[entity.type]}</span><strong>{entity.title}</strong><LocateFixed size={14} /></button>) : <p>{lt('legacy.route5')}</p>}</div>}
      </div>
      <div className="canvas-toolbar-actions"><button className="canvas-secondary" type="button" onClick={tidyLayout} title={lt('legacy.route31')}><Layers3 size={15} /><span>{lt('legacy.route6')}</span></button><button className="canvas-secondary" type="button" onClick={fitAll} title={lt('legacy.route32')}><LocateFixed size={15} /><span>{lt('legacy.route7')}</span></button><button className="canvas-add" type="button" onClick={() => onAdd()}><Plus size={16} />{lt('legacy.route8')}</button></div>
    </div>
    <div className="canvas-filterbar"><span>{activeChapter ? activeChapter.title : lt('canvas.ui16')}<b>{lt('canvas.nodes',{count:visibleEntities.length})}</b></span><div>{!activeScope && <label><input type="checkbox" checked={showDetails} onChange={event => setShowDetails(event.target.checked)} />{lt('legacy.route9')}</label>}<label><input type="checkbox" checked={showAssets} onChange={event => setShowAssets(event.target.checked)} />{lt('legacy.route10')}</label><button type="button" onClick={() => setShowHelp(value => !value)} aria-expanded={showHelp} aria-label={lt('legacy.route33')}><HelpCircle size={16} /></button></div></div>
    <div className="canvas-surface" ref={surfaceRef}>
      <ReactFlow key={project.id} nodes={nodes} edges={edges} nodeTypes={nodeTypes} onInit={initializeFlow} onNodesChange={handleNodeChanges} onNodeDragStop={flushPositions}
        onNodeClick={(_, node) => { locallySelected.current = node.id; store.select(node.id); }}
        onNodeDoubleClick={(_, node) => onInspect(node.id)}
        onPaneClick={() => { store.select(null); setQuery(''); }}
        onEdgeDoubleClick={(_, edge) => onInspect(edge.target)}
        onConnect={connection => { if (connection.source && connection.target) onConnect(connection.source, connection.target); }}
        onMoveEnd={(_, viewport) => store.setViewport(viewport)}
        defaultViewport={project.layout?.viewport || { x: 360, y: 80, zoom: .8 }}
        deleteKeyCode={null} minZoom={.15} maxZoom={1.6} nodeDragThreshold={4}
        nodesFocusable edgesFocusable disableKeyboardA11y={false} ariaLabelConfig={canvasAriaLabels(lt)}
        panOnScroll selectionOnDrag panOnDrag={[1, 2]} zoomOnScroll={false} zoomOnPinch zoomActivationKeyCode="Control" panActivationKeyCode="Space"
        connectionLineStyle={{ stroke: '#54735b', strokeWidth: 2 }} attributionPosition="bottom-right">
        <Background color="#cdd4c8" gap={22} size={1} />
        <Controls position="bottom-left" showInteractive={false} fitViewOptions={{ padding: .2, maxZoom: 1, minZoom: .2 }} />
        <MiniMap position="bottom-right" nodeColor={node => colors[node.data?.entity?.type] || '#b0bbae'} maskColor="rgba(239,242,233,.72)" pannable zoomable />
      </ReactFlow>
      {!visibleEntities.length && <div className="canvas-empty"><div><Layers3 size={30} /></div><h2>{lt('legacy.route11')}</h2><p>{lt('legacy.route12')}</p><button type="button" className="canvas-add" onClick={() => onAdd()}><Plus size={16} />{lt('legacy.route13')}</button></div>}
      {showHelp && <aside className="canvas-help"><button type="button" onClick={() => setShowHelp(false)} aria-label={lt('legacy.route34')}><X size={15} /></button><h3>{lt('legacy.route14')}</h3><p>{lt('legacy.route15')}<br />{lt('legacy.route16')}</p><p>{lt('legacy.explain32')}</p><p>{lt('legacy.explain31')}</p></aside>}
      {selected && <div className="canvas-selection-tools"><span>{typeLabels[selected.type]}<strong>{selected.title}</strong></span><div className="canvas-move-buttons" role="group" aria-label={lt('legacy.route35')}>{[[ArrowLeft,-40,0,lt('canvas.ui29')],[ArrowRight,40,0,lt('canvas.ui28')],[ArrowUp,0,-40,lt('canvas.ui26')],[ArrowDown,0,40,lt('canvas.ui27')]].map(([I,dx,dy,label])=><button key={label} type="button" aria-label={label} title={label} onClick={()=>moveSelected(dx,dy)}><I size={14}/></button>)}</div><button type="button" onClick={() => onInspect(selected.id)}><SlidersHorizontal size={15} />{lt('legacy.route2')}</button>{!cannotConnect.has(selected.type) && <button type="button" onClick={() => beginConnect(selected.id)}><Link2 size={15} />{lt('legacy.route3')}</button>}<button type="button" onClick={() => onDelete(selected.id)} aria-label={lt('canvas.delete',{title:selected.title})}><Trash2 size={15} /></button></div>}
      {sourceEntity && <div className="canvas-connect-panel"><div><h3>{lt('legacy.route17')}</h3><button type="button" onClick={() => setConnectionSource(null)} aria-label={lt('legacy.route36')}><X size={16} /></button></div><p>{lt('canvas.source',{title:sourceEntity.title})}</p><label>{lt('legacy.route18')}<select aria-label={lt('legacy.route37')} value={connectionTarget} onChange={event => setConnectionTarget(event.target.value)}><option value="">{lt('legacy.route19')}</option>{targetChoices.map(entity => <option key={entity.id} value={entity.id}>{typeLabels[entity.type]} · {entity.title}</option>)}</select></label>{!targetChoices.length && <p className="canvas-connect-hint">{lt('legacy.route20')}</p>}<button type="button" className="canvas-add" disabled={!connectionTarget} onClick={() => { onConnect(connectionSource, connectionTarget); setConnectionSource(null); }}>{lt('legacy.route21')}<ChevronRight size={14} /></button></div>}
    </div>
    <div className="canvas-statusbar"><span><i className="canvas-line hierarchy" />{lt('legacy.route22')}<i className="canvas-line reference" />{lt('legacy.route23')}</span><span>{hiddenCount > 0 ? lt('canvas.hidden',{count:hiddenCount}) : lt('canvas.allVisible')}<b>{lt('legacy.route24')}</b></span></div>
  </section>;
}

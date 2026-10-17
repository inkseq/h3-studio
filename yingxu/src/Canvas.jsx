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
const labels = { draft: '待完善', review: '待复核', ready: '已确认' };
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
  const { entity, childCount, onInspect, onBeginConnect } = data;
  const Icon = icons[entity.type] || StickyNote;
  const canReceive = ['shot', 'generation'].includes(entity.type);
  const canSend = !cannotConnect.has(entity.type);
  const text = entity.description || entity.data?.prompt || entity.data?.script || entity.data?.goal || entity.data?.fileName || (entity.type === 'generation' ? '关联参考，配置输入，确认后再执行。' : '打开详情，继续完善这个想法。');
  return <article className={`studio-node studio-node-${entity.type}${selected ? ' is-selected' : ''}`} data-entity-id={id}>
    <Handle type="target" position={Position.Left} id="hierarchy-in" isConnectable={false} className="hierarchy-handle" />
    <Handle type="source" position={Position.Right} id="hierarchy-out" isConnectable={false} className="hierarchy-handle" />
    {canReceive && <Handle type="target" position={Position.Left} id="input" aria-label={`${entity.title} 的参考输入`} />}
    {canSend && <Handle type="source" position={Position.Right} id="output" aria-label={`${entity.title} 的输出`} />}
    <div className="studio-node-top"><span className="studio-node-kind"><Icon size={14} aria-hidden="true" />{typeLabels[entity.type] || entity.type}</span><span className={`studio-node-status status-${entity.status}`}>{labels[entity.status] || '待完善'}</span></div>
    <h3 title={entity.title}>{entity.title || '未命名'}</h3>
    <p className="studio-node-description">{text}</p>
    <div className="studio-node-meta"><span>{childCount ? `${childCount} 个下级内容` : entity.data?.seconds ? `${entity.data.seconds} 秒` : entity.data?.fileName ? (entity.data?.cloudAssetId||entity.data?.cloudArtifactId?'私有云素材':'已添加本机素材') : `版本 ${entity.version || 1}`}</span>{entity.type === 'generation' && <span>尚未执行</span>}</div>
    <div className="studio-node-actions nodrag nopan"><button type="button" onClick={event => { event.stopPropagation(); onInspect(id); }} aria-label={`编辑 ${entity.title}`}>编辑<ChevronRight size={13} aria-hidden="true" /></button>{canSend && <button type="button" onClick={event => { event.stopPropagation(); onBeginConnect(id); }} aria-label={`连接 ${entity.title} 到镜头或生成步骤`}><Link2 size={13} aria-hidden="true" />连接</button>}</div>
  </article>;
});
const nodeTypes = { story: StoryNode };
const ariaLabels = {
  'node.a11yDescription.default': '按回车或空格选中节点，方向键移动节点，Escape 取消选择。使用卡片上的编辑和连接按钮操作。',
  'node.a11yDescription.keyboardDisabled': '按回车或空格选中节点。使用卡片上的编辑和连接按钮操作。',
  'node.a11yDescription.ariaLiveMessage': ({ x, y }) => `节点已移动到横坐标 ${Math.round(x)}，纵坐标 ${Math.round(y)}`,
  'edge.a11yDescription.default': '这是内容之间的关联。打开目标节点的详情管理关联。',
  'controls.ariaLabel': '画布视图控制',
  'controls.zoomIn.ariaLabel': '放大画布',
  'controls.zoomOut.ariaLabel': '缩小画布',
  'controls.fitView.ariaLabel': '显示全部可见节点',
  'controls.interactive.ariaLabel': '切换画布锁定',
  'minimap.ariaLabel': '画布小地图',
  'handle.ariaLabel': '连接端口',
};

export default function Canvas({ state, onAdd, onInspect, onDelete, onConnect }) {
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
    ariaLabel: `${typeLabels[entity.type] || entity.type}：${entity.title}，${labels[entity.status] || '待完善'}`,
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
  const moveSelected = (dx,dy) => {const node=instance?.getNode(selectedId);if(!node)return;store.setPositions({[selectedId]:{x:node.position.x+dx,y:node.position.y+dy}});store.notify('已移动节点位置，剧情顺序保持不变。');};
  const tidyLayout = () => {
    store.setPositions(tidyPositions(visibleEntities));
    store.notify('已整理当前可见节点。剧情顺序未改变，可以撤销。');
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
      type: 'smoothstep', label: entity.type === 'scene' ? '包含场戏' : entity.type === 'shot' ? '包含镜头' : '所属内容',
      style: { stroke: '#a8b1a7', strokeWidth: 1.3, strokeDasharray: '5 5' },
      labelStyle: { fill: '#66746b', fontSize: 10 }, labelBgStyle: { fill: '#f5f7f0', fillOpacity: .98 },
      deletable: false, ariaLabel: `剧情层级：${entity.title}`, data: { hierarchy: true },
    }));
    const links = visibleLinks.filter(link => ids.has(link.source) && ids.has(link.target)).map(link => ({
      id: link.id, source: link.source, target: link.target, sourceHandle: 'output', targetHandle: 'input',
      type: 'default', label: link.label || roleLabels[link.role] || link.role,
      markerEnd: { type: MarkerType.ArrowClosed, color: '#54735b', width: 16, height: 16 },
      style: { stroke: '#54735b', strokeWidth: 1.8 },
      labelStyle: { fill: '#35563b', fontSize: 10 }, labelBgStyle: { fill: '#f3f6ed', fillOpacity: .98 },
      deletable: false, ariaLabel: `${link.label || roleLabels[link.role] || link.role}关联；${link.role==='candidate'?'请在候选审核管理':link.derived?'由人物造型计算，请在人物与场景管理':'打开目标内容管理'}`,
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

  return <section className="studio-canvas" aria-label="无限画布工作区">
    <div className="canvas-toolbar">
      <div className="canvas-scope-control"><Layers3 size={16} aria-hidden="true" /><select aria-label="画布范围" value={activeScope || ''} onChange={event => { locallySelected.current = null; store.select(null); store.setScope(event.target.value || null); }}><option value="">全剧概览</option>{chapters.map((entity, index) => <option key={entity.id} value={entity.id}>{String(index + 1).padStart(2, '0')} · {entity.title}</option>)}</select></div>
      <div className="canvas-search"><Search size={15} aria-hidden="true" /><input value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') setQuery(''); if (event.key === 'Enter' && hits[0]) { event.preventDefault(); focusEntity(hits[0].id); } }} placeholder="查找人物、镜头、素材…" aria-label="搜索全部项目节点" aria-controls={query.trim() ? 'canvas-search-results' : undefined} />{query && <button type="button" onClick={() => setQuery('')} aria-label="清空节点搜索"><X size={14} /></button>}
        {query.trim() && <div className="canvas-search-results" id="canvas-search-results" aria-label="节点搜索结果">{hits.length ? hits.map(entity => <button type="button" key={entity.id} onClick={() => focusEntity(entity.id)}><span>{typeLabels[entity.type]}</span><strong>{entity.title}</strong><LocateFixed size={14} /></button>) : <p>没有找到，试试人物名或镜头标题。</p>}</div>}
      </div>
      <div className="canvas-toolbar-actions"><button className="canvas-secondary" type="button" onClick={tidyLayout} title="整理当前可见节点的位置，可撤销"><Layers3 size={15} /><span>整理排布</span></button><button className="canvas-secondary" type="button" onClick={fitAll} title="显示全部可见节点"><LocateFixed size={15} /><span>适应画布</span></button><button className="canvas-add" type="button" onClick={() => onAdd()}><Plus size={16} />添加节点</button></div>
    </div>
    <div className="canvas-filterbar"><span>{activeChapter ? activeChapter.title : '全剧概览'}<b>{visibleEntities.length} 个节点</b></span><div>{!activeScope && <label><input type="checkbox" checked={showDetails} onChange={event => setShowDetails(event.target.checked)} />展开场戏和镜头</label>}<label><input type="checkbox" checked={showAssets} onChange={event => setShowAssets(event.target.checked)} />全部共享素材</label><button type="button" onClick={() => setShowHelp(value => !value)} aria-expanded={showHelp} aria-label="画布使用帮助"><HelpCircle size={16} /></button></div></div>
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
        nodesFocusable edgesFocusable disableKeyboardA11y={false} ariaLabelConfig={ariaLabels}
        panOnScroll selectionOnDrag panOnDrag={[1, 2]} zoomOnScroll={false} zoomOnPinch zoomActivationKeyCode="Control" panActivationKeyCode="Space"
        connectionLineStyle={{ stroke: '#54735b', strokeWidth: 2 }} attributionPosition="bottom-right">
        <Background color="#cdd4c8" gap={22} size={1} />
        <Controls position="bottom-left" showInteractive={false} fitViewOptions={{ padding: .2, maxZoom: 1, minZoom: .2 }} />
        <MiniMap position="bottom-right" nodeColor={node => colors[node.data?.entity?.type] || '#b0bbae'} maskColor="rgba(239,242,233,.72)" pannable zoomable />
      </ReactFlow>
      {!visibleEntities.length && <div className="canvas-empty"><div><Layers3 size={30} /></div><h2>把第一个想法放上画布</h2><p>从章节、人物或一个参考素材开始，再连接成你的故事。</p><button type="button" className="canvas-add" onClick={() => onAdd()}><Plus size={16} />添加第一个节点</button></div>}
      {showHelp && <aside className="canvas-help"><button type="button" onClick={() => setShowHelp(false)} aria-label="关闭画布帮助"><X size={15} /></button><h3>按你习惯的方式工作</h3><p>滚轮平移 · Ctrl + 滚轮缩放<br />空格 + 拖动平移 · 拖动空白框选</p><p>拖动卡片只改变排布，不改变剧情顺序。虚线表示章节层级，实线表示引用。</p><p>卡片上的「编辑」「连接」也可用键盘操作。连线完成后确认用途，不会自动执行任务。</p></aside>}
      {selected && <div className="canvas-selection-tools"><span>{typeLabels[selected.type]}<strong>{selected.title}</strong></span><div className="canvas-move-buttons" role="group" aria-label="用点击移动节点">{[[ArrowLeft,-40,0,'画布左移'],[ArrowRight,40,0,'画布右移'],[ArrowUp,0,-40,'画布上移'],[ArrowDown,0,40,'画布下移']].map(([I,dx,dy,label])=><button key={label} type="button" aria-label={label} title={label} onClick={()=>moveSelected(dx,dy)}><I size={14}/></button>)}</div><button type="button" onClick={() => onInspect(selected.id)}><SlidersHorizontal size={15} />编辑</button>{!cannotConnect.has(selected.type) && <button type="button" onClick={() => beginConnect(selected.id)}><Link2 size={15} />连接</button>}<button type="button" onClick={() => onDelete(selected.id)} aria-label={`删除 ${selected.title}`}><Trash2 size={15} /></button></div>}
      {sourceEntity && <div className="canvas-connect-panel"><div><h3>连接到哪个镜头？</h3><button type="button" onClick={() => setConnectionSource(null)} aria-label="取消连接"><X size={16} /></button></div><p>将「{sourceEntity.title}」作为输入，下一步确认用途。</p><label>目标镜头或生成步骤<select aria-label="选择连接目标" value={connectionTarget} onChange={event => setConnectionTarget(event.target.value)}><option value="">请选择目标</option>{targetChoices.map(entity => <option key={entity.id} value={entity.id}>{typeLabels[entity.type]} · {entity.title}</option>)}</select></label>{!targetChoices.length && <p className="canvas-connect-hint">先添加一个镜头或生成步骤，再连接参考。</p>}<button type="button" className="canvas-add" disabled={!connectionTarget} onClick={() => { onConnect(connectionSource, connectionTarget); setConnectionSource(null); }}>下一步：选择用途<ChevronRight size={14} /></button></div>}
    </div>
    <div className="canvas-statusbar"><span><i className="canvas-line hierarchy" />剧情层级<i className="canvas-line reference" />素材引用</span><span>{hiddenCount > 0 ? `当前隐藏 ${hiddenCount} 个节点 · 搜索可定位全部内容` : '全部内容已显示'}<b>拖动不改变剧情顺序</b></span></div>
  </section>;
}

import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton, InlineConfirm, RelativeTime } from '../components/primitives';
import { errorText, useLoader } from '../lib/useLoader';
import { ENTITY_TYPES, type Entity, type EntityDetail, type EntityType, type Graph } from '../protocol/types';
import { useStore } from '../store/store';
import { useMediaQuery, DESKTOP_QUERY } from '../shell/useMediaQuery';
import { GraphView } from './GraphView';

function useDebounced(value: string, ms: number): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

/** Search → entity list → detail (aliases, edges, mentions, small graph), edit / merge / delete. */
export function KnowledgeScreen() {
  const version = useStore((s) => s.featureVersion.kg);
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 250);
  const [selected, setSelected] = useState<string | null>(null);
  const load = useCallback(() => api.kg.entities(dq, 50), [dq]);
  const { data, error, loading } = useLoader(load, version);
  const desktop = useMediaQuery(DESKTOP_QUERY);
  const showList = desktop || selected === null;
  const showDetail = selected !== null;
  // Ctrl/⌘+K focuses the search of whatever screen is showing; this one has its own box, so it
  // answers the same event as the sidebar's and the list screens' filters.
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const focus = () => {
      searchRef.current?.focus();
      searchRef.current?.select();
    };
    window.addEventListener('jarvis:focus-search', focus);
    return () => window.removeEventListener('jarvis:focus-search', focus);
  }, []);

  return (
    <div className="screen">
      <div className="screen-inner wide kg-layout">
        {showList && (
          <section className="kg-list">
            <h1>Knowledge</h1>
            <label className="kg-search">
              <Icon name="search" size={16} className="muted" />
              <input ref={searchRef} className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search people, projects, places…" aria-label="Search entities" />
            </label>
            {error && <div className="field-error">{error}</div>}
            {!loading && data?.length === 0 && (
              <div className="empty small">
                {dq ? `Nothing matches “${dq}”.` : 'Nothing learned yet. Jarvis adds entities after each conversation.'}
              </div>
            )}
            <div className="kg-entities">
              {(data ?? []).map((e) => (
                <button key={e.id} type="button" className={`kg-entity${selected === e.id ? ' active' : ''}`} onClick={() => setSelected(e.id)}>
                  <span className="row">
                    <span className="kg-name truncate">{e.name}</span>
                    <span className={`chip kg-type-${e.type}`}>{e.type}</span>
                  </span>
                  {e.summary && <span className="small muted truncate">{e.summary}</span>}
                  <span className="xs muted">
                    {e.mention_count} mention{e.mention_count === 1 ? '' : 's'}
                  </span>
                </button>
              ))}
            </div>
          </section>
        )}
        {showDetail && selected && <EntityPanel id={selected} version={version} onSelect={setSelected} onBack={() => setSelected(null)} desktop={desktop} />}
        {!showDetail && desktop && (
          <section className="kg-detail empty">
            <strong>Pick an entity</strong>
            <span>Its relations, mentions and a small graph appear here.</span>
          </section>
        )}
      </div>
    </div>
  );
}

interface PanelProps {
  id: string;
  version: number;
  onSelect: (id: string | null) => void;
  onBack: () => void;
  desktop: boolean;
}

function EntityPanel({ id, version, onSelect, onBack, desktop }: PanelProps) {
  const notify = useStore((s) => s.notify);
  const openConversation = useStore((s) => s.openConversation);
  const loadDetail = useCallback(() => api.kg.entity(id), [id]);
  const loadGraph = useCallback(() => api.kg.graph(id, 1, 40), [id]);
  const detail = useLoader(loadDetail, version);
  const graph = useLoader(loadGraph, version);
  const [editing, setEditing] = useState(false);
  const [merging, setMerging] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const e = detail.data;
  const fail = (what: string) => (err: unknown) => notify(`${what} failed: ${errorText(err)}`, 'error');

  if (detail.error) {
    return (
      <section className="kg-detail">
        <div className="field-error">{detail.error}</div>
      </section>
    );
  }
  if (!e) return <section className="kg-detail empty small">Loading…</section>;

  return (
    <section className="kg-detail" aria-label={e.name}>
      <header className="row">
        {!desktop && <IconButton icon="chevronLeft" label="Back to the list" onClick={onBack} />}
        <h2 className="grow truncate" style={{ fontSize: 'var(--fs-xl)' }}>
          {e.name}
        </h2>
        <span className={`chip kg-type-${e.type}`}>{e.type}</span>
        <IconButton icon="edit" label="Edit" active={editing} onClick={() => setEditing(!editing)} />
        <IconButton icon="merge" label="Merge into another entity" active={merging} onClick={() => setMerging(!merging)} />
        <IconButton icon="trash" label="Delete entity" onClick={() => setConfirmDelete(true)} />
      </header>
      {confirmDelete && (
        <InlineConfirm
          text={`Delete "${e.name}" with its aliases and relations?`}
          confirmLabel="Delete"
          danger
          onConfirm={() =>
            void api.kg
              .remove(e.id)
              .then(() => onSelect(null))
              .catch(fail('Delete'))
          }
          onCancel={() => setConfirmDelete(false)}
        />
      )}
      {editing ? (
        <EditForm entity={e} onDone={() => setEditing(false)} />
      ) : (
        <p className="kg-summary">{e.summary || <span className="muted">No summary yet.</span>}</p>
      )}
      {merging && <MergePicker entity={e} onMerged={(into) => (setMerging(false), onSelect(into))} onCancel={() => setMerging(false)} />}
      {e.aliases.length > 0 && (
        <div className="row" style={{ flexWrap: 'wrap' }}>
          <span className="field-label">Aliases</span>
          {e.aliases.map((a) => (
            <span key={a} className="chip chip-outline">
              {a}
            </span>
          ))}
        </div>
      )}
      {graph.data && graph.data.nodes.length > 1 && <GraphView graph={graph.data} centerId={e.id} onSelect={onSelect} />}
      <div className="kg-section">
        <div className="field-label">
          Relations · {e.edges.length}
        </div>
        {e.edges.length === 0 && <div className="small muted">None recorded.</div>}
        {e.edges.map((edge, i) => (
          <div key={`${edge.src}-${edge.dst}-${i}`} className="kg-edge-row">
            <span className="small muted">{edge.src === e.id ? edge.relation : `← ${edge.relation}`}</span>
            <button type="button" className="kg-link" onClick={() => onSelect(edge.other.id)}>
              {edge.other.name}
            </button>
            <span className={`chip kg-type-${edge.other.type}`}>{edge.other.type}</span>
            {edge.evidence && (
              <span className="xs muted truncate" title={edge.evidence}>
                {edge.evidence}
              </span>
            )}
          </div>
        ))}
      </div>
      <div className="kg-section">
        <div className="field-label">
          Recent mentions · {e.mention_count}
        </div>
        {e.mentions.length === 0 && <div className="small muted">No mentions stored.</div>}
        {e.mentions.map((m, i) => (
          <div key={`${m.message_id ?? i}`} className="kg-mention">
            <div className="small">{m.snippet ?? <span className="muted">(no snippet)</span>}</div>
            <div className="row xs muted">
              <RelativeTime ts={m.at} />
              {m.conversation_id && (
                <button type="button" className="kg-link xs" onClick={() => void openConversation(m.conversation_id)}>
                  <Icon name="link" size={11} /> open conversation
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function EditForm({ entity, onDone }: { entity: EntityDetail; onDone: () => void }) {
  const notify = useStore((s) => s.notify);
  const [name, setName] = useState(entity.name);
  const [type, setType] = useState<EntityType>(entity.type);
  const [summary, setSummary] = useState(entity.summary);
  const [err, setErr] = useState<string | null>(null);
  const save = () => {
    api.kg
      .patch(entity.id, { name: name.trim(), type, summary })
      .then(() => {
        notify('Saved.');
        onDone();
      })
      .catch((e: unknown) => setErr(errorText(e)));
  };
  return (
    <div className="card role-card">
      <div className="form-grid">
        <div className="field">
          <label>Name</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label>Type</label>
          <select className="select" value={type} onChange={(e) => setType(e.target.value as EntityType)}>
            {ENTITY_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </div>
        <div className="field span-2">
          <label>Summary</label>
          <textarea className="textarea" rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} />
        </div>
      </div>
      {err && <div className="field-error">{err}</div>}
      <div className="row">
        <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={!name.trim()}>
          Save
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onDone}>
          Cancel
        </button>
      </div>
    </div>
  );
}

function MergePicker({ entity, onMerged, onCancel }: { entity: Entity; onMerged: (into: string) => void; onCancel: () => void }) {
  const notify = useStore((s) => s.notify);
  const [q, setQ] = useState('');
  const dq = useDebounced(q.trim(), 200);
  const load = useCallback(() => api.kg.entities(dq, 10), [dq]);
  const { data } = useLoader(load, dq);
  const candidates = (data ?? []).filter((c) => c.id !== entity.id);
  const merge = (into: Entity) => {
    api.kg
      .merge(entity.id, into.id)
      .then(() => {
        notify(`Merged “${entity.name}” into “${into.name}”.`);
        onMerged(into.id);
      })
      .catch((e: unknown) => notify(`Merge failed: ${errorText(e)}`, 'error'));
  };
  return (
    <div className="card role-card">
      <div className="row">
        <Icon name="merge" size={16} className="muted" />
        <span className="small">
          Merge <b>{entity.name}</b> into… (its aliases and relations move to the target)
        </span>
      </div>
      <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search the target entity" autoFocus aria-label="Merge target" />
      <div className="kg-entities compact">
        {candidates.map((c) => (
          <button key={c.id} type="button" className="kg-entity" onClick={() => merge(c)}>
            <span className="row">
              <span className="kg-name truncate">{c.name}</span>
              <span className={`chip kg-type-${c.type}`}>{c.type}</span>
            </span>
          </button>
        ))}
        {dq && candidates.length === 0 && <div className="small muted">No other entity matches.</div>}
      </div>
      <button type="button" className="btn btn-ghost btn-sm" style={{ alignSelf: 'flex-start' }} onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}

export type { Graph };

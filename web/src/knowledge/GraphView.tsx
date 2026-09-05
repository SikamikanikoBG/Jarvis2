import type { Graph } from '../protocol/types';

interface Props {
  graph: Graph;
  centerId: string;
  onSelect: (id: string) => void;
}

const W = 520;
const H = 340;
const CX = W / 2;
const CY = H / 2;

/** Centre node with its neighbours on a circle; edges labelled with the relation. No library. */
export function GraphView({ graph, centerId, onSelect }: Props) {
  const center = graph.nodes.find((n) => n.id === centerId);
  const others = graph.nodes.filter((n) => n.id !== centerId).slice(0, 14);
  const r = others.length <= 6 ? 110 : 130;
  const pos = new Map<string, { x: number; y: number }>();
  if (center) pos.set(center.id, { x: CX, y: CY });
  others.forEach((n, i) => {
    const a = (i / others.length) * Math.PI * 2 - Math.PI / 2;
    pos.set(n.id, { x: CX + Math.cos(a) * r, y: CY + Math.sin(a) * r });
  });
  const edges = graph.edges.filter((e) => pos.has(e.src) && pos.has(e.dst));
  const label = (s: string, max = 16) => (s.length > max ? `${s.slice(0, max - 1)}…` : s);

  return (
    <svg className="kg-graph" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Relations of ${center?.name ?? 'entity'}`}>
      {edges.map((e, i) => {
        const a = pos.get(e.src);
        const b = pos.get(e.dst);
        if (!a || !b) return null;
        const mx = (a.x + b.x) / 2;
        const my = (a.y + b.y) / 2;
        return (
          <g key={`${e.src}-${e.dst}-${i}`} className="kg-g-edge">
            <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} strokeWidth={Math.min(3, 0.8 + e.weight)} />
            <text x={mx} y={my - 4} textAnchor="middle">
              {label(e.relation, 22)}
            </text>
          </g>
        );
      })}
      {graph.nodes
        .filter((n) => pos.has(n.id))
        .map((n) => {
          const p = pos.get(n.id);
          if (!p) return null;
          const isCenter = n.id === centerId;
          return (
            <g key={n.id} className={`kg-g-node kg-type-${n.type}${isCenter ? ' center' : ''}`} onClick={() => !isCenter && onSelect(n.id)} role={isCenter ? undefined : 'button'} tabIndex={isCenter ? -1 : 0} onKeyDown={(e) => e.key === 'Enter' && !isCenter && onSelect(n.id)}>
              <circle cx={p.x} cy={p.y} r={isCenter ? 22 : 15} />
              <text x={p.x} y={p.y + (isCenter ? 38 : 30)} textAnchor="middle">
                {label(n.name, isCenter ? 26 : 16)}
              </text>
            </g>
          );
        })}
    </svg>
  );
}

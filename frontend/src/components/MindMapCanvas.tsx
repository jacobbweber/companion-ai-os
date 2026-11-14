import React, { useEffect, useRef, useState } from 'react';
import type { MindMapGraph } from '../types';

interface MindMapCanvasProps {
  graph: MindMapGraph | null;
  selectedId?: string;
  onSelectNode: (notePath: string) => void;
}

interface SimNode {
  id: string;
  label: string;
  domain: string;
  folder: string;
  author: string;
  template: string;
  ontology: string[];
  orphan: boolean;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

const DOMAIN_PALETTE = [
  '#d99b43',
  '#4cc9f0',
  '#67b7a4',
  '#e07a5f',
  '#9b87f5',
  '#8ac926',
  '#f4a261',
];

function colorForDomain(domain: string, folder: string): string {
  if (folder === '01_inbox') return '#e09f3e';
  if (!domain) return '#8ea6c2';
  let hash = 0;
  for (let i = 0; i < domain.length; i++) {
    hash = (hash * 31 + domain.charCodeAt(i)) >>> 0;
  }
  return DOMAIN_PALETTE[hash % DOMAIN_PALETTE.length];
}

export const MindMapCanvas: React.FC<MindMapCanvasProps> = ({
  graph,
  selectedId,
  onSelectNode,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const viewRef = useRef({ ox: 0, oy: 0, scale: 1 });
  const dragRef = useRef<{ node: SimNode | null; panning: boolean; lx: number; ly: number }>({
    node: null,
    panning: false,
    lx: 0,
    ly: 0,
  });
  const [hovered, setHovered] = useState<SimNode | null>(null);
  const [showOntology, setShowOntology] = useState(true);

  useEffect(() => {
    if (!graph) return;
    const orphanSet = new Set(graph.orphan_notes || []);
    const count = Math.max(1, graph.nodes.length);
    const radius = Math.min(230, 70 + count * 15);
    nodesRef.current = graph.nodes.map((n, idx) => {
      const angle = (2 * Math.PI * idx) / count;
      return {
        id: n.id,
        label: n.label || n.id.split('/').pop() || n.id,
        domain: n.domain || n.folder,
        folder: n.folder,
        author: n.author,
        template: n.template || '',
        ontology: n.ontology || [],
        orphan: orphanSet.has(n.id),
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
        vx: 0,
        vy: 0,
      };
    });
  }, [graph]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !graph) return;
    let rafId = 0;

    const resize = () => {
      const parent = canvas.parentElement;
      if (!parent) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = parent.clientWidth * dpr;
      canvas.height = parent.clientHeight * dpr;
      canvas.style.width = `${parent.clientWidth}px`;
      canvas.style.height = `${parent.clientHeight}px`;
    };
    resize();
    window.addEventListener('resize', resize);

    let ticks = 0;
    const ontEdges = graph.ontology_edges || [];

    const step = () => {
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.width / dpr;
      const h = canvas.height / dpr;
      const nodes = nodesRef.current;
      const byId = new Map(nodes.map((n) => [n.id, n]));

      if (ticks < 240 || dragRef.current.node) {
        ticks++;
        for (let i = 0; i < nodes.length; i++) {
          for (let j = i + 1; j < nodes.length; j++) {
            const a = nodes[i];
            const b = nodes[j];
            const dx = b.x - a.x || 0.1;
            const dy = b.y - a.y || 0.1;
            const distSq = Math.max(160, dx * dx + dy * dy);
            const force = 2600 / distSq;
            const dist = Math.sqrt(distSq);
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;
            a.vx -= fx;
            a.vy -= fy;
            b.vx += fx;
            b.vy += fy;
          }
        }
        for (const e of graph.edges) {
          const s = byId.get(e.source);
          const t = byId.get(e.target);
          if (!s || !t) continue;
          const dx = t.x - s.x;
          const dy = t.y - s.y;
          const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
          const delta = (dist - 135) * 0.025;
          const fx = (dx / dist) * delta;
          const fy = (dy / dist) * delta;
          s.vx += fx;
          s.vy += fy;
          t.vx -= fx;
          t.vy -= fy;
        }
        if (showOntology) {
          for (const oe of ontEdges) {
            const s = byId.get(oe.source);
            const t = byId.get(oe.target);
            if (!s || !t) continue;
            const dx = t.x - s.x;
            const dy = t.y - s.y;
            const dist = Math.max(1, Math.sqrt(dx * dx + dy * dy));
            const delta = (dist - 185) * 0.011;
            const fx = (dx / dist) * delta;
            const fy = (dy / dist) * delta;
            s.vx += fx;
            s.vy += fy;
            t.vx -= fx;
            t.vy -= fy;
          }
        }
        for (const n of nodes) {
          if (dragRef.current.node === n) continue;
          n.vx -= n.x * 0.004;
          n.vy -= n.y * 0.004;
          n.vx *= 0.82;
          n.vy *= 0.82;
          n.x += n.vx;
          n.y += n.vy;
        }
      }

      const styles = getComputedStyle(document.documentElement);
      const gridColor = styles.getPropertyValue('--border-hairline').trim() || '#232833';
      const textColor = styles.getPropertyValue('--text-primary').trim() || '#e7eaf0';
      const mutedColor = styles.getPropertyValue('--text-muted').trim() || '#667085';
      const accentColor = styles.getPropertyValue('--accent-primary').trim() || '#d99b43';

      ctx.save();
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);

      // Subtle architectural coordinate grid
      ctx.strokeStyle = gridColor;
      ctx.lineWidth = 0.5;
      const stepPx = 44;
      for (let gx = (viewRef.current.ox % stepPx + stepPx) % stepPx; gx < w; gx += stepPx) {
        ctx.beginPath();
        ctx.moveTo(gx, 0);
        ctx.lineTo(gx, h);
        ctx.stroke();
      }
      for (let gy = (viewRef.current.oy % stepPx + stepPx) % stepPx; gy < h; gy += stepPx) {
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(w, gy);
        ctx.stroke();
      }

      ctx.translate(w / 2 + viewRef.current.ox, h / 2 + viewRef.current.oy);
      ctx.scale(viewRef.current.scale, viewRef.current.scale);

      // Draw dashed 2-hop ontology bridge edges first
      if (showOntology && ontEdges.length > 0) {
        ctx.save();
        ctx.setLineDash([5, 5]);
        for (const oe of ontEdges) {
          const s = byId.get(oe.source);
          const t = byId.get(oe.target);
          if (!s || !t) continue;
          const activeBridge =
            hovered?.id === s.id ||
            hovered?.id === t.id ||
            selectedId === s.id ||
            selectedId === t.id;
          ctx.strokeStyle = activeBridge ? '#4cc9f0' : 'rgba(76, 201, 240, 0.36)';
          ctx.lineWidth = activeBridge ? 1.6 : 1.05;
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(t.x, t.y);
          ctx.stroke();

          if (activeBridge && oe.shared_ontology?.length) {
            ctx.font = '500 10px JetBrains Mono, Consolas, monospace';
            ctx.fillStyle = '#4cc9f0';
            ctx.fillText(
              `~ ${oe.shared_ontology.slice(0, 2).join(', ')}`,
              (s.x + t.x) / 2 + 4,
              (s.y + t.y) / 2 - 4,
            );
          }
        }
        ctx.restore();
      }

      // Draw direct [[wikilink]] edges
      for (const e of graph.edges) {
        const s = byId.get(e.source);
        const t = byId.get(e.target);
        if (!s || !t) continue;
        ctx.strokeStyle =
          hovered?.id === s.id || hovered?.id === t.id ? accentColor : mutedColor;
        ctx.lineWidth = 1.35;
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(t.x, t.y);
        ctx.stroke();
      }

      // Draw nodes
      for (const n of nodes) {
        const col = colorForDomain(n.domain, n.folder);
        const isSel = n.id === selectedId;
        const isHub = n.template === 'concept-map-system-hub';
        const r = isSel ? 10.5 : isHub ? 9.5 : n.author === 'user' ? 8.5 : 7;

        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = col;
        ctx.fill();
        ctx.lineWidth = isSel ? 2.5 : isHub ? 2 : 1.2;
        ctx.strokeStyle = isSel ? textColor : n.orphan ? '#d9534f' : '#0b0c0f';
        ctx.stroke();

        ctx.font = '500 11px Inter, system-ui, sans-serif';
        ctx.fillStyle = textColor;
        ctx.fillText(n.label.slice(0, 28), n.x + r + 6, n.y + 4);
      }

      ctx.restore();
      rafId = requestAnimationFrame(step);
    };

    rafId = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener('resize', resize);
    };
  }, [graph, selectedId, showOntology, hovered?.id]);

  const screenToWorld = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const sx = clientX - rect.left - rect.width / 2 - viewRef.current.ox;
    const sy = clientY - rect.top - rect.height / 2 - viewRef.current.oy;
    return { x: sx / viewRef.current.scale, y: sy / viewRef.current.scale };
  };

  const findHit = (clientX: number, clientY: number): SimNode | null => {
    const { x, y } = screenToWorld(clientX, clientY);
    for (const n of nodesRef.current) {
      const dx = n.x - x;
      const dy = n.y - y;
      if (dx * dx + dy * dy <= 169) return n;
    }
    return null;
  };

  return (
    <div className="mindmap-wrap">
      <div className="mindmap-legend mono">
        <span>NODES: {graph?.stats.note_count ?? 0}</span>
        <span>WIKILINKS: {graph?.stats.edge_count ?? 0}</span>
        <button
          type="button"
          className={`btn ${showOntology ? 'btn-primary' : ''}`}
          style={{ padding: '1px 7px', fontSize: 10.5 }}
          onClick={() => setShowOntology((v) => !v)}
        >
          ONTOLOGY BRIDGES: {showOntology ? 'ON' : 'OFF'} (
          {graph?.stats.ontology_edge_count ?? graph?.ontology_edges?.length ?? 0})
        </button>
        <span>ORPHANS: {graph?.orphan_notes.length ?? 0}</span>
        {hovered && (
          <span style={{ color: 'var(--accent-primary)' }}>
            [{hovered.template || hovered.author.toUpperCase()}] {hovered.id}
            {hovered.ontology.length > 0 ? ` • ont:[${hovered.ontology.join(', ')}]` : ''}
          </span>
        )}
      </div>
      <canvas
        ref={canvasRef}
        style={{ touchAction: 'none' }}
        onMouseDown={(e) => {
          const hit = findHit(e.clientX, e.clientY);
          dragRef.current = {
            node: hit,
            panning: !hit,
            lx: e.clientX,
            ly: e.clientY,
          };
        }}
        onMouseMove={(e) => {
          const hit = findHit(e.clientX, e.clientY);
          setHovered(hit);
          if (dragRef.current.node) {
            const w = screenToWorld(e.clientX, e.clientY);
            dragRef.current.node.x = w.x;
            dragRef.current.node.y = w.y;
          } else if (dragRef.current.panning) {
            viewRef.current.ox += e.clientX - dragRef.current.lx;
            viewRef.current.oy += e.clientY - dragRef.current.ly;
            dragRef.current.lx = e.clientX;
            dragRef.current.ly = e.clientY;
          }
        }}
        onMouseUp={(e) => {
          const hit = findHit(e.clientX, e.clientY);
          if (hit && dragRef.current.node === hit) {
            onSelectNode(hit.id);
          }
          dragRef.current = { node: null, panning: false, lx: 0, ly: 0 };
        }}
        onTouchStart={(e) => {
          if (e.touches.length !== 1) return;
          const t = e.touches[0];
          const hit = findHit(t.clientX, t.clientY);
          setHovered(hit);
          dragRef.current = {
            node: hit,
            panning: !hit,
            lx: t.clientX,
            ly: t.clientY,
          };
        }}
        onTouchMove={(e) => {
          if (e.touches.length !== 1) return;
          const t = e.touches[0];
          if (dragRef.current.node) {
            const w = screenToWorld(t.clientX, t.clientY);
            dragRef.current.node.x = w.x;
            dragRef.current.node.y = w.y;
          } else if (dragRef.current.panning) {
            viewRef.current.ox += t.clientX - dragRef.current.lx;
            viewRef.current.oy += t.clientY - dragRef.current.ly;
            dragRef.current.lx = t.clientX;
            dragRef.current.ly = t.clientY;
          }
        }}
        onTouchEnd={(e) => {
          const t = e.changedTouches[0];
          if (t) {
            const hit = findHit(t.clientX, t.clientY);
            if (hit && dragRef.current.node === hit) {
              onSelectNode(hit.id);
            }
          }
          dragRef.current = { node: null, panning: false, lx: 0, ly: 0 };
        }}
        onWheel={(e) => {
          e.preventDefault();
          const factor = e.deltaY < 0 ? 1.1 : 0.9;
          viewRef.current.scale = Math.min(2.8, Math.max(0.35, viewRef.current.scale * factor));
        }}
      />
    </div>
  );
};

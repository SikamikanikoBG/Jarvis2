import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { Icon, type IconName } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { sizeLabel } from '../lib/format';
import type { Attachment } from '../protocol/types';

const ICON: Record<Attachment['kind'], IconName> = { image: 'image', document: 'file', text: 'file', email: 'inbox' };

/** Attachments as they appear ON a sent message: thumbnails for photos, chips for the rest. */
export function MessageAttachments({ attachments }: { attachments: Attachment[] }) {
  const [zoom, setZoom] = useState<Attachment | null>(null);
  if (attachments.length === 0) return null;
  return (
    <div className="att-row">
      {attachments.map((a) =>
        a.kind === 'image' ? (
          <button key={a.id} type="button" className="att-thumb" onClick={() => setZoom(a)} title={`${a.name} · ${sizeLabel(a.bytes)}`}>
            <img src={api.attachments.url(a.id, { thumb: true })} alt={a.name} loading="lazy" />
          </button>
        ) : (
          <a key={a.id} className="att-chip" href={api.attachments.url(a.id)} target="_blank" rel="noopener noreferrer" title={a.name}>
            <Icon name={ICON[a.kind]} size={13} />
            <span className="truncate">{a.name}</span>
            <span className="xs muted">{sizeLabel(a.bytes)}</span>
          </a>
        ),
      )}
      {zoom && <Lightbox attachment={zoom} onClose={() => setZoom(null)} />}
    </div>
  );
}

function Lightbox({ attachment, onClose }: { attachment: Attachment; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="lightbox" role="dialog" aria-label={attachment.name} onClick={onClose}>
      <img src={api.attachments.url(attachment.id)} alt={attachment.name} onClick={(e) => e.stopPropagation()} />
      <div className="lightbox-bar" onClick={(e) => e.stopPropagation()}>
        <span className="truncate">
          {attachment.name} · {sizeLabel(attachment.bytes)}
        </span>
        <a className="btn btn-sm btn-secondary" href={api.attachments.url(attachment.id)} download={attachment.name}>
          <Icon name="download" size={13} />
          Download
        </a>
        <IconButton icon="x" label="Close" onClick={onClose} />
      </div>
    </div>
  );
}

/** Attachments waiting to be sent, above the composer. */
export function PendingAttachments({
  pending,
  uploading,
  onRemove,
}: {
  pending: Attachment[];
  uploading: string[];
  onRemove: (id: string) => void;
}) {
  if (pending.length === 0 && uploading.length === 0) return null;
  return (
    <div className="att-row att-pending">
      {pending.map((a) => (
        <span key={a.id} className="att-chip">
          {a.kind === 'image' ? (
            <img className="att-chip-thumb" src={api.attachments.url(a.id, { thumb: true })} alt="" />
          ) : (
            <Icon name={ICON[a.kind]} size={13} />
          )}
          <span className="truncate">{a.name}</span>
          <span className="xs muted">{sizeLabel(a.bytes)}</span>
          <IconButton icon="x" label={`Remove ${a.name}`} size="sm" onClick={() => onRemove(a.id)} />
        </span>
      ))}
      {uploading.map((name) => (
        <span key={name} className="att-chip att-uploading">
          <Icon name="refresh" size={13} className="spin" />
          <span className="truncate">{name}</span>
        </span>
      ))}
    </div>
  );
}

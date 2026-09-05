import DOMPurify from 'dompurify';
import { useState } from 'react';
import { api } from '../api/client';
import { Icon } from '../components/Icon';
import { IconButton } from '../components/primitives';
import { errorText } from '../lib/useLoader';
import type { PairResponse } from '../protocol/types';
import { useStore } from '../store/store';

/** Pair a phone: QR (server-rendered SVG, sanitised) + the URL with the token. */
export function PairPanel() {
  const notify = useStore((s) => s.notify);
  const [pair, setPair] = useState<PairResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const show = () => {
    setBusy(true);
    api
      .pair()
      .then(setPair)
      .catch((e: unknown) => notify(`Pairing unavailable: ${errorText(e)}`, 'error'))
      .finally(() => setBusy(false));
  };
  const copy = () => pair && navigator.clipboard.writeText(pair.url).then(() => notify('Link copied.')).catch(() => notify('Copy failed.', 'error'));

  if (!pair) {
    return (
      <button type="button" className="btn btn-secondary btn-sm" onClick={show} disabled={busy}>
        <Icon name="qr" size={14} />
        {busy ? 'Preparing…' : 'Pair a phone'}
      </button>
    );
  }
  const svg = DOMPurify.sanitize(pair.qr_svg, { USE_PROFILES: { svg: true, svgFilters: true } });
  return (
    <div className="card pair-card" role="dialog" aria-label="Pair a phone">
      <div className="row">
        <h2 className="grow">Pair a phone</h2>
        <IconButton icon="x" label="Close" onClick={() => setPair(null)} />
      </div>
      <div className="pair-body">
        <div className="qr" dangerouslySetInnerHTML={{ __html: svg }} />
        <div className="grow" style={{ minWidth: 0 }}>
          <p className="small">Scan with the phone's camera, or open the link there. It carries the access token — share it with nobody.</p>
          <div className="row">
            <code className="key-code mono truncate">{pair.url}</code>
            <IconButton icon="copy" label="Copy link" onClick={() => void copy()} />
          </div>
        </div>
      </div>
    </div>
  );
}

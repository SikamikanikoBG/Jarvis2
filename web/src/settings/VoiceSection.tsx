import { Switch } from '../components/primitives';
import type { VoiceSettings } from '../protocol/types';

interface Props {
  value: VoiceSettings;
  /** Tool namespaces the core knows right now, so the allow-list is ticks, not typing. */
  namespaces: string[];
  onChange: (v: VoiceSettings) => void;
  error?: string;
}

/** What a call may reach for. Narrow by design: the loudspeaker and a misheard word are a bad
 *  place for an irreversible action, so mail and the shell are off until Arsen ticks them. */
export function VoiceSection({ value, namespaces, onChange, error }: Props) {
  const patch = (p: Partial<VoiceSettings>) => onChange({ ...value, ...p });
  const all = [...new Set([...namespaces, ...value.namespaces])].sort();
  const toggle = (ns: string, on: boolean) =>
    patch({ namespaces: on ? [...new Set([...value.namespaces, ns])] : value.namespaces.filter((n) => n !== ns) });
  return (
    <section className="card role-card">
      <div className="section-head">
        <h2>Voice — a call with Jarvis</h2>
        <p>The headset beside the microphone. Same conversation, spoken; these decide how he behaves on it.</p>
      </div>
      {error && <div className="field-error">{error}</div>}
      <div className="field">
        <span className="field-label">Tools on a call</span>
        <div className="voice-namespaces" role="group" aria-label="Tools on a call">
          {all.map((ns) => (
            <label key={ns} className={`chip chip-toggle voice-ns${value.namespaces.includes(ns) ? ' active' : ''}`}>
              <input type="checkbox" checked={value.namespaces.includes(ns)} onChange={(e) => toggle(ns, e.target.checked)} />
              {ns}
            </label>
          ))}
        </div>
        <div className="field-hint">
          On a call he thinks and remembers; he does not act. Notes and knowledge are on by default. Tick mail or the shell only if you want
          a misheard word to be able to send something.
        </div>
      </div>
      <div className="think-row">
        <Switch checked={value.think} onChange={(v) => patch({ think: v })} label="Think before answering on a call" />
        <span className="small">Thinking</span>
        <span className="field-hint">Off by default: every second of reasoning is a second of silence on the line.</span>
      </div>
      <div className="field">
        <label htmlFor="voice-style">How he speaks on a call</label>
        <textarea id="voice-style" className="textarea" rows={5} value={value.style} onChange={(e) => patch({ style: e.target.value })} />
        <div className="field-hint">Added to the turn's context on a call only, so the cached prompt for typed chats is untouched.</div>
      </div>
    </section>
  );
}

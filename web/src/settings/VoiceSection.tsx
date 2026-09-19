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
      <div className="field">
        <span className="field-label" id="seg-tts">
          His voice
        </span>
        <div className="seg" role="radiogroup" aria-labelledby="seg-tts">
          {(['server', 'device'] as const).map((o) => (
            <button key={o} type="button" role="radio" aria-checked={value.tts === o} className={value.tts === o ? 'active' : ''} onClick={() => patch({ tts: o })}>
              {o === 'server' ? 'Neural (core)' : 'Device'}
            </button>
          ))}
        </div>
        <div className="field-hint">
          Neural: the core synthesises each sentence with Microsoft's neural voices (natural Bulgarian and English; the text of each
          sentence goes to Microsoft), and the device voice steps in if the core cannot. Device: the phone's or laptop's own voice,
          offline, such as it is.
        </div>
      </div>
      <div className="field">
        <span className="field-label" id="seg-engine">
          Where the voice is made
        </span>
        <div className="seg" role="radiogroup" aria-labelledby="seg-engine">
          {(['edge', 'omnivoice'] as const).map((o) => (
            <button
              key={o}
              type="button"
              role="radio"
              aria-checked={value.engine === o}
              className={value.engine === o ? 'active' : ''}
              onClick={() => patch({ engine: o })}
              disabled={value.tts !== 'server'}
            >
              {o === 'edge' ? 'Microsoft (cloud)' : 'Own voice (ardi 3090)'}
            </button>
          ))}
        </div>
        <div className="field-hint">
          Own voice: OmniVoice on the home GPU clones a reference clip — any voice a few seconds of it can be found for — and nothing he
          says leaves the house. A sentence the local server cannot make is said by the cloud voice instead.
        </div>
      </div>
      {value.engine === 'omnivoice' && value.tts === 'server' && (
        <div className="form-grid">
          <div className="field">
            <label htmlFor="ov-bg">Bulgarian voice (own)</label>
            <input id="ov-bg" className="input" value={value.omnivoice_voices.bg ?? ''} onChange={(e) => patch({ omnivoice_voices: { ...value.omnivoice_voices, bg: e.target.value } })} />
          </div>
          <div className="field">
            <label htmlFor="ov-en">English voice (own)</label>
            <input id="ov-en" className="input" value={value.omnivoice_voices.en ?? ''} onChange={(e) => patch({ omnivoice_voices: { ...value.omnivoice_voices, en: e.target.value } })} />
          </div>
          <div className="field">
            <label htmlFor="ov-steps">Quality / speed</label>
            <select id="ov-steps" className="select" value={String(value.omnivoice_steps)} onChange={(e) => patch({ omnivoice_steps: Number(e.target.value) })}>
              <option value="8">Fast (8 steps, ~0.5 s a sentence)</option>
              <option value="16">Balanced (16 steps, ~1.1 s)</option>
              <option value="32">Best (32 steps, ~1.8 s)</option>
            </select>
          </div>
          <div className="field">
            <label htmlFor="ov-url">Server</label>
            <input id="ov-url" className="input mono" value={value.omnivoice_url} onChange={(e) => patch({ omnivoice_url: e.target.value })} />
            <div className="field-hint">A voice is a name in its voices/ folder (a clip + its words), or "design:male, low pitch".</div>
          </div>
        </div>
      )}
      <div className="form-grid">
        <div className="field">
          <label htmlFor="voice-bg">Bulgarian voice</label>
          <select id="voice-bg" className="select" value={value.voices.bg ?? ''} onChange={(e) => patch({ voices: { ...value.voices, bg: e.target.value } })} disabled={value.tts !== 'server'}>
            <option value="bg-BG-BorislavNeural">Borislav (male)</option>
            <option value="bg-BG-KalinaNeural">Kalina (female)</option>
          </select>
          {value.engine === 'omnivoice' && <div className="field-hint">The cloud voice is the fallback while the own voice is on.</div>}
        </div>
        <div className="field">
          <label htmlFor="voice-en">English voice</label>
          <select id="voice-en" className="select" value={value.voices.en ?? ''} onChange={(e) => patch({ voices: { ...value.voices, en: e.target.value } })} disabled={value.tts !== 'server'}>
            <option value="en-GB-RyanNeural">Ryan (British, male)</option>
            <option value="en-GB-SoniaNeural">Sonia (British, female)</option>
            <option value="en-US-GuyNeural">Guy (American, male)</option>
            <option value="en-US-AriaNeural">Aria (American, female)</option>
          </select>
        </div>
        <div className="field">
          <label htmlFor="voice-rate">Rate</label>
          <select id="voice-rate" className="select" value={value.rate} onChange={(e) => patch({ rate: e.target.value })} disabled={value.tts !== 'server'}>
            <option value="-10%">Slower</option>
            <option value="+0%">Normal</option>
            <option value="+10%">Faster</option>
            <option value="+20%">Fast</option>
          </select>
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

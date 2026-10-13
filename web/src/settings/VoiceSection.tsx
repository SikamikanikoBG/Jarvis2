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
          On a call he thinks and remembers; he does not act. Notes, knowledge and your other chats (sessions) are on by default. Tick mail or the shell only if you want
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
            <label htmlFor="ov-first">First sentence</label>
            <select
              id="ov-first"
              className="select"
              value={String(value.omnivoice_first_steps ?? 0)}
              onChange={(e) => patch({ omnivoice_first_steps: Number(e.target.value) || null })}
            >
              <option value="0">Same as the rest</option>
              <option value="8">Fast (8 steps, ~0.5 s)</option>
              <option value="12">Quicker (12 steps, ~0.7 s)</option>
            </select>
            <div className="field-hint">The first sentence is the one you wait for in silence; the rest are made while it plays.</div>
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
      <div className="form-grid">
        <div className="field">
          <label htmlFor="voice-waiting">While he thinks</label>
          <select
            id="voice-waiting"
            className="select"
            value={value.waiting_sound}
            onChange={(e) => patch({ waiting_sound: e.target.value === 'beeps' ? 'beeps' : e.target.value === 'silence' ? 'silence' : 'music' })}
          >
            <option value="music">Hold music</option>
            <option value="beeps">A soft pulse</option>
            <option value="silence">Silence</option>
          </select>
          <div className="field-hint">Starts only when an answer takes longer than a moment, and stops the instant he speaks. You can talk over it.</div>
        </div>
        {value.waiting_sound === 'music' && (
          <div className="field">
            <label htmlFor="voice-hold-volume">Music volume</label>
            <select id="voice-hold-volume" className="select" value={String(value.hold_volume)} onChange={(e) => patch({ hold_volume: Number(e.target.value) })}>
              <option value="0.2">Very quiet</option>
              <option value="0.4">Quiet</option>
              <option value="0.7">Clear</option>
            </select>
          </div>
        )}
      </div>
      <div className="think-row">
        <Switch checked={value.turn_detection} onChange={(v) => patch({ turn_detection: v })} label="Answer as soon as you have finished" />
        <span className="small">End of turn</span>
        <span className="field-hint">
          A small model hears from your voice whether a sentence is finished, and sends it without waiting out the pause. Words you add
          after it still reach him.
        </span>
      </div>
      {value.turn_detection && (
        <div className="form-grid">
          <div className="field">
            <label htmlFor="voice-turn">How sure it must be</label>
            <select id="voice-turn" className="select" value={String(value.turn_threshold)} onChange={(e) => patch({ turn_threshold: Number(e.target.value) })}>
              <option value="0.7">Less sure — answers sooner, cuts in more</option>
              <option value="0.8">Balanced</option>
              <option value="0.9">Very sure — waits more often</option>
            </select>
          </div>
        </div>
      )}
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

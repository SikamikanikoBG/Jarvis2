import { useState } from 'react';
import { Icon } from '../components/Icon';
import { TASK_STATE_LABEL, dodMark, taskTone } from '../lib/tasks';
import type { DodResult, Task } from '../protocol/types';
import { isTaskOpen } from '../protocol/types';
import { useStore } from '../store/store';
import { ConfirmCard } from './ConfirmCard';

function clock(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/**
 * A delegated task in the chat that asked (docs/stories/13_delegated_tasks.md, Gate A option A):
 * it updates in place - working, waiting on you, done - and carries its own approve / deny, its
 * question and an answer box, a steer box, cancel, the brief, and the way to its full work.
 */
export function TaskCard({ taskId }: { taskId: string }) {
  const task = useStore((s) => s.tasks[taskId]);
  const ttlS = useStore((s) => s.approvalTtlS);
  if (!task || task.state === 'dropped') return null;
  return <TaskCardBody task={task} approvalTtlS={ttlS} />;
}

function TaskCardBody({ task, approvalTtlS }: { task: Task; approvalTtlS: number | null }) {
  const cancelTask = useStore((s) => s.cancelTask);
  const steerTask = useStore((s) => s.steerTask);
  const openTaskWork = useStore((s) => s.openTaskWork);
  const [words, setWords] = useState('');
  const [sent, setSent] = useState(false);
  const open = isTaskOpen(task.state);
  const outcome = task.outcome;
  const items: DodResult[] =
    outcome && outcome.dod.length > 0
      ? outcome.dod
      : task.brief.dod.map((d) => ({ item: d.item, outward: d.outward, met: null, refs: [], verified: false, why: null, note: null }));
  const asking = task.state === 'needs_input' ? (outcome?.question ?? null) : null;
  const canSteer = task.state === 'running' || task.state === 'queued' || task.state === 'waiting';

  const send = () => {
    if (steerTask(task.id, words)) {
      setWords('');
      setSent(true);
    }
  };

  return (
    <article className={`task-card task-${taskTone(task.state)}`} id={`task-${task.id}`} aria-label={`Task: ${task.title}`}>
      <header className="task-head">
        <Icon name="list" size={15} />
        <span className="task-title">{task.title}</span>
        <span className={`task-pill pill-${taskTone(task.state)}`}>{TASK_STATE_LABEL[task.state]}</span>
        {task.paused && <span className="task-pill pill-muted">paused - a call or another task goes first</span>}
      </header>
      <div className="task-meta muted">
        {task.started_at ? `started ${clock(task.started_at)}` : `asked ${clock(task.created_at)}`}
        {task.finished_at && !open ? ` · ended ${clock(task.finished_at)}` : ''}
        {task.note ? ` · ${task.note}` : ''}
      </div>

      {items.length > 0 && (
        <ul className="task-dod" aria-label="Definition of Done">
          {items.map((r, i) => {
            const shown = open && r.met === null ? { mark: '◻️', label: '' } : dodMark(r);
            return (
              <li key={`${i}-${r.item}`} className={r.met === true && !r.verified ? 'task-unverified' : undefined}>
                <span aria-hidden="true">{shown.mark}</span> {r.item}
                {shown.label && shown.label !== 'done' && <span className="muted"> — {shown.label}</span>}
              </li>
            );
          })}
        </ul>
      )}

      {task.brief.assumptions.length > 0 && (
        <div className="task-assumed">
          <span className="muted">Assumed:</span> {task.brief.assumptions.join('; ')}
        </div>
      )}

      {outcome?.stopped_reason && !open && <div className="task-stopped">Stopped: {outcome.stopped_reason}</div>}

      {task.state === 'waiting' && task.waiting && (
        <>
          <ConfirmCard
            runId={task.waiting.run_id}
            callId={task.waiting.call_id}
            name={task.waiting.name}
            arguments={task.waiting.arguments}
            reason={task.waiting.reason}
          />
          {approvalTtlS !== null && (
            <div className="task-meta muted">Unanswered, it expires at {clock(new Date(Date.parse(task.waiting.since) + approvalTtlS * 1000).toISOString())}.</div>
          )}
        </>
      )}

      {asking !== null && <div className="task-question">{asking}</div>}

      {(asking !== null || canSteer) && (
        <div className="task-steer">
          <input
            className="input"
            value={words}
            onChange={(e) => {
              setWords(e.target.value);
              setSent(false);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={asking !== null ? 'Your answer' : 'Tell the task something (a change, a detail)'}
            aria-label={asking !== null ? 'Answer the task' : 'Steer the task'}
          />
          <button type="button" className="btn btn-secondary" onClick={send} disabled={!words.trim()}>
            <Icon name="send" size={15} />
            {asking !== null ? 'Answer' : 'Send'}
          </button>
          {sent && <span className="muted">Sent.</span>}
        </div>
      )}

      <details className="task-brief">
        <summary>
          <Icon name="chevronRight" size={12} className="chev" />
          Brief
        </summary>
        <BriefView task={task} />
      </details>

      <div className="task-actions">
        {task.conversation_id && (
          <button type="button" className="btn btn-ghost" onClick={() => openTaskWork(task)}>
            <Icon name="book" size={15} />
            Open work
          </button>
        )}
        {open && (
          <button type="button" className="btn btn-ghost" onClick={() => cancelTask(task.id)}>
            <Icon name="x" size={15} />
            Cancel
          </button>
        )}
      </div>
    </article>
  );
}

function BriefView({ task }: { task: Task }) {
  const b = task.brief;
  return (
    <div className="task-brief-body">
      {!b.written && <p className="muted">Noted on a call: the task writes its brief when it starts.</p>}
      {b.goal && (
        <p>
          <b>Goal:</b> {b.goal}
        </p>
      )}
      {b.asked.length > 0 && (
        <div>
          <b>Your words:</b>
          {b.asked.map((a, i) => (
            <blockquote key={i}>{a}</blockquote>
          ))}
        </div>
      )}
      {b.context && (
        <p>
          <b>Context:</b> {b.context}
        </p>
      )}
      {b.constraints.length > 0 && (
        <p>
          <b>Constraints:</b> {b.constraints.join('; ')}
        </p>
      )}
      {b.approvals.length > 0 && (
        <p>
          <b>Needs your approval:</b> {b.approvals.join('; ')}
        </p>
      )}
      {b.needs.length > 0 && (
        <p>
          <b>Uses:</b> {b.needs.join(', ')}
        </p>
      )}
    </div>
  );
}

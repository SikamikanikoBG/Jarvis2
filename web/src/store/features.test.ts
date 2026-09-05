import { describe, expect, it } from 'vitest';
import type { ServerEvent } from '../protocol/types';
import { applyFeatureEvents, initialFeatureState } from './features';

const ts = '2026-09-05T10:00:00.000Z';

describe('applyFeatureEvents', () => {
  it('bumps the matching area version and records what changed', () => {
    const s = applyFeatureEvents(initialFeatureState(), [
      { type: 'board.changed', ts, board_id: 'brd_1' },
      { type: 'kg.changed', ts, entity_ids: ['ent_a', 'ent_b'] },
      { type: 'skills.changed', ts },
      { type: 'schedule.changed', ts, schedule_id: null },
      { type: 'tools.changed', ts, provider: 'fetch' },
      { type: 'meeting.changed', ts, meeting_id: 'mtg_1', conversation_id: 'c', status: 'done' },
    ]);
    expect(s.featureVersion).toEqual({ boards: 1, kg: 1, skills: 1, schedules: 1, tools: 1, meetings: 1 });
    expect(s.changedBoardId).toBe('brd_1');
    expect(s.changedEntityIds).toEqual(['ent_a', 'ent_b']);
  });

  it('ignores chat events and returns the same state object', () => {
    const s0 = initialFeatureState();
    const s1 = applyFeatureEvents(s0, [{ type: 'pong', ts }, { type: 'conversation.deleted', ts, conversation_id: 'x' }]);
    expect(s1).toBe(s0);
  });

  it('appends live meeting segments in seq order and dedupes replays', () => {
    const seg = (seq: number): ServerEvent => ({ type: 'meeting.segment', ts, meeting_id: 'mtg_1', conversation_id: 'c', seq, t0: seq * 6, t1: seq * 6 + 5, text: `s${seq}` });
    const s = applyFeatureEvents(initialFeatureState(), [seg(2), seg(1), seg(2)]);
    expect(s.meetingSegments.mtg_1?.map((x) => x.seq)).toEqual([1, 2]);
    expect(s.featureVersion.meetings).toBe(0); // segments do not force a refetch
  });
});

import type { MeetingSegmentModel, ServerEvent } from '../protocol/types';

/** Feature areas that refetch when the server says something changed. */
export type FeatureArea = 'boards' | 'kg' | 'skills' | 'schedules' | 'tools' | 'meetings';

export type FeatureVersions = Record<FeatureArea, number>;

export interface FeatureState {
  /** Bumped by the matching `*.changed` event; screens refetch on change. */
  featureVersion: FeatureVersions;
  /** Most recently changed ids, for screens that want to refetch one thing (board / entity). */
  changedBoardId: string | null;
  changedEntityIds: string[];
  /** Live meeting transcript segments, appended from `meeting.segment`. */
  meetingSegments: Record<string, MeetingSegmentModel[] | undefined>;
}

export function initialFeatureState(): FeatureState {
  return {
    featureVersion: { boards: 0, kg: 0, skills: 0, schedules: 0, tools: 0, meetings: 0 },
    changedBoardId: null,
    changedEntityIds: [],
    meetingSegments: {},
  };
}

/** Pure: folds the feature-level events of a batch into `FeatureState`. */
export function applyFeatureEvents(state: FeatureState, events: ServerEvent[]): FeatureState {
  let next = state;
  const bump = (area: FeatureArea) => {
    next = { ...next, featureVersion: { ...next.featureVersion, [area]: next.featureVersion[area] + 1 } };
  };
  for (const ev of events) {
    switch (ev.type) {
      case 'board.changed':
        bump('boards');
        next = { ...next, changedBoardId: ev.board_id };
        break;
      case 'kg.changed':
        bump('kg');
        next = { ...next, changedEntityIds: ev.entity_ids };
        break;
      case 'skills.changed':
        bump('skills');
        break;
      case 'schedule.changed':
        bump('schedules');
        break;
      case 'tools.changed':
        bump('tools');
        break;
      case 'meeting.changed':
        bump('meetings');
        break;
      case 'meeting.segment': {
        const list = next.meetingSegments[ev.meeting_id] ?? [];
        if (list.some((s) => s.seq === ev.seq)) break;
        const seg: MeetingSegmentModel = { seq: ev.seq, t0: ev.t0, t1: ev.t1, text: ev.text };
        next = { ...next, meetingSegments: { ...next.meetingSegments, [ev.meeting_id]: [...list, seg].sort((a, b) => a.seq - b.seq) } };
        break;
      }
      default:
        break;
    }
  }
  return next;
}

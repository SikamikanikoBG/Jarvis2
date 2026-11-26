import { describe, expect, it } from 'vitest';
import type { Conversation, Project, Schedule } from '../protocol/types';
import { selectSidebar } from '../store/selectors';
import { groupByProject } from './projects';

const sch = (id: string, project_id: string | null = null): Schedule =>
  ({ id, name: id, prompt: '', cron: '0 1 * * *', at: null, tz: 'UTC', enabled: true, project_id }) as unknown as Schedule;
const prj = (id: string, schedule_ids: string[] = []): Project =>
  ({ id, name: `P ${id}`, goal: '', brief: '', ends_at: null, position: 0, schedule_ids }) as unknown as Project;

describe('groupByProject', () => {
  it('puts each schedule under its project, the rest last, unknown projects with the rest', () => {
    const out = groupByProject([sch('a', 'p1'), sch('b'), sch('c', 'p2'), sch('d', 'gone')], [prj('p1'), prj('p2'), prj('p3')], false);
    expect(out.map((x) => [x.project?.id ?? null, x.schedules.map((s) => s.id)])).toEqual([
      ['p1', ['a']],
      ['p2', ['c']],
      ['p3', []],
      [null, ['b', 'd']],
    ]);
  });

  it('drops the projects nothing matches while searching', () => {
    const out = groupByProject([sch('a', 'p1')], [prj('p1'), prj('p2')], true);
    expect(out.map((x) => x.project?.id ?? null)).toEqual(['p1']);
  });
});

describe('sidebar with projects', () => {
  const conv = (id: string, folder_key: string, updated_at: string): Conversation =>
    ({
      id,
      kind: 'scheduled',
      title: id,
      folder_key,
      folder_label: `label ${folder_key}`,
      archived: false,
      unread: false,
      pinned: false,
      activity: 'idle',
      updated_at,
      created_at: updated_at,
    }) as unknown as Conversation;

  it("files a project's fires under one group named after the project", () => {
    const convs = { x: conv('x', 's1', '2026-11-26T02'), y: conv('y', 's2', '2026-11-26T01'), z: conv('z', 's3', '2026-11-26T00') };
    const projectOf = { s1: { id: 'p1', name: 'FX trading' }, s2: { id: 'p1', name: 'FX trading' } };
    const scheduled = selectSidebar(convs, [], { projectOf }).folders.find((f) => f.kind === 'scheduled');
    expect(scheduled?.groups.map((g) => [g.label, g.conversations.map((c) => c.id)])).toEqual([
      ['FX trading', ['x', 'y']],
      ['label s3', ['z']],
    ]);
  });
});

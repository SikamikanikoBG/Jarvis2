import type { Project, Schedule } from '../protocol/types';

/** A section of the screen: one project with its schedules, or the ones on their own. */
export interface Section {
  project: Project | null;
  schedules: Schedule[];
}

/** Projects in their order, each with its schedules soonest first; then the ones on their own.
 * A project nothing matches is left out while searching, but an empty one stays otherwise - it
 * was just made, or its schedules were moved out, and it is where new ones go. */
export function groupByProject(schedules: Schedule[], projects: Project[], searching: boolean): Section[] {
  const known = new Set(projects.map((p) => p.id));
  const sections: Section[] = projects.map((project) => ({
    project,
    schedules: schedules.filter((s) => s.project_id === project.id),
  }));
  const loose = schedules.filter((s) => !s.project_id || !known.has(s.project_id));
  const out = searching ? sections.filter((x) => x.schedules.length > 0) : sections;
  if (loose.length > 0) out.push({ project: null, schedules: loose });
  return out;
}

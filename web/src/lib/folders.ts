/** Which of Arsen's chat folders are collapsed, remembered per device. */

const KEY = 'jarvis.collapsedFolders';

export function readCollapsedFolders(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

export function writeCollapsedFolders(ids: string[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(ids));
  } catch {
    /* private mode: folders just reopen on the next load */
  }
}

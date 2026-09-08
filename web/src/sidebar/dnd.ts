/**
 * Dragging chats into folders.
 *
 * The payload is a list, not one id: dragging a row that is part of a multi-selection moves the
 * whole selection, which is the only way filing thirty chats at once is bearable. A custom MIME
 * type keeps the sidebar from accepting text or files dropped on it by accident.
 */

export const CONVERSATION_DRAG_TYPE = 'application/x-jarvis-conversations';

export function setDragIds(dt: DataTransfer, ids: string[]): void {
  dt.setData(CONVERSATION_DRAG_TYPE, JSON.stringify(ids));
  dt.effectAllowed = 'move';
}

/** The dragged conversation ids, or [] when this is not one of our drags. */
export function readDragIds(dt: DataTransfer): string[] {
  try {
    const parsed: unknown = JSON.parse(dt.getData(CONVERSATION_DRAG_TYPE) || '[]');
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

/**
 * True when the drag carries chats. `getData` is empty during dragover in every browser, so the
 * only thing readable that early is the type list.
 */
export function isConversationDrag(dt: DataTransfer | null): boolean {
  return Boolean(dt) && Array.from(dt?.types ?? []).includes(CONVERSATION_DRAG_TYPE);
}

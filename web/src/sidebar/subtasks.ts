import { createContext } from "react";
import type { Conversation } from "../protocol/types";

/** The live delegated tasks per chat (selectors.ts `subtasks`), drawn as rows under it. */
export const SubtasksContext = createContext<Record<string, Conversation[]>>(
  {},
);

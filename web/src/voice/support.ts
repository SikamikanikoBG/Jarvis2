import { micSupported } from './listener';
import { ttsSupported } from './speaker';

/** Whether a call can happen on this device at all: something to hear with and something to speak with. */
export const callSupported = (): boolean => micSupported() && ttsSupported();

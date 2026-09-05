import { useEffect, useState } from 'react';
import { ApiError, describeError } from '../api/client';

export const errorText = (e: unknown): string => (e instanceof ApiError ? describeError(e.status, e.body) : e instanceof Error ? e.message : String(e));

interface LoaderResult<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** Replace the data locally (optimistic edits) without a refetch. */
  setData: (updater: (prev: T | null) => T | null) => void;
  reload: () => void;
}

/**
 * Fetch-on-mount and refetch whenever `key` changes (feature screens pass the server's
 * `*.changed` version in the key). `loader` must be referentially stable (useCallback).
 * State is only set inside promise callbacks, never synchronously in the effect body.
 */
export function useLoader<T>(loader: () => Promise<T>, key: string | number): LoaderResult<T> {
  const [res, setRes] = useState<{ key: string | number | null; data: T | null; error: string | null }>({ key: null, data: null, error: null });
  const [tick, setTick] = useState(0);
  const fullKey = `${key}#${tick}`;
  useEffect(() => {
    let alive = true;
    loader()
      .then((data) => {
        if (alive) setRes({ key: fullKey, data, error: null });
      })
      .catch((e: unknown) => {
        if (alive) setRes((prev) => ({ key: fullKey, data: prev.data, error: errorText(e) }));
      });
    return () => {
      alive = false;
    };
  }, [loader, fullKey]);
  return {
    data: res.data,
    error: res.error,
    loading: res.key !== fullKey,
    setData: (updater) => setRes((prev) => ({ ...prev, data: updater(prev.data) })),
    reload: () => setTick((t) => t + 1),
  };
}

const KEY = 'jarvis_token';

/** Bearer token: `?token=` on first load wins and is persisted, then localStorage. */
export function resolveToken(): string | null {
  let fromUrl: string | null = null;
  try {
    const url = new URL(window.location.href);
    fromUrl = url.searchParams.get('token');
    if (fromUrl) {
      url.searchParams.delete('token');
      window.history.replaceState(null, '', url.toString());
    }
  } catch {
    /* no URL API */
  }
  try {
    if (fromUrl) {
      localStorage.setItem(KEY, fromUrl);
      return fromUrl;
    }
    return localStorage.getItem(KEY);
  } catch {
    return fromUrl;
  }
}

let cached: string | null | undefined;

export function getToken(): string | null {
  if (cached === undefined) cached = resolveToken();
  return cached;
}

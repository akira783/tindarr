/**
 * The fetch wrapper turns `403 reauth_required` into a dialog. The dialog lives
 * in the React tree, so it registers itself here and the wrapper awaits it.
 */
type Requester = () => Promise<boolean>;

let requester: Requester | null = null;

export function setReauthRequester(fn: Requester | null): void {
  requester = fn;
}

export async function requestReauth(): Promise<boolean> {
  if (requester === null) return false;
  return await requester();
}

/**
 * What the end-to-end scenario needs outside the browser.
 *
 * Two kinds of caller live here and neither is the console:
 *
 * - **the app** — `POST /auth/pair/preview`, `/auth/pair`, `/auth/pair/complete` and
 *   the token pair they produce. It sends a bearer token and never a cookie, so these
 *   run on their own `APIRequestContext`, isolated from the browser's;
 * - **a media server client** — the Jellyfin app in which a user approves a Quick
 *   Connect code, and the plex.tv account that approves a PIN.
 *
 * Nothing here talks to TMDb, OMDb or an AI provider: step 2 has no such call.
 */
import { createHash, randomBytes } from "node:crypto";

import { expect, type APIRequestContext, type APIResponse } from "@playwright/test";

/** The client identity Jellyfin and Emby record for the approving "app". */
const SEED_CLIENT = 'Client="Tindarr e2e", Device="ci", DeviceId="tindarr-e2e-approver", Version="1"';

export interface PkcePair {
  verifier: string;
  challenge: string;
}

function base64url(value: Buffer): string {
  return value.toString("base64url");
}

/** A PKCE S256 pair, in the shapes `api/openapi.yaml` accepts (43 characters each). */
export function pkcePair(): PkcePair {
  const verifier = base64url(randomBytes(32));
  return { verifier, challenge: base64url(createHash("sha256").update(verifier).digest()) };
}

async function expectStatus(response: APIResponse, status: number, what: string): Promise<unknown> {
  const body = await response.text();
  expect(response.status(), `${what}: ${body}`).toBe(status);
  return body === "" ? null : JSON.parse(body);
}

// ------------------------------------------------------------------ the media server

export interface MediaServerSession {
  token: string;
  userId: string;
}

/** Sign in on Jellyfin or Emby the way one of their own apps would. */
export async function mediaServerSignIn(
  request: APIRequestContext,
  baseUrl: string,
  username: string,
  password: string,
): Promise<MediaServerSession> {
  const response = await request.post(`${baseUrl}/Users/AuthenticateByName`, {
    headers: { Authorization: `MediaBrowser ${SEED_CLIENT}` },
    data: { Username: username, Pw: password },
  });
  const payload = (await expectStatus(response, 200, "media server sign-in")) as {
    AccessToken: string;
    User: { Id: string };
  };
  return { token: payload.AccessToken, userId: payload.User.Id };
}

/**
 * Approve a Quick Connect code, as the user would in a Jellyfin client.
 *
 * `code` is a query parameter and the call needs a token; `userId` is optional for a
 * user token (it falls back to the caller) but is sent explicitly so the approval is
 * unambiguous. Jellyfin answers a bare `true`.
 */
export async function approveQuickConnect(
  request: APIRequestContext,
  baseUrl: string,
  session: MediaServerSession,
  code: string,
): Promise<void> {
  const response = await request.post(
    `${baseUrl}/QuickConnect/Authorize?code=${encodeURIComponent(code)}&userId=${session.userId}`,
    { headers: { Authorization: `MediaBrowser ${SEED_CLIENT}, Token="${session.token}"` } },
  );
  expect(await expectStatus(response, 200, "Quick Connect approval")).toBe(true);
}

// ------------------------------------------------------------------------- the app

export interface PairingPreview {
  server_name: string;
  user_name: string;
  expires_at: string;
}

export interface PairingRequested {
  pending: true;
  retry_after_ms: number;
  confirmation_code: string;
  expires_at: string;
}

export interface TokenPair {
  access_token: string;
  access_expires_at: string;
  refresh_token: string;
  refresh_expires_at: string;
}

export interface ApiUser {
  id: string;
  name: string;
  role: "admin" | "user";
  media_server_admin?: boolean;
}

export interface AuthResult extends TokenPair {
  user: ApiUser;
  setup_completed_now: boolean;
}

/** The device an app sends with `POST /auth/pair` (`DeviceInput` in the contract). */
export const PHONE = { name: "Pixel 9", platform: "android", app_version: "1.0.0" } as const;

export class AppClient {
  constructor(
    private readonly request: APIRequestContext,
    private readonly baseUrl: string,
  ) {}

  private url(path: string): string {
    return `${this.baseUrl}/api/v1${path}`;
  }

  async previewPairing(code: string): Promise<PairingPreview> {
    const response = await this.request.post(this.url("/auth/pair/preview"), { data: { code } });
    return (await expectStatus(response, 200, "pairing preview")) as PairingPreview;
  }

  async requestPairing(code: string, challenge: string): Promise<PairingRequested> {
    const response = await this.request.post(this.url("/auth/pair"), {
      data: { code, device: PHONE, code_challenge: challenge },
    });
    return (await expectStatus(response, 202, "pairing request")) as PairingRequested;
  }

  /** Poll `POST /auth/pair/complete` the way the app is told to: `202` until approved. */
  async completePairing(code: string, verifier: string, attempts = 30): Promise<AuthResult> {
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const response = await this.request.post(this.url("/auth/pair/complete"), {
        data: { code, code_verifier: verifier },
      });
      if (response.status() === 200) {
        return (await response.json()) as AuthResult;
      }
      expect(response.status(), `pairing completion: ${await response.text()}`).toBe(202);
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error("the pairing was never completed");
  }

  async me(accessToken: string): Promise<ApiUser> {
    const response = await this.request.get(this.url("/me"), {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    return (await expectStatus(response, 200, "GET /me")) as ApiUser;
  }

  async refresh(refreshToken: string): Promise<TokenPair> {
    const response = await this.request.post(this.url("/auth/refresh"), {
      data: { refresh_token: refreshToken },
    });
    return (await expectStatus(response, 200, "token refresh")) as TokenPair;
  }
}

/** The code a QR link carries: `tindarr://pair?server=<public_url>&code=<code>`. */
export function codeFromPairingLink(link: string): string {
  const query = link.slice(link.indexOf("?") + 1);
  const code = new URLSearchParams(query).get("code");
  expect(code, `no code in ${link}`).not.toBeNull();
  return code!;
}

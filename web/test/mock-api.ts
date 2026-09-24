import type { components, paths } from "../src/api/schema";

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
type Path = keyof paths;

export interface Call {
  method: string;
  path: string;
  headers: Headers;
  body: unknown;
  credentials: RequestCredentials;
}

export type Handler = (call: Call) => Response | Promise<Response>;

/** A typed JSON body, so every handler is written against the contract's schemas. */
export function ok<T>(body: T, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

export function pending(retryAfterMs = 1000): Response {
  return ok<components["schemas"]["Pending"]>({ pending: true, retry_after_ms: retryAfterMs }, 202);
}

export function problem(
  status: number,
  code: string,
  extra: Partial<components["schemas"]["Problem"]> = {},
): Response {
  const body: components["schemas"]["Problem"] = {
    type: "about:blank",
    title: code,
    status,
    code,
    ...extra,
  };
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

/** JSON where there is JSON, the raw text otherwise: an import uploads a file. */
function parseBody(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function toPattern(path: string): RegExp {
  const escaped = path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\\\{[^}]+\\\}/g, "[^/]+");
  return new RegExp(`^${escaped}$`);
}

/**
 * A tiny contract-typed stand-in for the server, used instead of a network:
 * handlers answer the paths of `api/openapi.yaml` with its own schemas, and
 * every call is recorded so the tests can assert on headers (CSRF) and bodies.
 */
export class MockApi {
  readonly calls: Call[] = [];
  private readonly routes: { method: Method; pattern: RegExp; handler: Handler }[] = [];

  on(method: Method, path: Path, handler: Handler): this {
    this.routes.push({ method, pattern: toPattern(path), handler });
    return this;
  }

  /** Replaces the handler of a route that was already registered. */
  replace(method: Method, path: Path, handler: Handler): this {
    const pattern = toPattern(path).source;
    const index = this.routes.findIndex(
      (route) => route.method === method && route.pattern.source === pattern,
    );
    if (index >= 0) this.routes.splice(index, 1);
    return this.on(method, path, handler);
  }

  readonly fetch = async (request: Request): Promise<Response> => {
    const url = new URL(request.url);
    const text = request.method === "GET" || request.method === "DELETE" ? "" : await request.clone().text();
    const call: Call = {
      method: request.method,
      path: url.pathname,
      headers: new Headers(request.headers),
      body: text === "" ? null : parseBody(text),
      credentials: request.credentials,
    };
    this.calls.push(call);

    const route = this.routes.find(
      (candidate) => candidate.method === request.method && candidate.pattern.test(url.pathname),
    );
    if (route === undefined) {
      return problem(404, "not_found");
    }
    return await route.handler(call);
  };

  callsTo(method: Method, path: string): Call[] {
    const pattern = toPattern(path);
    return this.calls.filter((call) => call.method === method && pattern.test(call.path));
  }
}

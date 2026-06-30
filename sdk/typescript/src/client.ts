import {
  AuthenticationError,
  ConflictError,
  InvalidRequestError,
  MemoryServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  ServiceUnavailableError,
} from "./errors.js";
import type {
  IngestTextRequest,
  IngestTextResponse,
  RecallRequest,
  RecallResponse,
  RetainRequest,
  RetainResponse,
  RetryPolicy,
} from "./types.js";

export interface ClientOptions {
  baseUrl?: string;
  apiKey?: string;
  timeoutMs?: number;
  retry?: RetryPolicy;
  fetch?: typeof fetch;
  sleep?: (milliseconds: number) => Promise<void>;
}

const defaultRetryStatuses = new Set([429, 502, 503, 504]);

export class MemoryClient {
  private readonly baseUrl: string;
  private readonly apiKey: string | undefined;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly baseDelayMs: number;
  private readonly retryStatuses: ReadonlySet<number>;
  private readonly fetchFn: typeof fetch;
  private readonly sleepFn: (milliseconds: number) => Promise<void>;

  constructor(options: ClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "http://localhost:8080").replace(/\/$/, "");
    this.apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.maxRetries = options.retry?.maxRetries ?? 3;
    this.baseDelayMs = options.retry?.baseDelayMs ?? 100;
    this.retryStatuses = options.retry?.retryStatuses ?? defaultRetryStatuses;
    this.fetchFn = options.fetch ?? globalThis.fetch;
    this.sleepFn =
      options.sleep ??
      ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  }

  async health(): Promise<{ status: string }> {
    return this.request("GET", "/health");
  }

  async retain(request: RetainRequest): Promise<RetainResponse> {
    const body = {
      layer: "semantic",
      scope: "workspace",
      kind: "fact",
      source_kind: "sdk-typescript",
      ...request,
      idempotency_key: request.idempotency_key ?? crypto.randomUUID(),
    };
    return this.request("POST", "/v1/memory/retain", body);
  }

  async recall(request: RecallRequest): Promise<RecallResponse> {
    return this.request("POST", "/v1/memory/recall", request);
  }

  async ingestText(request: IngestTextRequest): Promise<IngestTextResponse> {
    return this.request("POST", "/v1/ingest/text", request);
  }

  private async request<T>(
    method: string,
    path: string,
    body?: object,
  ): Promise<T> {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/json",
    };
    if (this.apiKey !== undefined) {
      headers.Authorization = `Bearer ${this.apiKey}`;
    }
    for (let attempt = 0; attempt <= this.maxRetries; attempt += 1) {
      let response: Response;
      try {
        const init: RequestInit = {
          method,
          headers,
          signal: AbortSignal.timeout(this.timeoutMs),
        };
        if (body !== undefined) {
          init.body = JSON.stringify(body);
        }
        response = await this.fetchFn(`${this.baseUrl}${path}`, init);
      } catch (error) {
        if (attempt === this.maxRetries) {
          throw new ServiceUnavailableError(String(error));
        }
        await this.sleepFn(this.baseDelayMs * 2 ** attempt);
        continue;
      }
      const payload = (await response.json().catch(() => ({}))) as Record<
        string,
        unknown
      >;
      if (response.ok) {
        return payload as T;
      }
      if (this.retryStatuses.has(response.status) && attempt < this.maxRetries) {
        await this.sleepFn(this.retryDelay(response, attempt));
        continue;
      }
      throwHttpError(response.status, payload);
    }
    throw new Error("retry loop must return or throw");
  }

  private retryDelay(response: Response, attempt: number): number {
    const value = response.headers.get("Retry-After");
    if (value !== null) {
      const seconds = Number(value);
      if (Number.isFinite(seconds) && seconds >= 0) {
        return seconds * 1000;
      }
    }
    return this.baseDelayMs * 2 ** attempt;
  }
}

function throwHttpError(status: number, body: Record<string, unknown>): never {
  const detail =
    typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  const errors: Record<number, typeof MemoryServerError> = {
    400: InvalidRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: InvalidRequestError,
    429: RateLimitError,
  };
  const ErrorType =
    errors[status] ??
    (status >= 500 ? ServiceUnavailableError : MemoryServerError);
  throw new ErrorType(detail, status);
}

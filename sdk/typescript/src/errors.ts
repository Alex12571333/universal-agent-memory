export class MemoryServerError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = new.target.name;
  }
}

export class InvalidRequestError extends MemoryServerError {}
export class AuthenticationError extends MemoryServerError {}
export class PermissionDeniedError extends MemoryServerError {}
export class NotFoundError extends MemoryServerError {}
export class ConflictError extends MemoryServerError {}
export class RateLimitError extends MemoryServerError {}
export class ServiceUnavailableError extends MemoryServerError {}

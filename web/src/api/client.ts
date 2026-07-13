export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

const configuredBaseUrl = (import.meta.env.VITE_API_BASE_URL ?? "").replace(
  /\/$/,
  "",
);

export async function apiGet<T>(
  path: string,
  query?: Readonly<Record<string, string | number | undefined>>,
  init?: RequestInit,
): Promise<T> {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) {
      search.set(key, String(value));
    }
  }
  const queryString = search.toString();
  const url = `${configuredBaseUrl}${path}${queryString ? `?${queryString}` : ""}`;
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  const response = await fetch(url, {
    ...init,
    method: "GET",
    headers,
  });
  const body: unknown = await response.json();
  if (!response.ok) {
    throw new ApiError(response.status, body);
  }
  return body as T;
}

export async function apiPost<TResponse, TBody>(
  path: string,
  body: TBody,
  init?: RequestInit,
): Promise<TResponse> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(`${configuredBaseUrl}${path}`, {
    ...init,
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  const responseBody: unknown = await response.json();
  if (!response.ok) {
    throw new ApiError(response.status, responseBody);
  }
  return responseBody as TResponse;
}

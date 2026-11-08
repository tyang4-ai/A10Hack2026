import { httpApi } from "./http";
import { mockApi } from "./mock";
import type { Api } from "./types";

// Set NEXT_PUBLIC_API_URL to the API Gateway stage URL to use the real backend.
// Without it the dashboard runs against the in-browser mock (docs/API.md).
const base = process.env.NEXT_PUBLIC_API_URL;

export const usingMock = !base;
export const api: Api = base ? httpApi(base) : mockApi();
export * from "./types";

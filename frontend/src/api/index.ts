import { httpApi } from "./http";
import { mockApi } from "./mock";
import type { Api } from "./types";

const base = import.meta.env.VITE_API_URL as string | undefined;

export const usingMock = !base;
export const api: Api = base ? httpApi(base) : mockApi();
export * from "./types";

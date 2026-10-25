// decide<T>() (spec §12): the structured-output primitive. A
// provider.complete on the small model with a deadline, the
// provider's one parse retry and an optional cache. The caller owns
// the prompt and the schema; decide knows none of its consumers.

import type { z } from "zod";
import { Settings } from "../config/settings.ts";
import type { Provider } from "../provider/types.ts";

export interface DecideOptions<T> {
  name: string;
  system: string;
  input: string;
  schema: z.ZodType<T>;
  model?: string; // default: the settings default smallModel
  timeoutMs?: number; // reject (and cancel the request) at the deadline
  cache?: Map<string, T>;
  cacheKey?: string; // default: `input`
  signal?: AbortSignal; // the caller's cancel (e.g. the run's)
}

export class DecisionTimeout extends Error {
  override name = "DecisionTimeout";
}

export const DEFAULT_MODEL = Settings.parse({}).smallModel;

// Rejects on a provider error, a second invalid answer, the deadline
// or `signal` (with its reason; the request is cancelled too); only
// answers that parsed are cached.
export async function decide<T>(
  provider: Provider,
  o: DecideOptions<T>,
): Promise<T> {
  const key = o.cacheKey ?? o.input;
  if (o.cache?.has(key)) return o.cache.get(key) as T;
  const request = {
    model: o.model ?? DEFAULT_MODEL,
    system: o.system,
    input: o.input,
    schema: o.schema,
    name: o.name,
  };
  const answer = await withDeadline(o, (signal) =>
    provider.complete(request, signal),
  );
  o.cache?.set(key, answer);
  return answer;
}

// Runs `request` under one controller that the deadline and the
// caller's signal both abort; either one rejects with its reason
// (the classifier's decisions() call shares it).
export async function withDeadline<T>(
  o: { name: string; timeoutMs?: number; signal?: AbortSignal },
  request: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  o.signal?.throwIfAborted();
  const abort = new AbortController();
  const cancel = (): void => abort.abort(o.signal?.reason);
  o.signal?.addEventListener("abort", cancel, { once: true });
  const timer =
    o.timeoutMs === undefined
      ? undefined
      : setTimeout(() => {
          const ms = o.timeoutMs;
          abort.abort(
            new DecisionTimeout(`${o.name}: no answer within ${ms} ms`),
          );
        }, o.timeoutMs);
  const stopped = new Promise<never>((_, reject) => {
    const { signal } = abort;
    signal.addEventListener("abort", () => reject(signal.reason), {
      once: true,
    });
  });
  try {
    return await Promise.race([request(abort.signal), stopped]);
  } finally {
    clearTimeout(timer);
    o.signal?.removeEventListener("abort", cancel);
  }
}

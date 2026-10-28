// The Decisions API 200 body (spec §5), parsed strictly by the real
// provider and by FakeProvider alike: a wrong answer shape is a
// ProviderError("invalid_response"), never a bare ZodError.

import { z } from "zod";
import {
  type DecisionsRequest,
  type DecisionsResponse,
  ProviderError,
} from "./types.ts";

const Probability = z.number().min(0).max(1);
const Probabilities = z.record(z.string(), Probability);

const WireAnswer = z.discriminatedUnion("type", [
  z.object({ type: z.literal("noul"), noul: Probability }),
  z.object({
    type: z.literal("choice"),
    choice: z.string(),
    confidence: Probability,
    probabilities: Probabilities,
  }),
  z.object({
    type: z.literal("score"),
    score: z.number(),
    confidence: Probability,
    probabilities: Probabilities,
    legend: z.record(z.string(), z.string()),
  }),
]);

const WireDecisions = z.object({
  id: z.string(),
  model: z.string(),
  provider: z.string(),
  answers: z.record(z.string(), WireAnswer),
  usage: z.object({
    input_tokens: z.number().int().nonnegative(),
    output_tokens: z.number().int().nonnegative(),
    cost: z.number().nonnegative(),
  }),
});

// Every question asked must come back answered, with its own type.
export function parseDecisions(
  req: DecisionsRequest,
  body: unknown,
): DecisionsResponse {
  const result = WireDecisions.safeParse(body);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `${i.path.join(".") || "(root)"}: ${i.message}`)
      .join("; ");
    throw invalid(issues);
  }
  const { usage, ...wire } = result.data;
  for (const [name, question] of Object.entries(req.questions)) {
    if (wire.answers[name]?.type !== question.type) {
      throw invalid(`no ${question.type} answer for "${name}"`);
    }
  }
  return {
    ...wire,
    usage: {
      inputTokens: usage.input_tokens,
      outputTokens: usage.output_tokens,
      cost: usage.cost,
    },
  };
}

function invalid(detail: string): ProviderError {
  return new ProviderError("invalid_response", `decisions: ${detail}`);
}

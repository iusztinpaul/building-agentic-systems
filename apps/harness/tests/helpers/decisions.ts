// The danger classifier's answer as a Decisions API 200 body, for
// FakeProvider's `decisions` script.

export function verdict(
  p: number, // p(dangerous)
  risk: "low" | "medium" | "high" = p >= 0.3 ? "high" : "low",
  { confidence = 1, cost = 0.00002 }: { confidence?: number; cost?: number } = {},
) {
  return {
    id: "gen-dec-fake",
    model: "typesafe/jev-1.14-20261014",
    provider: "TypeSafe",
    answers: {
      dangerous: { type: "noul", noul: p },
      risk: { type: "choice", choice: risk, confidence, probabilities: { [risk]: confidence } },
    },
    usage: { input_tokens: 500, output_tokens: 0, cost },
  };
}

// A harmless call, as jev-1.14 answered `ls -la` live.
export const safe = verdict(0.02, "low");

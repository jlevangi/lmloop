/**
 * Model catalog for pi, from the right authority for each kind of model.
 *
 * This lmloop-scoped extension registers only the self-hosted provider.
 *
 * Self-hosted models do NOT come through 9router. The router reports model
 * metadata rather than how llama-swap actually loaded the weights -- it
 * advertised 1000000 context for a model running with --ctx-size 65536, and
 * 262144 for one running with 98304. Declaring those numbers killed three runs
 * on HTTP 400 mid-iteration. Talking to llama-swap directly removes the
 * guesswork: it serves an OpenAI-compatible API of its own, and exposes the
 * loaded model's true n_ctx at /upstream/<model>/props.
 *
 * Startup must stay cheap and must never load a model: llama-swap holds one
 * model at a time, so probing an unloaded one would evict whatever a running
 * job is using. So this reads only the model list (free) and takes context
 * sizes from the cache written by `lmloop models --detect`.
 */

const HOME = process.env.HOME;

const BUDGETS_FILE = `${HOME}/.config/lmloop/model-budgets.json`;

// The split policy and the llama-swap address, shared with lmloop: models.py
// reads this same file. They used to be written down in both places and had
// already drifted -- lmloop reserved the default 8192 for Qwen where this file
// reserves 24576, so it believed in 16K of prompt room pi was never given.
//
// `readJson` is a function declaration and so is hoisted; the literals below
// each `??` are kept deliberately, so a missing or unparseable file leaves this
// extension behaving exactly as it did before the file existed. It is a place
// to edit, not a dependency.
const BUDGETS = readJson(BUDGETS_FILE) || {};

const LLAMA_SWAP = BUDGETS.llama_swap_url ?? "http://127.0.0.1:8080";
const CONTEXT_FILE = `${HOME}/.config/lmloop/model-context.json`;


// An agent once built a 68194-token request against a correctly declared 65536:
// system prompt and tool definitions escape whatever budget a harness compacts
// to, so the declared window sits below the real one. Capping self-hosted
// output at exactly this figure makes contextWindow + maxTokens land on the
// real window precisely, which is safe whether a harness reads contextWindow
// as the total budget or as the prompt budget alone.
const HEADROOM = BUDGETS.headroom ?? 8192;
const FETCH_TIMEOUT_MS = 8000;

// Per-model output budgets, for models the default split is wrong for.
//
// HEADROOM assumes the prompt is what runs out first. For a reasoning model it
// is the other way round: Qwen3.8-27B spent a whole lmloop iteration on
// poker-night inside a single reasoning block, hit the 8192 output cap
// mid-sentence, and ended the message having called no tool at all -- 19
// minutes, an untouched worktree. Its prompt budget was never the constraint:
// that iteration used 9575 input tokens against 90112 available.
//
// An override replaces HEADROOM on both sides of the split, so
// contextWindow + maxTokens still lands exactly on the real window and the
// HTTP 400s that overshooting causes stay impossible. It also bypasses the
// contextWindow/4 sanity cap, which is the right default but is exactly what a
// deliberate per-model choice is for.
const OUTPUT_OVERRIDE = BUDGETS.output_override ?? { "Qwen3.8-27B": 24576, "Laguna-S-2.1": 16384 };

// llama-swap serves embeddings and rerankers alongside chat models; they cannot
// drive an agent and only clutter the picker.
const UNMEASURED_CONTEXT = BUDGETS.unmeasured_context ?? 24576;

const NOT_CHAT = /embedding|rerank/i;

function readJson(path) {
  try {
    return JSON.parse(require("node:fs").readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

async function fetchJson(url, headers) {
  const response = await fetch(url, {
    headers: headers || {},
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function localModels() {
  const catalog = (await fetchJson(`${LLAMA_SWAP}/v1/models`)).data || [];
  const measured = readJson(CONTEXT_FILE) || {};
  const unmeasured = [];
  const models = catalog
    .filter((entry) => !NOT_CHAT.test(entry.id))
    .map((entry) => {
      const real = measured[entry.id];
      if (!real) unmeasured.push(entry.id);
      const override = OUTPUT_OVERRIDE[entry.id];
      const reserved = override ?? HEADROOM;
      // Without a measurement there is nothing honest to claim, so fall back to
      // a window small enough that no llama.cpp deployment rejects it.
      const contextWindow = real ? Math.max(real - reserved, 8192) : UNMEASURED_CONTEXT;
      return {
        id: entry.id,
        name: `${entry.id} (local)`,
        reasoning: true,
        input: ["text"],
        contextWindow,
        maxTokens: override ?? Math.min(HEADROOM, Math.floor(contextWindow / 4)),
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        // llama.cpp rejects the `developer` role and reasoning_effort.
        compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
      };
    });
  return { models, unmeasured };
}

export default async function (pi) {
  try {
    const { models, unmeasured } = await localModels();
    if (models.length) {
      pi.registerProvider("llama-swap", {
        name: "llama-swap (self-hosted)",
        baseUrl: `${LLAMA_SWAP}/v1`,
        // llama-swap needs no credential, but pi hides models that require auth
        // until one exists, so a placeholder keeps them selectable.
        apiKey: "local",
        api: "openai-completions",
        models,
      });
    }
    if (unmeasured.length) {
      console.error(
        `llama-swap: ${unmeasured.join(", ")} have no measured context and are ` +
          `capped at 24576; run \`lmloop models --detect\``,
      );
    }
  } catch (error) {
    // Expected whenever the GPU box is off; cloud models still work.
    console.error(`llama-swap unreachable (${error.message}); local models unavailable`);
  }
}

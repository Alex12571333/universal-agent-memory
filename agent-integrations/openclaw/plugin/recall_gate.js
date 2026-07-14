const VALID_MODES = new Set(["off", "adaptive", "always"]);

const explicitMemory = [
  "помнишь", "вспомни", "что ты знаешь обо мне", "из памяти", "в памяти",
  "remember", "recall", "from memory", "what do you know about me",
];
const historical = [
  "раньше", "прошлый", "прошлая", "прошлые", "до этого", "в прошлый раз",
  "мы использовали", "мы делали", "previous", "last time", "used before",
  "we used", "we did", "history",
];
const personal = [
  "мои настройки", "мои предпочтения", "мой профиль", "обо мне", "я предпочитаю",
  "my settings", "my preferences", "my profile", "about me", "i prefer",
];
const project = [
  "наш проект", "нашем проекте", "наша система", "нашей системе", "наш сервер",
  "нашем сервере", "наш репозиторий", "нашем репозитории", "этот проект",
  "этот репо", "в репозитории", "our project", "our system", "our server",
  "our repository", "this project", "this repo", "in the repository",
];
const ambiguous = [
  "та модель", "тот конфиг", "тот сервер", "тот проект", "это пофиксили",
  "там агенты", "как тогда", "как раньше", "that model", "that config",
  "that server", "that project", "the previous one", "as before",
];
const continuations = new Set([
  "продолжай", "продолжи", "дальше", "делай дальше", "continue",
  "continue please", "go on", "keep going",
]);
const shortCommands = new Set([
  "да", "нет", "ок", "окей", "делай", "готово", "спасибо", "yes", "no",
  "ok", "okay", "do it", "thanks", "thank you",
]);
const selfContainedPrefixes = [
  "напиши ", "объясни ", "перечисли ", "создай ", "сгенерируй ", "сравни ",
  "что такое ", "почему ", "как сделать ", "write ", "explain ", "list ",
  "create ", "generate ", "compare ", "what is ", "why ", "how to ",
];

function containsAny(text, needles) {
  return needles.some((needle) => text.includes(needle));
}

export function evaluateRecallGate(
  query,
  { mode = "adaptive", hasLiveContext = null, forceFullRecall = false } = {},
) {
  let normalizedMode = String(mode).trim().toLowerCase();
  if (!VALID_MODES.has(normalizedMode)) normalizedMode = "adaptive";
  const text = String(query || "").toLocaleLowerCase().trim().replace(/\s+/gu, " ");

  if (!text) return { shouldRecall: false, reason: "empty", tier: "none" };
  if (forceFullRecall) return { shouldRecall: true, reason: "explicit_full_recall", tier: "full" };
  if (normalizedMode === "off") return { shouldRecall: false, reason: "mode_off", tier: "none" };
  if (normalizedMode === "always") return { shouldRecall: true, reason: "mode_always", tier: "full" };
  if (containsAny(text, explicitMemory)) return { shouldRecall: true, reason: "explicit_memory", tier: "compact" };
  if (containsAny(text, historical)) return { shouldRecall: true, reason: "historical_reference", tier: "compact" };
  if (containsAny(text, personal)) return { shouldRecall: true, reason: "personal_context", tier: "compact" };
  if (containsAny(text, project)) return { shouldRecall: true, reason: "project_context", tier: "compact" };
  if (containsAny(text, ambiguous)) return { shouldRecall: true, reason: "ambiguous_reference", tier: "compact" };

  const command = text.replace(/[.!?,\s]+$/gu, "");
  if (continuations.has(command)) {
    if (hasLiveContext === false) {
      return { shouldRecall: true, reason: "continuation_with_context", tier: "compact" };
    }
    return { shouldRecall: false, reason: "short_command", tier: "none" };
  }
  if (/^(?:привет(?:ствую)?|здравствуй(?:те)?|доброе\s+(?:утро|день|вечер)|hello|hi|hey|good\s+(?:morning|afternoon|evening))[!,.?\s]*$/iu.test(text)) {
    return { shouldRecall: false, reason: "greeting", tier: "none" };
  }

  const arithmetic = text
    .replace(/^(?:сколько\s+будет|посчитай|вычисли|calculate|what\s+is)\s*/iu, "")
    .trim()
    .replace(/\?$/u, "");
  if (/^[\d\s.,()+*/%\-^=]+$/u.test(arithmetic) && /[+\-*/%^]/u.test(arithmetic)) {
    return { shouldRecall: false, reason: "simple_calculation", tier: "none" };
  }

  const words = text.match(/[\p{L}\p{N}_-]+/gu) || [];
  if (/^(?:переведи|перевод|translate)(?:\s+(?:на|в|to)\s+[\p{L}\p{N}_-]+)?\s*[:\-]?\s+/iu.test(text) && words.length <= 16) {
    return { shouldRecall: false, reason: "single_phrase_translation", tier: "none" };
  }
  if (shortCommands.has(command) || (words.length <= 2 && text.endsWith("!"))) {
    return { shouldRecall: false, reason: "short_command", tier: "none" };
  }
  if (selfContainedPrefixes.some((prefix) => text.startsWith(prefix)) || words.length >= 8) {
    return { shouldRecall: false, reason: "self_contained", tier: "none" };
  }
  return { shouldRecall: true, reason: "conservative_fallback", tier: "compact" };
}

class RecallGateMetrics {
  constructor() {
    this.decisions = new Map();
    this.recalls = 0;
    this.injectedTokens = 0;
    this.latencyMilliseconds = 0;
  }

  recordDecision(decision) {
    const outcome = decision.shouldRecall ? "recall" : "skip";
    const key = `${outcome}:${decision.reason}:${decision.tier}`;
    this.decisions.set(key, (this.decisions.get(key) || 0) + 1);
  }

  recordRecall({ latencyMilliseconds = 0, injectedTokens = 0 } = {}) {
    this.recalls += 1;
    this.latencyMilliseconds += Math.max(0, Number(latencyMilliseconds) || 0);
    this.injectedTokens += Math.max(0, Number(injectedTokens) || 0);
  }

  snapshot() {
    return {
      decisions: Object.fromEntries([...this.decisions.entries()].sort()),
      recalls_total: this.recalls,
      injected_tokens_total: this.injectedTokens,
      recall_latency_seconds_sum: this.latencyMilliseconds / 1000,
    };
  }

  reset() {
    this.decisions.clear();
    this.recalls = 0;
    this.injectedTokens = 0;
    this.latencyMilliseconds = 0;
  }
}

export const recallGateMetrics = new RecallGateMetrics();

export function recallGateMetricsSnapshot() {
  return recallGateMetrics.snapshot();
}

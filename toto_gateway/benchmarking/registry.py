"""The benchmark taxonomy — pure data, no I/O.

The registry names the benchmarks we ingest and pins each one's category, unit, and direction
(higher_is_better). It is a curated allow-list for DISPLAY/AGGREGATION, not a storage gate: the
store will happily persist a fact whose benchmark_id isn't here (a new source can land data before
we've catalogued its metric), so `get` returns None for an unknown id rather than raising — the
caller decides whether to surface an uncatalogued fact. Keep new entries grounded in a real
source; the ids follow snake_case, the OpenRouter-ish house convention.

`legacy_*` are pseudo-benchmarks: carriers for today's flat benchmarks.yaml scores (code/reasoning/
general as 0..1 fractions) so the seeder has real benchmark_ids to write against.
"""

from __future__ import annotations

from dataclasses import dataclass

CATEGORIES = frozenset({
    "coding", "reasoning", "math", "agentic", "writing", "long_context",
    "multilingual", "preference", "speed", "cost",
    "knowledge", "instruction_following", "conversation",
})


@dataclass(frozen=True)
class Benchmark:
    id: str
    name: str
    category: str
    unit: str            # 'fraction'|'pct'|'elo'|'arena'|'index'|'tok_s'|'ms'|'usd_per_mtok'
    higher_is_better: bool
    url: str


# (id, name, category, unit, higher_is_better, url). higher_is_better defaults True in the loop
# below — only speed/cost latency+price metrics flip it, so those pass False explicitly.
_ROWS: tuple[tuple, ...] = (
    # coding
    ("swe_bench_verified", "SWE-bench Verified", "coding", "pct", "https://www.swebench.com"),
    ("aider_polyglot", "Aider Polyglot", "coding", "pct", "https://aider.chat/docs/leaderboards/"),
    ("livecodebench", "LiveCodeBench", "coding", "pct", "https://livecodebench.github.io"),
    ("terminal_bench", "Terminal-Bench", "coding", "pct", "https://www.tbench.ai"),
    ("scicode", "SciCode", "coding", "pct", "https://scicode-bench.github.io"),
    ("humaneval", "HumanEval (legacy)", "coding", "pct", "https://github.com/openai/human-eval"),
    ("lmarena_coding_text", "LMArena Coding Elo (text)", "coding", "elo", "https://lmarena.ai"),
    # reasoning
    ("gpqa_diamond", "GPQA Diamond", "reasoning", "pct", "https://github.com/idavidrein/gpqa"),
    ("hle", "Humanity's Last Exam", "reasoning", "pct", "https://lastexam.ai"),
    ("mmlu_pro", "MMLU-Pro", "reasoning", "pct", "https://huggingface.co/datasets/TIGER-Lab/MMLU-Pro"),
    ("bbh", "BIG-Bench Hard", "reasoning", "pct", "https://github.com/suzgunmirac/BIG-Bench-Hard"),
    ("arc_agi", "ARC-AGI", "reasoning", "pct", "https://arcprize.org"),
    ("aa_omniscience", "AA Omniscience", "reasoning", "index", "https://artificialanalysis.ai"),
    ("livebench", "LiveBench", "reasoning", "pct", "https://livebench.ai"),
    ("simplebench", "SimpleBench", "reasoning", "fraction", "https://simple-bench.com"),
    ("epoch_capabilities_index", "Epoch Capabilities Index", "reasoning", "index",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("lmarena_expert", "LMArena Expert Elo", "reasoning", "elo", "https://lmarena.ai"),
    # math
    ("math_hard", "MATH Level 5", "math", "pct", "https://github.com/hendrycks/math"),
    ("aime", "AIME", "math", "pct", "https://artofproblemsolving.com/wiki/index.php/AIME"),
    ("frontiermath", "FrontierMath", "math", "pct", "https://epoch.ai/frontiermath"),
    ("lmarena_math", "LMArena Math Elo", "math", "elo", "https://lmarena.ai"),
    # agentic
    ("bfcl", "Berkeley Function-Calling Leaderboard", "agentic", "pct",
     "https://gorilla.cs.berkeley.edu/leaderboard.html"),
    ("tau_bench", "τ-bench", "agentic", "pct", "https://github.com/sierra-research/tau-bench"),
    ("gdpval", "GDPval", "agentic", "pct", "https://openai.com/index/gdpval/"),
    ("osworld_2", "OSWorld-Verified", "agentic", "fraction", "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("the_agent_company", "TheAgentCompany", "agentic", "fraction",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("lmarena_agent", "LMArena Agent", "agentic", "arena", "https://lmarena.ai"),
    # writing
    ("eq_bench_creative", "EQ-Bench Creative Writing", "writing", "index", "https://eqbench.com"),
    ("lmarena_creative_writing", "LMArena Creative Writing", "writing", "elo",
     "https://lmarena.ai"),
    ("lech_mazur_writing", "Lech Mazur Creative Writing", "writing", "index",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    # long_context
    ("ruler", "RULER", "long_context", "pct", "https://github.com/NVIDIA/RULER"),
    ("aa_lcr", "AA Long-Context Reasoning", "long_context", "pct", "https://artificialanalysis.ai"),
    ("musr", "MuSR", "long_context", "pct", "https://github.com/Zayne-sprague/MuSR"),
    ("fictionlivebench", "Fiction.liveBench (120k)", "long_context", "fraction",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    # knowledge / factuality
    ("mmlu", "MMLU", "knowledge", "fraction", "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("simpleqa_verified", "SimpleQA Verified", "knowledge", "fraction",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("trivia_qa", "TriviaQA", "knowledge", "fraction", "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("openbookqa", "OpenBookQA", "knowledge", "fraction", "https://epoch.ai/data/ai-benchmarking-dashboard"),
    ("boolq", "BoolQ (reading comprehension)", "knowledge", "fraction",
     "https://epoch.ai/data/ai-benchmarking-dashboard"),
    # multilingual
    ("mmmlu", "Multilingual MMLU", "multilingual", "pct", "https://huggingface.co/datasets/openai/MMMLU"),
    ("swe_bench_multilingual", "SWE-bench Multilingual", "multilingual", "pct",
     "https://www.swebench.com"),
    ("lmarena_multilingual", "LMArena Multilingual Elo", "multilingual", "elo", "https://lmarena.ai"),
    # instruction_following
    ("lmarena_if", "LMArena Instruction Following Elo", "instruction_following", "elo",
     "https://lmarena.ai"),
    # conversation
    ("lmarena_multiturn", "LMArena Multi-Turn Elo", "conversation", "elo", "https://lmarena.ai"),
    # preference
    ("lmarena_elo", "LMArena Overall Elo", "preference", "elo", "https://lmarena.ai"),
    ("lmarena_coding", "LMArena WebDev Elo", "preference", "elo", "https://lmarena.ai"),
    ("lmarena_hard_prompts", "LMArena Hard Prompts Elo", "preference", "elo", "https://lmarena.ai"),
    ("lmarena_style_control", "LMArena Style Control Elo", "preference", "elo", "https://lmarena.ai"),
    # indices (categorized by content, per the survey)
    ("aa_intelligence_index", "AA Intelligence Index", "reasoning", "index",
     "https://artificialanalysis.ai"),
    ("aa_coding_index", "AA Coding Index", "coding", "index", "https://artificialanalysis.ai"),
    ("aa_agentic_index", "AA Agentic Index", "agentic", "index", "https://artificialanalysis.ai"),
    # legacy seed carriers (today's benchmarks.yaml 0..1 scores)
    ("legacy_code", "Legacy seed — code", "coding", "fraction", ""),
    ("legacy_reasoning", "Legacy seed — reasoning", "reasoning", "fraction", ""),
    ("legacy_general", "Legacy seed — general", "reasoning", "fraction", ""),
)

# Latency (ttft_ms) and price (usd_per_mtok_blended) are lower-is-better — built separately so the
# _ROWS loop can keep the common higher_is_better=True default.
_LOWER_IS_BETTER: tuple[tuple, ...] = (
    ("tokens_per_second", "Output tokens/s", "speed", "tok_s", True, "https://artificialanalysis.ai"),
    ("ttft_ms", "Time to first token", "speed", "ms", False, "https://artificialanalysis.ai"),
    ("usd_per_mtok_blended", "Blended $/M tokens", "cost", "usd_per_mtok", False,
     "https://artificialanalysis.ai"),
)

BENCHMARKS: dict[str, Benchmark] = {}
for _id, _name, _cat, _unit, _url in _ROWS:
    BENCHMARKS[_id] = Benchmark(_id, _name, _cat, _unit, True, _url)
for _id, _name, _cat, _unit, _hib, _url in _LOWER_IS_BETTER:
    BENCHMARKS[_id] = Benchmark(_id, _name, _cat, _unit, _hib, _url)


def get(benchmark_id: str) -> Benchmark | None:
    """The Benchmark for an id, or None if it isn't catalogued (an uncatalogued fact is still
    storable — the store doesn't enforce registry membership)."""
    return BENCHMARKS.get(benchmark_id)


def by_category(category: str) -> list[Benchmark]:
    """All catalogued benchmarks in a category, in registry order."""
    return [b for b in BENCHMARKS.values() if b.category == category]

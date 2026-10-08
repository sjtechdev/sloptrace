# sloptrace

Is your Python codebase getting messier? sloptrace measures it, and tracks how
the numbers change over your git history.

It reports four things:

| Metric | What it tells you |
|---|---|
| **Erosion** | How much of your code's complexity is concentrated in a few huge functions, plus a ranked list of those functions. Watch the trend, not the absolute value. |
| **Clones** | Copy-pasted functions and code blocks, with `file:line` for each copy. |
| **Repeated literals** | Strings or lists of literals repeated across the code; usually a missing constant. |
| **Import cycles** | Modules that import each other and can't be understood on their own. |

Everything is computed from the source with plain AST analysis (no LLM), so
the same code always gives the same numbers. The approach is adapted from the
SlopCodeBench paper (Orlanski et al., 2025).

## Install

```bash
pip install git+https://github.com/sjtechdev/sloptrace.git
```

Requires Python 3.9+.

## Usage

```bash
sloptrace .                                  # analyse the current checkout
sloptrace /path/to/repo --history            # ...and how it changed over git history
sloptrace . --history --every 20 --max-commits 15   # sample every 20th commit, at most 15
sloptrace . --history --since 2024-01-01 -o out.json
sloptrace . --show-literals                  # also list repeated literals (noisy, off by default)
sloptrace --help                             # all options
```

Which files are scanned: in a git repo, everything tracked or untracked but
not ignored; otherwise the whole directory tree. Tests, docs, build output,
virtualenvs and vendored code are skipped by default; change that with
`--exclude`.

## Development

```bash
git clone https://github.com/sjtechdev/sloptrace.git
cd sloptrace
pip install -e ".[test]"
pytest
```

## Docs

[Metric reference](docs/metrics.html): the exact formula and a worked example
for each metric.

## Compared to scb-check

The paper's authors also publish [`scb-check`](https://pypi.org/project/scb-check/).
It supports many languages and flags code with lots of pattern rules.
sloptrace only handles Python, but gives you evidence (which functions, which
copies) and trends over history.

Its erosion number won't exactly match the paper's: the two tools count
branches slightly differently, so don't compare absolute values with the
paper's published baselines.

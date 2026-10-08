# sloptrace

Deterministic complexity-erosion metrics for a Python codebase. Adapted from
SlopCodeBench (Orlanski et al., 2025), with the LLM judge replaced by AST
analysis, so every number is reproducible from source.

- **Erosion**: share of total complexity sitting in high-complexity functions,
  plus a ranked list of the worst offenders. Watch the trend, not the value.
- **Clones**: duplicated functions and statement blocks, with file:line evidence.
- **Repeated literals**: strings or literal lists repeated across a module or
  repo; usually a missing constant.
- **Import cycles**: modules that can't be understood independently.

## Install

```bash
pip install git+https://github.com/sjtechdev/sloptrace.git
```

Development:

```bash
git clone https://github.com/sjtechdev/sloptrace.git && cd sloptrace
pip install -e ".[test]"
pytest
```

## Usage

```bash
sloptrace /path/to/repo                                      # HEAD only
sloptrace /path/to/repo --history --every 20 --max-commits 15
sloptrace /path/to/repo --history --since 2024-01-01 -o out.json
sloptrace /path/to/repo --show-literals                      # include repeated literals (noisy)
```

Inside a git repo it scans tracked and untracked-but-not-ignored files;
otherwise it walks the tree. Tests, docs, build output, virtualenvs and
vendored code are excluded by default (`--exclude` to change).

## Docs

- [Metric reference](docs/metrics.html): formulas and worked examples.
- Compared to [`scb-check`](https://pypi.org/project/scb-check/), the paper's
  reference tool: sloptrace is Python-only and lighter, focused on history and
  evidence. Erosion differs slightly (decision-point counting), so don't
  compare absolute numbers with the paper's baselines.

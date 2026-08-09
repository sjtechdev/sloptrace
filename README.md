# slop_score

Deterministic complexity-erosion metrics for a Python codebase.

Adapted from SlopCodeBench (Orlanski et al., 2025), with the LLM-as-a-judge
component replaced by hand-written AST analysis so that every number is
reproducible from source alone.

Four things, each earning its place independently:

1. **Erosion** — a single slow-moving trend line: the share of the codebase's
   total complexity mass sitting in functions above a cyclomatic-complexity
   threshold. Backed by a ranked list of the actual functions driving it,
   each shown with both its cyclomatic and cognitive complexity.
2. **Clones** — duplicated code, at whole-function and statement-block
   granularity, reported *with evidence*: a file:line for every member, not
   just a ratio.
3. **Repeated literals** — a string literal repeated 4+ times in one module,
   or the same list/tuple of literals (a column/field schema) reused
   verbatim across the repo — usually a hard-coded thing that wants to be a
   shared constant.
4. **Import cycles** — strongly-connected components of the intra-repo
   import graph: modules that cannot be understood independently of each
   other.

Deltas between snapshots (`--history`) matter only for erosion — one
codebase has no population to compare its absolute level against, so watch
the slope, not the value.

## Install

This project is not published on PyPI yet (its distribution name is
`slop_score`, since `slopscore` on PyPI belongs to an unrelated package).
For now, install straight from GitHub — this also works once it's published:

```bash
pip install git+https://github.com/sjtechdev/slop_score.git
```

For cognitive-complexity scoring (via [complexipy](https://pypi.org/project/complexipy/)):

```bash
pip install "slop_score[cognitive] @ git+https://github.com/sjtechdev/slop_score.git"
```

For local development, clone the repo and install in editable mode:

```bash
git clone https://github.com/sjtechdev/slop_score.git
cd slop_score
pip install -e .
```

## Uninstall

```bash
pip uninstall slop_score
```

## Usage

```bash
slopscore /path/to/repo                     # HEAD only
slopscore /path/to/repo --history --every 20 --max-commits 15
slopscore /path/to/repo --history --since 2024-01-01 -o out.json
slopscore /path/to/repo --show-literals     # also print REPEATED LITERALS (noisy; hidden by default)
```

## Metric reference

[`docs/metrics.html`](docs/metrics.html) walks through each metric one at a
time — the exact formula and a worked example — for erosion, the
function-level offender ranking, clones, repeated literals, import cycles,
and the erosion trend/movement table.

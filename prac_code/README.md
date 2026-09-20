## Prerequisites

* `uv` - install via `curl -LsSf https://astral.sh/uv/install.sh | sh`.
* `just` - install via `uv tool install rust-just` (in this directory).

Check Python environment with:
```bash
./problems/env_test.py
```
It should print a random number.

## Modules

Each problem is a separately runnable script with it own dependencies and test cases. In principle we stick to standard library only.

Format and lint the code with:
```bash
just lint
```

## Problem List

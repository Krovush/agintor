# Agintor CLI MVP

Agintor is an experimental CLI for bounded runtime search over agent topology,
memory, tooling, and control policies.

## Local setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e ".[dev,hosted]"
.\.venv\Scripts\python -m pytest
```

The `agintor` console script is installed into `.venv\Scripts\agintor.exe`.

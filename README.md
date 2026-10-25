# Citation author tool

[![tests](https://github.com/csleuth/citation-author-tool/actions/workflows/tests.yml/badge.svg)](https://github.com/csleuth/citation-author-tool/actions/workflows/tests.yml)

Proposes missing author names for English Wikipedia news citations. It reads the cited page, finds the byline, and a person reviews every edit before it is saved.

- **Fills blanks only.** It never changes an existing author.
- **Human-reviewed.** Nothing is saved without approval; the review app is a dry run unless started with `--live`.
- **Checked LLM reading.** An LLM reads the page, but every name must be quoted exactly from it, fixed rules decide confidence, and a second, independent model must agree.

How it works and how accurate it is: [docs/design.md](docs/design.md). On Wikipedia: [User:CipherSleuth/Citation author tool](https://en.wikipedia.org/wiki/User:CipherSleuth/Citation_author_tool).

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

Configuration: see [secrets.example](secrets.example).

## Usage

```bash
python -m refs discover 2026/10        # most-viewed articles
python -m refs scan                    # find citations without authors
python -m refs sample S --n 500 --seed 1
python -m refs fetch S --run R         # live page, else Wayback
python -m refs fetch-alt S --run R     # the other version, for comparison
python -m refs read S --run R          # metadata + LLM byline reading
python -m refs decide S --run R        # confidence tiers
python -m refs export-cases S --run R  # cases for the independent checker (checker/)
python -m refs app S --run R           # review app (dry run); add --live to save approved edits
```

## License

[MIT](LICENSE)

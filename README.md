# Gleaned

*A tiny, fast toolkit for “gleaning” structured facts from messy files.*  
Turn folders of CSV/JSON/Excel/Parquet (and their chaotic column names) into tidy, typed dataframes with validated metadata and simple pipelines you can run in code or from the CLI.

## Features

- **Smart column normalization** – map vendor/legacy headers to your canonical schema.
- **Lightweight pipelines** – compose steps like `load → normalize → validate → enrich → write`.
- **Schema & units checks** – optional soft/hard validation of required fields and units.
- **Fast I/O** – pandas/pyarrow based; reads CSV, XLSX, JSON, Parquet.
- **Declarative rules** – YAML/JSON rules for synonyms, dtypes, units, computed fields.
- **CLI & Python API** – run one-off or batch jobs locally or in CI.
- **Extensible** – drop-in custom steps and validators.

---

## Installation

Clone the repository. In the directory, install the package in editable mode. 

```bash
pip install -e .

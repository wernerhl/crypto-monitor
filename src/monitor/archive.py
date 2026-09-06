"""Parquet + DuckDB archive management (phase 2 fills this in).

Layout: data/processed/<table>.parquet is the current state; data/archive/<table>/YYYY-MM.parquet
are monthly partitions; DuckDB queries read both with `read_parquet` globs.
"""

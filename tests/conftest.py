import os

# polars' thread pool and OpenBLAS threads deadlocked the statsmodels fit when both ran in one
# pytest process (0 % CPU hang after test_positioning); single-threaded BLAS in tests is enough
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")


import pytest  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test; set LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)

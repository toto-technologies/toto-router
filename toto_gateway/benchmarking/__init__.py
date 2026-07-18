"""Canonical multi-source benchmark store (chunk B1).

The new home for model benchmark reference data: a global BenchmarkStore of score facts +
model-name aliases, a frozen benchmark taxonomy (registry), and a seeder that migrates today's
flat benchmarks.yaml into it. Routing still reads benchmarks.py/benchmarks.yaml — this store is
built ALONGSIDE it and mounts nowhere yet (that's chunk B2). Nothing here touches routing.
"""

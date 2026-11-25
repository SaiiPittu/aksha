"""Blob stores: content-addressed, write-once bytes beside the event store (decision 25).

Each implements :class:`aksha.runtime.artifacts.ports.BlobStore` and passes
``tests/contract/blobs``. Built: a filesystem store, the default for a laptop, an offline
run and the tests. Planned: an object-store backend for unattended runs,
whose object lock also gives the write-once anchor sink.
"""

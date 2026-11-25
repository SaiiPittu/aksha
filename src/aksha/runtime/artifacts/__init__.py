"""The artifact store and lineage (ADR-P04): typed, immutable versions that record what they read.

Built: the values (``types.py``), refusals (``errors.py``), the ``BlobStore`` port
(``ports.py``), the three hashes and the canonical form (``hashing.py``), reading versions
from a view (``versions.py``), recorded reads and the lineage graph (``lineage.py``), typed
relations and their components (``relations.py``), the one save path (``store.py``) and
payload offload to the blob store (``blobs.py``). Planned: validators with an evidence
view (S1), derived kinds (D9), lifecycle and choice bindings, phrase rules, pending
revisions (ADR-P05) and citation spans (ADR-P13).
"""

"""Recording adapters: every model call, search and fetch of a run, kept and answered again.

``sink.py`` appends a recording's lines (one JSON object per line, per stream);
``models.py`` wraps the bound model adapters so each answer is recorded, and answers the
same calls from a recording; ``web.py`` does the same for the web ports. A recording is
a folder (``aksha.recording``) that ``aksha replay`` runs a whole run again from, offline.
"""

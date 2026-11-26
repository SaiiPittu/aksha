"""The web as runtime ports (ADR-P26 A3): search and fetch, a snippet never evidence.

``web_search`` returns ranked results and mints nothing; ``fetch_url`` fetches one page
through ``ToolContext.web``, which keeps the page as a text the runtime returned, so its
passages are minted as spans of the source ``web`` and a citation names the page. Which
web answers (a world's offline web, a live provider, a recording) is the composition
root's choice; a live fetch reaches the network only under the egress policy
``aksha.web``.
"""

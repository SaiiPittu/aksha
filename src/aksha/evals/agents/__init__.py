"""Agents under test: adapters that put an agent through an episode.

``scripted`` holds the two agents the runner is tested with: :class:`ScriptedAgent`, a strict
list of steps for unit tests, and :class:`ScriptedResearchAgent`, a small rule-based agent
that reads the competitor pages and reviews of a world and saves research outputs. It never
reads the key: its rules work on the files' formats, so it behaves the same on a renamed
world, which is what the invariance runs check.

The real agents' adapters go here, one module each, and are registered in
:func:`aksha.app.eval_agents`.
"""

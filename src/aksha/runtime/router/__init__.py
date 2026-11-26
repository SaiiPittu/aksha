"""The request router (ADR-P23): which agent handles a request that names none.

Built: ``types.py`` (the ``runtime.route`` alias, the routable agent, the decision and the
pending question), ``errors.py``, ``question.py`` (the classification question built
from the loaded agents' purpose lines, and the clarifying question), ``fake.py`` (the
offline classifier over purpose words), ``facts.py`` (``user.message``, ``route.decided``
and the run they open), ``decisions.py`` (the route decisions and labels read from the
log), ``step.py`` (the classification as a durable step in the session's routing
namespace), ``router.py`` (``Router``, ``route_message``, ``settle_routes``) and
``commands.py`` (``send_message``, ``answer_route``, ``submit``). Planned: a live
classification adapter for ``runtime.route`` and the "other" choice as a free-text
follow-up (RT-2 A).
"""

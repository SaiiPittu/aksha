"""The harness's form tools (ADR-P10 amendment F1): read a blank form and fill it, checked.

``tools.py`` declares ``read_form`` and ``fill_form`` for any agent that lists them with
:func:`~aksha.runtime.forms.tools.form_tools`. A blank form is an input like any other; the
agent names which of its document kinds is a blank form and which fields of that kind's
extraction list the form's fields (:class:`~aksha.runtime.forms.tools.FormSpec`, declared
data). The field statuses and the rules that refuse a fill are the runtime's, the same for
every agent.
"""

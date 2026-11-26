"""Extensions: packages that contribute declared data to agents (ADR-P20 D1, D3).

Each extension is ``src/aksha/extensions/<name>/`` exporting one ``Extension`` value. It
imports only :mod:`aksha.runtime.api`. ``otel`` (ADR-P21) is the trace exporter;
``memory`` (ADR-P09) is planned.
"""

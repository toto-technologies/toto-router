"""Org-level AI-usage observability — provider-observed usage, cost, and people data.

The admin_usage plane reports what THIS gateway saw. This package reports what the
PROVIDER saw for the whole org (including spend that never touched the gateway): an org
admin pastes a provider admin key (Anthropic `sk-ant-admin01-...` / OpenAI `sk-admin-...`),
the connectors pull the org's usage/cost/members from the provider admin APIs, and
insights.py derives the dashboard: spend over time, by model, top workspaces/users,
cache efficiency, and routing-savings candidates. The delta between the two planes is
Toto's pitch — how much of the org's provider spend the gateway could be routing.
"""

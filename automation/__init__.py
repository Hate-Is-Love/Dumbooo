"""Module 4: laptop automation.

Every request follows the same path, for voice and typed input alike:

    user request -> authentication -> intent (rules, or the AI planner) -> relevant
    memory retrieval -> structured plan -> policy + risk checks -> confirmation ->
    execution -> verification -> task history -> answer

The AI may *propose* a plan, but only registered capabilities with validated
arguments are ever executed, and every step is checked again by the policy
engine right before it runs.
"""

"""Module 5: web search, research and live news intelligence.

Flow for every research request (voice and typed alike):

    request -> intent -> authentication/permission -> relevant memory (only when needed)
    -> search plan -> web/news search -> source retrieval -> evidence extraction
    -> cross-source comparison -> AI synthesis (from the retrieved sources only)
    -> citation validation -> answer (+ optional memory saving) -> research history

Everything fetched from the internet is untrusted data: it is quoted to the AI as
material to analyse and can never trigger laptop automation or change DUMBOO's rules.
"""

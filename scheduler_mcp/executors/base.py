"""Interface commune des executors (BUILD_BRIEF.md commits 5 a 7).

Chaque executor recoit un job et son contexte, execute, et retourne un resultat
structure (succes/echec + detail) qui alimente la table runs et le Journal.
"""

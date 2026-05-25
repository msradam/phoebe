# Why Do Multi-Agent LLM Systems Fail? (MAST)

- Source: Cemri, Pan, Yang, et al., "Why Do Multi-Agent LLM Systems Fail?"
- URLs: https://arxiv.org/abs/2503.13657 ; project page https://sky.cs.berkeley.edu/project/mast/ ; taxonomy repo https://github.com/multi-agent-systems-failure-taxonomy/MAST
- Authors/affiliation: UC Berkeley Sky Computing Lab (with collaborators). Submitted March 2025.
- What it is: the first empirically grounded taxonomy of how multi-agent LLM systems fail. The MAST taxonomy IBM's IT-Bench analysis uses.
- Why it matters to us: it establishes that agent failures are structural and nameable, and that piling on more agents does not fix reliability. It gives the FM-x.y vocabulary Phoebe's pitch uses.

## Headline findings

- Multi-agent LLM systems fail at surprisingly high rates. Example cited: "ChatDev achieves only 33.33% correctness on our ProgramDev benchmark."
- More agents is not a reliability win: "their performance gains often remain minimal compared to single-agent frameworks or simple baselines like best-of-N sampling." (The scaling-by-adding-agents assumption does not hold.)

## The taxonomy

- 14 unique failure modes organized into 3 overarching categories:
  1. Specification issues (the task/role/termination spec is wrong or unfollowed)
  2. Inter-agent misalignment (agents talk past each other, lose history, derail)
  3. Task verification (the system declares success without verifying)

## Methodology (why it is credible)

- 7 popular MAS frameworks analyzed (MetaGPT, ChatDev, HyperAgent, OpenManus, AppWorld, Magentic, AG2).
- 200+ execution traces hand-annotated, each averaging 15,000+ tokens, using grounded theory with expert annotators.
- Inter-annotator agreement Cohen's Kappa 0.88; an LLM-as-judge pipeline reaches 94% accuracy / 0.77 Kappa against humans.
- MAST-Data: 1600+ annotated traces across the 7 frameworks.

## Bearing on Phoebe

- The "task verification" category and FM-3.3 (incorrect verification) are the failures a state machine cannot fix; name them as the boundary.
- The "specification / termination" failures are exactly what an FSM enforces structurally. Phoebe turns the spec into executable transitions, so the model cannot violate ordering or termination.

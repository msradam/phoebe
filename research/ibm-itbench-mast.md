# IBM Research: IT-Bench + MAST failure analysis

- Source: "IT-Bench and MAST" (IBM Research, on Hugging Face blog)
- URL: https://huggingface.co/blog/ibm-research/itbenchandmast
- What it is: IBM Research's agent benchmark for IT automation / SRE / compliance (IT-Bench), analyzed through the Multi-Agent System failure Taxonomy (MAST). Includes a per-model failure-mode breakdown and explicit engineering prescriptions.
- Why it matters to us: this is the strongest external validation of Phoebe's thesis. It measures that prompt engineering barely helps, that structural control helps a lot, and it names finite state machines as the fix. It also profiles Kimi-K2 specifically, the model Phoebe runs.

## Performance (Mean Recall on IT-Bench traces)

- Gemini-3-Flash: 75.5% (100 traces)
- Kimi-K2: 28.6% (105 traces)
- GPT-OSS-120B: 12.4% (105 traces)

Frontier models are middling; large open models are poor. Off-the-shelf agents are not reliable at this work.

## The dominant, nameable failure modes

> "Across all models, the strongest predictor of failure is FM-3.3 (Incorrect Verification). Agents consistently 'declare victory' without checking ground truth."

Failure-mode density (cascading):

> "Frontier models like Gemini-3-Flash fail cleanly (2.6 failure modes/trace), typically hitting isolated bottlenecks like verification. Large open models like GPT-OSS-120B suffer from cascading failure modes (5.3 failure modes/trace). A single reasoning mismatch early in the run poisons the context, leading to compounding hallucinations."

Kimi-K2 (the model Phoebe runs) signature:

> "Kimi-K2 struggles to recognize when a task is done. It exhibits a massive spike in Premature Termination (+46%) and Unaware of Termination Conditions (+43%), often quitting just before solving the problem or looping indefinitely."

(Phoebe's FSM addresses exactly this: termination is a graph property, not the model's call. Our own runs showed Kimi hitting the loop guard and the empty-turn behavior, which the harness contains.)

## The headline for our pitch: structure beats prompting

> "prompt engineering won't help much ... with manual interventions like prompt engineering for memory related failures, we can get only up to around 15.6% performance improvements, whereas ... by introducing context management mechanisms (such as a stricter State Machine to enforce termination ...), we can get up to 53% performance improvement as these tackle more fundamental issues with the system."

## The explicit prescription: finite state machines

> "Put termination + loop control outside the model: Termination issues are common killers (FM-1.5). Add explicit stop conditions + loop detectors for repeated tool calls/actions or implement Finite State Machines."

> "For Kimi-K2: Use a deterministic state machine to fix the model's frequent struggle with recognizing task completion."

> "Never let the LLM grade its own homework. Require hard tool evidence before exit." (Externalize verification, FM-3.3.)

## Fatal vs. recoverable failure modes

- Fatal: FM-1.5 (unaware of termination), FM-3.1 (premature termination), FM-1.4 (loss of conversation history), FM-2.3 (task derailment), FM-2.2 (fail to ask for clarification).
- Recoverable: FM-1.3 (step repetition), FM-3.3 (incorrect verification, mitigable with external verification).

## How Phoebe maps onto this

- Structurally prevents: FM-1.5, FM-3.1 (termination is the graph's `conclude` gate), FM-1.1 ordering, unbounded action space, FM-1.3 step repetition (loop guard).
- Does not fix: FM-3.3 (incorrect verification), FM-2.6 (reasoning-action mismatch). Phoebe's own benchmark misses land exactly here, which is the honest boundary to state, not hide.

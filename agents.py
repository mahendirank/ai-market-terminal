# ===== MULTI-AGENT SYSTEM =====

def planner_agent(user_input):
    return f"""
You are a senior project planner.

Break this task into clear steps:

Task:
{user_input}

Output format:
1. Step 1
2. Step 2
3. Step 3

Be logical and structured.
"""


def executor_agent(user_input, plan, skills):
    return f"""
You are an expert executor.

Context:
{skills}

Plan:
{plan}

Task:
{user_input}

Execute step-by-step.

Provide:
- Strategy
- Implementation
- Code (if needed)

Be precise and professional.
"""


def reviewer_agent(user_input, output):
    return f"""
You are a senior reviewer (hedge fund / senior engineer).

Review this output:

Task:
{user_input}

Output:
{output}

Check:
- Logic
- Completeness
- Risks
- Improvements

Then provide a FINAL improved version.

No fluff. Only high-quality output.
"""

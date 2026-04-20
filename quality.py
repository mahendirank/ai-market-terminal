def review_output(output):
    score = 0

    # Basic checks
    if len(output) > 300:
        score += 1
    if "risk" in output.lower():
        score += 1
    if "strategy" in output.lower():
        score += 1
    if "entry" in output.lower():
        score += 1
    if "stop" in output.lower():
        score += 1

    return score


def improve_prompt(original_prompt, output):
    return f"""
You are an institutional expert.

The previous output was weak.

Improve it with:
- More precision
- Better structure
- Strong reasoning
- Professional tone

Original request:
{original_prompt}

Previous output:
{output}

Now generate a superior version.
"""

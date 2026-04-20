import os
import sys
import requests

sys.path.insert(0, os.path.dirname(__file__))
from executor import handle_output
from quality import review_output, improve_prompt
from agents import planner_agent, executor_agent, reviewer_agent


def run_qwen(prompt):
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen2.5:7b",
            "prompt": prompt,
            "stream": False
        }
    )
    return response.json()["response"]

def is_critical_task(prompt):
    prompt = prompt.lower()

    keywords = [
        "ea", "expert advisor", "mt5",
        "tradingview", "pine",
        "strategy", "scalping",
        "architecture", "system design",
        "optimize", "improve logic"
    ]

    return any(k in prompt for k in keywords)


def detect_domains(prompt):
    prompt = prompt.lower()
    domains = []

    if any(x in prompt for x in ["trading", "gold", "forex", "xauusd", "nifty"]):
        domains.append("trading")

    if any(x in prompt for x in ["youtube", "content", "script", "viral"]):
        domains.append("content")

    if any(x in prompt for x in ["marketing", "ads", "funnel"]):
        domains.append("marketing")

    if any(x in prompt for x in ["app", "dashboard", "code"]):
        domains.append("dev")

    if any(x in prompt for x in ["image", "poster", "design"]):
        domains.append("media")

    if any(x in prompt for x in ["video", "reel"]):
        domains.append("media")

    if any(x in prompt for x in ["mt5", "ea", "expert advisor", "tradingview", "pine"]):
        domains.append("trading-dev")

    return domains


def load_skills(domains):
    content = ""

    for domain in domains:
        skill_path = f"skills/{domain}"
        if os.path.exists(skill_path):
            for file in os.listdir(skill_path):
                with open(os.path.join(skill_path, file), "r") as f:
                    content += f.read() + "\n"

    return content


def build_prompt(user_input):
    domains = detect_domains(user_input)
    skills = load_skills(domains)

    final_prompt = f"""
You are an expert AI system trained in trading, consulting, and execution.

You MUST think like:
- Institutional trader
- Strategy consultant
- Product builder

Active Domains: {domains}

{skills}

User Request:
{user_input}

STRICT RULES:
- Output must be structured
- No generic explanations
- Use frameworks, logic, numbers
- Think step-by-step before answering
- For PPT → follow SLIDE format strictly

INSTITUTIONAL RULES:
- Think like hedge fund / senior engineer
- No generic explanation
- Provide actionable output
- Include edge cases and risks
- Be concise but powerful

IMPORTANT:
- Use LIVE NEWS for reasoning
- Identify high-impact events
- Correlate with gold (XAUUSD)
- Avoid generic answers

MACRO ANALYSIS RULES:
- Correlate USD vs Gold
- Analyze bond yields (2Y vs 10Y)
- Identify risk-on / risk-off
- Include oil impact
- Consider geopolitical risks
- Link macro → trade decision

GLOBAL MARKET RULES:
- Track USD strength vs yields
- Correlate stocks (Mag7, semiconductors)
- Identify risk-on / risk-off sentiment
- Link equities → gold movement
- Include oil impact
- Consider geopolitical risk
- Use news as catalyst

PRICE ACTION RULES:
- Combine macro + structure
- Identify reversal zones
- Use Fibonacci + pivot + liquidity zones
- Align entry with sentiment
- Avoid trading in middle ranges

Deliver elite-level output.
"""

    return final_prompt


if __name__ == "__main__":
    user_input = input("Enter task: ")

    if is_critical_task(user_input):
        print("\n⚠️ CRITICAL TASK → Use Claude Pro\n")
        print(build_prompt(user_input))
    else:
        # Step 1 — Planner
        plan_prompt = planner_agent(user_input)
        plan = run_qwen(plan_prompt)

        print("\n=== PLAN ===\n")
        print(plan)

        # Step 2 — Load skills
        domains = detect_domains(user_input)
        skills = load_skills(domains)

        # Step 3 — Executor
        exec_prompt = executor_agent(user_input, plan, skills)
        result = run_qwen(exec_prompt)

        print("\n=== EXECUTION ===\n")
        print(result)

        # Step 4 — Reviewer (quality upgrade)
        review_prompt = reviewer_agent(user_input, result)
        final_output = run_qwen(review_prompt)

        print("\n=== FINAL OUTPUT ===\n")
        print(final_output)

        # Save result
        handle_output(user_input, final_output)

import os
import anthropic
from loader import build_system_prompt, detect_domains

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def run(user_input):
    system_prompt = build_system_prompt(user_input)
    domains = detect_domains(user_input)

    print(f"\nDetected domains: {domains}")
    print("Generating response...\n")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_input}]
    )

    return message.content[0].text

if __name__ == "__main__":
    user_input = input("Enter your task: ")
    result = run(user_input)
    print("\n" + "="*60)
    print(result)
